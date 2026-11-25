"""Unit tests for Splitch Agent Chat store, API, opt-in tool security, and sandboxed tools."""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.agent import main as agent_main
from backend.router.chat_api import ChatStore, build_chat_router
from backend.router.config import SplitchConfig
from backend.router.telemetry import ClusterState
from backend.router.web_search import _is_public_url
from backend.shared.models import (
    AliasConfig,
    ChatMessageRecord,
    ConversationCreate,
    LoadedModel,
    NodeStatus,
    NodeTelemetry,
    ToolExecRequest,
)


def test_chat_store_persistence_and_trim(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chats.db")
    c = store.create_conversation(ConversationCreate(title="Test Chat", model="fast"))
    assert c.title == "Test Chat"
    assert len(store.list_conversations()) == 1

    cluster = ClusterState(SplitchConfig())
    app = FastAPI()
    app.include_router(build_chat_router(cluster, tmp_path, tmp_path / "skills"))
    tc = TestClient(app)

    r = tc.patch(
        f"/api/chat/conversations/{c.id}",
        json={"title": "Renamed", "auto_approve": True, "enabled_skills": ["demo"]},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"
    assert r.json()["auto_approve"] is True
    assert r.json()["enabled_skills"] == ["demo"]

    assert tc.delete(f"/api/chat/conversations/{c.id}").status_code == 200
    assert tc.get("/api/chat/conversations").json() == []
    trash = tc.get("/api/chat/trash").json()
    assert len(trash) == 1 and trash[0]["id"] == c.id and trash[0]["deleted_at"] is not None

    # Restore from trash
    restored = tc.post(f"/api/chat/conversations/{c.id}/restore")
    assert restored.status_code == 200 and restored.json()["deleted_at"] is None
    assert len(tc.get("/api/chat/conversations").json()) == 1
    assert tc.get("/api/chat/trash").json() == []

    # Move back to trash and permanently delete
    assert tc.delete(f"/api/chat/conversations/{c.id}").status_code == 200
    assert tc.delete(f"/api/chat/conversations/{c.id}/permanent").status_code == 200
    assert tc.get("/api/chat/trash").json() == []

    # Create two more, soft-delete both, and empty trash
    c2 = tc.post(
        "/api/chat/conversations",
        json={
            "title": "B",
            "tools_enabled": False,
            "web_search_enabled": True,
            "auto_compact": False,
        },
    ).json()
    assert c2["tools_enabled"] is False
    assert c2["web_search_enabled"] is True
    assert c2["auto_compact"] is False

    # Verify non-destructive compaction persistence + clear
    saved_c = store.save_compaction(c2["id"], "- User asked about Splitch", 100.0)
    assert saved_c is not None and saved_c.compacted_summary.startswith("- User")
    cleared_res = tc.delete(f"/api/chat/conversations/{c2['id']}/compact")
    assert cleared_res.status_code == 200
    assert cleared_res.json()["compacted_summary"] == ""
    assert cleared_res.json()["compacted_through_ts"] == 0.0

    # Manual compact on empty chat returns compacted=False cleanly
    noop_c = tc.post(f"/api/chat/conversations/{c2['id']}/compact")
    assert noop_c.status_code == 200 and noop_c.json()["compacted"] is False

    tc.delete(f"/api/chat/conversations/{c2['id']}")
    empty_res = tc.delete("/api/chat/trash")
    assert empty_res.status_code == 200 and empty_res.json()["deleted_count"] == 1
    assert tc.get("/api/chat/trash").json() == []

    # Bulk soft-delete, bulk restore, and bulk permanent delete
    bulk_ids = [
        tc.post("/api/chat/conversations", json={"title": f"Cat {i}"}).json()["id"]
        for i in range(4)
    ]
    bd = tc.post("/api/chat/conversations/bulk-delete", json={"ids": bulk_ids[:3]})
    assert bd.status_code == 200 and bd.json()["affected_count"] == 3
    assert [x["id"] for x in tc.get("/api/chat/conversations").json()] == [bulk_ids[3]]
    assert len(tc.get("/api/chat/trash").json()) == 3

    br = tc.post("/api/chat/conversations/bulk-restore", json={"ids": bulk_ids[:2]})
    assert br.status_code == 200 and br.json()["affected_count"] == 2
    assert len(tc.get("/api/chat/conversations").json()) == 3
    assert len(tc.get("/api/chat/trash").json()) == 1

    bp = tc.post(
        "/api/chat/conversations/bulk-permanent-delete",
        json={"ids": [bulk_ids[2], bulk_ids[3]]},
    )
    assert bp.status_code == 200 and bp.json()["affected_count"] == 2
    assert len(tc.get("/api/chat/conversations").json()) == 2
    assert tc.get("/api/chat/trash").json() == []

    # Pinning floats older chat to top while preserving updated_at; unpinning restores order
    ordered_before = tc.get("/api/chat/conversations").json()
    older_id, newer_id = ordered_before[1]["id"], ordered_before[0]["id"]
    older_ts = ordered_before[1]["updated_at"]

    pin_res = tc.patch(f"/api/chat/conversations/{older_id}", json={"pinned": True})
    assert pin_res.status_code == 200
    assert pin_res.json()["pinned"] is True
    assert pin_res.json()["updated_at"] == older_ts
    assert [x["id"] for x in tc.get("/api/chat/conversations").json()] == [older_id, newer_id]

    unpin_res = tc.patch(f"/api/chat/conversations/{older_id}", json={"pinned": False})
    assert unpin_res.status_code == 200 and unpin_res.json()["pinned"] is False
    assert [x["id"] for x in tc.get("/api/chat/conversations").json()] == [newer_id, older_id]

    # Global and per-workspace (project) Skills & MCP scope defaults
    sc0 = tc.get("/api/chat/scopes").json()
    assert sc0["global_scope"] == {"skills": [], "mcps": []}

    sc_glob = tc.put(
        "/api/chat/scopes/global",
        json={"skills": ["code-reviewer"], "mcps": ["graphify"]},
    ).json()
    assert sc_glob["global_scope"]["mcps"] == ["graphify"]

    sc_ws = tc.put(
        "/api/chat/scopes/workspace",
        json={"workspace": "/projects/splitch/", "skills": ["rust-perf"], "mcps": ["git"]},
    ).json()
    assert sc_ws["workspaces"]["projects/splitch"]["skills"] == ["rust-perf"]
    assert sc_ws["workspaces"]["projects/splitch"]["mcps"] == ["git"]

    # Clearing workspace scope removes the key cleanly
    sc_clear = tc.put(
        "/api/chat/scopes/workspace",
        json={"workspace": "projects/splitch", "skills": [], "mcps": []},
    ).json()
    assert "projects/splitch" not in sc_clear["workspaces"]


def test_web_fetch_ssrf_guard() -> None:
    assert _is_public_url("http://127.0.0.1:8080/health")[0] is False
    assert _is_public_url("http://localhost:8090/metrics")[0] is False
    assert _is_public_url("http://192.168.1.1/")[0] is False
    assert _is_public_url("file:///etc/passwd")[0] is False


def test_workspace_tools_opt_in_and_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_main, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(agent_main, "AGENT_TOKEN", "secret-node-tok")
    monkeypatch.setattr(agent_main, "ENABLE_TOOLS", False)

    tc = TestClient(agent_main.app)

    # Missing bearer token must be rejected
    assert tc.get("/metrics").status_code == 401
    # Health remains accessible
    assert tc.get("/health").status_code == 200
    # Tools disabled by default -> 403 even with valid token
    r_disabled = tc.post(
        "/tools/execute",
        headers={"Authorization": "Bearer secret-node-tok"},
        json={"tool": "list_dir", "arguments": {"path": "."}},
    )
    assert r_disabled.status_code == 403

    # Opt-in tools enabled
    monkeypatch.setattr(agent_main, "ENABLE_TOOLS", True)
    r_ok = tc.post(
        "/tools/execute",
        headers={"Authorization": "Bearer secret-node-tok"},
        json={"tool": "list_dir", "arguments": {"path": "."}},
    )
    assert r_ok.status_code == 200 and r_ok.json()["ok"] is True

    # Write + diff
    w1 = agent_main._exec_fs_tool(
        ToolExecRequest(tool="write_file", arguments={"path": "hello.py", "content": "print('v1')\n"})
    )
    assert w1.ok is True
    assert "+print('v1')" in (w1.diff or "")

    # Overwrite diff shows change cleanly
    w2 = agent_main._exec_fs_tool(
        ToolExecRequest(tool="write_file", arguments={"path": "hello.py", "content": "print('v2')\n"})
    )
    assert "-print('v1')" in (w2.diff or "")
    assert "+print('v2')" in (w2.diff or "")

    # Read + grep + list
    rd = agent_main._exec_fs_tool(ToolExecRequest(tool="read_file", arguments={"path": "hello.py"}))
    assert "1: print('v2')" in rd.output

    gr = agent_main._exec_fs_tool(ToolExecRequest(tool="grep_search", arguments={"query": "v2"}))
    assert "hello.py:1:" in gr.output

    ls = agent_main._exec_fs_tool(ToolExecRequest(tool="list_dir", arguments={"path": "."}))
    assert "hello.py" in ls.output

    # Workspace directory browser inside sandbox
    (tmp_path / "proj_a").mkdir()
    (tmp_path / "proj_b").mkdir()
    r_dirs = tc.get(
        "/workspace/dirs",
        headers={"Authorization": "Bearer secret-node-tok"},
    )
    assert r_dirs.status_code == 200
    assert r_dirs.json()["dirs"] == ["proj_a", "proj_b"]
    assert (
        tc.get(
            "/workspace/dirs?subpath=../../etc",
            headers={"Authorization": "Bearer secret-node-tok"},
        ).status_code
        == 403
    )

    # Path traversal must be blocked with 403
    with pytest.raises(HTTPException) as exc:
        agent_main._exec_fs_tool(
            ToolExecRequest(tool="read_file", arguments={"path": "../../etc/passwd"})
        )
    assert exc.value.status_code == 403


def _parse_sse_events(raw_text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    cur_event = ""
    for line in raw_text.splitlines():
        if line.startswith("event: "):
            cur_event = line[7:].strip()
        elif line.startswith("data: "):
            events.append((cur_event, json.loads(line[6:])))
    return events


def _run_chat_send_against_loopback(
    tmp_path: Path,
    *,
    send_payload: dict[str, Any],
    upstream_outcomes: list[tuple[int, str]],
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any]]], int]:
    """Start a real loopback HTTP server simulating llama-server and send a chat message."""
    recorded_bodies: list[dict[str, Any]] = []
    outcomes = list(upstream_outcomes)

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            recorded_bodies.append(json.loads(raw.decode("utf-8")))
            status, body_text = outcomes.pop(0) if outcomes else (500, "No more outcomes")
            if status >= 400:
                payload = body_text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            sse_chunk = (
                f"data: {json.dumps({'choices': [{'delta': {'content': body_text}}]})}\n\n"
                "data: [DONE]\n\n"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse_chunk)))
            self.end_headers()
            self.wfile.write(sse_chunk)

        def log_message(self, format: str, *args: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        host, port = srv.server_address
        engine_url = f"http://{host}:{port}"

        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        cluster.nodes["gpu-1"] = NodeStatus(
            node_id="gpu-1",
            display_name="GPU 1",
            role="gpu",
            agent_url=engine_url,
            engine_url=engine_url,
            online=True,
            telemetry=NodeTelemetry(
                node_id="gpu-1",
                role="gpu",
                platform="linux",
                timestamp=1.0,
                online=True,
                engine="llama-server",
                engine_healthy=True,
                context_length=32768,
                models=[LoadedModel(id="qwen-32b", status="loaded")],
            ),
        )

        @asynccontextmanager
        async def _lifespan(_app: FastAPI):
            cluster._proxy_client = httpx.AsyncClient()
            try:
                yield
            finally:
                await cluster._proxy_client.aclose()

        app = FastAPI(lifespan=_lifespan)
        app.include_router(build_chat_router(cluster, tmp_path, tmp_path / "skills"))
        with TestClient(app) as tc:
            conv = tc.post(
                "/api/chat/conversations",
                json={
                    "title": "Reasoning Test",
                    "model": "qwen-32b",
                    "tools_enabled": False,
                    "web_search_enabled": False,
                },
            ).json()
            resp = tc.post(
                f"/api/chat/conversations/{conv['id']}/send",
                json=send_payload,
            )
            return recorded_bodies, _parse_sse_events(resp.text), resp.status_code
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2.0)


def test_chat_reasoning_effort_xhigh_accepted_and_forwarded(tmp_path: Path) -> None:
    bodies, events, status_code = _run_chat_send_against_loopback(
        tmp_path,
        send_payload={"content": "hi", "reasoning_effort": "xhigh"},
        upstream_outcomes=[(200, "ok-xhigh")],
    )
    assert status_code == 200
    assert len(bodies) == 1
    assert bodies[0]["reasoning_effort"] == "xhigh"
    assert ("token", {"text": "ok-xhigh"}) in events
    assert any(ev == "done" for ev, _ in events)


def test_chat_reasoning_effort_retry_high_to_xhigh(tmp_path: Path) -> None:
    err = (
        "Jinja Exception: Unexpected reasoning effort high. "
        "Supported types are xhigh (default), medium, and low."
    )
    bodies, events, status_code = _run_chat_send_against_loopback(
        tmp_path,
        send_payload={"content": "solve", "reasoning_effort": "high"},
        upstream_outcomes=[(500, err), (200, "retried-ok")],
    )
    assert status_code == 200
    assert len(bodies) == 2
    assert bodies[0]["reasoning_effort"] == "high"
    assert bodies[1]["reasoning_effort"] == "xhigh"
    assert ("token", {"text": "retried-ok"}) in events
    assert not any(ev == "error" for ev, _ in events)


def test_chat_reasoning_effort_retry_xhigh_to_high(tmp_path: Path) -> None:
    err = (
        "Jinja Exception: Unexpected reasoning effort xhigh. "
        "Supported types are high, medium, and low."
    )
    bodies, events, status_code = _run_chat_send_against_loopback(
        tmp_path,
        send_payload={"content": "solve", "reasoning_effort": "xhigh"},
        upstream_outcomes=[(400, err), (200, "retried-high")],
    )
    assert status_code == 200
    assert len(bodies) == 2
    assert bodies[0]["reasoning_effort"] == "xhigh"
    assert bodies[1]["reasoning_effort"] == "high"
    assert ("token", {"text": "retried-high"}) in events
    assert not any(ev == "error" for ev, _ in events)


def test_chat_reasoning_effort_retry_off_none_to_off(tmp_path: Path) -> None:
    err = (
        "Jinja Exception: Unexpected reasoning effort none. "
        "Supported types are off, low, medium, and high."
    )
    bodies, events, status_code = _run_chat_send_against_loopback(
        tmp_path,
        send_payload={"content": "solve", "reasoning_effort": "off"},
        upstream_outcomes=[(400, err), (200, "retried-off")],
    )
    assert status_code == 200
    assert len(bodies) == 2
    assert bodies[0]["reasoning_effort"] == "none"
    assert bodies[1]["reasoning_effort"] == "off"
    assert ("token", {"text": "retried-off"}) in events


def test_chat_reasoning_effort_strips_when_no_matching_replacement(tmp_path: Path) -> None:
    err = (
        "Jinja Exception: Unexpected reasoning effort medium. "
        "Supported types are minimal."
    )
    bodies, events, status_code = _run_chat_send_against_loopback(
        tmp_path,
        send_payload={"content": "solve", "reasoning_effort": "medium"},
        upstream_outcomes=[(500, err), (200, "stripped-ok")],
    )
    assert status_code == 200
    assert len(bodies) == 2
    assert bodies[0]["reasoning_effort"] == "medium"
    assert "reasoning_effort" not in bodies[1]
    assert ("token", {"text": "stripped-ok"}) in events
    assert not any(ev == "error" for ev, _ in events)


def test_chat_reasoning_effort_no_retry_on_unrelated_error(tmp_path: Path) -> None:
    bodies, events, status_code = _run_chat_send_against_loopback(
        tmp_path,
        send_payload={"content": "solve", "reasoning_effort": "high"},
        upstream_outcomes=[(500, "Out of memory"), (200, "should-not-reach")],
    )
    assert status_code == 200
    assert len(bodies) == 1
    assert bodies[0]["reasoning_effort"] == "high"
    assert ("error", {"message": "Upstream HTTP 500: Out of memory"}) in events
    assert not any(ev == "token" for ev, _ in events)


def test_chat_reasoning_effort_retries_at_most_once(tmp_path: Path) -> None:
    err = (
        "Jinja Exception: Unexpected reasoning effort high. "
        "Supported types are xhigh (default), medium, and low."
    )
    bodies, events, status_code = _run_chat_send_against_loopback(
        tmp_path,
        send_payload={"content": "solve", "reasoning_effort": "high"},
        upstream_outcomes=[(500, err), (500, "Still failing"), (200, "never")],
    )
    assert status_code == 200
    assert len(bodies) == 2
    assert ("error", {"message": "Upstream HTTP 500: Still failing"}) in events


def _run_agent_loop_against_loopback(
    tmp_path: Path,
    *,
    max_agent_steps: int,
    round_deltas: list[dict[str, Any]],
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
    """Run a multi-round chat turn against a loopback HTTP server and return SSE events + stored messages."""
    deltas = list(round_deltas)

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            if length > 0:
                self.rfile.read(length)
            delta = deltas.pop(0) if deltas else {"content": "fallback"}
            sse_chunk = (
                f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n"
                "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse_chunk)))
            self.end_headers()
            self.wfile.write(sse_chunk)

        def log_message(self, format: str, *args: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        host, port = srv.server_address
        engine_url = f"http://{host}:{port}"

        cfg = SplitchConfig()
        cfg.router.max_agent_steps = max_agent_steps
        cluster = ClusterState(cfg, data_dir=tmp_path)
        cluster.nodes["gpu-1"] = NodeStatus(
            node_id="gpu-1",
            display_name="GPU 1",
            role="gpu",
            agent_url=engine_url,
            engine_url=engine_url,
            online=True,
            telemetry=NodeTelemetry(
                node_id="gpu-1",
                role="gpu",
                platform="linux",
                timestamp=1.0,
                online=True,
                engine="llama-server",
                engine_healthy=True,
                context_length=32768,
                models=[LoadedModel(id="qwen-32b", status="loaded")],
            ),
        )

        @asynccontextmanager
        async def _lifespan(_app: FastAPI):
            cluster._proxy_client = httpx.AsyncClient()
            try:
                yield
            finally:
                await cluster._proxy_client.aclose()

        app = FastAPI(lifespan=_lifespan)
        app.include_router(build_chat_router(cluster, tmp_path, tmp_path / "skills"))
        with TestClient(app) as tc:
            conv = tc.post(
                "/api/chat/conversations",
                json={
                    "title": "Max Rounds Test",
                    "model": "qwen-32b",
                    "tools_enabled": False,
                    "web_search_enabled": True,
                    "auto_approve": True,
                },
            ).json()
            resp = tc.post(
                f"/api/chat/conversations/{conv['id']}/send",
                json={"content": "test max rounds"},
            )
            assert resp.status_code == 200
            events = _parse_sse_events(resp.text)
            msgs_resp = tc.get(f"/api/chat/conversations/{conv['id']}/messages")
            assert msgs_resp.status_code == 200
            return events, msgs_resp.json()
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2.0)


def test_chat_max_rounds_reached_emits_round_meta_and_persists_notice(tmp_path: Path) -> None:
    tool_delta_1 = {
        "tool_calls": [
            {
                "index": 0,
                "id": "call_r1",
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "arguments": '{"url": "http://127.0.0.1/blocked"}',
                },
            }
        ]
    }
    tool_delta_2 = {
        "tool_calls": [
            {
                "index": 0,
                "id": "call_r2",
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "arguments": '{"url": "http://127.0.0.1/blocked"}',
                },
            }
        ]
    }
    events, msgs = _run_agent_loop_against_loopback(
        tmp_path,
        max_agent_steps=2,
        round_deltas=[tool_delta_1, tool_delta_2],
    )

    meta_events = [data for ev, data in events if ev == "meta"]
    assert [(m["round"], m["max_rounds"]) for m in meta_events] == [(1, 2), (2, 2)]

    done_events = [data for ev, data in events if ev == "done"]
    assert len(done_events) == 1
    assert done_events[0]["max_rounds_reached"] is True
    assert done_events[0]["max_rounds"] == 2

    asst_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(asst_msgs) == 2
    assert "max_rounds_reached" not in asst_msgs[0]["meta"]
    assert asst_msgs[1]["meta"]["max_rounds_reached"] is True
    assert asst_msgs[1]["meta"]["steps_used"] == 2
    assert asst_msgs[1]["meta"]["max_steps"] == 2


def test_chat_normal_turn_does_not_set_max_rounds_reached(tmp_path: Path) -> None:
    events, msgs = _run_agent_loop_against_loopback(
        tmp_path,
        max_agent_steps=2,
        round_deltas=[{"content": "hello world"}],
    )

    meta_events = [data for ev, data in events if ev == "meta"]
    assert [(m["round"], m["max_rounds"]) for m in meta_events] == [(1, 2)]

    done_events = [data for ev, data in events if ev == "done"]
    assert len(done_events) == 1
    assert "max_rounds_reached" not in done_events[0]

    asst_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(asst_msgs) == 1
    assert "max_rounds_reached" not in asst_msgs[0]["meta"]
    assert "steps_used" not in asst_msgs[0]["meta"]
    assert "max_steps" not in asst_msgs[0]["meta"]


def test_chat_final_round_without_tool_calls_does_not_set_max_rounds_reached(
    tmp_path: Path,
) -> None:
    tool_delta_1 = {
        "tool_calls": [
            {
                "index": 0,
                "id": "call_r1",
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "arguments": '{"url": "http://127.0.0.1/blocked"}',
                },
            }
        ]
    }
    events, msgs = _run_agent_loop_against_loopback(
        tmp_path,
        max_agent_steps=2,
        round_deltas=[tool_delta_1, {"content": "done after tool"}],
    )

    meta_events = [data for ev, data in events if ev == "meta"]
    assert [(m["round"], m["max_rounds"]) for m in meta_events] == [(1, 2), (2, 2)]

    done_events = [data for ev, data in events if ev == "done"]
    assert len(done_events) == 1
    assert "max_rounds_reached" not in done_events[0]

    asst_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(asst_msgs) == 2
    assert all("max_rounds_reached" not in m["meta"] for m in asst_msgs)


def _run_compaction_against_loopback(
    tmp_path: Path,
    *,
    gaming_mode: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Run /compact against two real loopback servers (gpu-1=heavy, mac-1=fast)."""
    gpu_requests: list[dict[str, Any]] = []
    mac_requests: list[dict[str, Any]] = []

    def _make_handler(sink: list[dict[str, Any]], summary_text: str):
        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                sink.append(json.loads(raw.decode("utf-8")))
                payload = json.dumps(
                    {
                        "choices": [
                            {"message": {"role": "assistant", "content": summary_text}}
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        return _Handler

    gpu_srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(gpu_requests, "- GPU summary"))
    mac_srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(mac_requests, "- Mac summary"))
    gpu_th = threading.Thread(target=gpu_srv.serve_forever, daemon=True)
    mac_th = threading.Thread(target=mac_srv.serve_forever, daemon=True)
    gpu_th.start()
    mac_th.start()
    try:
        gpu_url = f"http://{gpu_srv.server_address[0]}:{gpu_srv.server_address[1]}"
        mac_url = f"http://{mac_srv.server_address[0]}:{mac_srv.server_address[1]}"

        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        cluster.control.gaming_mode = gaming_mode
        cluster.nodes["gpu-1"] = NodeStatus(
            node_id="gpu-1",
            display_name="GPU 1",
            role="gpu",
            agent_url=gpu_url,
            engine_url=gpu_url,
            online=True,
            yield_on_gaming=True,
            telemetry=NodeTelemetry(
                node_id="gpu-1",
                role="gpu",
                platform="linux",
                timestamp=1.0,
                online=True,
                engine="llama-server",
                engine_healthy=True,
                context_length=32768,
                models=[LoadedModel(id="qwen-32b", status="loaded")],
            ),
        )
        cluster.nodes["mac-1"] = NodeStatus(
            node_id="mac-1",
            display_name="Mac 1",
            role="unified",
            agent_url=mac_url,
            engine_url=mac_url,
            online=True,
            yield_on_gaming=False,
            telemetry=NodeTelemetry(
                node_id="mac-1",
                role="unified",
                platform="darwin",
                timestamp=1.0,
                online=True,
                engine="llama-server",
                engine_healthy=True,
                context_length=32768,
                models=[LoadedModel(id="qwen-7b", status="loaded")],
            ),
        )
        cluster.aliases["heavy"] = AliasConfig(
            alias="heavy",
            task_kind="code",
            primary_node="gpu-1",
            primary_model="qwen-32b",
        )
        cluster.aliases["fast"] = AliasConfig(
            alias="fast",
            task_kind="chat",
            primary_node="mac-1",
            primary_model="qwen-7b",
        )

        @asynccontextmanager
        async def _lifespan(_app: FastAPI):
            cluster._proxy_client = httpx.AsyncClient()
            try:
                yield
            finally:
                await cluster._proxy_client.aclose()

        app = FastAPI(lifespan=_lifespan)
        app.include_router(build_chat_router(cluster, tmp_path, tmp_path / "skills"))
        with TestClient(app) as tc:
            conv = tc.post(
                "/api/chat/conversations",
                json={"title": "Compaction Routing Test", "model": "fast"},
            ).json()
            store = ChatStore(tmp_path / "chats.db")
            for i in range(20):
                store.add_message(
                    ChatMessageRecord(
                        id=f"m_{i}",
                        conversation_id=conv["id"],
                        role="user" if i % 2 == 0 else "assistant",
                        content=f"msg{i}_" + ("x" * 2500),
                        created_at=float(1000 + i),
                    )
                )
            resp = tc.post(f"/api/chat/conversations/{conv['id']}/compact?keep_recent=4")
            assert resp.status_code == 200, resp.text
            return resp.json(), gpu_requests, mac_requests
    finally:
        gpu_srv.shutdown()
        gpu_srv.server_close()
        mac_srv.shutdown()
        mac_srv.server_close()
        gpu_th.join(timeout=2.0)
        mac_th.join(timeout=2.0)


def test_compaction_prefers_heavy_node_and_caps_payload(tmp_path: Path) -> None:
    body, gpu_reqs, mac_reqs = _run_compaction_against_loopback(tmp_path, gaming_mode=False)
    assert body["compacted"] is True
    assert body["summary"] == "- GPU summary"
    assert len(gpu_reqs) == 1
    assert len(mac_reqs) == 0

    req = gpu_reqs[0]
    assert req["model"] == "qwen-32b"
    assert req["max_tokens"] == 500
    assert req["chat_template_kwargs"] == {"enable_thinking": False}

    transcript = req["messages"][1]["content"]
    assert len(transcript) <= 14_000
    assert " ...[truncated]" in transcript
    for line in transcript.split("\n\n"):
        assert len(line) <= 1030


def test_compaction_falls_back_to_fast_when_gpu_gaming(tmp_path: Path) -> None:
    body, gpu_reqs, mac_reqs = _run_compaction_against_loopback(tmp_path, gaming_mode=True)
    assert body["compacted"] is True
    assert body["summary"] == "- Mac summary"
    assert len(gpu_reqs) == 0
    assert len(mac_reqs) == 1

    req = mac_reqs[0]
    assert req["model"] == "qwen-7b"
    assert req["max_tokens"] == 500
    assert req["chat_template_kwargs"] == {"enable_thinking": False}


def test_mcp_awareness_system_prompt_and_suggest_mcp_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _offline_get(self: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("registry unreachable in test")

    monkeypatch.setattr(httpx.AsyncClient, "get", _offline_get)

    captured_reqs: list[dict[str, Any]] = []
    round_deltas: list[dict[str, Any]] = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_pg",
                    "type": "function",
                    "function": {
                        "name": "suggest_mcp",
                        "arguments": json.dumps(
                            {"query": "postgres", "reason": "Need SQL database"}
                        ),
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_gh",
                    "type": "function",
                    "function": {
                        "name": "suggest_mcp",
                        "arguments": json.dumps(
                            {"query": "github", "reason": "Check GitHub issues"}
                        ),
                    },
                }
            ]
        },
        {"content": "Done."},
    ]

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            if self.path == "/mcp":
                rpc_resp = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload.get("id", 2),
                        "result": {
                            "tools": [
                                {
                                    "name": "list_issues",
                                    "description": "List repo issues",
                                    "inputSchema": {"type": "object", "properties": {}},
                                }
                            ]
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(rpc_resp)))
                self.end_headers()
                self.wfile.write(rpc_resp)
                return

            captured_reqs.append(payload)
            delta = round_deltas.pop(0) if round_deltas else {"content": "fallback"}
            sse_chunk = (
                f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n"
                "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse_chunk)))
            self.end_headers()
            self.wfile.write(sse_chunk)

        def log_message(self, format: str, *args: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        host, port = srv.server_address
        engine_url = f"http://{host}:{port}"

        (tmp_path / "mcp_servers.json").write_text(
            json.dumps(
                [
                    {
                        "id": "github",
                        "name": "GitHub",
                        "description": "GitHub repositories and issues",
                        "transport": "http",
                        "url": f"{engine_url}/mcp",
                        "enabled": True,
                    },
                    {
                        "id": "postgres",
                        "name": "Postgres",
                        "description": "PostgreSQL database queries",
                        "transport": "stdio",
                        "enabled": False,
                    },
                ]
            ),
            encoding="utf-8",
        )

        cfg = SplitchConfig()
        cluster = ClusterState(cfg, data_dir=tmp_path)
        cluster.nodes["gpu-1"] = NodeStatus(
            node_id="gpu-1",
            display_name="GPU 1",
            role="gpu",
            agent_url=engine_url,
            engine_url=engine_url,
            online=True,
            telemetry=NodeTelemetry(
                node_id="gpu-1",
                role="gpu",
                platform="linux",
                timestamp=1.0,
                online=True,
                engine="llama-server",
                engine_healthy=True,
                context_length=32768,
                models=[LoadedModel(id="qwen-32b", status="loaded")],
            ),
        )

        @asynccontextmanager
        async def _lifespan(_app: FastAPI):
            cluster._proxy_client = httpx.AsyncClient()
            try:
                yield
            finally:
                await cluster._proxy_client.aclose()

        app = FastAPI(lifespan=_lifespan)
        app.include_router(build_chat_router(cluster, tmp_path, tmp_path / "skills"))
        with TestClient(app) as tc:
            conv = tc.post(
                "/api/chat/conversations",
                json={
                    "title": "MCP Awareness Test",
                    "model": "qwen-32b",
                    "tools_enabled": True,
                    "enabled_mcps": ["github"],
                    "auto_approve": True,
                },
            ).json()
            resp = tc.post(
                f"/api/chat/conversations/{conv['id']}/send",
                json={"content": "Query postgres and check github"},
            )
            assert resp.status_code == 200, resp.text
            events = _parse_sse_events(resp.text)

        assert len(captured_reqs) == 3
        first_req = captured_reqs[0]
        tool_names = [t["function"]["name"] for t in first_req.get("tools", [])]
        assert "suggest_mcp" in tool_names
        assert "mcp__github__list_issues" in tool_names

        sys_prompt = first_req["messages"][0]["content"]
        assert "Enabled MCP Servers" in sys_prompt
        assert "`github` (GitHub)" in sys_prompt
        assert "mcp__github__list_issues" in sys_prompt
        assert "Installed-but-Inactive MCP Servers" in sys_prompt
        assert "`postgres` (Postgres)" in sys_prompt
        assert "suggest_mcp(query, reason)" in sys_prompt

        tool_results = [
            json.loads(data["output"])
            for ev, data in events
            if ev == "tool_result" and data.get("name") == "suggest_mcp"
        ]
        assert len(tool_results) == 2

        pg_res, gh_res = tool_results
        assert pg_res["query"] == "postgres"
        assert pg_res["registry_matches"] == []
        assert len(pg_res["local_matches"]) == 1
        assert pg_res["local_matches"][0]["id"] == "postgres"
        assert pg_res["local_matches"][0]["enabled_in_chat"] is False
        assert pg_res["local_matches"][0]["server_enabled"] is False

        assert gh_res["query"] == "github"
        assert gh_res["registry_matches"] == []
        assert len(gh_res["local_matches"]) == 1
        assert gh_res["local_matches"][0]["id"] == "github"
        assert gh_res["local_matches"][0]["enabled_in_chat"] is True
        assert gh_res["local_matches"][0]["server_enabled"] is True
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2.0)


def test_mcp_awareness_omits_suggest_mcp_when_tools_disabled(tmp_path: Path) -> None:
    captured_reqs: list[dict[str, Any]] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            captured_reqs.append(json.loads(raw.decode("utf-8")))
            sse_chunk = (
                f"data: {json.dumps({'choices': [{'delta': {'content': 'No tools.'}}]})}\n\n"
                "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse_chunk)))
            self.end_headers()
            self.wfile.write(sse_chunk)

        def log_message(self, format: str, *args: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        host, port = srv.server_address
        engine_url = f"http://{host}:{port}"
        (tmp_path / "mcp_servers.json").write_text(
            json.dumps(
                [
                    {
                        "id": "postgres",
                        "name": "Postgres",
                        "description": "PostgreSQL database queries",
                        "transport": "stdio",
                        "enabled": True,
                    }
                ]
            ),
            encoding="utf-8",
        )
        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        cluster.nodes["gpu-1"] = NodeStatus(
            node_id="gpu-1",
            display_name="GPU 1",
            role="gpu",
            agent_url=engine_url,
            engine_url=engine_url,
            online=True,
            telemetry=NodeTelemetry(
                node_id="gpu-1",
                role="gpu",
                platform="linux",
                timestamp=1.0,
                online=True,
                engine="llama-server",
                engine_healthy=True,
                context_length=32768,
                models=[LoadedModel(id="qwen-32b", status="loaded")],
            ),
        )

        @asynccontextmanager
        async def _lifespan(_app: FastAPI):
            cluster._proxy_client = httpx.AsyncClient()
            try:
                yield
            finally:
                await cluster._proxy_client.aclose()

        app = FastAPI(lifespan=_lifespan)
        app.include_router(build_chat_router(cluster, tmp_path, tmp_path / "skills"))
        with TestClient(app) as tc:
            conv = tc.post(
                "/api/chat/conversations",
                json={
                    "title": "No Tools Test",
                    "model": "qwen-32b",
                    "tools_enabled": False,
                },
            ).json()
            resp = tc.post(
                f"/api/chat/conversations/{conv['id']}/send",
                json={"content": "Hello"},
            )
            assert resp.status_code == 200, resp.text

        assert len(captured_reqs) == 1
        tool_names = [t["function"]["name"] for t in captured_reqs[0].get("tools", [])]
        assert "suggest_mcp" not in tool_names
        sys_prompt = captured_reqs[0]["messages"][0]["content"]
        assert "Enabled MCP Servers" not in sys_prompt
        assert "Installed-but-Inactive MCP Servers" not in sys_prompt
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2.0)




