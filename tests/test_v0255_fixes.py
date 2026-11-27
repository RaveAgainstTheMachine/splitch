"""Independent adversarial & edge-case tests for Splitch v0.25.5 fixes."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.agent import main as agent_main
from backend.router.anthropic_api import handle_anthropic_messages
from backend.router.chat_api import ChatStore, build_chat_router
from backend.router.chat_context import (
    _records_to_openai_messages,
    adjust_reasoning_effort_on_error,
    execute_discovery_tool,
    requires_tool_approval,
    run_compaction,
)
from backend.router.chat_mcp import call_mcp_tool
from backend.router.config import SplitchConfig
from backend.router.policy import RouteDecision
from backend.router.swarm import SwarmManager
from backend.router.telemetry import AgentEd25519Auth, ClusterState
from backend.shared.models import (
    AliasConfig,
    ChatMessageRecord,
    ConversationCreate,
    LoadedModel,
    NodeStatus,
    NodeTelemetry,
)
from backend.shared.secrets import (
    generate_ed25519_keypair,
    load_ed25519_public_key,
    verify_agent_request_ed25519,
)


def _parse_sse_events(raw_text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    cur_ev = ""
    for line in raw_text.splitlines():
        if line.startswith("event: "):
            cur_ev = line[7:].strip()
        elif line.startswith("data: "):
            payload = line[6:].strip()
            if payload == "[DONE]":
                continue
            try:
                events.append((cur_ev, json.loads(payload)))
            except ValueError:
                pass
    return events


def _seed_overflow_history(store: ChatStore, conv_id: str, turns: int = 8) -> None:
    for i in range(turns):
        store.add_message(
            ChatMessageRecord(
                id=f"u_{i}",
                conversation_id=conv_id,
                role="user",
                content=f"Question {i}",
                created_at=float(i * 2 + 1),
            )
        )
        store.add_message(
            ChatMessageRecord(
                id=f"a_{i}",
                conversation_id=conv_id,
                role="assistant",
                content=f"Answer {i}",
                created_at=float(i * 2 + 2),
            )
        )


def test_agent_ed25519_auth_post_body_tamper_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, compact_pub = generate_ed25519_keypair(tmp_path / "router_ed25519.pem")
    pub_key = load_ed25519_public_key(compact_pub)
    cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
    cluster.agent_token = ""
    assert cluster.router_private_key is not None

    monkeypatch.setattr(agent_main, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_main, "ROUTER_PUBLIC_KEY", pub_key)
    monkeypatch.setenv("SPLITCH_NO_DESKTOP", "1")

    captured_headers: list[dict[str, str]] = []

    class _CapturingAuth(httpx.Auth):
        requires_request_body = True

        def __init__(self, inner: httpx.Auth) -> None:
            self._inner = inner

        def auth_flow(self, request: httpx.Request):
            flow = self._inner.auth_flow(request)
            req = next(flow)
            captured_headers.append(dict(req.headers))
            yield req

    agent_auth = cluster.agent_auth()
    assert isinstance(agent_auth, AgentEd25519Auth)
    assert agent_auth.requires_request_body is True

    async def _run_auth_checks() -> None:
        transport = httpx.ASGITransport(app=agent_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Signed POST with non-empty JSON body -> 200 OK
            r_ok = await client.post(
                "/desktop/sync-theme",
                json={"accent": "#10b981", "theme_mode": "dark"},
                auth=_CapturingAuth(agent_auth),
            )
            assert r_ok.status_code == 200, r_ok.text
            assert r_ok.json()["ok"] is True
            assert len(captured_headers) == 1
            signed_hdrs = {
                k: v
                for k, v in captured_headers[0].items()
                if k.lower().startswith("x-splitch-auth-")
            }
            assert "x-splitch-auth-body-sha256" in {k.lower() for k in signed_hdrs}
            assert "x-splitch-auth-nonce" in {k.lower() for k in signed_hdrs}

            # 2. Signed GET with empty body -> 200 OK
            r_get = await client.get("/metrics", auth=agent_auth)
            assert r_get.status_code == 200

            # 3. Replayed nonce with exact same headers & body -> 401 Unauthorized
            r_replay = await client.post(
                "/desktop/sync-theme",
                content=json.dumps({"accent": "#10b981", "theme_mode": "dark"}).encode(),
                headers={"Content-Type": "application/json", **signed_hdrs},
            )
            assert r_replay.status_code == 401

            # 4. Tampered X-Splitch-Auth-Body-Sha256 -> 401 Unauthorized
            captured_headers.clear()
            r_ok2 = await client.post(
                "/desktop/sync-theme",
                json={"accent": "#f59e0b"},
                auth=_CapturingAuth(agent_auth),
            )
            assert r_ok2.status_code == 200
            tampered_hdrs = {
                k: v
                for k, v in captured_headers[0].items()
                if k.lower().startswith("x-splitch-auth-")
            }
            for k in list(tampered_hdrs):
                if k.lower() == "x-splitch-auth-body-sha256":
                    tampered_hdrs[k] = "0" * 64
            r_tamper_sha = await client.post(
                "/desktop/sync-theme",
                json={"accent": "#f59e0b"},
                headers=tampered_hdrs,
            )
            assert r_tamper_sha.status_code == 401

            # 5. Tampered JSON body with fresh signature computed for different body -> 401
            auth_req = client.build_request(
                "POST",
                "/desktop/sync-theme",
                json={"accent": "#38bdf8"},
            )
            list(agent_auth.auth_flow(auth_req))
            unused_hdrs = {
                k: v
                for k, v in auth_req.headers.items()
                if k.lower().startswith("x-splitch-auth-")
            }
            r_tamper_body = await client.post(
                "/desktop/sync-theme",
                json={"accent": "#ef4444"},
                headers=unused_hdrs,
            )
            assert r_tamper_body.status_code == 401

    asyncio.run(_run_auth_checks())


def test_suggest_mcp_real_registry_results_and_chat_tool_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _registry_handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "registry.modelcontextprotocol.io":
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {
                            "server": {
                                "name": "io.github.acme/filesystem-mcp",
                                "description": "Read and write workspace files",
                                "version": "1.2.0",
                                "packages": [
                                    {
                                        "registryType": "npm",
                                        "identifier": "@acme/filesystem-mcp",
                                    }
                                ],
                            },
                            "_meta": {
                                "io.modelcontextprotocol.registry/official": {
                                    "status": "active"
                                }
                            },
                        }
                    ]
                },
            )
        if host == "registry.npmjs.org":
            return httpx.Response(
                200,
                json={
                    "objects": [
                        {
                            "package": {
                                "name": "@acme/postgres-mcp",
                                "description": "Postgres query MCP",
                                "version": "0.4.0",
                                "links": {"repository": "https://github.com/acme/postgres-mcp"},
                            },
                            "downloads": {"weekly": 420},
                        }
                    ]
                },
            )
        return httpx.Response(404)

    orig_async_client = httpx.AsyncClient
    active_handler = _registry_handler

    class _TransportAsyncClient(orig_async_client):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(active_handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _TransportAsyncClient)

    ok, raw_out = asyncio.run(
        execute_discovery_tool(
            "suggest_mcp",
            {"query": "postgres", "reason": "Need DB inspection"},
            data_dir=tmp_path,
            skills_dir=tmp_path / "skills",
            mcp_map={},
            eff_skills=[],
        )
    )
    assert ok is True
    parsed = json.loads(raw_out)
    reg_matches = parsed["registry_matches"]
    assert len(reg_matches) == 2
    by_source = {item["source"]: item for item in reg_matches}
    assert by_source["official"]["registry_name"] == "io.github.acme/filesystem-mcp"
    assert by_source["official"]["name"] == "io.github.acme/filesystem-mcp"
    assert by_source["npm"]["registry_name"] == "@acme/postgres-mcp"
    assert by_source["npm"]["name"] == "@acme/postgres-mcp"

    # Upstream registry HTTP 500 degrades gracefully without crashing
    def _error_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="registry unavailable")

    active_handler = _error_handler
    ok_err, raw_err = asyncio.run(
        execute_discovery_tool(
            "suggest_mcp",
            {"query": "postgres", "reason": "test"},
            data_dir=tmp_path,
            skills_dir=tmp_path / "skills",
            mcp_map={},
            eff_skills=[],
        )
    )
    assert ok_err is True
    assert json.loads(raw_err)["registry_matches"] == []


def test_records_to_openai_messages_sanitizes_orphans_and_transitions() -> None:
    records = [
        # 1. Leading orphan tool message (no preceding assistant tool_calls) -> dropped
        ChatMessageRecord(
            id="t_lead",
            conversation_id="c1",
            role="tool",
            content="orphan start",
            tool_call_id="call_lead",
            tool_name="read_file",
            created_at=1.0,
        ),
        ChatMessageRecord(
            id="u1",
            conversation_id="c1",
            role="user",
            content="First question",
            created_at=2.0,
        ),
        # 2. Orphan tool after user -> dropped
        ChatMessageRecord(
            id="t_after_user",
            conversation_id="c1",
            role="tool",
            content="orphan after user",
            tool_call_id="call_bad",
            tool_name="list_dir",
            created_at=3.0,
        ),
        # 3. Assistant with 2 tool_calls where only 1 has a recorded tool result before next user
        ChatMessageRecord(
            id="a1",
            conversation_id="c1",
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "grep_search", "arguments": "{}"},
                },
            ],
            created_at=4.0,
        ),
        ChatMessageRecord(
            id="t1",
            conversation_id="c1",
            role="tool",
            content="file contents",
            tool_call_id="call_1",
            tool_name="read_file",
            created_at=5.0,
        ),
        ChatMessageRecord(
            id="u2",
            conversation_id="c1",
            role="user",
            content="Second question",
            created_at=6.0,
        ),
        # 4. Valid tool call + tool result followed directly by user -> inserts interrupted assistant
        ChatMessageRecord(
            id="a2",
            conversation_id="c1",
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_3",
                    "type": "function",
                    "function": {"name": "list_dir", "arguments": "{}"},
                }
            ],
            created_at=7.0,
        ),
        ChatMessageRecord(
            id="t3",
            conversation_id="c1",
            role="tool",
            content="dir listing",
            tool_call_id="call_3",
            tool_name="list_dir",
            created_at=8.0,
        ),
        ChatMessageRecord(
            id="u3",
            conversation_id="c1",
            role="user",
            content="Third question",
            created_at=9.0,
        ),
        # 5. Trailing orphaned assistant(tool_calls) at end of history -> stub tool appended
        ChatMessageRecord(
            id="a3",
            conversation_id="c1",
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_tail",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                }
            ],
            created_at=10.0,
        ),
    ]

    msgs = _records_to_openai_messages("System", records)
    roles = [m["role"] for m in msgs]
    assert roles == [
        "system",
        "user",       # u1 (t_lead & t_after_user dropped)
        "assistant",  # a1 (call_1, call_2)
        "tool",       # call_1
        "tool",       # stub call_2
        "assistant",  # [Previous turn interrupted]
        "user",       # u2
        "assistant",  # a2 (call_3)
        "tool",       # call_3
        "assistant",  # [Previous turn interrupted]
        "user",       # u3
        "assistant",  # a3 (call_tail)
        "tool",       # stub call_tail
    ]
    assert msgs[4]["tool_call_id"] == "call_2"
    assert msgs[4]["name"] == "grep_search"
    assert msgs[4]["content"] == "[Interrupted before tool result was recorded]"
    assert msgs[5]["content"] == "[Previous turn interrupted]"
    assert msgs[9]["content"] == "[Previous turn interrupted]"
    assert msgs[12]["tool_call_id"] == "call_tail"
    assert msgs[12]["name"] == "run_command"
    assert "tool" not in roles[:2]


def test_chat_api_failover_502_primary_to_fallback(tmp_path: Path) -> None:
    primary_calls: list[dict[str, Any]] = []
    fallback_calls: list[dict[str, Any]] = []

    class _Primary502(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            primary_calls.append(json.loads(self.rfile.read(n).decode()))
            body = b"bad gateway from primary"
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    class _Fallback200(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            fallback_calls.append(json.loads(self.rfile.read(n).decode()))
            sse = (
                b'data: {"choices":[{"delta":{"content":"from-fallback"}}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            self.wfile.write(sse)

        def log_message(self, format: str, *args: object) -> None:
            return

    srv_a = ThreadingHTTPServer(("127.0.0.1", 0), _Primary502)
    srv_b = ThreadingHTTPServer(("127.0.0.1", 0), _Fallback200)
    th_a = threading.Thread(target=srv_a.serve_forever, daemon=True)
    th_b = threading.Thread(target=srv_b.serve_forever, daemon=True)
    th_a.start()
    th_b.start()
    try:
        url_a = f"http://{srv_a.server_address[0]}:{srv_a.server_address[1]}"
        url_b = f"http://{srv_b.server_address[0]}:{srv_b.server_address[1]}"
        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        cluster.aliases["fast"] = AliasConfig(
            alias="fast",
            primary_node="node-a",
            primary_model="m-primary",
            fallback_node="node-b",
            fallback_model="m-fallback",
        )
        for nid, url, mid in (("node-a", url_a, "m-primary"), ("node-b", url_b, "m-fallback")):
            cluster.nodes[nid] = NodeStatus(
                node_id=nid,
                display_name=nid,
                role="gpu",
                agent_url=url,
                engine_url=url,
                online=True,
                telemetry=NodeTelemetry(
                    node_id=nid,
                    role="gpu",
                    platform="linux",
                    timestamp=1.0,
                    online=True,
                    engine="llama-server",
                    engine_healthy=True,
                    context_length=32768,
                    models=[LoadedModel(id=mid, status="loaded")],
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
                json={"title": "Failover", "model": "fast", "tools_enabled": False},
            ).json()
            resp = tc.post(
                f"/api/chat/conversations/{conv['id']}/send",
                json={"content": "hello"},
            )
            assert resp.status_code == 200
            events = _parse_sse_events(resp.text)
            metas = [p for ev, p in events if ev == "meta"]
            assert [m["node"] for m in metas] == ["node-a", "node-b"]
            assert ("token", {"text": "from-fallback"}) in events
            assert any(ev == "done" for ev, _ in events)
            assert not any(ev == "error" for ev, _ in events)
            assert len(primary_calls) == 1
            assert len(fallback_calls) == 1
            assert cluster.nodes["node-a"].in_flight == 0
            assert cluster.nodes["node-b"].in_flight == 0
    finally:
        srv_a.shutdown()
        srv_a.server_close()
        srv_b.shutdown()
        srv_b.server_close()
        th_a.join(timeout=2.0)
        th_b.join(timeout=2.0)


def test_chat_api_overflow_autocompact_retry_single_node(tmp_path: Path) -> None:
    stream_calls: list[dict[str, Any]] = []
    compact_calls: list[dict[str, Any]] = []

    class _OverflowHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n).decode())
            if not body.get("stream", False):
                compact_calls.append(body)
                out = json.dumps(
                    {"choices": [{"message": {"content": "- Compacted older turns"}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

            stream_calls.append(body)
            if len(stream_calls) == 1:
                err = json.dumps(
                    {
                        "error": {
                            "type": "exceed_context_size_error",
                            "message": "request exceeds the available context size",
                        }
                    }
                ).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return

            sse = (
                b'data: {"choices":[{"delta":{"content":"recovered-after-compact"}}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":4,"total_tokens":24}}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            self.wfile.write(sse)

        def log_message(self, format: str, *args: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _OverflowHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://{srv.server_address[0]}:{srv.server_address[1]}"
        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        cluster.nodes["node-1"] = NodeStatus(
            node_id="node-1",
            display_name="Node 1",
            role="gpu",
            agent_url=url,
            engine_url=url,
            online=True,
            telemetry=NodeTelemetry(
                node_id="node-1",
                role="gpu",
                platform="linux",
                timestamp=1.0,
                online=True,
                engine="llama-server",
                engine_healthy=True,
                context_length=32768,
                models=[LoadedModel(id="m1", status="loaded")],
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
                    "title": "Overflow",
                    "model": "m1",
                    "tools_enabled": False,
                    "auto_compact": True,
                },
            ).json()
            _seed_overflow_history(ChatStore(tmp_path / "chats.db"), conv["id"], turns=8)
            resp = tc.post(
                f"/api/chat/conversations/{conv['id']}/send",
                json={"content": "trigger overflow"},
            )
            assert resp.status_code == 200
            events = _parse_sse_events(resp.text)
            assert any(ev == "compacted" and pl.get("compacted") is True for ev, pl in events)
            assert not any(ev == "error" for ev, _ in events), events
            assert ("token", {"text": "recovered-after-compact"}) in events
            assert len(compact_calls) == 1
            assert len(stream_calls) == 2
            assert len(stream_calls[1]["messages"]) < len(stream_calls[0]["messages"])
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2.0)


def test_resolve_upstream_target_strata_proxy_across_callers(tmp_path: Path) -> None:
    _, compact_pub = generate_ed25519_keypair(tmp_path / "router_ed25519.pem")
    pub_key = load_ed25519_public_key(compact_pub)

    recorded_requests: list[tuple[str, dict[str, str], bytes]] = []

    class _StrataAgentHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n)
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            recorded_requests.append((self.path, hdrs, raw))

            ok = verify_agent_request_ed25519(
                pub_key,
                hdrs.get("x-splitch-auth-ts", ""),
                hdrs.get("x-splitch-auth-sig", ""),
                "POST",
                self.path,
                nonce_header=hdrs.get("x-splitch-auth-nonce", ""),
                body=raw,
                body_sha256_header=hdrs.get("x-splitch-auth-body-sha256", ""),
            )
            if not ok:
                self.send_response(401)
                self.end_headers()
                return

            parsed = json.loads(raw.decode())
            if parsed.get("stream"):
                sse = (
                    b'data: {"choices":[{"delta":{"content":"strata-ok"}}]}\n\n'
                    b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
                    b"data: [DONE]\n\n"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(sse)))
                self.end_headers()
                self.wfile.write(sse)
            else:
                out = json.dumps(
                    {
                        "id": "chatcmpl-1",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "strata-sync-ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        def log_message(self, format: str, *args: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StrataAgentHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        agent_url = f"http://{srv.server_address[0]}:{srv.server_address[1]}"
        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        node = NodeStatus(
            node_id="strata-node",
            display_name="Strata Node",
            role="gpu",
            agent_url=agent_url,
            engine_url="http://127.0.0.1:9",  # unreachable direct port to prove proxy is used
            online=True,
            telemetry=NodeTelemetry(
                node_id="strata-node",
                role="gpu",
                platform="linux",
                timestamp=1.0,
                online=True,
                engine="llama-server",
                engine_healthy=True,
                context_length=32768,
                strata_healthy=True,
                strata_models=[LoadedModel(id="strata-70b", status="loaded")],
            ),
        )
        cluster.nodes["strata-node"] = node

        # Direct unit check of resolve_upstream_target
        d_strata = RouteDecision(
            node=node, target_model="strata-70b", requested_model="strata-70b", use_strata_proxy=True
        )
        u_strata, auth_strata = cluster.resolve_upstream_target(d_strata, "/v1/chat/completions")
        assert u_strata == f"{agent_url}/strata/proxy/v1/chat/completions"
        assert isinstance(auth_strata, AgentEd25519Auth)

        d_plain = RouteDecision(
            node=node, target_model="m", requested_model="m", use_strata_proxy=False
        )
        u_plain, auth_plain = cluster.resolve_upstream_target(d_plain, "/v1/chat/completions")
        assert u_plain == "http://127.0.0.1:9/v1/chat/completions"
        assert auth_plain is None

        @asynccontextmanager
        async def _lifespan(_app: FastAPI):
            cluster._proxy_client = httpx.AsyncClient()
            try:
                yield
            finally:
                await cluster._proxy_client.aclose()

        app = FastAPI(lifespan=_lifespan)
        app.include_router(build_chat_router(cluster, tmp_path, tmp_path / "skills"))

        @app.post("/v1/messages")
        async def _anth_route(request: Request):
            return await handle_anthropic_messages(request, cluster)

        with TestClient(app) as tc:
            # 1. chat_api.py via Strata proxy
            conv = tc.post(
                "/api/chat/conversations",
                json={"title": "Strata", "model": "strata-70b", "tools_enabled": False},
            ).json()
            r_chat = tc.post(
                f"/api/chat/conversations/{conv['id']}/send",
                json={"content": "hi strata"},
            )
            assert r_chat.status_code == 200
            assert ("token", {"text": "strata-ok"}) in _parse_sse_events(r_chat.text)

            # 2. anthropic_api.py non-streaming & streaming via Strata proxy
            r_anth_sync = tc.post(
                "/v1/messages",
                json={
                    "model": "strata-70b",
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert r_anth_sync.status_code == 200
            assert r_anth_sync.json()["content"][0]["text"] == "strata-sync-ok"

            r_anth_stream = tc.post(
                "/v1/messages",
                json={
                    "model": "strata-70b",
                    "max_tokens": 32,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert r_anth_stream.status_code == 200
            assert "strata-ok" in r_anth_stream.text

        # 3. run_compaction via Strata proxy + 4. SwarmManager via Strata proxy
        async def _run_async() -> None:
            cluster._proxy_client = httpx.AsyncClient()
            try:
                store = ChatStore(tmp_path / "chats.db")
                c2 = store.create_conversation(
                    ConversationCreate(title="Comp", model="strata-70b", tools_enabled=False)
                )
                _seed_overflow_history(store, c2.id, turns=6)
                c_res = await run_compaction(cluster, store, c2, keep_recent=4)
                assert c_res.compacted is True

                swarm_mgr = SwarmManager(
                    cluster,
                    store,
                    {},
                    requires_tool_approval,
                    adjust_reasoning_effort_on_error,
                    call_mcp_tool,
                )
                run = await swarm_mgr.start_run(c2)
                worker = await swarm_mgr.spawn_worker(
                    run_id=run.id,
                    conv=c2,
                    role="Verifier",
                    task="Reply briefly",
                    model="strata-70b",
                    readonly=True,
                )
                done_workers = await swarm_mgr.wait_agents(run.id, [worker.id], timeout_s=10.0)
                assert done_workers[0].status == "done"
                assert done_workers[0].result == "strata-ok"
            finally:
                await cluster._proxy_client.aclose()

        asyncio.run(_run_async())

        # All 5 calls (chat, anth sync, anth stream, compaction, swarm) hit /strata/proxy/v1/chat/completions
        assert len(recorded_requests) == 5
        assert all(p == "/strata/proxy/v1/chat/completions" for p, _, _ in recorded_requests)
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2.0)
