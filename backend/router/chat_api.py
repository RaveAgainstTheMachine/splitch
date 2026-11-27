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
import json
import secrets
import time
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.router.chat_context import (
    adjust_reasoning_effort_on_error as _adjust_reasoning_effort_on_error,
)
from backend.router.chat_context import (
    execute_discovery_tool,
    load_chat_rules,
    load_scopes,
    norm_ws_key,
    prepare_chat_turn,
    run_compaction,
    save_chat_rules,
    save_scopes,
)
from backend.router.chat_context import (
    matches_cmd_prefix_or_pattern as _matches_cmd_prefix_or_pattern,  # noqa: F401
)
from backend.router.chat_context import (
    requires_tool_approval as _requires_tool_approval,
)
from backend.router.chat_context import (
    sse_event as _sse,
)
from backend.router.chat_mcp import (
    call_mcp_tool as _call_mcp_tool,
)
from backend.router.chat_store import ChatStore
from backend.router.media_api import MediaService
from backend.router.policy import resolve_route
from backend.router.swarm import SwarmManager
from backend.router.telemetry import ClusterState
from backend.router.web_search import execute_web_fetch, execute_web_search
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

    @router.get("/scopes", response_model=ScopedExtensionsConfig)
    async def get_scopes() -> ScopedExtensionsConfig:
        return await asyncio.to_thread(load_scopes, scopes_file)

    @router.get("/rules", response_model=ChatRulesConfig)
    async def get_chat_rules() -> ChatRulesConfig:
        return await asyncio.to_thread(load_chat_rules, data_dir)

    @router.put("/rules", response_model=ChatRulesConfig)
    async def put_chat_rules(cfg: ChatRulesConfig) -> ChatRulesConfig:
        return await asyncio.to_thread(save_chat_rules, data_dir, cfg)

    @router.put("/scopes/global", response_model=ScopedExtensionsConfig)
    async def update_global_scope(rule: ScopeRule) -> ScopedExtensionsConfig:
        def _upd() -> ScopedExtensionsConfig:
            cfg = load_scopes(scopes_file)
            cfg.global_scope = ScopeRule(
                skills=list(dict.fromkeys(rule.skills)),
                mcps=list(dict.fromkeys(rule.mcps)),
            )
            return save_scopes(data_dir, scopes_file, cfg)

        return await asyncio.to_thread(_upd)

    @router.put("/scopes/workspace", response_model=ScopedExtensionsConfig)
    async def update_workspace_scope(req: WorkspaceScopeUpdate) -> ScopedExtensionsConfig:
        def _upd() -> ScopedExtensionsConfig:
            cfg = load_scopes(scopes_file)
            key = norm_ws_key(req.workspace)
            skills = list(dict.fromkeys(req.skills))
            mcps = list(dict.fromkeys(req.mcps))
            if not skills and not mcps:
                cfg.workspaces.pop(key, None)
            else:
                cfg.workspaces[key] = ScopeRule(skills=skills, mcps=mcps)
            return save_scopes(data_dir, scopes_file, cfg)

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

    @router.post("/conversations/{conv_id}/compact", response_model=CompactResponse)
    async def compact_conversation(conv_id: str, keep_recent: int = 4) -> CompactResponse:
        conv = await asyncio.to_thread(store.get_conversation, conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return await run_compaction(
            cluster, store, conv, keep_recent=max(2, min(keep_recent, 12))
        )

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

            prep_events, turn = await prepare_chat_turn(
                conv=conv,
                cluster=cluster,
                store=store,
                data_dir=data_dir,
                skills_dir=skills_dir,
                scopes_file=scopes_file,
                media_service=media_service,
            )
            for pkt in prep_events:
                yield pkt
            active_conv = turn.active_conv
            eff_skills = turn.eff_skills
            mcp_map = turn.mcp_map
            mcp_tools_only = turn.mcp_tools_only
            tools_payload = turn.tools_payload
            openai_msgs = turn.openai_msgs

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

                    exclude_nodes: set[str] = set()
                    max_failovers = max(2, len(cluster.nodes) + 1)
                    upstream_ok = False
                    compacted_on_overflow = False
                    last_err_msg = ""

                    for _failover_idx in range(max_failovers):
                        decision, skip_reasons = resolve_route(
                            endpoint="/v1/chat/completions",
                            requested_model=conv.model,
                            body=req_body,
                            nodes=cluster.nodes,
                            aliases=cluster.aliases,
                            control=cluster.control,
                            guards=cluster.config.guards,
                            heavy_char_threshold=cluster.config.router.auto_heavy_char_threshold,
                            exclude_nodes=exclude_nodes or None,
                        )
                        if decision is None:
                            if last_err_msg:
                                yield _sse("error", {"message": last_err_msg})
                            else:
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
                        retry_next_node = False

                        decision.node.in_flight += 1
                        try:
                            for attempt in range(2):
                                target_url, upstream_auth = cluster.resolve_upstream_target(
                                    decision, "/v1/chat/completions"
                                )
                                async with cluster.proxy_client.stream(
                                    "POST",
                                    target_url,
                                    json=req_body,
                                    auth=upstream_auth,
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
                                        err_low = err_body.lower()
                                        if (
                                            status_code == 400
                                            and active_conv.auto_compact
                                            and not compacted_on_overflow
                                            and (
                                                "exceed_context_size_error" in err_low
                                                or "exceeds the available context size" in err_low
                                            )
                                        ):
                                            compacted_on_overflow = True
                                            try:
                                                c_res = await run_compaction(
                                                    cluster, store, active_conv, keep_recent=6
                                                )
                                                if c_res.compacted:
                                                    refreshed = await asyncio.to_thread(
                                                        store.get_conversation, conv.id
                                                    )
                                                    if refreshed:
                                                        active_conv = refreshed
                                                    yield _sse("compacted", c_res.model_dump())
                                                    _, rebuilt_turn = await prepare_chat_turn(
                                                        conv=active_conv,
                                                        cluster=cluster,
                                                        store=store,
                                                        data_dir=data_dir,
                                                        skills_dir=skills_dir,
                                                        scopes_file=scopes_file,
                                                        media_service=media_service,
                                                    )
                                                    openai_msgs = rebuilt_turn.openai_msgs
                                                    req_body["messages"] = openai_msgs
                                                    retry_next_node = True
                                                    break
                                            except HTTPException:
                                                pass
                                        last_err_msg = f"Upstream HTTP {status_code}: {err_body}"
                                        if status_code in (502, 503, 504):
                                            exclude_nodes.add(decision.node.node_id)
                                            retry_next_node = True
                                            break
                                        yield _sse("error", {"message": last_err_msg})
                                        return

                                    upstream_ok = True
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
                                                ttft_ms = round(
                                                    (time.perf_counter() - started) * 1000.0, 1
                                                )
                                            approx_tokens += 1
                                            reasoning_buf.append(rc)
                                            yield _sse("reasoning", {"text": rc})

                                        txt = delta.get("content")
                                        if txt:
                                            if ttft_ms is None:
                                                ttft_ms = round(
                                                    (time.perf_counter() - started) * 1000.0, 1
                                                )
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
                                                    "id": tc.get("id")
                                                    or f"call_{uuid.uuid4().hex[:8]}",
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
                            if not upstream_ok:
                                last_err_msg = f"Stream error: {exc}"
                                exclude_nodes.add(decision.node.node_id)
                                retry_next_node = True
                            else:
                                yield _sse("error", {"message": f"Stream error: {exc}"})
                                return
                        finally:
                            decision.node.in_flight = max(0, decision.node.in_flight - 1)

                        if upstream_ok or not retry_next_node:
                            break

                    if not upstream_ok:
                        yield _sse("error", {"message": last_err_msg or "Upstream request failed"})
                        return

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
                        elif t_name in ("suggest_mcp", "suggest_skill"):
                            try:
                                tool_ok, tool_output = await execute_discovery_tool(
                                    t_name,
                                    parsed_args,
                                    data_dir=data_dir,
                                    skills_dir=skills_dir,
                                    mcp_map=mcp_map,
                                    eff_skills=eff_skills,
                                )
                            except Exception as exc:  # noqa: BLE001
                                tool_ok = False
                                tool_output = f"Discovery tool error ({t_name}): {exc}"
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
                                        auth=cluster.agent_auth(),
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
