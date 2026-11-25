"""Independent verification tests for Splitch Multi-Agent Swarm Orchestration."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.router.chat_api import ChatStore, build_chat_router
from backend.router.config import SplitchConfig
from backend.router.settings_api import build_settings_router
from backend.router.swarm import SwarmManager
from backend.router.telemetry import ClusterState
from backend.shared.models import (
    ConversationSummary,
    LoadedModel,
    NodeStatus,
    NodeTelemetry,
    SwarmAgentRecord,
)


def _parse_sse_events(raw_text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    cur_event = "message"
    cur_data: list[str] = []
    for line in raw_text.splitlines():
        if line.startswith("event: "):
            cur_event = line[7:].strip()
        elif line.startswith("data: "):
            cur_data.append(line[6:])
        elif line == "" and cur_data:
            payload = "\n".join(cur_data)
            try:
                events.append((cur_event, json.loads(payload)))
            except ValueError:
                events.append((cur_event, {"raw": payload}))
            cur_event = "message"
            cur_data = []
    if cur_data:
        payload = "\n".join(cur_data)
        try:
            events.append((cur_event, json.loads(payload)))
        except ValueError:
            events.append((cur_event, {"raw": payload}))
    return events


def _make_node(node_id: str, url: str, model_id: str, *, tools_enabled: bool = True) -> NodeStatus:
    return NodeStatus(
        node_id=node_id,
        display_name=node_id.upper(),
        role="gpu" if node_id == "pc" else "unified",
        agent_url=url,
        engine_url=url,
        online=True,
        telemetry=NodeTelemetry(
            node_id=node_id,
            role="gpu" if node_id == "pc" else "unified",
            platform="linux" if node_id == "pc" else "darwin",
            timestamp=1.0,
            online=True,
            engine="llama-server",
            engine_healthy=True,
            tools_enabled=tools_enabled,
            context_length=32768,
            models=[LoadedModel(id=model_id, status="loaded")],
        ),
    )


def _make_chat_app(cluster: ClusterState, tmp_path: Path) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        cluster._proxy_client = httpx.AsyncClient()
        try:
            yield
        finally:
            await cluster._proxy_client.aclose()

    app = FastAPI(lifespan=_lifespan)
    app.include_router(build_chat_router(cluster, tmp_path, tmp_path / "skills"))
    return app


def test_conversation_swarm_enabled_defaults_and_overrides(tmp_path: Path) -> None:
    # Single node -> swarm_enabled defaults to False when omitted
    cluster_single = ClusterState(SplitchConfig(), data_dir=tmp_path / "single")
    cluster_single.nodes["pc"] = _make_node("pc", "http://127.0.0.1:9991", "pc-model")
    app_single = FastAPI()
    app_single.include_router(
        build_chat_router(cluster_single, tmp_path / "single", tmp_path / "skills")
    )
    with TestClient(app_single) as tc:
        c_default = tc.post("/api/chat/conversations", json={"title": "Single Default"}).json()
        assert c_default["swarm_enabled"] is False
        c_explicit_on = tc.post(
            "/api/chat/conversations",
            json={"title": "Single Explicit On", "swarm_enabled": True},
        ).json()
        assert c_explicit_on["swarm_enabled"] is True

    # Multi-node (>1) -> swarm_enabled defaults to True when omitted
    cluster_multi = ClusterState(SplitchConfig(), data_dir=tmp_path / "multi")
    cluster_multi.nodes["pc"] = _make_node("pc", "http://127.0.0.1:9991", "pc-model")
    cluster_multi.nodes["mac"] = _make_node("mac", "http://127.0.0.1:9992", "mac-model")
    app_multi = FastAPI()
    app_multi.include_router(
        build_chat_router(cluster_multi, tmp_path / "multi", tmp_path / "skills")
    )
    with TestClient(app_multi) as tc:
        c_default = tc.post("/api/chat/conversations", json={"title": "Multi Default"}).json()
        assert c_default["swarm_enabled"] is True
        c_explicit_off = tc.post(
            "/api/chat/conversations",
            json={"title": "Multi Explicit Off", "swarm_enabled": False},
        ).json()
        assert c_explicit_off["swarm_enabled"] is False


def test_swarm_parallel_multi_node_execution_sse_and_persistence(tmp_path: Path) -> None:
    pc_requests: list[dict[str, Any]] = []
    mac_requests: list[dict[str, Any]] = []
    lock = threading.Lock()

    class _PcHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            with lock:
                pc_requests.append(body)
            msgs = body.get("messages") or []
            sys_content = msgs[0].get("content", "") if msgs else ""
            is_worker = "Splitch Swarm Worker" in sys_content

            if is_worker:
                delta = {"content": "PC worker result"}
            else:
                # Coordinator turns: 1) spawn two workers, 2) wait_agents, 3) final summary
                tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                if len(tool_msgs) == 0:
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_spawn_pc",
                                "type": "function",
                                "function": {
                                    "name": "spawn_agent",
                                    "arguments": json.dumps(
                                        {
                                            "role": "PC Analyst",
                                            "task": "Inspect PC node",
                                            "node_id": "pc",
                                            "model": "pc-model",
                                            "readonly": True,
                                        }
                                    ),
                                },
                            },
                            {
                                "index": 1,
                                "id": "call_spawn_mac",
                                "type": "function",
                                "function": {
                                    "name": "spawn_agent",
                                    "arguments": json.dumps(
                                        {
                                            "role": "Mac Analyst",
                                            "task": "Inspect Mac node",
                                            "node_id": "mac",
                                            "model": "mac-model",
                                            "readonly": True,
                                        }
                                    ),
                                },
                            },
                        ]
                    }
                elif len(tool_msgs) == 2:
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_wait",
                                "type": "function",
                                "function": {
                                    "name": "wait_agents",
                                    "arguments": json.dumps({"timeout_s": 30}),
                                },
                            }
                        ]
                    }
                else:
                    delta = {"content": "Final synthesis: PC + Mac done."}

            sse = (
                f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n"
                "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            self.wfile.write(sse)

        def log_message(self, format: str, *args: object) -> None:
            return

    class _MacHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            with lock:
                mac_requests.append(body)
            sse = (
                f"data: {json.dumps({'choices': [{'delta': {'content': 'Mac worker result'}}]})}\n\n"
                "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            self.wfile.write(sse)

        def log_message(self, format: str, *args: object) -> None:
            return

    pc_srv = ThreadingHTTPServer(("127.0.0.1", 0), _PcHandler)
    mac_srv = ThreadingHTTPServer(("127.0.0.1", 0), _MacHandler)
    pc_th = threading.Thread(target=pc_srv.serve_forever, daemon=True)
    mac_th = threading.Thread(target=mac_srv.serve_forever, daemon=True)
    pc_th.start()
    mac_th.start()
    try:
        pc_url = f"http://{pc_srv.server_address[0]}:{pc_srv.server_address[1]}"
        mac_url = f"http://{mac_srv.server_address[0]}:{mac_srv.server_address[1]}"

        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        cluster.nodes["pc"] = _make_node("pc", pc_url, "pc-model")
        cluster.nodes["mac"] = _make_node("mac", mac_url, "mac-model")

        app = _make_chat_app(cluster, tmp_path)
        with TestClient(app) as tc:
            conv = tc.post(
                "/api/chat/conversations",
                json={"title": "Swarm Parallel Test", "model": "pc-model"},
            ).json()
            assert conv["swarm_enabled"] is True

            resp = tc.post(
                f"/api/chat/conversations/{conv['id']}/send",
                json={"content": "Run parallel analysis across PC and Mac"},
            )
            assert resp.status_code == 200, resp.text
            events = _parse_sse_events(resp.text)

            # Coordinator received all 4 swarm tools
            coord_req = next(
                r for r in pc_requests if "Splitch Swarm Worker" not in r["messages"][0]["content"]
            )
            coord_tool_names = {t["function"]["name"] for t in coord_req.get("tools", [])}
            assert {"spawn_agent", "wait_agents", "list_agents", "cancel_agent"} <= coord_tool_names

            # SSE lifecycle events emitted
            event_names = [ev for ev, _ in events]
            assert "swarm_run_started" in event_names
            assert "swarm_agent_update" in event_names
            assert "swarm_run_done" in event_names

            # Both nodes executed their respective worker
            assert any("Splitch Swarm Worker" in r["messages"][0]["content"] for r in pc_requests)
            assert len(mac_requests) == 1
            assert "Splitch Swarm Worker" in mac_requests[0]["messages"][0]["content"]

            # SQLite persistence via REST endpoints
            swarms_resp = tc.get(f"/api/chat/conversations/{conv['id']}/swarms")
            assert swarms_resp.status_code == 200
            runs = swarms_resp.json()
            assert len(runs) == 1
            assert runs[0]["status"] == "done"
            agents = runs[0]["agents"]
            assert len(agents) == 2
            by_node = {a["node_id"]: a for a in agents}
            assert by_node["pc"]["status"] == "done"
            assert by_node["pc"]["result"] == "PC worker result"
            assert by_node["mac"]["status"] == "done"
            assert by_node["mac"]["result"] == "Mac worker result"

            for ag in agents:
                msgs_resp = tc.get(f"/api/chat/swarms/agents/{ag['id']}/messages")
                assert msgs_resp.status_code == 200
                w_msgs = msgs_resp.json()
                assert [m["role"] for m in w_msgs] == ["user", "assistant"]
    finally:
        pc_srv.shutdown()
        pc_srv.server_close()
        mac_srv.shutdown()
        mac_srv.server_close()
        pc_th.join(timeout=2.0)
        mac_th.join(timeout=2.0)


def test_readonly_worker_strips_and_blocks_mutating_tools(tmp_path: Path) -> None:
    worker_requests: list[dict[str, Any]] = []
    tool_exec_calls: list[dict[str, Any]] = []
    lock = threading.Lock()

    class _NodeHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/tools/execute":
                with lock:
                    tool_exec_calls.append(body)
                payload = json.dumps({"ok": True, "output": "should not run"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            msgs = body.get("messages") or []
            sys_content = msgs[0].get("content", "") if msgs else ""
            if "Splitch Swarm Worker" in sys_content:
                with lock:
                    worker_requests.append(body)
                tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                if not tool_msgs:
                    # Worker attempts to call write_file despite being readonly
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_ro_write",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps({"path": "pwn.txt", "content": "x"}),
                                },
                            }
                        ]
                    }
                else:
                    delta = {"content": f"Blocked observed: {tool_msgs[0]['content']}"}
            else:
                tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                if len(tool_msgs) == 0:
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_spawn_ro",
                                "type": "function",
                                "function": {
                                    "name": "spawn_agent",
                                    "arguments": json.dumps(
                                        {
                                            "role": "RO Worker",
                                            "task": "Try writing",
                                            "node_id": "pc",
                                            "model": "pc-model",
                                            "readonly": True,
                                        }
                                    ),
                                },
                            }
                        ]
                    }
                elif len(tool_msgs) == 1:
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_wait_ro",
                                "type": "function",
                                "function": {"name": "wait_agents", "arguments": "{}"},
                            }
                        ]
                    }
                else:
                    delta = {"content": "Coordinator finished."}

            sse = (
                f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n"
                "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            self.wfile.write(sse)

        def log_message(self, format: str, *args: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _NodeHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://{srv.server_address[0]}:{srv.server_address[1]}"
        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        cluster.nodes["pc"] = _make_node("pc", url, "pc-model", tools_enabled=True)

        app = _make_chat_app(cluster, tmp_path)
        with TestClient(app) as tc:
            conv = tc.post(
                "/api/chat/conversations",
                json={
                    "title": "Readonly Test",
                    "model": "pc-model",
                    "tools_enabled": True,
                    "swarm_enabled": True,
                },
            ).json()

            resp = tc.post(
                f"/api/chat/conversations/{conv['id']}/send",
                json={"content": "Run readonly worker"},
            )
            assert resp.status_code == 200

            assert len(worker_requests) >= 1
            worker_tool_names = {t["function"]["name"] for t in worker_requests[0].get("tools", [])}
            assert "write_file" not in worker_tool_names
            assert "edit_file" not in worker_tool_names
            assert "run_command" not in worker_tool_names
            assert "read_file" in worker_tool_names

            # /tools/execute must never have been called
            assert tool_exec_calls == []

            runs = tc.get(f"/api/chat/conversations/{conv['id']}/swarms").json()
            agent_id = runs[0]["agents"][0]["id"]
            w_msgs = tc.get(f"/api/chat/swarms/agents/{agent_id}/messages").json()
            tool_res = next(m for m in w_msgs if m["role"] == "tool")
            assert tool_res["meta"]["ok"] is False
            assert "blocked for read-only worker" in tool_res["content"]

        # Direct _execute_worker_tool verification for all three mutating tools
        store = ChatStore(tmp_path / "direct.db")
        mgr = SwarmManager(
            cluster=cluster,
            store=store,
            pending_approvals={},
            approval_checker=lambda *a, **kw: False,
            adjust_reasoning_fn=lambda *a, **kw: None,
            mcp_caller=lambda *a, **kw: None,
        )
        ro_agent = SwarmAgentRecord(
            id="ag1",
            run_id="r1",
            conversation_id="c1",
            role="Auditor",
            task="Audit",
            readonly=True,
            created_at=1.0,
            updated_at=1.0,
        )
        conv_obj = ConversationSummary(
            id="c1", title="C1", created_at=1.0, updated_at=1.0, swarm_enabled=True
        )

        async def _check_blocked() -> None:
            for tool_name in ("write_file", "edit_file", "run_command"):
                ok, out, _ = await mgr._execute_worker_tool(
                    agent=ro_agent,
                    conv=conv_obj,
                    t_name=tool_name,
                    parsed_args={},
                    mcp_map={},
                )
                assert ok is False
                assert "blocked for read-only worker" in out

        asyncio.run(_check_blocked())
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2.0)


def test_max_parallel_agents_settings_persistence_and_bounds(tmp_path: Path) -> None:
    cluster1 = ClusterState(SplitchConfig(), data_dir=tmp_path)
    app1 = FastAPI()
    app1.include_router(build_settings_router(cluster1, tmp_path, api_key_getter=lambda: ""))
    with TestClient(app1) as tc1:
        r0 = tc1.get("/api/settings")
        assert r0.status_code == 200
        assert r0.json()["max_parallel_agents"] == 2

        # Out-of-range values rejected (1..6)
        assert tc1.patch("/api/settings", json={"max_parallel_agents": 0}).status_code == 422
        assert tc1.patch("/api/settings", json={"max_parallel_agents": 7}).status_code == 422

        r_patch = tc1.patch("/api/settings", json={"max_parallel_agents": 5})
        assert r_patch.status_code == 200
        assert r_patch.json()["max_parallel_agents"] == 5

    # Fresh ClusterState + router loads persisted max_parallel_agents from settings.json
    cluster2 = ClusterState(SplitchConfig(), data_dir=tmp_path)
    app2 = FastAPI()
    app2.include_router(build_settings_router(cluster2, tmp_path, api_key_getter=lambda: ""))
    with TestClient(app2) as tc2:
        r2 = tc2.get("/api/settings")
        assert r2.status_code == 200
        assert r2.json()["max_parallel_agents"] == 5


def test_cancel_swarm_agent_endpoint_and_404(tmp_path: Path) -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()

    class _SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            worker_started.set()
            release_worker.wait(timeout=5.0)
            sse = (
                f"data: {json.dumps({'choices': [{'delta': {'content': 'late'}}]})}\n\n"
                "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            try:
                self.wfile.write(sse)
            except OSError:
                pass

        def log_message(self, format: str, *args: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://{srv.server_address[0]}:{srv.server_address[1]}"
        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        cluster.nodes["pc"] = _make_node("pc", url, "pc-model")

        # Unknown agent ID -> 404
        app = _make_chat_app(cluster, tmp_path)
        with TestClient(app) as tc:
            r404 = tc.post("/api/chat/swarms/agents/does_not_exist/cancel")
            assert r404.status_code == 404

        # Running agent cancellation via SwarmManager + API check
        from backend.shared.models import ConversationCreate

        store = ChatStore(tmp_path / "chats.db")
        conv = store.create_conversation(
            ConversationCreate(title="Cancel Test", model="pc-model", swarm_enabled=True)
        )

        async def _exercise_cancel() -> None:
            cluster._proxy_client = httpx.AsyncClient()
            try:
                mgr = SwarmManager(
                    cluster=cluster,
                    store=store,
                    pending_approvals={},
                    approval_checker=lambda *a, **kw: False,
                    adjust_reasoning_fn=lambda *a, **kw: None,
                    mcp_caller=lambda *a, **kw: None,
                )
                run = await mgr.start_run(conv)
                ag = await mgr.spawn_worker(
                    run_id=run.id,
                    conv=conv,
                    role="Slow Worker",
                    task="Wait forever",
                    model="pc-model",
                    node_id="pc",
                )
                assert await asyncio.to_thread(worker_started.wait, 3.0)
                cancelled = await mgr.cancel_agent(ag.id)
                assert cancelled is not None
                assert cancelled.status == "cancelled"
                assert cancelled.phase == "cancelled"
            finally:
                release_worker.set()
                await cluster._proxy_client.aclose()

        asyncio.run(_exercise_cancel())

        # Also verify POST /api/chat/swarms/agents/{agent_id}/cancel returns 200 for persisted agent
        runs = store.list_swarm_runs(conv.id)
        ag_id = runs[0].agents[0].id
        with TestClient(app) as tc:
            r_ok = tc.post(f"/api/chat/swarms/agents/{ag_id}/cancel")
            assert r_ok.status_code == 200
            assert r_ok.json()["ok"] is True
            assert r_ok.json()["agent"]["status"] == "cancelled"
    finally:
        release_worker.set()
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2.0)
