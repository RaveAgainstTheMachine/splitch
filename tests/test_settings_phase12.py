"""Independent verification tests for Phase 1 & Phase 2 Settings Overhaul."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.agent import main as agent_mod
from backend.router import app as router_app_mod
from backend.router.chat_api import ChatStore, build_chat_router
from backend.router.config import NodeConfig, SplitchConfig
from backend.router.models_api import build_models_router
from backend.router.settings_api import apply_persisted_settings, build_settings_router
from backend.router.telemetry import ClusterState
from backend.router.web_search import execute_web_search
from backend.shared.models import (
    ChatSendRequest,
    ConversationCreate,
    EngineRuntimeConfig,
    LoadedModel,
    NodeStatus,
    NodeTelemetry,
    NodeUpdateRequest,
)
from backend.shared.secrets import SecretVault


@contextmanager
def _loopback_http_server(
    handler_fn,
):
    """Run a real loopback HTTP/1.1 server on 127.0.0.1:0 without external mocks."""

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length > 0 else b""
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            status, resp_body = handler_fn(method, self.path, hdrs, raw_body)
            payload = json.dumps(resp_body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

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


def _create_sparse_file(path: Path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.truncate(size_bytes)


# ── 1. Vault Secrets & Web Search Provider & Desktop Config ─────────────────


def test_vault_secrets_non_leakage_delete_and_hf_token_pull_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env_key in (
        "BRAVE_API_KEY",
        "TAVILY_API_KEY",
        "HF_TOKEN",
        "SPLITCH_AGENT_TOKEN",
        "SPLITCH_ROUTER_PRIVATE_KEY",
    ):
        monkeypatch.delenv(env_key, raising=False)

    data_dir = tmp_path / "data"
    cfg = SplitchConfig()
    cluster = ClusterState(cfg, data_dir=data_dir)
    app = FastAPI()
    app.include_router(build_settings_router(cluster, data_dir, api_key_getter=lambda: ""))
    app.include_router(build_models_router(cluster, data_dir))
    client = TestClient(app)

    # Initial state: all three secret flags False
    r_init = client.get("/api/settings")
    assert r_init.status_code == 200
    init_json = r_init.json()
    assert init_json["brave_api_key_set"] is False
    assert init_json["tavily_api_key_set"] is False
    assert init_json["hf_token_set"] is False

    secrets = {
        "brave_api_key": "BSA-secret-value-9911",
        "tavily_api_key": "tvly-secret-value-8822",
        "hf_token": "hf_secret_token_7733",
    }

    for name, raw_secret in secrets.items():
        # Include surrounding whitespace to verify stripping
        r_set = client.post(
            "/api/settings/secrets",
            json={"name": name, "value": f"  {raw_secret}  "},
        )
        assert r_set.status_code == 200
        assert r_set.json()[f"{name}_set"] is True
        # Secret plaintext MUST NOT appear anywhere in POST or GET JSON responses or encrypted vault file
        assert raw_secret not in r_set.text
        r_get = client.get("/api/settings")
        assert r_get.status_code == 200
        assert r_get.json()[f"{name}_set"] is True
        assert raw_secret not in r_get.text

    vault = SecretVault(data_dir)
    vault_raw = vault.vault_path.read_text(encoding="utf-8")
    for name, raw_secret in secrets.items():
        assert raw_secret not in vault_raw
        assert vault.get(name) == raw_secret

    # Unknown secret name rejected
    assert client.post("/api/settings/secrets", json={"name": "openai_api_key", "value": "x"}).status_code == 422
    assert client.delete("/api/settings/secrets/openai_api_key").status_code == 422

    # Verify HF token in SecretVault is forwarded via x-splitch-hf-token to agent /files/models/pull
    captured_pulls: list[dict[str, Any]] = []

    def _agent_handler(method: str, path: str, hdrs: dict[str, str], body: bytes) -> tuple[int, dict]:
        captured_pulls.append(
            {
                "method": method,
                "path": path,
                "headers": hdrs,
                "json": json.loads(body.decode("utf-8")) if body else {},
            }
        )
        return 200, {
            "id": "dl_1",
            "node_id": "agent-1",
            "repo_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
            "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
            "status": "downloading",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "percent": 0.0,
            "error": None,
        }

    with _loopback_http_server(_agent_handler) as agent_url:
        cluster.nodes["agent-1"] = NodeStatus(
            node_id="agent-1",
            display_name="Agent 1",
            role="gpu",
            agent_url=agent_url,
            engine_url="http://127.0.0.1:8081",
            online=True,
        )
        r_pull = client.post(
            "/api/models/pull",
            json={
                "node_id": "agent-1",
                "repo_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
                "format": "gguf",
            },
        )
        assert r_pull.status_code == 200
        assert len(captured_pulls) == 1
        assert captured_pulls[0]["path"] == "/files/models/pull"
        assert captured_pulls[0]["headers"].get("x-splitch-hf-token") == secrets["hf_token"]

    # Deleting via whitespace-only upsert flips flag False
    r_blank = client.post("/api/settings/secrets", json={"name": "hf_token", "value": "   "})
    assert r_blank.status_code == 200
    assert r_blank.json()["hf_token_set"] is False
    assert SecretVault(data_dir).get("hf_token") is None

    # Deleting via DELETE flips flags False
    for name in ("brave_api_key", "tavily_api_key"):
        r_del = client.delete(f"/api/settings/secrets/{name}")
        assert r_del.status_code == 200
        assert r_del.json()[f"{name}_set"] is False
        assert SecretVault(data_dir).get(name) is None

    r_final = client.get("/api/settings").json()
    assert r_final["brave_api_key_set"] is False
    assert r_final["tavily_api_key_set"] is False
    assert r_final["hf_token_set"] is False


@pytest.mark.asyncio
async def test_web_search_provider_off_brave_error_and_searxng_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SPLITCH_DATA_DIR", str(data_dir))
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SPLITCH_SEARXNG_URL", raising=False)
    monkeypatch.setenv("SPLITCH_WEB_SEARCH_PROVIDER", "auto")

    try:
        cfg = SplitchConfig()
        cluster = ClusterState(cfg, data_dir=data_dir)
        app = FastAPI()
        app.include_router(build_settings_router(cluster, data_dir, api_key_getter=lambda: ""))
        client = TestClient(app)

        # Invalid provider rejected
        assert client.patch("/api/settings", json={"web_search_provider": "bing"}).status_code == 422

        # Store Tavily secret + configure loopback SearXNG to prove Brave mode does NOT silently fall back
        sx_hits: list[str] = []

        def _sx_handler(method: str, path: str, hdrs: dict[str, str], body: bytes) -> tuple[int, dict]:
            sx_hits.append(path)
            return 200, {
                "results": [
                    {
                        "title": "Loopback SearXNG Result",
                        "url": "https://example.org/splitch",
                        "content": "Verified result from local SearXNG",
                    }
                ]
            }

        with _loopback_http_server(_sx_handler) as sx_url:
            client.post("/api/settings/secrets", json={"name": "tavily_api_key", "value": "tvly-key-123"})

            # 1) web_search_provider="off" disables execute_web_search() with explicit message
            r_off = client.patch(
                "/api/settings",
                json={"web_search_provider": "off", "searxng_url": sx_url},
            )
            assert r_off.status_code == 200
            assert r_off.json()["web_search_provider"] == "off"

            ok_off, msg_off = await execute_web_search("splitch local router")
            assert ok_off is False
            assert "disabled" in msg_off.lower()
            assert sx_hits == []

            # Verify persisted across restart via apply_persisted_settings
            cluster2 = ClusterState(SplitchConfig(), data_dir=data_dir)
            apply_persisted_settings(cluster2, data_dir)
            ok_off2, _ = await execute_web_search("splitch local router")
            assert ok_off2 is False

            # 2) web_search_provider="brave" with unset Brave key returns explicit config error
            #    and MUST NOT fall back to SearXNG, Tavily, or open sources
            r_brave = client.patch("/api/settings", json={"web_search_provider": "brave"})
            assert r_brave.status_code == 200
            assert r_brave.json()["web_search_provider"] == "brave"

            ok_brave, msg_brave = await execute_web_search("python 3.13 release")
            assert ok_brave is False
            assert "brave api key is not configured" in msg_brave.lower()
            assert sx_hits == []

            # 3) web_search_provider="tavily" with unset Tavily key returns explicit config error
            client.delete("/api/settings/secrets/tavily_api_key")
            client.patch("/api/settings", json={"web_search_provider": "tavily"})
            ok_tav, msg_tav = await execute_web_search("python 3.13 release")
            assert ok_tav is False
            assert "tavily api key is not configured" in msg_tav.lower()
            assert sx_hits == []

            # 4) web_search_provider="searxng" with empty URL returns explicit error
            client.patch("/api/settings", json={"web_search_provider": "searxng", "searxng_url": ""})
            ok_sx_empty, msg_sx_empty = await execute_web_search("python 3.13 release")
            assert ok_sx_empty is False
            assert "searxng url is empty" in msg_sx_empty.lower()

            # 5) web_search_provider="searxng" with loopback URL succeeds cleanly
            client.patch("/api/settings", json={"web_search_provider": "searxng", "searxng_url": sx_url})
            ok_sx, msg_sx = await execute_web_search("splitch")
            assert ok_sx is True
            assert "Loopback SearXNG Result" in msg_sx
            assert "https://example.org/splitch" in msg_sx
            assert len(sx_hits) == 1
    finally:
        monkeypatch.delenv("SPLITCH_WEB_SEARCH_PROVIDER", raising=False)
        monkeypatch.delenv("SPLITCH_SEARXNG_URL", raising=False)


def test_desktop_config_patch_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SPLITCH_NO_DESKTOP", "1")

    data_dir = tmp_path / "data"
    cluster = ClusterState(SplitchConfig(), data_dir=data_dir)
    app = FastAPI()
    app.include_router(build_settings_router(cluster, data_dir, api_key_getter=lambda: ""))
    client = TestClient(app)

    r0 = client.get("/api/desktop/config")
    assert r0.status_code == 200
    assert r0.json() == {
        "launch_mode": "app",
        "auto_tray": True,
        "autostart": False,
        "desktop_icon": True,
    }

    r1 = client.patch(
        "/api/desktop/config",
        json={
            "launch_mode": "browser",
            "auto_tray": False,
            "autostart": True,
            "desktop_icon": False,
        },
    )
    assert r1.status_code == 200
    assert r1.json() == {
        "launch_mode": "browser",
        "auto_tray": False,
        "autostart": True,
        "desktop_icon": False,
    }
    desktop_json = tmp_path / "desktop.json"
    assert desktop_json.is_file()
    persisted = json.loads(desktop_json.read_text(encoding="utf-8"))
    assert persisted["launch_mode"] == "browser"
    assert persisted["auto_tray"] is False
    assert persisted["autostart"] is True
    assert persisted["desktop_icon"] is False

    # Partial update preserves untouched fields
    r2 = client.patch("/api/desktop/config", json={"launch_mode": "firefoxpwa"})
    assert r2.status_code == 200
    assert r2.json() == {
        "launch_mode": "firefoxpwa",
        "auto_tray": False,
        "autostart": True,
        "desktop_icon": False,
    }
    assert client.get("/api/desktop/config").json()["launch_mode"] == "firefoxpwa"

    # Invalid launch_mode rejected
    assert client.patch("/api/desktop/config", json={"launch_mode": "kiosk"}).status_code == 422


# ── 2. Live Node Editing & Restart Persistence ──────────────────────────────


@pytest.mark.asyncio
async def test_patch_node_http_validation_404_and_restart_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_app_mod, "API_KEY", "")
    monkeypatch.setattr(router_app_mod, "API_KEY_HASH", "")
    monkeypatch.setattr(router_app_mod.cluster, "data_dir", tmp_path)

    old_nodes = dict(router_app_mod.cluster.nodes)
    old_overridden = set(router_app_mod.cluster._overridden_node_ids)
    old_poll_client = router_app_mod.cluster._poll_client

    try:
        router_app_mod.cluster._poll_client = None
        router_app_mod.cluster._overridden_node_ids.clear()
        router_app_mod.cluster.nodes.clear()
        router_app_mod.cluster.nodes["rig-1"] = NodeStatus(
            node_id="rig-1",
            display_name="Initial Rig",
            role="gpu",
            yield_on_gaming=True,
            yield_gpu_indices=[0],
            agent_url="http://127.0.0.1:8090",
            engine_url="http://127.0.0.1:8081",
            media_url="http://127.0.0.1:8095",
        )

        client = TestClient(router_app_mod.app)

        # Unknown node -> 404
        r_404 = client.patch("/api/nodes/nonexistent-node", json={"display_name": "Ghost"})
        assert r_404.status_code == 404

        # Invalid URLs -> 400
        assert client.patch("/api/nodes/rig-1", json={"agent_url": ""}).status_code == 400
        assert client.patch("/api/nodes/rig-1", json={"engine_url": "ftp://127.0.0.1:8081"}).status_code == 400
        assert (
            client.patch("/api/nodes/rig-1", json={"agent_url": "http://169.254.169.254:8090"}).status_code
            == 400
        )

        # Full update strips trailing slashes and updates all fields
        r_ok = client.patch(
            "/api/nodes/rig-1",
            json={
                "display_name": "  Studio Rig  ",
                "role": "unified",
                "yield_on_gaming": False,
                "yield_gpu_indices": [1, 2],
                "agent_url": "http://127.0.0.1:8190/",
                "engine_url": "http://127.0.0.1:8181/",
                "media_url": "http://127.0.0.1:8195/",
            },
        )
        assert r_ok.status_code == 200
        body = r_ok.json()
        assert body["display_name"] == "Studio Rig"
        assert body["role"] == "unified"
        assert body["yield_on_gaming"] is False
        assert body["yield_gpu_indices"] == [1, 2]
        assert body["agent_url"] == "http://127.0.0.1:8190"
        assert body["engine_url"] == "http://127.0.0.1:8181"
        assert body["media_url"] == "http://127.0.0.1:8195"

        # Whitespace-only display_name does not wipe existing name; empty media_url clears media_url
        r_clear = client.patch(
            "/api/nodes/rig-1",
            json={"display_name": "   ", "media_url": ""},
        )
        assert r_clear.status_code == 200
        assert r_clear.json()["display_name"] == "Studio Rig"
        assert r_clear.json()["media_url"] == ""
    finally:
        router_app_mod.cluster.nodes.clear()
        router_app_mod.cluster.nodes.update(old_nodes)
        router_app_mod.cluster._overridden_node_ids.clear()
        router_app_mod.cluster._overridden_node_ids.update(old_overridden)
        router_app_mod.cluster._poll_client = old_poll_client

    # Static node override persistence across simulated restart (_load_dynamic_nodes)
    restart_dir = tmp_path / "restart_data"
    static_cfg = SplitchConfig(
        nodes=[
            NodeConfig(
                id="local",
                display_name="Original Local",
                role="gpu",
                yield_on_gaming=True,
                yield_gpu_indices=[0],
                agent_url="http://127.0.0.1:8090",
                engine_url="http://127.0.0.1:8081",
            )
        ]
    )
    c1 = ClusterState(static_cfg, data_dir=restart_dir)
    updated = await c1.update_node(
        "local",
        NodeUpdateRequest(
            display_name="Overridden Local",
            role="cpu",
            yield_on_gaming=False,
            yield_gpu_indices=[1],
            agent_url="http://127.0.0.1:9090/",
            engine_url="http://127.0.0.1:9081/",
            media_url="http://127.0.0.1:9099/",
        ),
    )
    assert updated is not None
    assert (restart_dir / "nodes.json").is_file()

    # Simulated router restart with identical static TOML config
    c2 = ClusterState(static_cfg, data_dir=restart_dir)
    restored = c2.nodes["local"]
    assert restored.display_name == "Overridden Local"
    assert restored.role == "cpu"
    assert restored.yield_on_gaming is False
    assert restored.yield_gpu_indices == [1]
    assert restored.agent_url == "http://127.0.0.1:9090"
    assert restored.engine_url == "http://127.0.0.1:9081"
    assert restored.media_url == "http://127.0.0.1:9099"
    assert "local" in c2._overridden_node_ids

    # Persisting dynamic nodes again keeps static overridden node in nodes.json
    await c2._persist_dynamic_nodes()
    c3 = ClusterState(static_cfg, data_dir=restart_dir)
    assert c3.nodes["local"].display_name == "Overridden Local"


# ── 3. Phase 2 Chat Sampling & Reasoning ────────────────────────────────────


def test_chat_send_request_validation_bounds() -> None:
    # Inclusive lower/upper boundary values succeed
    ok_min = ChatSendRequest(
        content="hi",
        temperature=0.0,
        top_p=0.0,
        top_k=0,
        min_p=0.0,
        repeat_penalty=0.5,
        max_tokens=1,
        seed=-1,
        stop=["</s>"],
        reasoning_effort="off",
        enable_thinking=False,
    )
    assert ok_min.temperature == 0.0 and ok_min.max_tokens == 1

    ok_max = ChatSendRequest(
        content="hi",
        temperature=2.0,
        top_p=1.0,
        top_k=500,
        min_p=1.0,
        repeat_penalty=2.5,
        max_tokens=262144,
        seed=99999,
        stop=[],
        reasoning_effort="high",
        enable_thinking=True,
    )
    assert ok_max.top_k == 500 and ok_max.max_tokens == 262144

    bad_cases: list[dict[str, Any]] = [
        {"temperature": -0.01},
        {"temperature": 2.01},
        {"top_p": -0.01},
        {"top_p": 1.01},
        {"top_k": -1},
        {"top_k": 501},
        {"min_p": -0.01},
        {"min_p": 1.01},
        {"repeat_penalty": 0.49},
        {"repeat_penalty": 2.51},
        {"max_tokens": 0},
        {"max_tokens": 262145},
        {"reasoning_effort": "extreme"},
    ]
    for bad in bad_cases:
        with pytest.raises(ValidationError):
            ChatSendRequest(content="test", **bad)


@pytest.mark.asyncio
async def test_chat_send_forwards_sampling_and_reasoning_to_upstream_asgi(
    tmp_path: Path,
) -> None:
    captured_requests: list[dict[str, Any]] = []

    upstream_app = FastAPI()

    @upstream_app.post("/v1/chat/completions")
    async def _upstream_chat(request: Request):
        payload = await request.json()
        captured_requests.append(payload)

        async def _sse_chunks():
            chunk1 = {
                "choices": [{"delta": {"reasoning_content": "step 1", "content": ""}}]
            }
            chunk2 = {
                "choices": [{"delta": {"content": "hello world"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
            yield f"data: {json.dumps(chunk1)}\n\n".encode()
            yield f"data: {json.dumps(chunk2)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(_sse_chunks(), media_type="text/event-stream")

    cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
    cluster.nodes["gpu-1"] = NodeStatus(
        node_id="gpu-1",
        display_name="GPU 1",
        role="gpu",
        agent_url="http://127.0.0.1:8090",
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
            context_length=32768,
            models=[LoadedModel(id="qwen-32b", status="loaded")],
        ),
    )

    proxy_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream_app))
    cluster._proxy_client = proxy_client
    try:
        router_app = FastAPI()
        router_app.include_router(build_chat_router(cluster, tmp_path, tmp_path / "skills"))
        store = ChatStore(tmp_path / "chats.db")
        conv = store.create_conversation(
            ConversationCreate(
                title="Sampling Test",
                model="qwen-32b",
                tools_enabled=False,
                web_search_enabled=False,
            )
        )

        client = TestClient(router_app)

        # Case 1: falsy zero/false sampling params + reasoning_effort="off" -> "none" + enable_thinking=False
        r1 = client.post(
            f"/api/chat/conversations/{conv.id}/send",
            json={
                "content": "Say hi",
                "temperature": 0.0,
                "top_p": 0.0,
                "top_k": 0,
                "min_p": 0.0,
                "repeat_penalty": 1.15,
                "max_tokens": 256,
                "seed": 0,
                "stop": ["<|end|>", "\nUser:"],
                "reasoning_effort": "off",
                "enable_thinking": False,
            },
        )
        assert r1.status_code == 200
        assert "event: done" in r1.text
        assert "hello world" in r1.text
        assert len(captured_requests) == 1

        req1 = captured_requests[0]
        assert req1["model"] == "qwen-32b"
        assert req1["temperature"] == 0.0
        assert req1["top_p"] == 0.0
        assert req1["top_k"] == 0
        assert req1["min_p"] == 0.0
        assert req1["repeat_penalty"] == 1.15
        assert req1["max_tokens"] == 256
        assert req1["seed"] == 0
        assert req1["stop"] == ["<|end|>", "\nUser:"]
        assert req1["reasoning_effort"] == "none"
        assert req1["chat_template_kwargs"] == {"enable_thinking": False}

        # Case 2: omitted optional params + empty stop=[] + reasoning_effort="high" + enable_thinking=True
        r2 = client.post(
            f"/api/chat/conversations/{conv.id}/send",
            json={
                "content": "Think hard",
                "stop": [],
                "reasoning_effort": "high",
                "enable_thinking": True,
            },
        )
        assert r2.status_code == 200
        assert len(captured_requests) == 2

        req2 = captured_requests[1]
        for omitted_key in (
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "repeat_penalty",
            "max_tokens",
            "seed",
            "stop",
        ):
            assert omitted_key not in req2
        assert req2["reasoning_effort"] == "high"
        assert req2["chat_template_kwargs"] == {"enable_thinking": True}

        # Case 3: HTTP 422 on invalid sampling bounds
        r_bad = client.post(
            f"/api/chat/conversations/{conv.id}/send",
            json={"content": "Bad", "temperature": 2.5, "max_tokens": 0},
        )
        assert r_bad.status_code == 422
    finally:
        await proxy_client.aclose()
        cluster._proxy_client = None


# ── 4. Phase 2 Agent Engine Config & Safe Cache Cleanup ─────────────────────


def test_apply_engine_runtime_config_systemd_override_and_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agent_mod.sys, "platform", "linux")
    monkeypatch.setattr(agent_mod, "STORAGE_PATH", str(tmp_path / "models"))
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_mod, "ROUTER_PUBLIC_KEY", None)

    # Without systemd unit file: supported=False
    initial = agent_mod._read_engine_runtime_config()
    assert initial.supported is False
    assert initial.unit_name == ""
    assert initial.override_path == ""

    systemd_user_dir = tmp_path / ".config" / "systemd" / "user"
    systemd_user_dir.mkdir(parents=True)
    unit_file = systemd_user_dir / "splitch-llama.service"
    unit_file.write_text(
        "[Unit]\n"
        "Description=Splitch llama-server\n\n"
        "[Service]\n"
        "ExecStart=/usr/local/bin/llama-server \\\n"
        "  --host 0.0.0.0 --port 8081 --models-dir /var/lib/splitch/models \\\n"
        "  -c 4096 --cache-type-k=f16 -ctv q4_0 --flash-attn off \\\n"
        "  --models-max 2 --sleep-idle-seconds 120 -np 4 --metrics\n",
        encoding="utf-8",
    )

    applied = agent_mod._apply_engine_runtime_config(
        EngineRuntimeConfig(
            ctx_size=65536,
            cache_type_k="q8_0",
            cache_type_v="q8_0",
            flash_attn=True,
            models_max=3,
            sleep_idle_seconds=900,
            parallel=2,
            apply_restart=False,
        )
    )
    override_file = systemd_user_dir / "splitch-llama.service.d" / "99-splitch-runtime.conf"
    assert override_file.is_file()
    assert applied.supported is True
    assert applied.unit_name == "splitch-llama.service"
    assert applied.override_path == str(override_file)
    assert applied.apply_restart is False

    override_txt = override_file.read_text(encoding="utf-8")
    assert override_txt.startswith("[Service]\nExecStart=\nExecStart=")
    exec_line = next(
        ln
        for ln in override_txt.splitlines()
        if ln.startswith("ExecStart=") and ln != "ExecStart="
    )
    tokens = shlex.split(exec_line.split("=", 1)[1])

    # Base binary + preserved flags kept intact
    assert tokens[0] == "/usr/local/bin/llama-server"
    assert "--host" in tokens and tokens[tokens.index("--host") + 1] == "0.0.0.0"
    assert "--port" in tokens and tokens[tokens.index("--port") + 1] == "8081"
    assert "--models-dir" in tokens and tokens[tokens.index("--models-dir") + 1] == "/var/lib/splitch/models"
    assert "--metrics" in tokens

    # Old managed flags stripped completely (zero stale duplicates)
    for stale in ("-c", "4096", "--cache-type-k=f16", "-ctv", "q4_0", "off", "120", "-np", "4"):
        assert stale not in tokens

    # New managed flags applied once
    expected_pairs = {
        "--ctx-size": "65536",
        "--cache-type-k": "q8_0",
        "--cache-type-v": "q8_0",
        "--flash-attn": "on",
        "--models-max": "3",
        "--sleep-idle-seconds": "900",
        "--parallel": "2",
    }
    for flag, val in expected_pairs.items():
        assert tokens.count(flag) == 1
        assert tokens[tokens.index(flag) + 1] == val

    # Agent HTTP endpoints GET/POST + apply_restart=True systemctl invocation
    run_calls: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: run_calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0),
    )

    tc = TestClient(agent_mod.app)
    r_post = tc.post(
        "/engine/runtime-config",
        json={
            "ctx_size": 16384,
            "cache_type_k": "f16",
            "cache_type_v": "q4_0",
            "flash_attn": False,
            "models_max": 1,
            "sleep_idle_seconds": 0,
            "parallel": 4,
            "apply_restart": True,
        },
    )
    assert r_post.status_code == 200
    assert r_post.json()["apply_restart"] is False
    assert run_calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "restart", "splitch-llama.service"],
    ]

    r_get = tc.get("/engine/runtime-config")
    assert r_get.status_code == 200
    g = r_get.json()
    assert g["ctx_size"] == 16384
    assert g["cache_type_k"] == "f16"
    assert g["cache_type_v"] == "q4_0"
    assert g["flash_attn"] is False
    assert g["models_max"] == 1
    assert g["sleep_idle_seconds"] == 0
    assert g["parallel"] == 4
    assert g["supported"] is True

    # Re-check updated override has no duplicates
    exec_line2 = next(
        ln
        for ln in override_file.read_text(encoding="utf-8").splitlines()
        if ln.startswith("ExecStart=") and ln != "ExecStart="
    )
    tokens2 = shlex.split(exec_line2.split("=", 1)[1])
    assert tokens2.count("--ctx-size") == 1 and tokens2[tokens2.index("--ctx-size") + 1] == "16384"
    assert tokens2[tokens2.index("--flash-attn") + 1] == "off"
    assert tokens2[tokens2.index("--parallel") + 1] == "4"


def test_storage_usage_and_safe_cache_cleanup_preserves_completed_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models_dir = tmp_path / "models"
    sib_cache = tmp_path / "cache"
    llama_cache = tmp_path / "llama_cache"
    hf_hub = tmp_path / ".cache" / "huggingface" / "hub"

    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("LLAMA_CACHE", str(llama_cache))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agent_mod, "STORAGE_PATH", str(models_dir))
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_mod, "ROUTER_PUBLIC_KEY", None)

    mib = 1024 * 1024
    # Completed models that MUST NEVER be deleted
    gguf_file = models_dir / "llama-3-8b-q4_k_m.gguf"
    strata_file = models_dir / "strata" / "qwen-7b.strata"
    _create_sparse_file(gguf_file, 32 * mib)
    _create_sparse_file(strata_file, 32 * mib)

    # Incomplete downloads (.part and .*.part files + .part directory) that MUST be deleted
    part_file = models_dir / "model-in-progress.gguf.part"
    hidden_part_file = models_dir / ".hidden-download.strata.part"
    mlx_part_dir = models_dir / "mlx" / ".org--repo.part"
    _create_sparse_file(part_file, 16 * mib)
    _create_sparse_file(hidden_part_file, 16 * mib)
    _create_sparse_file(mlx_part_dir / "chunk-000.bin", 16 * mib)

    # Cache entries across sibling cache, LLAMA_CACHE, and HuggingFace hub cache
    sib_cache_file = sib_cache / "prompt-cache.bin"
    llama_cache_file = llama_cache / "manifest.json"
    hf_blob_file = hf_hub / "models--org--repo" / "blobs" / "abc123"
    _create_sparse_file(sib_cache_file, 16 * mib)
    _create_sparse_file(llama_cache_file, 16 * mib)
    _create_sparse_file(hf_blob_file, 16 * mib)

    usage = agent_mod._scan_storage_usage()
    assert usage.storage_path == str(models_dir)
    assert usage.free_gb > 0.0
    assert usage.models_gb == round((64 * mib) / (1024**3), 2)
    assert usage.partial_gb == round((48 * mib) / (1024**3), 2)
    assert usage.cache_gb == round((48 * mib) / (1024**3), 2)
    assert usage.partial_count == 3
    assert usage.cache_entries == 3

    tc = TestClient(agent_mod.app)
    r_usage = tc.get("/files/storage/usage")
    assert r_usage.status_code == 200
    assert r_usage.json() == usage.model_dump()

    r_clean = tc.post("/files/storage/clean-cache")
    assert r_clean.status_code == 200
    clean_data = r_clean.json()
    assert clean_data["removed_files"] == 6
    assert clean_data["freed_gb"] == round((96 * mib) / (1024**3), 2)

    # All partial downloads & cache entries removed
    assert not part_file.exists()
    assert not hidden_part_file.exists()
    assert not mlx_part_dir.exists()
    assert not sib_cache_file.exists()
    assert not llama_cache_file.exists()
    assert not hf_blob_file.exists()

    # Completed .gguf and .strata files MUST remain untouched
    assert gguf_file.is_file() and gguf_file.stat().st_size == 32 * mib
    assert strata_file.is_file() and strata_file.stat().st_size == 32 * mib

    # Post-clean scan shows 0 partials and 0 cache while models remain intact
    after = agent_mod._scan_storage_usage()
    assert after.partial_count == 0
    assert after.partial_gb == 0.0
    assert after.cache_entries == 0
    assert after.cache_gb == 0.0
    assert after.models_gb == round((64 * mib) / (1024**3), 2)


def test_router_proxy_endpoints_engine_config_and_storage_offline_and_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_app_mod, "API_KEY", "")
    monkeypatch.setattr(router_app_mod, "API_KEY_HASH", "")

    old_nodes = dict(router_app_mod.cluster.nodes)

    def _agent_handler(method: str, path: str, hdrs: dict[str, str], body: bytes) -> tuple[int, dict]:
        if method == "GET" and path == "/engine/runtime-config":
            return 200, EngineRuntimeConfig(ctx_size=32768, unit_name="splitch-llama.service").model_dump()
        if method == "POST" and path == "/engine/runtime-config":
            data = json.loads(body.decode("utf-8"))
            data["apply_restart"] = False
            return 200, data
        if method == "GET" and path == "/files/storage/usage":
            return 200, {"storage_path": str(tmp_path), "free_gb": 50.0, "models_gb": 12.0}
        if method == "POST" and path == "/files/storage/clean-cache":
            return 200, {"removed_files": 2, "freed_gb": 0.5}
        return 404, {"detail": "not found"}

    try:
        router_app_mod.cluster.nodes.clear()
        router_app_mod.cluster.nodes["offline-node"] = NodeStatus(
            node_id="offline-node",
            display_name="Offline",
            role="gpu",
            agent_url="http://127.0.0.1:8090",
            engine_url="http://127.0.0.1:8081",
            online=False,
        )
        client = TestClient(router_app_mod.app)
        assert client.get("/api/nodes/offline-node/engine-config").status_code == 503
        assert client.get("/api/nodes/offline-node/storage/usage").status_code == 503
        assert client.post("/api/nodes/offline-node/storage/clean-cache").status_code == 503

        with _loopback_http_server(_agent_handler) as agent_url:
            router_app_mod.cluster.nodes["live-node"] = NodeStatus(
                node_id="live-node",
                display_name="Live",
                role="gpu",
                agent_url=agent_url,
                engine_url="http://127.0.0.1:8081",
                online=True,
            )
            r_ec = client.get("/api/nodes/live-node/engine-config")
            assert r_ec.status_code == 200 and r_ec.json()["ctx_size"] == 32768

            r_ec_set = client.post(
                "/api/nodes/live-node/engine-config",
                json={"ctx_size": 16384, "parallel": 2},
            )
            assert r_ec_set.status_code == 200 and r_ec_set.json()["ctx_size"] == 16384

            r_su = client.get("/api/nodes/live-node/storage/usage")
            assert r_su.status_code == 200 and r_su.json()["models_gb"] == 12.0

            r_sc = client.post("/api/nodes/live-node/storage/clean-cache")
            assert r_sc.status_code == 200 and r_sc.json()["removed_files"] == 2
    finally:
        router_app_mod.cluster.nodes.clear()
        router_app_mod.cluster.nodes.update(old_nodes)
