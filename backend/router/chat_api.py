"""
Splitch Agent Chat Backend:
- SQLite WAL persistence (off-thread via asyncio.to_thread, zero event-loop blocking)
- Native OpenAI streaming (tokens + reasoning_content + native tool_calls)
- Routes every turn through Splitch's real resolve_route() policy + Gaming Mode failover
- Workspace-sandboxed tool execution via target node's splitch-agent + HITL approval gates
- Injects selected Skills (SKILL.md) and live MCP server tools
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import time
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.router.chat_mcp import (
    call_mcp_tool as _call_mcp_tool,
)
from backend.router.chat_mcp import (
    fetch_mcp_tools as _fetch_mcp_tools,
)
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
from backend.router.swarm import SwarmManager
from backend.router.telemetry import ClusterState
from backend.router.web_search import WEB_TOOLS_SCHEMA, execute_web_fetch, execute_web_search
from backend.shared.models import (
    ApprovalDecision,
    ChatMessageRecord,
    ChatRulesConfig,
    ChatSendRequest,
    CompactResponse,
    ConversationBulkRequest,
    ConversationBulkResponse,
    ConversationCreate,
    ConversationSummary,
    ConversationUpdate,
    McpServerConfig,
    MediaGenerateRequest,
    ScopedExtensionsConfig,
    ScopeRule,
    SwarmRunRecord,
    SwarmWorkerMessage,
    TraceEntry,
    WorkspaceScopeUpdate,
)
from backend.shared.secrets import sign_tool_approval_ed25519
from backend.shared.security import sign_tool_approval


def _adjust_reasoning_effort_on_error(
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
    elif cur == "xhigh" and re.search(r"\bhigh\b", supported) and not re.search(r"\bxhigh\b", supported):
        updated["reasoning_effort"] = "high"
    elif cur == "none" and re.search(r"\boff\b", supported):
        updated["reasoning_effort"] = "off"
    else:
        updated.pop("reasoning_effort", None)
    return updated


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _matches_cmd_prefix_or_pattern(cmd: str, entries: list[str], *, anywhere: bool) -> bool:
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


def _requires_tool_approval(
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
        if _matches_cmd_prefix_or_pattern(cmd_str, denylist, anywhere=True):
            return True
        if conv_auto_approve or policy == "full_auto":
            return False
        if policy == "turbo_safe":
            allowlist = list(getattr(router_cfg, "shell_allowlist", []) or [])
            return not _matches_cmd_prefix_or_pattern(cmd_str, allowlist, anywhere=False)
        return True
    return not (conv_auto_approve or policy in ("auto_edits", "turbo_safe", "full_auto"))


def build_chat_router(
    cluster: ClusterState,
    data_dir: Path,
    skills_dir: Path,
    media_service: MediaService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/chat", tags=["chat"])
    store = ChatStore(data_dir / "chats.db")
    scopes_file = data_dir / "workspace_scopes.json"
    pending_approvals: dict[str, asyncio.Future[bool]] = {}
    swarm_mgr = SwarmManager(
        cluster,
        store,
        pending_approvals,
        _requires_tool_approval,
        _adjust_reasoning_effort_on_error,
        _call_mcp_tool,
    )

    def _norm_ws_key(subpath: str) -> str:
        return subpath.strip().strip("/") or "."

    def _load_scopes() -> ScopedExtensionsConfig:
        if not scopes_file.exists():
            return ScopedExtensionsConfig()
        try:
            return ScopedExtensionsConfig.model_validate(
                json.loads(scopes_file.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            return ScopedExtensionsConfig()

    def _save_scopes(cfg: ScopedExtensionsConfig) -> ScopedExtensionsConfig:
        data_dir.mkdir(parents=True, exist_ok=True)
        scopes_file.write_text(
            json.dumps(cfg.model_dump(mode="json"), indent=2), encoding="utf-8"
        )
        return cfg

    def _resolve_effective_extensions(
        workspace_subpath: str,
        chat_skills: list[str],
        chat_mcps: list[str],
    ) -> tuple[list[str], list[str]]:
        scopes = _load_scopes()
        ws_rule = scopes.workspaces.get(_norm_ws_key(workspace_subpath), ScopeRule())
        eff_skills = list(
            dict.fromkeys([*scopes.global_scope.skills, *ws_rule.skills, *chat_skills])
        )
        eff_mcps = list(
            dict.fromkeys([*scopes.global_scope.mcps, *ws_rule.mcps, *chat_mcps])
        )
        return eff_skills, eff_mcps

    def _load_selected_skills(skill_ids: list[str]) -> str:
        if not skill_ids or not skills_dir.exists():
            return ""
        blocks: list[str] = []
        for sid in skill_ids:
            md = (skills_dir / sid / "SKILL.md").resolve()
            if md.is_relative_to(skills_dir.resolve()) and md.is_file():
                with contextlib.suppress(OSError):
                    blocks.append(f"<skill id=\"{sid}\">\n{md.read_text(encoding='utf-8')[:8000]}\n</skill>")
        return "\n\n".join(blocks)

    def _load_all_mcp_configs() -> list[McpServerConfig]:
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

    def _load_mcp_configs(mcp_ids: list[str]) -> dict[str, McpServerConfig]:
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

    @router.get("/scopes", response_model=ScopedExtensionsConfig)
    async def get_scopes() -> ScopedExtensionsConfig:
        return await asyncio.to_thread(_load_scopes)

    def _rules_file() -> Path:
        return data_dir / "rules.json"

    def _load_rules() -> ChatRulesConfig:
        p = _rules_file()
        if not p.exists():
            return ChatRulesConfig()
        try:
            return ChatRulesConfig.model_validate(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return ChatRulesConfig()

    def _save_rules(cfg: ChatRulesConfig) -> ChatRulesConfig:
        data_dir.mkdir(parents=True, exist_ok=True)
        _rules_file().write_text(cfg.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return cfg

    @router.get("/rules", response_model=ChatRulesConfig)
    async def get_chat_rules() -> ChatRulesConfig:
        return await asyncio.to_thread(_load_rules)

    @router.put("/rules", response_model=ChatRulesConfig)
    async def put_chat_rules(cfg: ChatRulesConfig) -> ChatRulesConfig:
        return await asyncio.to_thread(_save_rules, cfg)

    @router.put("/scopes/global", response_model=ScopedExtensionsConfig)
    async def update_global_scope(rule: ScopeRule) -> ScopedExtensionsConfig:
        def _upd() -> ScopedExtensionsConfig:
            cfg = _load_scopes()
            cfg.global_scope = ScopeRule(
                skills=list(dict.fromkeys(rule.skills)),
                mcps=list(dict.fromkeys(rule.mcps)),
            )
            return _save_scopes(cfg)

        return await asyncio.to_thread(_upd)

    @router.put("/scopes/workspace", response_model=ScopedExtensionsConfig)
    async def update_workspace_scope(req: WorkspaceScopeUpdate) -> ScopedExtensionsConfig:
        def _upd() -> ScopedExtensionsConfig:
            cfg = _load_scopes()
            key = _norm_ws_key(req.workspace)
            skills = list(dict.fromkeys(req.skills))
            mcps = list(dict.fromkeys(req.mcps))
            if not skills and not mcps:
                cfg.workspaces.pop(key, None)
            else:
                cfg.workspaces[key] = ScopeRule(skills=skills, mcps=mcps)
            return _save_scopes(cfg)

        return await asyncio.to_thread(_upd)

    @router.get("/conversations", response_model=list[ConversationSummary])
    async def list_conversations() -> list[ConversationSummary]:
        return await asyncio.to_thread(store.list_conversations)

    @router.post("/conversations", response_model=ConversationSummary)
    async def create_conversation(req: ConversationCreate) -> ConversationSummary:
        if req.swarm_enabled is None:
            req = req.model_copy(update={"swarm_enabled": len(cluster.nodes) > 1})
        return await asyncio.to_thread(store.create_conversation, req)

    @router.get("/conversations/{conv_id}/swarms", response_model=list[SwarmRunRecord])
    async def list_conversation_swarms(conv_id: str) -> list[SwarmRunRecord]:
        return await asyncio.to_thread(store.list_swarm_runs, conv_id)

    @router.get("/swarms/agents/{agent_id}/messages", response_model=list[SwarmWorkerMessage])
    async def list_swarm_agent_messages(agent_id: str) -> list[SwarmWorkerMessage]:
        return await asyncio.to_thread(store.list_swarm_agent_messages, agent_id)

    @router.post("/swarms/agents/{agent_id}/cancel")
    async def cancel_swarm_agent(agent_id: str) -> dict:
        rec = await swarm_mgr.cancel_agent(agent_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Swarm agent not found")
        return {"ok": True, "agent": rec.model_dump()}

    @router.post("/conversations/bulk-delete", response_model=ConversationBulkResponse)
    async def bulk_delete_conversations(
        req: ConversationBulkRequest,
    ) -> ConversationBulkResponse:
        affected = await asyncio.to_thread(store.soft_delete_conversations, req.ids)
        return ConversationBulkResponse(ok=True, affected_count=len(affected), ids=affected)

    @router.post("/conversations/bulk-restore", response_model=ConversationBulkResponse)
    async def bulk_restore_conversations(
        req: ConversationBulkRequest,
    ) -> ConversationBulkResponse:
        affected = await asyncio.to_thread(store.restore_conversations, req.ids)
        return ConversationBulkResponse(ok=True, affected_count=len(affected), ids=affected)

    @router.post(
        "/conversations/bulk-permanent-delete", response_model=ConversationBulkResponse
    )
    async def bulk_permanent_delete_conversations(
        req: ConversationBulkRequest,
    ) -> ConversationBulkResponse:
        affected = await asyncio.to_thread(store.hard_delete_conversations, req.ids)
        return ConversationBulkResponse(ok=True, affected_count=len(affected), ids=affected)

    @router.patch("/conversations/{conv_id}", response_model=ConversationSummary)
    async def update_conversation(
        conv_id: str, patch: ConversationUpdate
    ) -> ConversationSummary:
        res = await asyncio.to_thread(store.update_conversation, conv_id, patch)
        if not res:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return res

    @router.delete("/conversations/{conv_id}")
    async def delete_conversation(conv_id: str) -> dict:
        deleted = await asyncio.to_thread(store.soft_delete_conversation, conv_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"ok": True, "trashed": conv_id}

    @router.get("/trash", response_model=list[ConversationSummary])
    async def list_trash() -> list[ConversationSummary]:
        return await asyncio.to_thread(store.list_trash)

    @router.post("/conversations/{conv_id}/restore", response_model=ConversationSummary)
    async def restore_conversation(conv_id: str) -> ConversationSummary:
        restored = await asyncio.to_thread(store.restore_conversation, conv_id)
        if not restored:
            raise HTTPException(status_code=404, detail="Conversation not found in trash")
        return restored

    @router.delete("/conversations/{conv_id}/permanent")
    async def permanent_delete_conversation(conv_id: str) -> dict:
        deleted = await asyncio.to_thread(store.hard_delete_conversation, conv_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"ok": True, "deleted": conv_id}

    @router.delete("/trash")
    async def empty_trash() -> dict:
        count = await asyncio.to_thread(store.empty_trash)
        return {"ok": True, "deleted_count": count}

    @router.get("/conversations/{conv_id}/messages", response_model=list[ChatMessageRecord])
    async def get_messages(conv_id: str) -> list[ChatMessageRecord]:
        if not await asyncio.to_thread(store.get_conversation, conv_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return await asyncio.to_thread(store.list_messages, conv_id)

    @router.delete("/messages/{message_id}")
    async def trim_message(message_id: str) -> dict:
        conv_id = await asyncio.to_thread(store.trim_from_message, message_id)
        if not conv_id:
            raise HTTPException(status_code=404, detail="Message not found")
        return {"ok": True, "conversation_id": conv_id}

    @router.post("/approvals/{approval_id}")
    async def resolve_approval(approval_id: str, decision: ApprovalDecision) -> dict:
        fut = pending_approvals.get(approval_id)
        if fut is None or fut.done():
            raise HTTPException(status_code=404, detail="Approval request expired or not found")
        fut.set_result(decision.approved)
        return {"ok": True, "approved": decision.approved}

    def _estimate_msgs_tokens(
        summary: str, sys_text: str, msgs: list[ChatMessageRecord]
    ) -> int:
        chars = len(sys_text) + len(summary)
        for m in msgs:
            chars += len(m.content or "") + len(m.reasoning or "") + len(json.dumps(m.tool_calls))
        return max(1, chars // 4)

    def _split_for_compaction(
        uncompacted: list[ChatMessageRecord], keep_recent: int = 6
    ) -> tuple[list[ChatMessageRecord], list[ChatMessageRecord]]:
        if len(uncompacted) <= keep_recent:
            return [], uncompacted
        split_idx = len(uncompacted) - keep_recent
        # Prefer starting kept tail on a user message so tool_calls/results never split mid-turn
        while split_idx > 2 and uncompacted[split_idx].role != "user":
            split_idx -= 1
        if split_idx < 2:
            return [], uncompacted
        return uncompacted[:split_idx], uncompacted[split_idx:]

    async def _run_compaction(
        conv: ConversationSummary, keep_recent: int = 6
    ) -> CompactResponse:
        all_msgs = await asyncio.to_thread(store.list_messages, conv.id)
        uncompacted = [m for m in all_msgs if m.created_at > conv.compacted_through_ts]
        older, recent = _split_for_compaction(uncompacted, keep_recent=keep_recent)
        est_before = _estimate_msgs_tokens(conv.compacted_summary, conv.system_prompt, uncompacted)
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
        summary_text = ""
        try:
            resp = await cluster.proxy_client.post(
                f"{decision.node.engine_url}/v1/chat/completions",
                json=compact_req,
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
            # Deterministic fallback summary if model emitted empty content
            summary_text = "\n".join(
                f"- **{m.role}**: {(m.content or '')[:180].replace(chr(10), ' ')}" for m in older[:12]
            )

        watermark = older[-1].created_at
        await asyncio.to_thread(store.save_compaction, conv.id, summary_text, watermark)
        est_after = _estimate_msgs_tokens(summary_text, conv.system_prompt, recent)
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

    @router.post("/conversations/{conv_id}/compact", response_model=CompactResponse)
    async def compact_conversation(conv_id: str, keep_recent: int = 4) -> CompactResponse:
        conv = await asyncio.to_thread(store.get_conversation, conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return await _run_compaction(conv, keep_recent=max(2, min(keep_recent, 12)))

    @router.delete("/conversations/{conv_id}/compact", response_model=ConversationSummary)
    async def clear_conversation_compaction(conv_id: str) -> ConversationSummary:
        cleared = await asyncio.to_thread(store.clear_compaction, conv_id)
        if not cleared:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return cleared

    @router.post("/conversations/{conv_id}/send")
    async def send_chat(conv_id: str, req: ChatSendRequest) -> StreamingResponse:
        conv = await asyncio.to_thread(store.get_conversation, conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

        user_msg = ChatMessageRecord(
            id=uuid.uuid4().hex[:12],
            conversation_id=conv_id,
            role="user",
            content=req.content,
            images=req.images,
            created_at=time.time(),
        )
        await asyncio.to_thread(store.add_message, user_msg)

        # Auto-title new conversations from first user message
        if conv.title == "New Chat" and req.content.strip():
            short_title = req.content.strip().splitlines()[0][:54]
            await asyncio.to_thread(
                store.update_conversation, conv_id, ConversationUpdate(title=short_title)
            )

        async def _event_stream() -> AsyncGenerator[bytes, None]:
            yield _sse("user_saved", user_msg.model_dump())

            # Assemble system prompt + global rules + injected skills + MCP tools
            sys_parts = [
                "You are Splitch, a precise local AI engineering assistant.",
                "Be concise, accurate, and use tools directly when inspecting or modifying workspace files.",
            ]
            rules_cfg = await asyncio.to_thread(_load_rules)
            if rules_cfg.global_rules.strip():
                sys_parts.append(f"Global User Rules:\n{rules_cfg.global_rules.strip()}")
            if conv.system_prompt.strip():
                sys_parts.append(conv.system_prompt.strip())

            eff_skills, eff_mcps = await asyncio.to_thread(
                _resolve_effective_extensions,
                conv.workspace_subpath,
                conv.enabled_skills,
                conv.enabled_mcps,
            )

            skills_text = await asyncio.to_thread(_load_selected_skills, eff_skills)
            if skills_text:
                sys_parts.append(f"Active Skills:\n{skills_text}")

            if conv.web_search_enabled:
                sys_parts.append(
                    "Web search is enabled. Use `web_search` for current facts or documentation, "
                    "optionally call `web_fetch` on a result URL for full details, and cite source URLs."
                )

            # Auto-compact older turns non-destructively when active context crosses 80%
            active_conv = conv
            all_history = await asyncio.to_thread(store.list_messages, conv_id)
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
            est_active_tokens = _estimate_msgs_tokens(
                active_conv.compacted_summary, "\n\n".join(sys_parts), uncompacted_msgs
            )
            compact_threshold = max(0.50, min(0.95, float(cluster.config.router.auto_compact_threshold)))
            if (
                active_conv.auto_compact
                and ctx_budget > 0
                and (est_active_tokens / ctx_budget) >= compact_threshold
                and len(uncompacted_msgs) > 7
            ):
                yield _sse(
                    "compacting",
                    {"estimated_tokens": est_active_tokens, "context_window": ctx_budget},
                )
                try:
                    c_res = await _run_compaction(active_conv, keep_recent=6)
                    if c_res.compacted:
                        refreshed = await asyncio.to_thread(store.get_conversation, conv_id)
                        if refreshed:
                            active_conv = refreshed
                        yield _sse("compacted", c_res.model_dump())
                except HTTPException:
                    pass

            if active_conv.compacted_summary.strip():
                sys_parts.append(
                    f"<conversation_summary>\n{active_conv.compacted_summary.strip()}\n</conversation_summary>"
                )

            mcp_map = await asyncio.to_thread(_load_mcp_configs, eff_mcps)
            all_installed_mcps = await asyncio.to_thread(_load_all_mcp_configs)
            tools_payload: list[dict[str, Any]] = []
            mcp_tools_only: list[dict[str, Any]] = []
            active_mcp_lines: list[str] = []
            if active_conv.tools_enabled:
                tools_payload.extend(WORKSPACE_TOOLS_SCHEMA)
                tools_payload.extend(MCP_DISCOVERY_TOOLS_SCHEMA)
                tools_payload.extend(SKILL_DISCOVERY_TOOLS_SCHEMA)
                for srv in mcp_map.values():
                    srv_tools = await _fetch_mcp_tools(
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
                    mcp_ctx_lines.append("Enabled MCP Servers (use `mcp__<id>__<tool>` directly):\n" + "\n".join(active_mcp_lines))
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
            openai_msgs: list[dict[str, Any]] = [
                {"role": "system", "content": "\n\n".join(sys_parts)}
            ]
            for m in history_records:
                if m.role == "user":
                    if m.images:
                        parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
                        for img in m.images:
                            parts.append({"type": "image_url", "image_url": {"url": img}})
                        openai_msgs.append({"role": "user", "content": parts})
                    else:
                        openai_msgs.append({"role": "user", "content": m.content})
                elif m.role == "assistant":
                    entry: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                    if m.tool_calls:
                        entry["tool_calls"] = m.tool_calls
                    openai_msgs.append(entry)
                elif m.role == "tool":
                    openai_msgs.append(
                        {
                            "role": "tool",
                            "tool_call_id": m.tool_call_id or "call_0",
                            "name": m.tool_name or "tool",
                            "content": m.content,
                        }
                    )

            active_swarm_run: SwarmRunRecord | None = None

            def _drain_swarm(run_id: str) -> list[bytes]:
                q = swarm_mgr.get_queue(run_id)
                out: list[bytes] = []
                while not q.empty():
                    ev, pl = q.get_nowait()
                    out.append(_sse(ev, pl))
                return out

            max_rounds = max(1, min(32, int(cluster.config.router.max_agent_steps)))
            try:
                for _round in range(max_rounds):
                    req_body: dict[str, Any] = {
                        "model": conv.model,
                        "messages": openai_msgs,
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    }
                    if req.temperature is not None:
                        req_body["temperature"] = req.temperature
                    if req.top_p is not None:
                        req_body["top_p"] = req.top_p
                    if req.top_k is not None:
                        req_body["top_k"] = req.top_k
                    if req.min_p is not None:
                        req_body["min_p"] = req.min_p
                    if req.repeat_penalty is not None:
                        req_body["repeat_penalty"] = req.repeat_penalty
                    if req.max_tokens is not None:
                        req_body["max_tokens"] = req.max_tokens
                    if req.seed is not None:
                        req_body["seed"] = req.seed
                    if req.stop:
                        req_body["stop"] = req.stop
                    if req.reasoning_effort is not None:
                        req_body["reasoning_effort"] = (
                            "none" if req.reasoning_effort == "off" else req.reasoning_effort
                        )
                    if req.enable_thinking is not None:
                        req_body["chat_template_kwargs"] = {"enable_thinking": req.enable_thinking}
                    if tools_payload:
                        req_body["tools"] = tools_payload

                    decision, skip_reasons = resolve_route(
                        endpoint="/v1/chat/completions",
                        requested_model=conv.model,
                        body=req_body,
                        nodes=cluster.nodes,
                        aliases=cluster.aliases,
                        control=cluster.control,
                        guards=cluster.config.guards,
                        heavy_char_threshold=cluster.config.router.auto_heavy_char_threshold,
                    )
                    if decision is None:
                        yield _sse(
                            "error",
                            {"message": f"No eligible Splitch node available: {skip_reasons}"},
                        )
                        return

                    req_body["model"] = decision.target_model
                    yield _sse(
                        "meta",
                        {
                            "node": decision.node.node_id,
                            "model": decision.target_model,
                            "fallback_reason": decision.fallback_reason,
                            "round": _round + 1,
                            "max_rounds": max_rounds,
                        },
                    )

                    started = time.perf_counter()
                    ttft_ms: float | None = None
                    content_buf: list[str] = []
                    reasoning_buf: list[str] = []
                    tool_calls_acc: dict[int, dict[str, Any]] = {}
                    approx_tokens = 0
                    prompt_tokens_real: int | None = None
                    completion_tokens_real: int | None = None
                    total_tokens_real: int | None = None
                    status_code = 200

                    decision.node.in_flight += 1
                    try:
                        for attempt in range(2):
                            async with cluster.proxy_client.stream(
                                "POST",
                                f"{decision.node.engine_url}/v1/chat/completions",
                                json=req_body,
                            ) as upstream:
                                status_code = upstream.status_code
                                if status_code >= 400:
                                    err_body = (await upstream.aread()).decode(
                                        "utf-8", errors="replace"
                                    )[:400]
                                    if attempt == 0:
                                        retry_body = _adjust_reasoning_effort_on_error(
                                            req_body, err_body
                                        )
                                        if retry_body is not None:
                                            req_body = retry_body
                                            continue
                                    yield _sse(
                                        "error", {"message": f"Upstream HTTP {status_code}: {err_body}"}
                                    )
                                    return

                                async for line in upstream.aiter_lines():
                                    if not line.startswith("data: "):
                                        continue
                                    raw_data = line[6:].strip()
                                    if not raw_data or raw_data == "[DONE]":
                                        continue
                                    try:
                                        chunk = json.loads(raw_data)
                                    except ValueError:
                                        continue

                                    usage = chunk.get("usage")
                                    if isinstance(usage, dict):
                                        if isinstance(usage.get("prompt_tokens"), int):
                                            prompt_tokens_real = usage["prompt_tokens"]
                                        if isinstance(usage.get("completion_tokens"), int):
                                            completion_tokens_real = usage["completion_tokens"]
                                        if isinstance(usage.get("total_tokens"), int):
                                            total_tokens_real = usage["total_tokens"]

                                    choices = chunk.get("choices") or []
                                    if not choices:
                                        continue
                                    delta = choices[0].get("delta") or {}

                                    rc = delta.get("reasoning_content")
                                    if rc:
                                        if ttft_ms is None:
                                            ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                                        approx_tokens += 1
                                        reasoning_buf.append(rc)
                                        yield _sse("reasoning", {"text": rc})

                                    txt = delta.get("content")
                                    if txt:
                                        if ttft_ms is None:
                                            ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                                        approx_tokens += 1
                                        content_buf.append(txt)
                                        yield _sse("token", {"text": txt})

                                    for tc in delta.get("tool_calls") or []:
                                        if not isinstance(tc, dict):
                                            continue
                                        idx = int(tc.get("index", 0))
                                        slot = tool_calls_acc.setdefault(
                                            idx,
                                            {
                                                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                                "type": "function",
                                                "function": {"name": "", "arguments": ""},
                                            },
                                        )
                                        if tc.get("id"):
                                            slot["id"] = tc["id"]
                                        fn = tc.get("function") or {}
                                        if fn.get("name"):
                                            slot["function"]["name"] += fn["name"]
                                        if fn.get("arguments"):
                                            slot["function"]["arguments"] += fn["arguments"]
                                break
                    except httpx.HTTPError as exc:
                        yield _sse("error", {"message": f"Stream error: {exc}"})
                        return
                    finally:
                        decision.node.in_flight = max(0, decision.node.in_flight - 1)

                    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
                    final_comp_tokens = (
                        completion_tokens_real if completion_tokens_real is not None else approx_tokens
                    )
                    est_prompt_tokens = max(1, len(json.dumps(openai_msgs)) // 4)
                    final_prompt_tokens = (
                        prompt_tokens_real if prompt_tokens_real is not None else est_prompt_tokens
                    )
                    final_total_tokens = (
                        total_tokens_real
                        if total_tokens_real is not None
                        else (final_prompt_tokens + final_comp_tokens)
                    )
                    ctx_window = (
                        decision.node.telemetry.context_length
                        if decision.node.telemetry and decision.node.telemetry.context_length
                        else 32768
                    )
                    gen_s = max(0.001, (elapsed_ms - (ttft_ms or 0.0)) / 1000.0)
                    tps = round(final_comp_tokens / gen_s, 1) if final_comp_tokens > 0 else None

                    asyncio.create_task(
                        cluster.record_trace(
                            TraceEntry(
                                id=uuid.uuid4().hex[:10],
                                timestamp=time.time(),
                                endpoint="/api/chat/send",
                                requested_model=conv.model,
                                resolved_node=decision.node.node_id,
                                resolved_model=decision.target_model,
                                fallback_reason=decision.fallback_reason,
                                status_code=status_code,
                                stream=True,
                                ttft_ms=ttft_ms,
                                duration_ms=elapsed_ms,
                                completion_tokens=final_comp_tokens,
                                tokens_per_sec=tps,
                            )
                        )
                    )

                    assembled_tools = [tool_calls_acc[k] for k in sorted(tool_calls_acc.keys())]
                    assistant_meta: dict[str, Any] = {
                        "node": decision.node.node_id,
                        "model": decision.target_model,
                        "fallback_reason": decision.fallback_reason,
                        "ttft_ms": ttft_ms,
                        "duration_ms": elapsed_ms,
                        "tokens_per_sec": tps,
                        "prompt_tokens": final_prompt_tokens,
                        "completion_tokens": final_comp_tokens,
                        "total_tokens": final_total_tokens,
                        "context_window": ctx_window,
                    }
                    if assembled_tools and _round == max_rounds - 1:
                        assistant_meta["max_rounds_reached"] = True
                        assistant_meta["steps_used"] = max_rounds
                        assistant_meta["max_steps"] = max_rounds
                    asst_msg = ChatMessageRecord(
                        id=uuid.uuid4().hex[:12],
                        conversation_id=conv_id,
                        role="assistant",
                        content="".join(content_buf),
                        reasoning="".join(reasoning_buf),
                        tool_calls=assembled_tools,
                        meta=assistant_meta,
                        created_at=time.time(),
                    )
                    await asyncio.to_thread(store.add_message, asst_msg)

                    if not assembled_tools:
                        if active_swarm_run is not None:
                            await swarm_mgr.finish_run(active_swarm_run.id, "done")
                            for pkt in _drain_swarm(active_swarm_run.id):
                                yield pkt
                            active_swarm_run = None
                        yield _sse("done", {"message": asst_msg.model_dump()})
                        return

                    # Execute native tool calls and loop back to model
                    openai_msgs.append(
                        {
                            "role": "assistant",
                            "content": asst_msg.content or None,
                            "tool_calls": assembled_tools,
                        }
                    )

                    for tc in assembled_tools:
                        t_id = tc["id"]
                        t_name = tc["function"]["name"]
                        raw_args = tc["function"]["arguments"]
                        try:
                            parsed_args = json.loads(raw_args) if raw_args else {}
                        except ValueError:
                            parsed_args = {}

                        yield _sse("tool_call", {"id": t_id, "name": t_name, "arguments": parsed_args})

                        is_external_mcp = False
                        external_mcp_target: str | None = None
                        if t_name.startswith("mcp__"):
                            _m_parts = t_name.split("__", 2)
                            _m_srv = mcp_map.get(_m_parts[1] if len(_m_parts) == 3 else "")
                            if _m_srv and _m_srv.egress != "local":
                                is_external_mcp = True
                                external_mcp_target = _m_srv.egress_target or _m_srv.url or _m_srv.id

                        needs_approval = _requires_tool_approval(
                            t_name,
                            parsed_args,
                            is_external_mcp=is_external_mcp,
                            conv_auto_approve=conv.auto_approve,
                            router_cfg=cluster.config.router,
                        )
                        approved = True
                        if needs_approval:
                            approval_id = uuid.uuid4().hex[:10]
                            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                            pending_approvals[approval_id] = fut
                            target_sandbox: str | None = None
                            target_node_label = conv.exec_node
                            if not is_external_mcp:
                                preview_node = cluster.nodes.get(conv.exec_node) if conv.exec_node else None
                                if preview_node is None or not preview_node.online:
                                    preview_node = next(
                                        (
                                            n
                                            for n in cluster.nodes.values()
                                            if n.online and n.telemetry and n.telemetry.tools_enabled
                                        ),
                                        next((n for n in cluster.nodes.values() if n.online), None),
                                    )
                                if preview_node is not None:
                                    target_node_label = preview_node.node_id
                                    if t_name == "run_command":
                                        target_sandbox = (
                                            (preview_node.telemetry.cmd_sandbox or "unknown")
                                            if preview_node.telemetry
                                            else "unknown"
                                        )
                            yield _sse(
                                "approval_required",
                                {
                                    "approval_id": approval_id,
                                    "tool_call_id": t_id,
                                    "name": t_name,
                                    "arguments": parsed_args,
                                    "exec_node": (
                                        f"EXTERNAL MCP ({external_mcp_target})"
                                        if is_external_mcp
                                        else target_node_label
                                    ),
                                    "sandbox": target_sandbox,
                                },
                            )
                            try:
                                approved = await asyncio.wait_for(fut, timeout=300.0)
                            except TimeoutError:
                                approved = False
                            finally:
                                pending_approvals.pop(approval_id, None)

                        tool_diff: str | None = None
                        tool_verification: dict | None = None
                        tool_sandbox: str | None = None
                        tool_media_url: str | None = None
                        tool_media_kind: str | None = None
                        if not approved:
                            tool_output = "Tool execution denied by user."
                            tool_ok = False
                        elif t_name == "web_search":
                            tool_ok, tool_output = await execute_web_search(
                                str(parsed_args.get("query", "")),
                                int(parsed_args.get("max_results") or 6),
                            )
                        elif t_name == "web_fetch":
                            tool_ok, tool_output = await execute_web_fetch(
                                str(parsed_args.get("url", ""))
                            )
                        elif t_name == "suggest_mcp":
                            q_mcp = str(parsed_args.get("query") or "").strip()
                            reason_mcp = str(parsed_args.get("reason") or "").strip()
                            if not q_mcp:
                                tool_ok = False
                                tool_output = "Missing 'query' argument for suggest_mcp."
                            else:
                                q_low = q_mcp.lower()
                                installed_list = await asyncio.to_thread(_load_all_mcp_configs)
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
                                    found_reg = await search_mcp_registry_items(q_mcp, limit=6)
                                    reg_matches = [
                                        {
                                            "id": r.id,
                                            "name": r.name,
                                            "registry_name": r.registry_name,
                                            "description": r.description,
                                            "source": r.source,
                                        }
                                        for r in found_reg
                                    ]
                                except (httpx.HTTPError, ValueError):
                                    reg_matches = []
                                tool_ok = True
                                tool_output = json.dumps(
                                    {
                                        "query": q_mcp,
                                        "reason": reason_mcp,
                                        "local_matches": local_matches,
                                        "registry_matches": reg_matches,
                                    }
                                )
                        elif t_name == "suggest_skill":
                            q_sk = str(parsed_args.get("query") or "").strip()
                            reason_sk = str(parsed_args.get("reason") or "").strip()
                            if not q_sk:
                                tool_ok = False
                                tool_output = "Missing 'query' argument for suggest_skill."
                            else:
                                q_low = q_sk.lower()
                                installed_skills = await asyncio.to_thread(
                                    scan_installed_skills, skills_dir
                                )
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
                                reg_matches = []
                                try:
                                    found_sk = await search_skills_registry_items(q_sk, limit=6)
                                    reg_matches = [
                                        {
                                            "id": r.id,
                                            "name": r.name,
                                            "source": r.source,
                                            "installs": r.installs,
                                        }
                                        for r in found_sk
                                    ]
                                except (httpx.HTTPError, ValueError):
                                    reg_matches = []
                                tool_ok = True
                                tool_output = json.dumps(
                                    {
                                        "query": q_sk,
                                        "reason": reason_sk,
                                        "local_matches": local_matches,
                                        "registry_matches": reg_matches,
                                    }
                                )
                        elif t_name == "spawn_agent":
                            if not active_conv.swarm_enabled:
                                tool_ok = False
                                tool_output = "Swarm orchestration is disabled for this conversation."
                            else:
                                if active_swarm_run is None:
                                    active_swarm_run = await swarm_mgr.start_run(active_conv)
                                    for pkt in _drain_swarm(active_swarm_run.id):
                                        yield pkt
                                w_agent = await swarm_mgr.spawn_worker(
                                    run_id=active_swarm_run.id,
                                    conv=active_conv,
                                    role=str(parsed_args.get("role") or "Worker"),
                                    task=str(parsed_args.get("task") or ""),
                                    model=str(parsed_args.get("model") or "auto"),
                                    node_id=str(parsed_args.get("node_id") or ""),
                                    readonly=bool(parsed_args.get("readonly", True)),
                                    mcp_map=mcp_map,
                                    mcp_tools=mcp_tools_only,
                                )
                                for pkt in _drain_swarm(active_swarm_run.id):
                                    yield pkt
                                tool_ok = True
                                tool_output = json.dumps(
                                    {
                                        "ok": True,
                                        "run_id": active_swarm_run.id,
                                        "agent_id": w_agent.id,
                                        "role": w_agent.role,
                                        "status": w_agent.status,
                                    }
                                )
                        elif t_name == "wait_agents":
                            if active_swarm_run is None:
                                tool_ok = False
                                tool_output = "No active swarm run in this turn."
                            else:
                                raw_ids = parsed_args.get("agent_ids")
                                id_list = [str(x) for x in raw_ids] if isinstance(raw_ids, list) else None
                                timeout_s = float(parsed_args.get("timeout_s") or 180.0)
                                wait_task = asyncio.create_task(
                                    swarm_mgr.wait_agents(
                                        active_swarm_run.id, agent_ids=id_list, timeout_s=timeout_s
                                    )
                                )
                                q_swarm = swarm_mgr.get_queue(active_swarm_run.id)
                                while not wait_task.done():
                                    try:
                                        ev, pl = await asyncio.wait_for(q_swarm.get(), timeout=0.15)
                                        yield _sse(ev, pl)
                                    except TimeoutError:
                                        continue
                                for pkt in _drain_swarm(active_swarm_run.id):
                                    yield pkt
                                finished_agents = await wait_task
                                tool_ok = True
                                tool_output = json.dumps(
                                    [
                                        {
                                            "agent_id": a.id,
                                            "role": a.role,
                                            "status": a.status,
                                            "node_id": a.node_id,
                                            "model": a.model,
                                            "result": a.result,
                                            "error": a.error,
                                        }
                                        for a in finished_agents
                                    ]
                                )
                        elif t_name == "list_agents":
                            if active_swarm_run is None:
                                tool_ok = True
                                tool_output = "[]"
                            else:
                                cur_agents = await asyncio.to_thread(
                                    store.list_swarm_agents, active_swarm_run.id
                                )
                                tool_ok = True
                                tool_output = json.dumps([a.model_dump() for a in cur_agents])
                        elif t_name == "cancel_agent":
                            aid = str(parsed_args.get("agent_id") or "")
                            c_agent = await swarm_mgr.cancel_agent(aid)
                            if active_swarm_run is not None:
                                for pkt in _drain_swarm(active_swarm_run.id):
                                    yield pkt
                            tool_ok = c_agent is not None
                            tool_output = (
                                json.dumps({"ok": True, "agent_id": aid, "status": c_agent.status})
                                if c_agent
                                else f"Swarm agent '{aid}' not found."
                            )
                        elif t_name in ("generate_image", "generate_video"):
                            if media_service is None:
                                tool_ok = False
                                tool_output = "Media generation service is not configured."
                            else:
                                is_vid = t_name == "generate_video"
                                try:
                                    m_job = await media_service.generate_sync(
                                        MediaGenerateRequest(
                                            kind="video" if is_vid else "image",
                                            prompt=str(parsed_args.get("prompt", "")),
                                            negative_prompt=str(parsed_args.get("negative_prompt", "")),
                                            model=str(parsed_args.get("model", "")),
                                            width=int(parsed_args.get("width") or (768 if is_vid else 1024)),
                                            height=int(parsed_args.get("height") or (512 if is_vid else 1024)),
                                            steps=int(parsed_args.get("steps") or 20),
                                            frames=int(parsed_args.get("frames") or 16),
                                            fps=int(parsed_args.get("fps") or 8),
                                        )
                                    )
                                    if m_job.status == "completed" and m_job.file_url:
                                        tool_ok = True
                                        tool_media_url = m_job.file_url
                                        tool_media_kind = m_job.kind
                                        tool_output = json.dumps(
                                            {
                                                "ok": True,
                                                "kind": m_job.kind,
                                                "url": m_job.file_url,
                                                "node": m_job.node_id,
                                                "engine": m_job.media_engine,
                                                "model": m_job.model,
                                                "seed": m_job.seed,
                                                "size": f"{m_job.width}x{m_job.height}",
                                                "duration_ms": m_job.duration_ms,
                                            }
                                        )
                                    else:
                                        tool_ok = False
                                        tool_output = f"Media generation failed: {m_job.error or 'unknown error'}"
                                except HTTPException as exc:
                                    tool_ok = False
                                    tool_output = f"Media generation unavailable: {exc.detail}"
                                except (httpx.HTTPError, OSError, RuntimeError, ValueError, TypeError) as exc:
                                    tool_ok = False
                                    tool_output = f"Media generation error: {exc}"
                        elif t_name.startswith("mcp__"):
                            parts = t_name.split("__", 2)
                            srv_id = parts[1] if len(parts) == 3 else ""
                            real_tool = parts[2] if len(parts) == 3 else t_name
                            srv_cfg = mcp_map.get(srv_id)
                            if not srv_cfg:
                                tool_output = f"MCP server '{srv_id}' not enabled for this chat."
                                tool_ok = False
                            else:
                                tool_output = await _call_mcp_tool(
                                    srv_cfg,
                                    real_tool,
                                    parsed_args,
                                    active_conv.workspace_subpath,
                                    vault=cluster.vault,
                                )
                                tool_ok = not tool_output.startswith("MCP error:")
                        else:
                            exec_node = cluster.nodes.get(conv.exec_node) if conv.exec_node else None
                            if exec_node is None or not exec_node.online:
                                exec_node = next(
                                    (
                                        n
                                        for n in cluster.nodes.values()
                                        if n.online and n.telemetry and n.telemetry.tools_enabled
                                    ),
                                    next((n for n in cluster.nodes.values() if n.online), None),
                                )
                            if not exec_node or not exec_node.online:
                                tool_output = f"Execution node '{conv.exec_node or 'auto'}' is offline."
                                tool_ok = False
                            else:
                                try:
                                    approval_nonce = secrets.token_hex(8)
                                    tool_headers: dict[str, str] = {
                                        **cluster.agent_headers("POST", "/tools/execute"),
                                        "X-Splitch-Router-Approved": "1",
                                        "X-Splitch-Approval-Nonce": approval_nonce,
                                    }
                                    if cluster.router_private_key is not None:
                                        ts_hdr, sig_hdr = sign_tool_approval_ed25519(
                                            cluster.router_private_key,
                                            t_name,
                                            parsed_args,
                                            conv.workspace_subpath,
                                            nonce=approval_nonce,
                                        )
                                        tool_headers["X-Splitch-Approval-Ts"] = ts_hdr
                                        tool_headers["X-Splitch-Approval-Sig"] = sig_hdr
                                    elif cluster.agent_token:
                                        ts_hdr, sig_hdr = sign_tool_approval(
                                            cluster.agent_token,
                                            t_name,
                                            parsed_args,
                                            conv.workspace_subpath,
                                            nonce=approval_nonce,
                                        )
                                        tool_headers["X-Splitch-Approval-Ts"] = ts_hdr
                                        tool_headers["X-Splitch-Approval-Sig"] = sig_hdr
                                    async with httpx.AsyncClient(
                                        timeout=25.0,
                                        headers=tool_headers,
                                        verify=cluster.httpx_verify,
                                    ) as http:
                                        r = await http.post(
                                            f"{exec_node.agent_url}/tools/execute",
                                            json={
                                                "tool": t_name,
                                                "arguments": parsed_args,
                                                "workspace_subpath": conv.workspace_subpath,
                                            },
                                        )
                                        if r.status_code == 200:
                                            body = r.json()
                                            tool_ok = bool(body.get("ok", False))
                                            tool_output = str(body.get("output", ""))
                                            tool_diff = body.get("diff")
                                            tool_verification = body.get("verification")
                                            tool_sandbox = body.get("sandbox")
                                        else:
                                            tool_ok = False
                                            tool_output = f"HTTP {r.status_code}: {r.text[:300]}"
                                except httpx.HTTPError as exc:
                                    tool_ok = False
                                    tool_output = f"Agent tool call failed: {exc}"

                        tool_msg = ChatMessageRecord(
                            id=uuid.uuid4().hex[:12],
                            conversation_id=conv_id,
                            role="tool",
                            content=tool_output,
                            tool_call_id=t_id,
                            tool_name=t_name,
                            meta={
                                "ok": tool_ok,
                                "diff": tool_diff,
                                "verification": tool_verification,
                                "sandbox": tool_sandbox,
                                "media_url": tool_media_url,
                                "media_kind": tool_media_kind,
                            },
                            created_at=time.time(),
                        )
                        await asyncio.to_thread(store.add_message, tool_msg)
                        openai_msgs.append(
                            {
                                "role": "tool",
                                "tool_call_id": t_id,
                                "name": t_name,
                                "content": tool_output,
                            }
                        )
                        yield _sse(
                            "tool_result",
                            {
                                "id": t_id,
                                "name": t_name,
                                "ok": tool_ok,
                                "output": tool_output,
                                "diff": tool_diff,
                                "verification": tool_verification,
                                "sandbox": tool_sandbox,
                                "media_url": tool_media_url,
                                "media_kind": tool_media_kind,
                            },
                        )

                if active_swarm_run is not None:
                    await swarm_mgr.finish_run(active_swarm_run.id, "done")
                    for pkt in _drain_swarm(active_swarm_run.id):
                        yield pkt
                    active_swarm_run = None
                yield _sse(
                    "done",
                    {
                        "message": asst_msg.model_dump(),
                        "max_rounds_reached": True,
                        "max_rounds": max_rounds,
                    },
                )
            finally:
                if active_swarm_run is not None:
                    await swarm_mgr.cancel_run(active_swarm_run.id)

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
