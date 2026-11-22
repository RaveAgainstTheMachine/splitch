"""Security hardening, TLS certificate, reverse-proxy, rate-limiting, and confinement tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from backend.agent import main as agent_mod
from backend.router import app as router_mod
from backend.shared.tls import (
    generate_self_signed_cert,
    inspect_certificate,
    issue_letsencrypt_cert,
    resolve_tls_files,
)


def test_self_signed_tls_generation_san_and_permissions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path))
    cert, key = generate_self_signed_cert(
        domains=["splitch.internal"],
        ips=["192.168.1.99"],
        days=30,
    )
    assert cert.is_file() and key.is_file()
    if sys.platform != "win32":
        assert (key.stat().st_mode & 0o777) == 0o600

    info = inspect_certificate(cert)
    assert info["exists"] is True
    assert "splitch.internal" in info.get("san", "")
    assert "192.168.1.99" in info.get("san", "")
    assert "127.0.0.1" in info.get("san", "")

    resolved_c, resolved_k = resolve_tls_files(certfile=str(cert), keyfile=str(key))
    assert resolved_c == str(cert) and resolved_k == str(key)

    # Dry-run Let's Encrypt validates domain & builds certbot command without touching system
    le = issue_letsencrypt_cert(
        "ai.example.com",
        email="admin@example.com",
        method="standalone",
        staging=True,
        dry_run=True,
    )
    assert le["ok"] is True and le["dry_run"] is True
    assert "--staging" in le["command"] and "-d ai.example.com" in le["command"]

    # Reject invalid domain injection
    try:
        issue_letsencrypt_cert("bad domain; rm -rf /", email="a@b.com", dry_run=True)
        raise AssertionError("Expected ValueError for invalid domain")
    except ValueError:
        pass


def test_login_rate_limit_https_cookie_hsts_csp_and_node_url_validation(monkeypatch) -> None:
    monkeypatch.setattr(router_mod, "API_KEY", "super-secret-test-key")
    monkeypatch.setattr(router_mod.config.security, "login_max_attempts", 3)
    monkeypatch.setattr(router_mod.config.security, "login_window_s", 60.0)
    router_mod._LOGIN_FAILURES.clear()

    tc = TestClient(router_mod.app)

    # Unauthenticated API call blocked + strict CSP header emitted
    r_unauth = tc.get("/api/status")
    assert r_unauth.status_code == 401
    assert "script-src 'self'" in r_unauth.headers.get("content-security-policy", "")
    assert "'unsafe-inline'" not in (
        r_unauth.headers.get("content-security-policy", "").split("script-src")[1].split(";")[0]
    )

    # 3 failed logins trigger 429 rate-limit on 4th attempt
    for _ in range(3):
        r_bad = tc.post(
            "/api/auth/login",
            json={"api_key": "wrong"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert r_bad.status_code == 401
    r_429 = tc.post(
        "/api/auth/login",
        json={"api_key": "wrong"},
        headers={"X-Forwarded-For": "203.0.113.10"},
    )
    assert r_429.status_code == 429
    assert int(r_429.headers.get("retry-after", "0")) >= 1

    # Valid login behind HTTPS reverse proxy sets Secure HttpOnly cookie + HSTS
    r_ok = tc.post(
        "/api/auth/login",
        json={"api_key": "super-secret-test-key"},
        headers={"X-Forwarded-For": "203.0.113.11", "X-Forwarded-Proto": "https"},
    )
    assert r_ok.status_code == 200
    set_cookie = r_ok.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie or "samesite=strict" in set_cookie.lower()
    assert "max-age=31536000" in r_ok.headers.get("strict-transport-security", "")

    # POST /api/nodes rejects non-HTTP(S) URL schemes and SSRF link-local/metadata/credential targets
    for bad_agent_url in (
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://0.0.0.0:8090",
        "http://224.0.0.1:8090",
        "http://admin:secret@192.168.1.50:8090",
    ):
        r_bad_url = tc.post(
            "/api/nodes",
            headers={"Authorization": "Bearer super-secret-test-key"},
            json={
                "id": "evil-node",
                "display_name": "Evil",
                "role": "cpu",
                "agent_url": bad_agent_url,
                "engine_url": "http://127.0.0.1:8081",
            },
        )
        assert r_bad_url.status_code == 400, f"Expected 400 for {bad_agent_url}"


def test_agent_mutating_tool_execution_requires_approval_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_mod, "ENABLE_TOOLS", True)

    tc = TestClient(agent_mod.app)

    # Read-only list_dir works when tools are enabled
    r_list = tc.post(
        "/tools/execute",
        json={"tool": "list_dir", "arguments": {"path": "."}},
    )
    assert r_list.status_code == 200

    # Direct unapproved mutating calls (run_command, write_file, edit_file) are blocked with 403
    for mut_tool, mut_args in (
        ("run_command", {"command": "echo pwned"}),
        ("write_file", {"path": "x.txt", "content": "hi"}),
        ("edit_file", {"path": "x.txt", "target": "a", "replacement": "b"}),
    ):
        r_blocked = tc.post(
            "/tools/execute",
            json={"tool": mut_tool, "arguments": mut_args},
        )
        assert r_blocked.status_code == 403, f"Expected 403 for unapproved {mut_tool}"

    # Router-approved mutating call succeeds
    r_approved = tc.post(
        "/tools/execute",
        headers={"X-Splitch-Router-Approved": "1"},
        json={"tool": "write_file", "arguments": {"path": "approved.txt", "content": "safe\n"}},
    )
    assert r_approved.status_code == 200 and r_approved.json()["ok"] is True
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "safe\n"


def test_model_aware_vram_guard_and_mid_connection_failover(monkeypatch) -> None:
    import httpx
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from backend.router.config import GuardConfig
    from backend.router.policy import check_node_eligibility, estimate_model_vram_mb
    from backend.shared.models import (
        AliasConfig,
        ControlState,
        LoadedModel,
        NodeStatus,
        NodeTelemetry,
    )

    # 1. Model-aware VRAM estimation blocks an unloaded 70B Q4 model (~43 GB) on a 12 GB free GPU
    est_70b = estimate_model_vram_mb("Llama-3.3-70B-Instruct-Q4_K_M.gguf")
    assert est_70b is not None and est_70b > 35_000
    est_8b = estimate_model_vram_mb("Qwen3-8B-Q4_K_M.gguf")
    assert est_8b is not None and 4_000 < est_8b < 7_500

    gpu_node = NodeStatus(
        node_id="gpu-rig",
        display_name="GPU Rig",
        role="gpu",
        agent_url="http://gpu-rig:8090",
        engine_url="http://gpu-rig:8081",
        online=True,
        telemetry=NodeTelemetry(
            node_id="gpu-rig",
            role="gpu",
            platform="linux",
            timestamp=1.0,
            vram_total_mb=24576.0,
            vram_used_mb=12288.0,
            vram_free_mb=12288.0,  # 12 GB free (> 4 GB min_vram_free_mb, but < 43 GB needed for 70B!)
            storage_mounted=True,
            engine="llama-server",
            engine_healthy=True,
            models=[LoadedModel(id="Llama-3.3-70B-Instruct-Q4_K_M.gguf", status="unloaded")],
        ),
    )
    block = check_node_eligibility(
        gpu_node,
        "Llama-3.3-70B-Instruct-Q4_K_M.gguf",
        ControlState(),
        GuardConfig(min_vram_free_mb=4096),
    )
    assert block == "gpu-rig_vram_low"

    # 8B unloaded model fits in 12 GB free VRAM
    block_8b = check_node_eligibility(
        gpu_node,
        "Qwen3-8B-Q4_K_M.gguf",
        ControlState(),
        GuardConfig(min_vram_free_mb=4096),
    )
    assert block_8b is None

    # 2. Pre-first-byte connection failover: primary node returns 503 -> router transparently fails over to fallback node
    upstream_app = FastAPI()

    @upstream_app.post("/primary/v1/chat/completions")
    async def primary_down():
        return JSONResponse({"error": "engine crashed"}, status_code=503)

    @upstream_app.post("/standby/v1/chat/completions")
    async def standby_ok():
        return JSONResponse(
            {
                "id": "chatcmpl-fb",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Served by standby"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
        )

    monkeypatch.setattr(router_mod, "API_KEY", "")
    old_nodes = dict(router_mod.cluster.nodes)
    old_aliases = dict(router_mod.cluster.aliases)
    old_client = router_mod.cluster._proxy_client
    router_mod.cluster._proxy_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream_app),
        base_url="http://cluster.mock",
    )
    try:
        router_mod.cluster.nodes.clear()
        router_mod.cluster.nodes["gpu-rig"] = NodeStatus(
            node_id="gpu-rig",
            display_name="GPU Rig",
            role="gpu",
            agent_url="http://cluster.mock/primary",
            engine_url="http://cluster.mock/primary",
            online=True,
            telemetry=NodeTelemetry(
                node_id="gpu-rig",
                role="gpu",
                platform="linux",
                timestamp=1.0,
                vram_total_mb=24576.0,
                vram_free_mb=20000.0,
                storage_mounted=True,
                engine="llama-server",
                engine_healthy=True,
                models=[LoadedModel(id="qwen3-30b", status="loaded")],
            ),
        )
        router_mod.cluster.nodes["standby-mac"] = NodeStatus(
            node_id="standby-mac",
            display_name="Standby Mac",
            role="unified",
            agent_url="http://cluster.mock/standby",
            engine_url="http://cluster.mock/standby",
            online=True,
            telemetry=NodeTelemetry(
                node_id="standby-mac",
                role="unified",
                platform="darwin",
                timestamp=1.0,
                vram_total_mb=36864.0,
                vram_free_mb=28000.0,
                storage_mounted=True,
                engine="llama-server",
                engine_healthy=True,
                models=[LoadedModel(id="qwen3-8b", status="loaded")],
            ),
        )
        router_mod.cluster.aliases["heavy"] = AliasConfig(
            alias="heavy",
            task_kind="code",
            primary_node="gpu-rig",
            primary_model="qwen3-30b",
            fallback_node="standby-mac",
            fallback_model="qwen3-8b",
        )
        tc = TestClient(router_mod.app)
        resp = tc.post(
            "/v1/chat/completions",
            json={"model": "heavy", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert resp.status_code == 200
        assert resp.headers.get("x-splitch-node") == "standby-mac"
        assert resp.headers.get("x-splitch-fallback") == "gpu-rig_upstream_error"
        assert resp.json()["choices"][0]["message"]["content"] == "Served by standby"
    finally:
        router_mod.cluster._proxy_client = old_client
        router_mod.cluster.nodes.clear()
        router_mod.cluster.nodes.update(old_nodes)
        router_mod.cluster.aliases.clear()
        router_mod.cluster.aliases.update(old_aliases)


def test_agent_sync_skills_home_confinement_and_headers() -> None:
    tc = TestClient(agent_mod.app)
    r = tc.get("/health")
    assert r.status_code == 200
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"

    if os.name != "nt":
        r_escape = tc.post(
            "/sync/skills",
            json={"skills": [], "target_dirs": ["/etc/splitch-escape"]},
        )
        assert r_escape.status_code == 403
