"""Router-native multi-agent SwarmManager for parallel local cluster orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from backend.router.chat_store import (
    APPROVAL_REQUIRED_TOOLS,
    WORKSPACE_TOOLS_SCHEMA,
    ChatStore,
)
from backend.router.policy import resolve_route
from backend.router.telemetry import ClusterState
from backend.router.web_search import WEB_TOOLS_SCHEMA, execute_web_fetch, execute_web_search
from backend.shared.models import (
    ConversationSummary,
    McpServerConfig,
    SwarmAgentRecord,
    SwarmRunRecord,
    SwarmWorkerMessage,
)
from backend.shared.secrets import sign_tool_approval_ed25519


class SwarmManager:
    """Manages async Swarm worker execution, concurrency limits, approvals, and SSE events."""

    def __init__(
        self,
        cluster: ClusterState,
        store: ChatStore,
        pending_approvals: dict[str, asyncio.Future[bool]],
        approval_checker: Callable[..., bool],
        adjust_reasoning_fn: Callable[[dict[str, Any], str], dict[str, Any] | None],
        mcp_caller: Callable[..., Any],
    ) -> None:
        self.cluster = cluster
        self.store = store
        self.pending_approvals = pending_approvals
        self._requires_tool_approval = approval_checker
        self._adjust_reasoning = adjust_reasoning_fn
        self._call_mcp_tool = mcp_caller
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._run_agents: dict[str, list[str]] = {}
        self._queues: dict[str, asyncio.Queue[tuple[str, dict[str, Any]]]] = {}
        self._sem_limit: int = 0
        self._sem: asyncio.Semaphore | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        limit = max(1, min(6, int(self.cluster.config.router.max_parallel_agents)))
        if self._sem is None or self._sem_limit != limit:
            self._sem = asyncio.Semaphore(limit)
            self._sem_limit = limit
        return self._sem

    def get_queue(self, run_id: str) -> asyncio.Queue[tuple[str, dict[str, Any]]]:
        q = self._queues.get(run_id)
        if q is None:
            q = asyncio.Queue()
            self._queues[run_id] = q
        return q

    def _emit(self, run_id: str, event: str, payload: dict[str, Any]) -> None:
        q = self._queues.get(run_id)
        if q is not None:
            q.put_nowait((event, payload))

    async def start_run(self, conv: ConversationSummary) -> SwarmRunRecord:
        run = await asyncio.to_thread(self.store.create_swarm_run, conv.id)
        self._run_agents[run.id] = []
        self.get_queue(run.id)
        self._emit(run.id, "swarm_run_started", run.model_dump())
        return run

    async def finish_run(self, run_id: str, status: str = "done") -> None:
        await asyncio.to_thread(self.store.update_swarm_run_status, run_id, status)
        agents = await asyncio.to_thread(self.store.list_swarm_agents, run_id)
        self._emit(
            run_id,
            "swarm_run_done",
            {"run_id": run_id, "status": status, "agents": [a.model_dump() for a in agents]},
        )

    async def spawn_worker(
        self,
        *,
        run_id: str,
        conv: ConversationSummary,
        role: str,
        task: str,
        model: str = "auto",
        node_id: str = "",
        readonly: bool = True,
        mcp_map: dict[str, McpServerConfig] | None = None,
        mcp_tools: list[dict[str, Any]] | None = None,
    ) -> SwarmAgentRecord:
        max_rounds = max(1, min(16, int(self.cluster.config.router.max_agent_steps)))
        agent = await asyncio.to_thread(
            self.store.create_swarm_agent,
            run_id=run_id,
            conversation_id=conv.id,
            role=role.strip()[:48] or "Worker",
            task=task.strip(),
            model=(model or "auto").strip() or "auto",
            node_id=(node_id or "").strip(),
            readonly=bool(readonly),
            max_rounds=max_rounds,
        )
        self._run_agents.setdefault(run_id, []).append(agent.id)
        self._emit(run_id, "swarm_agent_update", agent.model_dump())

        t = asyncio.create_task(
            self._run_worker(
                agent=agent,
                conv=conv,
                mcp_map=mcp_map or {},
                mcp_tools=mcp_tools or [],
            )
        )
        self._tasks[agent.id] = t
        return agent

    async def cancel_agent(self, agent_id: str) -> SwarmAgentRecord | None:
        task = self._tasks.get(agent_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        rec = await asyncio.to_thread(self.store.get_swarm_agent, agent_id)
        if rec and rec.status in ("queued", "running", "waiting_approval"):
            rec = await asyncio.to_thread(
                self.store.update_swarm_agent,
                agent_id,
                status="cancelled",
                phase="cancelled",
                active_tool=None,
            )
            if rec:
                self._emit(rec.run_id, "swarm_agent_update", rec.model_dump())
        return rec

    async def cancel_run(self, run_id: str) -> None:
        for aid in list(self._run_agents.get(run_id, [])):
            await self.cancel_agent(aid)
        await self.finish_run(run_id, "cancelled")
        self._queues.pop(run_id, None)
        self._run_agents.pop(run_id, None)

    async def wait_agents(
        self,
        run_id: str,
        agent_ids: list[str] | None = None,
        timeout_s: float = 180.0,
    ) -> list[SwarmAgentRecord]:
        target_ids = agent_ids or list(self._run_agents.get(run_id, []))
        tasks = [self._tasks[aid] for aid in target_ids if aid in self._tasks and not self._tasks[aid].done()]
        if tasks:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(5.0, min(600.0, float(timeout_s))),
                )
        all_agents = await asyncio.to_thread(self.store.list_swarm_agents, run_id)
        if agent_ids:
            wanted = set(agent_ids)
            return [a for a in all_agents if a.id in wanted]
        return all_agents

    async def _update_and_emit(self, agent_id: str, run_id: str, **fields: Any) -> SwarmAgentRecord | None:
        rec = await asyncio.to_thread(self.store.update_swarm_agent, agent_id, **fields)
        if rec is not None:
            self._emit(run_id, "swarm_agent_update", rec.model_dump())
        return rec

    async def _execute_worker_tool(
        self,
        *,
        agent: SwarmAgentRecord,
        conv: ConversationSummary,
        t_name: str,
        parsed_args: dict[str, Any],
        mcp_map: dict[str, McpServerConfig],
    ) -> tuple[bool, str, dict[str, Any]]:
        if agent.readonly and t_name in APPROVAL_REQUIRED_TOOLS:
            return False, f"Tool '{t_name}' is blocked for read-only worker '{agent.role}'.", {}

        if t_name == "web_search":
            ok, out = await execute_web_search(
                str(parsed_args.get("query", "")),
                int(parsed_args.get("max_results") or 6),
            )
            return ok, out, {}
        if t_name == "web_fetch":
            ok, out = await execute_web_fetch(str(parsed_args.get("url", "")))
            return ok, out, {}
        if t_name.startswith("mcp__"):
            parts = t_name.split("__", 2)
            srv_id = parts[1] if len(parts) == 3 else ""
            real_tool = parts[2] if len(parts) == 3 else t_name
            srv_cfg = mcp_map.get(srv_id)
            if not srv_cfg:
                return False, f"MCP server '{srv_id}' not enabled for this chat.", {}
            out = await self._call_mcp_tool(
                srv_cfg, real_tool, parsed_args, conv.workspace_subpath, vault=self.cluster.vault
            )
            return (not out.startswith("MCP error:")), out, {}

        pref_id = agent.node_id or conv.exec_node
        exec_node = self.cluster.nodes.get(pref_id) if pref_id else None
        if exec_node is None or not exec_node.online:
            exec_node = next(
                (
                    n
                    for n in self.cluster.nodes.values()
                    if n.online and n.telemetry and n.telemetry.tools_enabled
                ),
                next((n for n in self.cluster.nodes.values() if n.online), None),
            )
        if not exec_node or not exec_node.online:
            return False, f"Execution node '{pref_id or 'auto'}' is offline.", {}

        try:
            approval_nonce = secrets.token_hex(8)
            tool_headers: dict[str, str] = {
                "X-Splitch-Router-Approved": "1",
                "X-Splitch-Approval-Nonce": approval_nonce,
            }
            if self.cluster.router_private_key is not None:
                ts_hdr, sig_hdr = sign_tool_approval_ed25519(
                    self.cluster.router_private_key,
                    t_name,
                    parsed_args,
                    conv.workspace_subpath,
                    nonce=approval_nonce,
                )
                tool_headers["X-Splitch-Approval-Ts"] = ts_hdr
                tool_headers["X-Splitch-Approval-Sig"] = sig_hdr
            r = await self.cluster.proxy_client.post(
                f"{exec_node.agent_url}/tools/execute",
                json={
                    "tool": t_name,
                    "arguments": parsed_args,
                    "workspace_subpath": conv.workspace_subpath,
                },
                headers=tool_headers,
                auth=self.cluster.agent_auth(),
                timeout=90.0,
            )
            if r.status_code >= 400:
                return False, f"Agent tool error HTTP {r.status_code}: {r.text[:300]}", {}
            res_data = r.json()
            return (
                bool(res_data.get("ok")),
                str(res_data.get("output") or ""),
                {
                    "diff": res_data.get("diff"),
                    "verification": res_data.get("verification"),
                    "sandbox": res_data.get("sandbox"),
                },
            )
        except (httpx.HTTPError, ValueError) as exc:
            return False, f"Tool execution failed: {exc}", {}

    async def _run_worker(
        self,
        *,
        agent: SwarmAgentRecord,
        conv: ConversationSummary,
        mcp_map: dict[str, McpServerConfig],
        mcp_tools: list[dict[str, Any]],
    ) -> None:
        run_id = agent.run_id
        try:
            await asyncio.to_thread(
                self.store.add_swarm_agent_message,
                SwarmWorkerMessage(
                    id=uuid.uuid4().hex[:12],
                    agent_id=agent.id,
                    role="user",
                    content=agent.task,
                    created_at=time.time(),
                ),
            )

            tools_payload: list[dict[str, Any]] = []
            if conv.tools_enabled:
                for schema in WORKSPACE_TOOLS_SCHEMA:
                    fn_name = schema.get("function", {}).get("name", "")
                    if agent.readonly and fn_name in APPROVAL_REQUIRED_TOOLS:
                        continue
                    tools_payload.append(schema)
                tools_payload.extend(mcp_tools)
            if conv.web_search_enabled:
                tools_payload.extend(WEB_TOOLS_SCHEMA)

            sys_prompt = (
                f"You are a focused Splitch Swarm Worker acting as '{agent.role}'. "
                "Complete your assigned task concisely and accurately using your tools when needed. "
                + ("You are READ-ONLY and cannot modify files or run shell commands. " if agent.readonly else "")
                + "Return a crisp, structured summary of your findings or changes."
            )
            openai_msgs: list[dict[str, Any]] = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": agent.task},
            ]

            sem = self._get_semaphore()
            final_text = ""
            for rnd in range(agent.max_rounds):
                nodes_pool = (
                    {agent.node_id: self.cluster.nodes[agent.node_id]}
                    if agent.node_id and agent.node_id in self.cluster.nodes
                    else self.cluster.nodes
                )
                req_body: dict[str, Any] = {
                    "model": agent.model,
                    "messages": openai_msgs,
                    "stream": True,
                }
                if tools_payload:
                    req_body["tools"] = tools_payload

                decision, skip_reasons = resolve_route(
                    endpoint="/v1/chat/completions",
                    requested_model=agent.model,
                    body=req_body,
                    nodes=nodes_pool,
                    aliases=self.cluster.aliases,
                    control=self.cluster.control,
                    guards=self.cluster.config.guards,
                    heavy_char_threshold=self.cluster.config.router.auto_heavy_char_threshold,
                )
                if decision is None:
                    raise RuntimeError(f"No eligible node for worker '{agent.role}': {skip_reasons}")

                req_body["model"] = decision.target_model
                await self._update_and_emit(
                    agent.id,
                    run_id,
                    status="running",
                    round=rnd + 1,
                    phase="thinking",
                    node_id=decision.node.node_id,
                    model=decision.target_model,
                    active_tool=None,
                )

                started = time.perf_counter()
                content_buf: list[str] = []
                reasoning_buf: list[str] = []
                tool_calls_acc: dict[int, dict[str, Any]] = {}
                approx_tokens = 0
                comp_tokens_real: int | None = None

                async with sem:
                    decision.node.in_flight += 1
                    try:
                        for attempt in range(2):
                            target_url, upstream_auth = self.cluster.resolve_upstream_target(
                                decision, "/v1/chat/completions"
                            )
                            async with self.cluster.proxy_client.stream(
                                "POST",
                                target_url,
                                json=req_body,
                                auth=upstream_auth,
                            ) as upstream:
                                if upstream.status_code >= 400:
                                    err_body = (await upstream.aread()).decode("utf-8", errors="replace")[:400]
                                    if attempt == 0:
                                        retry_body = self._adjust_reasoning(req_body, err_body)
                                        if retry_body is not None:
                                            req_body = retry_body
                                            continue
                                    raise RuntimeError(f"Upstream HTTP {upstream.status_code}: {err_body}")

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
                                    if isinstance(usage, dict) and isinstance(usage.get("completion_tokens"), int):
                                        comp_tokens_real = usage["completion_tokens"]
                                    choices = chunk.get("choices") or []
                                    if not choices:
                                        continue
                                    delta = choices[0].get("delta") or {}
                                    rc = delta.get("reasoning_content")
                                    if rc:
                                        approx_tokens += 1
                                        reasoning_buf.append(rc)
                                        self._emit(run_id, "swarm_agent_token", {"agent_id": agent.id, "kind": "reasoning", "text": rc})
                                    txt = delta.get("content")
                                    if txt:
                                        approx_tokens += 1
                                        content_buf.append(txt)
                                        self._emit(run_id, "swarm_agent_token", {"agent_id": agent.id, "kind": "token", "text": txt})
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
                    finally:
                        decision.node.last_splitch_work_ts = time.time()
                        decision.node.in_flight = max(0, decision.node.in_flight - 1)

                elapsed_s = max(0.01, time.perf_counter() - started)
                final_tokens = comp_tokens_real if comp_tokens_real is not None else approx_tokens
                tps = round(final_tokens / elapsed_s, 1) if final_tokens > 0 else None
                assembled_tools = [tool_calls_acc[k] for k in sorted(tool_calls_acc.keys())]
                final_text = "".join(content_buf)

                await asyncio.to_thread(
                    self.store.add_swarm_agent_message,
                    SwarmWorkerMessage(
                        id=uuid.uuid4().hex[:12],
                        agent_id=agent.id,
                        role="assistant",
                        content=final_text,
                        reasoning="".join(reasoning_buf),
                        tool_calls=assembled_tools,
                        meta={"node": decision.node.node_id, "model": decision.target_model, "tokens_per_sec": tps},
                        created_at=time.time(),
                    ),
                )

                if not assembled_tools:
                    await self._update_and_emit(
                        agent.id,
                        run_id,
                        status="done",
                        phase="done",
                        active_tool=None,
                        result=final_text,
                        tokens_per_sec=tps,
                    )
                    return

                openai_msgs.append(
                    {
                        "role": "assistant",
                        "content": final_text or None,
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

                    await self._update_and_emit(
                        agent.id, run_id, phase="tool", active_tool=t_name, tokens_per_sec=tps
                    )
                    self._emit(
                        run_id,
                        "swarm_agent_tool",
                        {"agent_id": agent.id, "kind": "call", "id": t_id, "name": t_name, "arguments": parsed_args},
                    )

                    is_external_mcp = False
                    external_target: str | None = None
                    if t_name.startswith("mcp__"):
                        _mp = t_name.split("__", 2)
                        _ms = mcp_map.get(_mp[1] if len(_mp) == 3 else "")
                        if _ms and _ms.egress != "local":
                            is_external_mcp = True
                            external_target = _ms.egress_target or _ms.url or _ms.id

                    approved = True
                    if not (agent.readonly and t_name in APPROVAL_REQUIRED_TOOLS) and self._requires_tool_approval(
                        t_name,
                        parsed_args,
                        is_external_mcp=is_external_mcp,
                        conv_auto_approve=conv.auto_approve,
                        router_cfg=self.cluster.config.router,
                    ):
                        approval_id = uuid.uuid4().hex[:10]
                        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                        self.pending_approvals[approval_id] = fut
                        await self._update_and_emit(
                            agent.id,
                            run_id,
                            status="waiting_approval",
                            phase="approval",
                            active_tool=t_name,
                        )
                        self._emit(
                            run_id,
                            "approval_required",
                            {
                                "approval_id": approval_id,
                                "tool_call_id": t_id,
                                "name": t_name,
                                "arguments": parsed_args,
                                "exec_node": f"EXTERNAL MCP ({external_target})" if is_external_mcp else (agent.node_id or conv.exec_node or "auto"),
                                "sandbox": None,
                                "swarm_agent_id": agent.id,
                                "swarm_role": agent.role,
                            },
                        )
                        try:
                            approved = await asyncio.wait_for(fut, timeout=300.0)
                        except TimeoutError:
                            approved = False
                        finally:
                            self.pending_approvals.pop(approval_id, None)
                        await self._update_and_emit(
                            agent.id, run_id, status="running", phase="tool", active_tool=t_name
                        )

                    if not approved:
                        tool_ok, tool_out, tool_meta = False, "Tool execution denied by user.", {}
                    else:
                        tool_ok, tool_out, tool_meta = await self._execute_worker_tool(
                            agent=agent,
                            conv=conv,
                            t_name=t_name,
                            parsed_args=parsed_args,
                            mcp_map=mcp_map,
                        )

                    await asyncio.to_thread(
                        self.store.add_swarm_agent_message,
                        SwarmWorkerMessage(
                            id=uuid.uuid4().hex[:12],
                            agent_id=agent.id,
                            role="tool",
                            content=tool_out,
                            tool_call_id=t_id,
                            tool_name=t_name,
                            meta={"ok": tool_ok, **tool_meta},
                            created_at=time.time(),
                        ),
                    )
                    self._emit(
                        run_id,
                        "swarm_agent_tool",
                        {"agent_id": agent.id, "kind": "result", "id": t_id, "name": t_name, "ok": tool_ok, "output": tool_out},
                    )
                    openai_msgs.append(
                        {"role": "tool", "tool_call_id": t_id, "name": t_name, "content": tool_out}
                    )

            await self._update_and_emit(
                agent.id,
                run_id,
                status="done",
                phase="done",
                active_tool=None,
                result=final_text or "Reached max worker steps.",
            )
        except asyncio.CancelledError:
            await self._update_and_emit(
                agent.id, run_id, status="cancelled", phase="cancelled", active_tool=None
            )
            raise
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            await self._update_and_emit(
                agent.id, run_id, status="error", phase="error", active_tool=None, error=str(exc)
            )
