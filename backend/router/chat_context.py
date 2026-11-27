"""
Agent Chat context assembly, scoped extensions/rules, history serialization,
non-destructive auto-compaction, and MCP/Skill discovery tool execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from backend.router.chat_mcp import fetch_mcp_tools
from backend.router.chat_store import (
    APPROVAL_REQUIRED_TOOLS,
    MCP_DISCOVERY_TOOLS_SCHEMA,
    MEDIA_TOOLS_SCHEMA,
    SKILL_DISCOVERY_TOOLS_SCHEMA,
    SWARM_TOOLS_SCHEMA,
    WORKSPACE_TOOLS_SCHEMA,
    ChatStore,
)
from backend.router.mcp_api import annotate_mcp_egress, search_mcp_registry_items
from backend.router.media_api import MediaService
from backend.router.policy import resolve_route
from backend.router.skills_api import scan_installed_skills, search_skills_registry_items
from backend.router.telemetry import ClusterState
from backend.router.web_search import WEB_TOOLS_SCHEMA
from backend.shared.models import (
    ChatMessageRecord,
    ChatRulesConfig,
    CompactResponse,
    ConversationSummary,
    McpServerConfig,
    ScopedExtensionsConfig,
    ScopeRule,
)


def sse_event(event: str, data: dict[str, Any]) -> bytes:
    """Serialize a Server-Sent Event packet."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def adjust_reasoning_effort_on_error(
    req_body: dict[str, Any], err_body: str
) -> dict[str, Any] | None:
    """Remap or strip reasoning_effort when an upstream Jinja template rejects it."""
    if "reasoning_effort" not in req_body:
        return None
    lower = err_body.lower()
    if "unexpected reasoning effort" not in lower and "reasoning_effort" not in lower:
        return None
    cur = str(req_body["reasoning_effort"]).lower()
    supported = lower.split("supported", 1)[-1] if "supported" in lower else lower
    updated = dict(req_body)
    if cur == "high" and re.search(r"\bxhigh\b", supported):
        updated["reasoning_effort"] = "xhigh"
    elif (
        cur == "xhigh"
        and re.search(r"\bhigh\b", supported)
        and not re.search(r"\bxhigh\b", supported)
    ):
        updated["reasoning_effort"] = "high"
    elif cur == "none" and re.search(r"\boff\b", supported):
        updated["reasoning_effort"] = "off"
    else:
        updated.pop("reasoning_effort", None)
    return updated


def matches_cmd_prefix_or_pattern(cmd: str, entries: list[str], *, anywhere: bool) -> bool:
    low = cmd.strip().lower()
    if not low:
        return False
    for pat in entries:
        p = pat.strip().lower()
        if not p:
            continue
        if anywhere:
            if p in low:
                return True
        elif low == p or low.startswith((f"{p} ", f"{p}\t")):
            return True
    return False


def requires_tool_approval(
    tool_name: str,
    parsed_args: dict[str, Any],
    *,
    is_external_mcp: bool,
    conv_auto_approve: bool,
    router_cfg: Any,
) -> bool:
    """Evaluate tiered approval policy + shell allow/denylist (denylist always wins)."""
    policy = getattr(router_cfg, "approval_policy", "ask")
    if is_external_mcp:
        return not (conv_auto_approve or policy == "full_auto")
    if tool_name not in APPROVAL_REQUIRED_TOOLS:
        return False
    if tool_name == "run_command":
        cmd_str = str(parsed_args.get("command") or "")
        denylist = list(getattr(router_cfg, "shell_denylist", []) or [])
        if matches_cmd_prefix_or_pattern(cmd_str, denylist, anywhere=True):
            return True
        if conv_auto_approve or policy == "full_auto":
            return False
        if policy == "turbo_safe":
            allowlist = list(getattr(router_cfg, "shell_allowlist", []) or [])
            return not matches_cmd_prefix_or_pattern(cmd_str, allowlist, anywhere=False)
        return True
    return not (conv_auto_approve or policy in ("auto_edits", "turbo_safe", "full_auto"))


def norm_ws_key(subpath: str) -> str:
    return subpath.strip().strip("/") or "."


def load_scopes(scopes_file: Path) -> ScopedExtensionsConfig:
    if not scopes_file.exists():
        return ScopedExtensionsConfig()
    try:
        return ScopedExtensionsConfig.model_validate(
            json.loads(scopes_file.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return ScopedExtensionsConfig()


def save_scopes(data_dir: Path, scopes_file: Path, cfg: ScopedExtensionsConfig) -> ScopedExtensionsConfig:
    data_dir.mkdir(parents=True, exist_ok=True)
    scopes_file.write_text(json.dumps(cfg.model_dump(mode="json"), indent=2), encoding="utf-8")
    return cfg


def resolve_effective_extensions(
    scopes_file: Path,
    workspace_subpath: str,
    chat_skills: list[str],
    chat_mcps: list[str],
) -> tuple[list[str], list[str]]:
    scopes = load_scopes(scopes_file)
    ws_rule = scopes.workspaces.get(norm_ws_key(workspace_subpath), ScopeRule())
    eff_skills = list(
        dict.fromkeys([*scopes.global_scope.skills, *ws_rule.skills, *chat_skills])
    )
    eff_mcps = list(
        dict.fromkeys([*scopes.global_scope.mcps, *ws_rule.mcps, *chat_mcps])
    )
    return eff_skills, eff_mcps


def load_selected_skills(skills_dir: Path, skill_ids: list[str]) -> str:
    if not skill_ids or not skills_dir.exists():
        return ""
    blocks: list[str] = []
    for sid in skill_ids:
        md = (skills_dir / sid / "SKILL.md").resolve()
        if md.is_relative_to(skills_dir.resolve()) and md.is_file():
            with contextlib.suppress(OSError):
                blocks.append(f'<skill id="{sid}">\n{md.read_text(encoding="utf-8")[:8000]}\n</skill>')
    return "\n\n".join(blocks)


def load_all_mcp_configs(data_dir: Path) -> list[McpServerConfig]:
    reg = data_dir / "mcp_servers.json"
    if not reg.exists():
        return []
    try:
        raw = json.loads(reg.read_text(encoding="utf-8"))
        return [
            annotate_mcp_egress(McpServerConfig.model_validate(item))
            for item in raw
            if isinstance(item, dict) and item.get("id")
        ]
    except (OSError, ValueError):
        return []


def load_mcp_configs(data_dir: Path, mcp_ids: list[str]) -> dict[str, McpServerConfig]:
    reg = data_dir / "mcp_servers.json"
    if not mcp_ids or not reg.exists():
        return {}
    try:
        raw = json.loads(reg.read_text(encoding="utf-8"))
        wanted = set(mcp_ids)
        return {
            item["id"]: annotate_mcp_egress(McpServerConfig.model_validate(item))
            for item in raw
            if isinstance(item, dict) and item.get("id") in wanted and item.get("enabled", True)
        }
    except (OSError, ValueError):
        return {}


def load_chat_rules(data_dir: Path) -> ChatRulesConfig:
    p = data_dir / "rules.json"
    if not p.exists():
        return ChatRulesConfig()
    try:
        return ChatRulesConfig.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ChatRulesConfig()


def save_chat_rules(data_dir: Path, cfg: ChatRulesConfig) -> ChatRulesConfig:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "rules.json").write_text(cfg.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return cfg


def estimate_msgs_tokens(
    summary: str, sys_text: str, msgs: list[ChatMessageRecord]
) -> int:
    chars = len(sys_text) + len(summary)
    for m in msgs:
        chars += len(m.content or "") + len(m.reasoning or "") + len(json.dumps(m.tool_calls))
    return max(1, chars // 4)


def split_for_compaction(
    uncompacted: list[ChatMessageRecord], keep_recent: int = 6
) -> tuple[list[ChatMessageRecord], list[ChatMessageRecord]]:
    if len(uncompacted) <= keep_recent:
        return [], uncompacted
    split_idx = len(uncompacted) - keep_recent
    while split_idx > 2 and uncompacted[split_idx].role != "user":
        split_idx -= 1
    if split_idx < 2:
        return [], uncompacted
    return uncompacted[:split_idx], uncompacted[split_idx:]


async def run_compaction(
    cluster: ClusterState,
    store: ChatStore,
    conv: ConversationSummary,
    *,
    keep_recent: int = 6,
) -> CompactResponse:
    all_msgs = await asyncio.to_thread(store.list_messages, conv.id)
    uncompacted = [m for m in all_msgs if m.created_at > conv.compacted_through_ts]
    older, recent = split_for_compaction(uncompacted, keep_recent=keep_recent)
    est_before = estimate_msgs_tokens(conv.compacted_summary, conv.system_prompt, uncompacted)
    if not older:
        return CompactResponse(
            ok=True,
            compacted=False,
            compacted_messages=0,
            kept_messages=len(uncompacted),
            summary=conv.compacted_summary,
            compacted_through_ts=conv.compacted_through_ts,
            estimated_tokens_before=est_before,
            estimated_tokens_after=est_before,
            detail="Conversation is already concise enough — fewer than 2 older turns to compact.",
        )

    transcript_lines: list[str] = []
    if conv.compacted_summary.strip():
        transcript_lines.append(f"[Previous Summary]\n{conv.compacted_summary.strip()}\n")
    for m in older:
        role_tag = m.role.upper() if not m.tool_name else f"TOOL({m.tool_name})"
        body = (m.content or "").strip()
        if len(body) > 1000:
            body = body[:1000] + " ...[truncated]"
        transcript_lines.append(f"{role_tag}: {body}")

    compact_candidates: list[str] = []
    for cand in ("heavy", "fast"):
        if cand in cluster.aliases and cand not in compact_candidates:
            compact_candidates.append(cand)
    if conv.model not in compact_candidates:
        compact_candidates.append(conv.model)

    compact_req = {
        "model": compact_candidates[0],
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 500,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Condense the conversation history into a crisp, high-signal engineering summary. "
                    "Preserve: user goals, technical decisions, file paths, symbol names, tool results, "
                    "errors fixed, and unresolved next steps. Omit pleasantries and verbatim large outputs. "
                    "Use concise markdown bullets."
                ),
            },
            {
                "role": "user",
                "content": "\n\n".join(transcript_lines)[:14_000],
            },
        ],
    }

    decision = None
    skip_reasons: dict[str, str] = {}
    for cand_model in compact_candidates:
        compact_req["model"] = cand_model
        decision, skip_reasons = resolve_route(
            endpoint="/v1/chat/completions",
            requested_model=cand_model,
            body=compact_req,
            nodes=cluster.nodes,
            aliases=cluster.aliases,
            control=cluster.control,
            guards=cluster.config.guards,
            heavy_char_threshold=cluster.config.router.auto_heavy_char_threshold,
        )
        if decision is not None:
            break
    if decision is None:
        raise HTTPException(
            status_code=503, detail=f"No eligible node for compaction: {skip_reasons}"
        )

    compact_req["model"] = decision.target_model
    target_url, upstream_auth = cluster.resolve_upstream_target(
        decision, "/v1/chat/completions"
    )
    summary_text = ""
    try:
        resp = await cluster.proxy_client.post(
            target_url,
            json=compact_req,
            auth=upstream_auth,
        )
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502, detail=f"Compaction upstream HTTP {resp.status_code}"
            )
        data = resp.json()
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg_obj = choices[0].get("message") or {}
            summary_text = str(msg_obj.get("content") or "").strip()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Compaction failed: {exc}") from exc

    if not summary_text:
        summary_text = "\n".join(
            f"- **{m.role}**: {(m.content or '')[:180].replace(chr(10), ' ')}" for m in older[:12]
        )

    watermark = older[-1].created_at
    await asyncio.to_thread(store.save_compaction, conv.id, summary_text, watermark)
    est_after = estimate_msgs_tokens(summary_text, conv.system_prompt, recent)
    return CompactResponse(
        ok=True,
        compacted=True,
        compacted_messages=len(older),
        kept_messages=len(recent),
        summary=summary_text,
        compacted_through_ts=watermark,
        estimated_tokens_before=est_before,
        estimated_tokens_after=est_after,
    )


@dataclass
class PreparedChatTurn:
    active_conv: ConversationSummary
    eff_skills: list[str]
    mcp_map: dict[str, McpServerConfig]
    mcp_tools_only: list[dict[str, Any]]
    tools_payload: list[dict[str, Any]]
    openai_msgs: list[dict[str, Any]]


def _records_to_openai_messages(
    system_prompt: str, history_records: list[ChatMessageRecord]
) -> list[dict[str, Any]]:
    openai_msgs: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    pending_tool_calls: list[tuple[str, str]] = []
    last_role: str = "system"

    def _flush_orphaned_tool_calls() -> None:
        nonlocal last_role
        if not pending_tool_calls:
            return
        for tid, tname in pending_tool_calls:
            openai_msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tid,
                    "name": tname,
                    "content": "[Interrupted before tool result was recorded]",
                }
            )
            last_role = "tool"
        pending_tool_calls.clear()

    for m in history_records:
        if m.role == "user":
            _flush_orphaned_tool_calls()
            if last_role == "tool":
                openai_msgs.append(
                    {"role": "assistant", "content": "[Previous turn interrupted]"}
                )
            if m.images:
                parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
                for img in m.images:
                    parts.append({"type": "image_url", "image_url": {"url": img}})
                openai_msgs.append({"role": "user", "content": parts})
            else:
                openai_msgs.append({"role": "user", "content": m.content})
            last_role = "user"
        elif m.role == "assistant":
            _flush_orphaned_tool_calls()
            entry: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                entry["tool_calls"] = m.tool_calls
                for tc in m.tool_calls:
                    if isinstance(tc, dict):
                        tid = str(tc.get("id") or "call_0")
                        fn = tc.get("function") or {}
                        tname = str(fn.get("name") or "tool") if isinstance(fn, dict) else "tool"
                        pending_tool_calls.append((tid, tname))
            openai_msgs.append(entry)
            last_role = "assistant"
        elif m.role == "tool":
            tid = m.tool_call_id or "call_0"
            tname = m.tool_name or "tool"
            if pending_tool_calls:
                pending_tool_calls = [p for p in pending_tool_calls if p[0] != tid]
            elif last_role not in ("assistant", "tool"):
                # Drop leading orphan tool message when no assistant tool_calls preceded it
                continue
            openai_msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tid,
                    "name": tname,
                    "content": m.content,
                }
            )
            last_role = "tool"

    _flush_orphaned_tool_calls()
    return openai_msgs


async def prepare_chat_turn(
    *,
    conv: ConversationSummary,
    cluster: ClusterState,
    store: ChatStore,
    data_dir: Path,
    skills_dir: Path,
    scopes_file: Path,
    media_service: MediaService | None,
) -> tuple[list[bytes], PreparedChatTurn]:
    """Assemble system prompt, auto-compact history when needed, and load tools."""
    events: list[bytes] = []
    sys_parts = [
        "You are Splitch, a precise local AI engineering assistant.",
        "Be concise, accurate, and use tools directly when inspecting or modifying workspace files.",
    ]
    rules_cfg = await asyncio.to_thread(load_chat_rules, data_dir)
    if rules_cfg.global_rules.strip():
        sys_parts.append(f"Global User Rules:\n{rules_cfg.global_rules.strip()}")
    if conv.system_prompt.strip():
        sys_parts.append(conv.system_prompt.strip())

    eff_skills, eff_mcps = await asyncio.to_thread(
        resolve_effective_extensions,
        scopes_file,
        conv.workspace_subpath,
        conv.enabled_skills,
        conv.enabled_mcps,
    )

    skills_text = await asyncio.to_thread(load_selected_skills, skills_dir, eff_skills)
    if skills_text:
        sys_parts.append(f"Active Skills:\n{skills_text}")

    if conv.web_search_enabled:
        sys_parts.append(
            "Web search is enabled. Use `web_search` for current facts or documentation, "
            "optionally call `web_fetch` on a result URL for full details, and cite source URLs."
        )

    active_conv = conv
    all_history = await asyncio.to_thread(store.list_messages, conv.id)
    uncompacted_msgs = [
        m for m in all_history if m.created_at > active_conv.compacted_through_ts
    ]
    ctx_budget = next(
        (
            n.telemetry.context_length
            for n in cluster.nodes.values()
            if n.online and n.telemetry and n.telemetry.context_length
        ),
        32768,
    )
    est_active_tokens = estimate_msgs_tokens(
        active_conv.compacted_summary, "\n\n".join(sys_parts), uncompacted_msgs
    )
    compact_threshold = max(0.50, min(0.95, float(cluster.config.router.auto_compact_threshold)))
    if (
        active_conv.auto_compact
        and ctx_budget > 0
        and (est_active_tokens / ctx_budget) >= compact_threshold
        and len(uncompacted_msgs) > 7
    ):
        events.append(
            sse_event(
                "compacting",
                {"estimated_tokens": est_active_tokens, "context_window": ctx_budget},
            )
        )
        try:
            c_res = await run_compaction(cluster, store, active_conv, keep_recent=6)
            if c_res.compacted:
                refreshed = await asyncio.to_thread(store.get_conversation, conv.id)
                if refreshed:
                    active_conv = refreshed
                events.append(sse_event("compacted", c_res.model_dump()))
        except HTTPException:
            pass

    if active_conv.compacted_summary.strip():
        sys_parts.append(
            f"<conversation_summary>\n{active_conv.compacted_summary.strip()}\n</conversation_summary>"
        )

    mcp_map = await asyncio.to_thread(load_mcp_configs, data_dir, eff_mcps)
    all_installed_mcps = await asyncio.to_thread(load_all_mcp_configs, data_dir)
    tools_payload: list[dict[str, Any]] = []
    mcp_tools_only: list[dict[str, Any]] = []
    active_mcp_lines: list[str] = []
    if active_conv.tools_enabled:
        tools_payload.extend(WORKSPACE_TOOLS_SCHEMA)
        tools_payload.extend(MCP_DISCOVERY_TOOLS_SCHEMA)
        tools_payload.extend(SKILL_DISCOVERY_TOOLS_SCHEMA)
        for srv in mcp_map.values():
            srv_tools = await fetch_mcp_tools(
                srv, active_conv.workspace_subpath, vault=cluster.vault
            )
            mcp_tools_only.extend(srv_tools)
            tools_payload.extend(srv_tools)
            t_names = [
                str(t.get("function", {}).get("name") or "")
                for t in srv_tools
                if t.get("function", {}).get("name")
            ]
            active_mcp_lines.append(
                f"- `{srv.id}` ({srv.name}): {srv.description or 'MCP server'}"
                + (f" — tools: {', '.join(t_names[:8])}" if t_names else " — (0 tools discovered)")
            )
        inactive_mcps = [s for s in all_installed_mcps if s.id not in mcp_map]
        mcp_ctx_lines: list[str] = []
        if active_mcp_lines:
            mcp_ctx_lines.append(
                "Enabled MCP Servers (use `mcp__<id>__<tool>` directly):\n" + "\n".join(active_mcp_lines)
            )
        if inactive_mcps:
            mcp_ctx_lines.append(
                "Installed-but-Inactive MCP Servers (not enabled in this chat): "
                + ", ".join(f"`{s.id}` ({s.name})" for s in inactive_mcps[:12])
            )
        mcp_ctx_lines.append(
            "If a task would benefit from an inactive or uninstalled MCP integration, call "
            "`suggest_mcp(query, reason)` to search local + registry MCPs and surface a 1-click recommendation card."
        )
        all_installed_skills = await asyncio.to_thread(scan_installed_skills, skills_dir)
        active_skill_set = set(eff_skills)
        inactive_skills = [s for s in all_installed_skills if s.id not in active_skill_set]
        if inactive_skills:
            mcp_ctx_lines.append(
                "Installed-but-Inactive Skills (not enabled in this chat): "
                + ", ".join(f"`{s.id}` ({s.name})" for s in inactive_skills[:12])
            )
        mcp_ctx_lines.append(
            "If a task would benefit from an inactive or uninstalled workflow skill, call "
            "`suggest_skill(query, reason)` to search local + skills.sh skills and surface a 1-click recommendation card."
        )
        sys_parts.append("\n".join(mcp_ctx_lines))
    if active_conv.web_search_enabled:
        tools_payload.extend(WEB_TOOLS_SCHEMA)
    if media_service is not None and media_service.has_healthy_backend():
        tools_payload.extend(MEDIA_TOOLS_SCHEMA)
    if active_conv.swarm_enabled:
        tools_payload.extend(SWARM_TOOLS_SCHEMA)
        sys_parts.append(
            "Swarm orchestration is enabled. For multi-part tasks that benefit from parallel "
            "research, review, or multi-node execution, call `spawn_agent` to launch focused "
            "workers (default `readonly=true`) and `wait_agents` to collect their results."
        )

    history_records = [
        m for m in all_history if m.created_at > active_conv.compacted_through_ts
    ]
    openai_msgs = _records_to_openai_messages("\n\n".join(sys_parts), history_records)
    return events, PreparedChatTurn(
        active_conv=active_conv,
        eff_skills=eff_skills,
        mcp_map=mcp_map,
        mcp_tools_only=mcp_tools_only,
        tools_payload=tools_payload,
        openai_msgs=openai_msgs,
    )


async def execute_discovery_tool(
    t_name: str,
    parsed_args: dict[str, Any],
    *,
    data_dir: Path,
    skills_dir: Path,
    mcp_map: dict[str, McpServerConfig],
    eff_skills: list[str],
) -> tuple[bool, str]:
    """Run `suggest_mcp` or `suggest_skill` against local installs and public registries."""
    q = str(parsed_args.get("query") or "").strip()
    reason = str(parsed_args.get("reason") or "").strip()
    if not q:
        return False, f"Missing 'query' argument for {t_name}."
    q_low = q.lower()

    if t_name == "suggest_mcp":
        installed_list = await asyncio.to_thread(load_all_mcp_configs, data_dir)
        local_matches = [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "enabled_in_chat": s.id in mcp_map,
                "server_enabled": s.enabled,
            }
            for s in installed_list
            if q_low in s.id.lower()
            or q_low in s.name.lower()
            or q_low in (s.description or "").lower()
        ][:6]
        reg_matches: list[dict[str, Any]] = []
        try:
            found_reg = await search_mcp_registry_items(q, limit=6)
            reg_matches = [
                {
                    "id": r.id,
                    "name": r.registry_name,
                    "registry_name": r.registry_name,
                    "description": r.description,
                    "source": r.source,
                }
                for r in found_reg
            ]
        except (httpx.HTTPError, ValueError):
            reg_matches = []
        return True, json.dumps(
            {
                "query": q,
                "reason": reason,
                "local_matches": local_matches,
                "registry_matches": reg_matches,
            }
        )

    installed_skills = await asyncio.to_thread(scan_installed_skills, skills_dir)
    active_sk_set = set(eff_skills)
    local_matches = [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "enabled_in_chat": s.id in active_sk_set,
            "server_enabled": s.enabled,
        }
        for s in installed_skills
        if q_low in s.id.lower()
        or q_low in s.name.lower()
        or q_low in (s.description or "").lower()
    ][:6]
    sk_reg_matches: list[dict[str, Any]] = []
    try:
        found_sk = await search_skills_registry_items(q, limit=6)
        sk_reg_matches = [
            {
                "id": r.id,
                "name": r.name,
                "source": r.source,
                "installs": r.installs,
            }
            for r in found_sk
        ]
    except (httpx.HTTPError, ValueError):
        sk_reg_matches = []
    return True, json.dumps(
        {
            "query": q,
            "reason": reason,
            "local_matches": local_matches,
            "registry_matches": sk_reg_matches,
        }
    )
