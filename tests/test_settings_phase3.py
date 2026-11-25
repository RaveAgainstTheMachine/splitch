"""Independent verification tests for Phase 3 Agent & Chat Settings Overhaul."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.agent import main as agent_mod
from backend.router.chat_api import ChatStore, _requires_tool_approval, build_chat_router
from backend.router.config import RouterSettings, SplitchConfig
from backend.router.settings_api import apply_persisted_settings, build_settings_router
from backend.router.telemetry import ClusterState
from backend.router.workspace_api import build_workspace_router
from backend.shared.models import (
    ChatMessageRecord,
    ChatRulesConfig,
    ConversationCreate,
    LoadedModel,
    NodeStatus,
    NodeTelemetry,
    PromptPersona,
    RuntimeSettingsPatch,
)


@contextmanager
def _agent_bridge_server(agent_client: TestClient):
    """Real loopback HTTP/1.1 server forwarding requests to `agent_client` (zero mocks)."""

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length > 0 else b""
            hdrs = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "connection")
            }
            resp = agent_client.request(method, self.path, content=raw_body or None, headers=hdrs)
            body = resp.content
            self.send_response(resp.status_code)
            self.send_header("Content-Type", resp.headers.get("content-type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, format: str, *args: object) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2.0)


# ── 1. Approval Policy & Shell Allow/Denylist ────────────────────────────────


def test_requires_tool_approval_policies_allowlist_and_denylist_precedence() -> None:
    # ask: mutating tools and shell commands require approval; read-only tools do not
    cfg_ask = RouterSettings(approval_policy="ask")
    assert _requires_tool_approval(
        "read_file", {"path": "README.md"}, is_external_mcp=False, conv_auto_approve=False, router_cfg=cfg_ask
    ) is False
    for tool in ("write_file", "edit_file", "run_command"):
        args = {"command": "pytest -q"} if tool == "run_command" else {"path": "a.py"}
        assert _requires_tool_approval(
            tool, args, is_external_mcp=False, conv_auto_approve=False, router_cfg=cfg_ask
        ) is True

    # auto_edits: write_file/edit_file skipped; run_command still requires approval even if allowlisted
    cfg_auto_edits = RouterSettings(approval_policy="auto_edits")
    assert _requires_tool_approval(
        "write_file", {"path": "a.py"}, is_external_mcp=False, conv_auto_approve=False, router_cfg=cfg_auto_edits
    ) is False
    assert _requires_tool_approval(
        "edit_file", {"path": "a.py"}, is_external_mcp=False, conv_auto_approve=False, router_cfg=cfg_auto_edits
    ) is False
    assert _requires_tool_approval(
        "run_command", {"command": "pytest -q"}, is_external_mcp=False, conv_auto_approve=False, router_cfg=cfg_auto_edits
    ) is True

    # turbo_safe: write/edit skipped, allowlisted prefixes skipped, unlisted commands & prefix-lookalikes require approval
    cfg_turbo = RouterSettings(approval_policy="turbo_safe")
    assert _requires_tool_approval(
        "write_file", {"path": "a.py"}, is_external_mcp=False, conv_auto_approve=False, router_cfg=cfg_turbo
    ) is False
    assert _requires_tool_approval(
        "edit_file", {"path": "a.py"}, is_external_mcp=False, conv_auto_approve=False, router_cfg=cfg_turbo
    ) is False
    for allowed_cmd in ("pytest -q", "pytest", "  GIT STATUS  ", "git status --short", "rg TODO backend/"):
        assert _requires_tool_approval(
            "run_command", {"command": allowed_cmd}, is_external_mcp=False, conv_auto_approve=False, router_cfg=cfg_turbo
        ) is False

    for unlisted_cmd in ("python3 script.py", "pytest_evil", "lsblk", "", "   "):
        assert _requires_tool_approval(
            "run_command", {"command": unlisted_cmd}, is_external_mcp=False, conv_auto_approve=False, router_cfg=cfg_turbo
        ) is True

    # External MCP tools require approval unless conv_auto_approve=True or policy=="full_auto"
    assert _requires_tool_approval(
        "ext_tool", {}, is_external_mcp=True, conv_auto_approve=False, router_cfg=cfg_turbo
    ) is True
    assert _requires_tool_approval(
        "ext_tool", {}, is_external_mcp=True, conv_auto_approve=True, router_cfg=cfg_turbo
    ) is False

    # full_auto / conv_auto_approve: unlisted commands skip approval
    cfg_full = RouterSettings(approval_policy="full_auto")
    assert _requires_tool_approval(
        "run_command", {"command": "python3 script.py"}, is_external_mcp=False, conv_auto_approve=False, router_cfg=cfg_full
    ) is False
    assert _requires_tool_approval(
        "ext_tool", {}, is_external_mcp=True, conv_auto_approve=False, router_cfg=cfg_full
    ) is False

    # DENYLIST ALWAYS WINS: even in turbo_safe, full_auto, conv_auto_approve=True, or when allowlist prefix matches
    denied_cmds = (
        "rm -rf /tmp/foo",
        "sudo ls",
        "git push origin main",
        "pytest -q && sudo ls",
        "git status && rm -rf /",
        "GIT PUSH --force",
    )
    for denied in denied_cmds:
        for cfg in (cfg_ask, cfg_auto_edits, cfg_turbo, cfg_full):
            for conv_auto in (False, True):
                assert _requires_tool_approval(
                    "run_command",
                    {"command": denied},
                    is_external_mcp=False,
                    conv_auto_approve=conv_auto,
                    router_cfg=cfg,
                ) is True, f"Denylist failed to block {denied!r} under policy={cfg.approval_policy} conv_auto={conv_auto}"


# ── 2. RuntimeSettings Persistence & Validation ──────────────────────────────


def test_runtime_settings_phase3_persistence_validation_and_reload(tmp_path: Path) -> None:
    cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
    app = FastAPI()
    app.include_router(
        build_settings_router(
            cluster,
            tmp_path,
            api_key_getter=lambda: "",
        )
    )
    client = TestClient(app)

    # Defaults
    r_get = client.get("/api/settings")
    assert r_get.status_code == 200
    d0 = r_get.json()
    assert d0["approval_policy"] == "ask"
    assert d0["max_agent_steps"] == 8
    assert d0["auto_compact_threshold"] == pytest.approx(0.80)
    assert d0["thinking_display"] == "collapsed"
    assert d0["completion_notifications"] is False
    assert "pytest" in d0["shell_allowlist"]
    assert "rm -rf" in d0["shell_denylist"]

    # Boundary validation errors on PATCH
    for bad_payload in (
        {"max_agent_steps": 0},
        {"max_agent_steps": 33},
        {"auto_compact_threshold": 0.49},
        {"auto_compact_threshold": 0.96},
        {"approval_policy": "yolo"},
        {"thinking_display": "raw"},
    ):
        bad_resp = client.patch("/api/settings", json=bad_payload)
        assert bad_resp.status_code == 422

    # Direct Pydantic schema validation
    with pytest.raises(ValidationError):
        RuntimeSettingsPatch(max_agent_steps=33)
    with pytest.raises(ValidationError):
        RuntimeSettingsPatch(auto_compact_threshold=0.45)

    # Valid PATCH persists to settings.json and updates live cluster.config.router
    r_patch = client.patch(
        "/api/settings",
        json={
            "approval_policy": "turbo_safe",
            "shell_allowlist": ["pytest", "  git status  ", ""],
            "shell_denylist": ["rm -rf", "sudo", ""],
            "max_agent_steps": 16,
            "auto_compact_threshold": 0.90,
            "thinking_display": "expanded",
            "completion_notifications": True,
        },
    )
    assert r_patch.status_code == 200
    d1 = r_patch.json()
    assert d1["approval_policy"] == "turbo_safe"
    assert d1["shell_allowlist"] == ["pytest", "git status"]
    assert d1["shell_denylist"] == ["rm -rf", "sudo"]
    assert d1["max_agent_steps"] == 16
    assert d1["auto_compact_threshold"] == pytest.approx(0.90)
    assert d1["thinking_display"] == "expanded"
    assert d1["completion_notifications"] is True

    # Reload into a fresh ClusterState via apply_persisted_settings()
    fresh = ClusterState(SplitchConfig(), data_dir=tmp_path)
    apply_persisted_settings(fresh, tmp_path)
    assert fresh.config.router.approval_policy == "turbo_safe"
    assert fresh.config.router.shell_allowlist == ["pytest", "git status"]
    assert fresh.config.router.shell_denylist == ["rm -rf", "sudo"]
    assert fresh.config.router.max_agent_steps == 16
    assert fresh.config.router.auto_compact_threshold == pytest.approx(0.90)
    assert fresh.config.router.thinking_display == "expanded"
    assert fresh.config.router.completion_notifications is True

    # Clamping when settings.json contains out-of-range values on disk
    settings_path = tmp_path / "settings.json"
    on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
    on_disk["max_agent_steps"] = 999
    on_disk["auto_compact_threshold"] = 0.10
    settings_path.write_text(json.dumps(on_disk), encoding="utf-8")

    clamped = ClusterState(SplitchConfig(), data_dir=tmp_path)
    apply_persisted_settings(clamped, tmp_path)
    assert clamped.config.router.max_agent_steps == 32
    assert clamped.config.router.auto_compact_threshold == pytest.approx(0.50)


# ── 3. Global Rules, Personas, max_agent_steps & auto_compact_threshold ──────


def test_chat_rules_personas_and_send_injection_steps_compaction(tmp_path: Path) -> None:
    captured_requests: list[dict[str, Any]] = []
    stream_call_count = 0
    tool_loop_mode = {"enabled": False}

    upstream_app = FastAPI()

    @upstream_app.post("/v1/chat/completions")
    async def _mock_upstream_completions(request: Request):
        nonlocal stream_call_count
        body = await request.json()
        captured_requests.append(body)

        # Non-streaming compaction call
        if not body.get("stream", False):
            return JSONResponse(
                {
                    "id": "cmp-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Compacted summary."},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

        stream_call_count += 1
        call_idx = stream_call_count

        async def _sse_chunks():
            if tool_loop_mode["enabled"]:
                # Emit a read-only tool call on every round to test max_agent_steps cap
                chunk = {
                    "id": f"c-{call_idx}",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": f"call_{call_idx}",
                                        "type": "function",
                                        "function": {
                                            "name": "list_dir",
                                            "arguments": json.dumps({"path": "."}),
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n".encode()
                yield b"data: [DONE]\n\n"
                return

            chunk = {
                "id": f"c-{call_idx}",
                "choices": [{"index": 0, "delta": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(_sse_chunks(), media_type="text/event-stream")

    @upstream_app.post("/tools/execute")
    async def _mock_agent_tools(request: Request):
        payload = await request.json()
        return JSONResponse({"ok": True, "tool": payload.get("tool"), "output": "README.md"})

    cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
    cluster.nodes["gpu-1"] = NodeStatus(
        node_id="gpu-1",
        display_name="GPU 1",
        role="gpu",
        agent_url="http://upstream.asgi",
        engine_url="http://upstream.asgi",
        online=True,
        telemetry=NodeTelemetry(
            node_id="gpu-1",
            role="gpu",
            platform="linux",
            timestamp=1.0,
            online=True,
            engine="llama-server",
            engine_healthy=True,
            context_length=100,
            models=[LoadedModel(id="qwen-32b", status="loaded")],
        ),
    )

    proxy_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream_app))
    cluster._proxy_client = proxy_client
    try:
        app = FastAPI()
        app.include_router(build_chat_router(cluster, tmp_path, tmp_path / "skills"))
        client = TestClient(app)

        # GET /api/chat/rules when missing returns defaults
        r_empty = client.get("/api/chat/rules")
        assert r_empty.status_code == 200
        assert r_empty.json() == {"global_rules": "", "personas": []}

        # PUT /api/chat/rules persists to data_dir/rules.json
        rules_payload = ChatRulesConfig(
            global_rules="Always cite exact file paths.\nNever guess.",
            personas=[PromptPersona(id="reviewer", name="Code Reviewer", prompt="Be strict.")],
        )
        r_put = client.put("/api/chat/rules", json=rules_payload.model_dump())
        assert r_put.status_code == 200
        assert (tmp_path / "rules.json").is_file()

        r_get = client.get("/api/chat/rules")
        assert r_get.status_code == 200
        assert r_get.json()["global_rules"] == "Always cite exact file paths.\nNever guess."
        assert len(r_get.json()["personas"]) == 1
        assert r_get.json()["personas"][0]["id"] == "reviewer"

        # 1) Global User Rules injected into upstream system message
        store = ChatStore(tmp_path / "chats.db")
        conv = store.create_conversation(
            ConversationCreate(
                title="Rules Test",
                model="qwen-32b",
                tools_enabled=True,
                web_search_enabled=False,
            )
        )
        r_send = client.post(
            f"/api/chat/conversations/{conv.id}/send",
            json={"content": "Hello"},
        )
        assert r_send.status_code == 200
        assert "event: done" in r_send.text
        sys_msg = captured_requests[0]["messages"][0]
        assert sys_msg["role"] == "system"
        assert "Global User Rules:\nAlways cite exact file paths.\nNever guess." in sys_msg["content"]

        # 2) max_agent_steps caps tool-call loop rounds
        captured_requests.clear()
        stream_call_count = 0
        tool_loop_mode["enabled"] = True
        cluster.config.router.max_agent_steps = 2

        r_steps = client.post(
            f"/api/chat/conversations/{conv.id}/send",
            json={"content": "Loop tools"},
        )
        assert r_steps.status_code == 200
        assert stream_call_count == 2

        # 3) auto_compact_threshold triggers compaction when ratio >= configured threshold
        tool_loop_mode["enabled"] = False
        captured_requests.clear()
        conv_compact = store.create_conversation(
            ConversationCreate(
                title="Compact Test",
                model="qwen-32b",
                tools_enabled=False,
                web_search_enabled=False,
                auto_compact=True,
            )
        )
        # Seed 8 messages (~30 chars each -> > 50 tokens with system prompt on context_length=100)
        base_ts = time.time() - 20.0
        for i in range(8):
            store.add_message(
                ChatMessageRecord(
                    id=f"m-{i}",
                    conversation_id=conv_compact.id,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"Turn {i}: " + ("x" * 24),
                    created_at=base_ts + i,
                )
            )

        # With threshold=0.95 and context_length=250 (ratio ~ 140/250 = 0.56), should NOT compact
        cluster.nodes["gpu-1"].telemetry.context_length = 250  # type: ignore[union-attr]
        cluster.config.router.auto_compact_threshold = 0.95
        r_no_cmp = client.post(
            f"/api/chat/conversations/{conv_compact.id}/send",
            json={"content": "Check no compact"},
        )
        assert r_no_cmp.status_code == 200
        assert "event: compacted" not in r_no_cmp.text

        # With threshold=0.50 and same context_length=250, ratio >= 0.50 -> MUST compact
        cluster.config.router.auto_compact_threshold = 0.50
        r_cmp = client.post(
            f"/api/chat/conversations/{conv_compact.id}/send",
            json={"content": "Trigger compact"},
        )
        assert r_cmp.status_code == 200
        assert "event: compacting" in r_cmp.text
        assert "event: compacted" in r_cmp.text
    finally:
        import asyncio

        asyncio.run(proxy_client.aclose())


# ── 4. Workspace AGENTS.md (Agent + Router Proxy) ────────────────────────────


def test_workspace_agents_md_direct_and_router_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_root = tmp_path / "ws"
    sub_dir = ws_root / "proj"
    sub_dir.mkdir(parents=True)

    monkeypatch.setattr(agent_mod, "WORKSPACE_ROOT", str(ws_root))
    monkeypatch.setattr(agent_mod, "ENABLE_TOOLS", True)
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_mod, "ROUTER_PUBLIC_KEY", None)

    agent_client = TestClient(agent_mod.app)

    # Direct agent validation: reject path traversal in subpath with 400
    r_trav = agent_client.post(
        "/workspace/agents-md",
        json={"subpath": "../outside", "content": "bad"},
    )
    assert r_trav.status_code == 400

    # Reject non-existent subpath directory with 400
    r_missing_dir = agent_client.post(
        "/workspace/agents-md",
        json={"subpath": "does-not-exist", "content": "bad"},
    )
    assert r_missing_dir.status_code == 400

    # Router proxy end-to-end over real loopback HTTP server
    with _agent_bridge_server(agent_client) as agent_url:
        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        cluster.nodes["gpu-1"] = NodeStatus(
            node_id="gpu-1",
            display_name="GPU 1",
            role="gpu",
            agent_url=agent_url,
            engine_url="http://127.0.0.1:8080",
            online=True,
        )
        router_app = FastAPI()
        router_app.include_router(
            build_workspace_router(
                cluster,
                internal_auth_headers_getter=dict,
                app_ref=router_app,
            )
        )
        router_client = TestClient(router_app)

        # Missing AGENTS.md -> exists=False
        r_get_missing = router_client.get("/api/nodes/gpu-1/workspace/agents-md")
        assert r_get_missing.status_code == 200
        assert r_get_missing.json() == {"content": "", "exists": False, "path": "AGENTS.md"}

        # PUT saves AGENTS.md at root and round-trips via GET
        r_put_root = router_client.put(
            "/api/nodes/gpu-1/workspace/agents-md",
            json={"subpath": "", "content": "# Root Agents\nAlways run pytest.\n"},
        )
        assert r_put_root.status_code == 200
        assert r_put_root.json() == {
            "content": "# Root Agents\nAlways run pytest.\n",
            "exists": True,
            "path": "AGENTS.md",
        }
        assert (ws_root / "AGENTS.md").read_text(encoding="utf-8") == "# Root Agents\nAlways run pytest.\n"

        r_get_root = router_client.get("/api/nodes/gpu-1/workspace/agents-md")
        assert r_get_root.status_code == 200
        assert r_get_root.json()["exists"] is True
        assert r_get_root.json()["content"] == "# Root Agents\nAlways run pytest.\n"

        # Subpath round-trip
        r_put_sub = router_client.put(
            "/api/nodes/gpu-1/workspace/agents-md",
            json={"subpath": "proj", "content": "# Subproject Agents\n"},
        )
        assert r_put_sub.status_code == 200
        assert (sub_dir / "AGENTS.md").read_text(encoding="utf-8") == "# Subproject Agents\n"

        r_get_sub = router_client.get("/api/nodes/gpu-1/workspace/agents-md", params={"subpath": "proj"})
        assert r_get_sub.status_code == 200
        assert r_get_sub.json() == {
            "content": "# Subproject Agents\n",
            "exists": True,
            "path": "AGENTS.md",
        }

        # Path traversal through router proxy returns 400
        r_put_trav = router_client.put(
            "/api/nodes/gpu-1/workspace/agents-md",
            json={"subpath": "../../escape", "content": "nope"},
        )
        assert r_put_trav.status_code == 400
