"""Security hardening, TLS certificate, reverse-proxy, rate-limiting, and confinement tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
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
    monkeypatch.setattr(router_mod.config.security, "trusted_proxies", "127.0.0.1,::1,testclient")
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

    # Loopback X-Splitch-Router-Approved header alone MUST be blocked (403) when no signing key is configured
    r_unauth_loopback = tc.post(
        "/tools/execute",
        headers={"X-Splitch-Router-Approved": "1"},
        json={"tool": "write_file", "arguments": {"path": "blocked.txt", "content": "nope\n"}},
    )
    assert r_unauth_loopback.status_code == 403
    assert not (tmp_path / "blocked.txt").exists()

    # Only explicit ALLOW_UNAUTH_TOOLS=True + loopback + router-approval header permits unsigned local dev execution
    monkeypatch.setattr(agent_mod, "ALLOW_UNAUTH_TOOLS", True)
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


def test_semver_single_source_of_truth() -> None:
    import json
    import tomllib

    from backend.version import __version__, is_valid_semver

    repo = Path(__file__).resolve().parents[1]
    assert is_valid_semver(__version__)
    assert not is_valid_semver("v0.4.0")
    assert not is_valid_semver("0.4")

    py_ver = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    pkg_ver = json.loads((repo / "dashboard" / "package.json").read_text(encoding="utf-8"))[
        "version"
    ]
    assert py_ver == __version__
    assert pkg_ver == __version__
    assert router_mod.VERSION == __version__
    assert agent_mod.VERSION == __version__


def test_spoofed_xff_cannot_bypass_login_rate_limit(monkeypatch) -> None:
    monkeypatch.setattr(router_mod, "API_KEY", "super-secret-test-key")
    monkeypatch.setattr(router_mod.config.security, "login_max_attempts", 3)
    monkeypatch.setattr(router_mod.config.security, "login_window_s", 60.0)
    monkeypatch.setattr(router_mod.config.security, "trusted_proxies", "127.0.0.1,::1")
    router_mod._LOGIN_FAILURES.clear()

    tc = TestClient(router_mod.app)
    for idx in range(3):
        r = tc.post(
            "/api/auth/login",
            json={"api_key": "wrong"},
            headers={
                "X-Forwarded-For": f"198.51.100.{idx + 1}",
                "X-Forwarded-Proto": "https",
            },
        )
        assert r.status_code == 401
        assert "strict-transport-security" not in r.headers

    # Rotating X-Forwarded-For from an untrusted peer MUST still hit 429 on 4th attempt
    r4 = tc.post(
        "/api/auth/login",
        json={"api_key": "wrong"},
        headers={"X-Forwarded-For": "198.51.100.99", "X-Forwarded-Proto": "https"},
    )
    assert r4.status_code == 429


def test_csrf_cross_origin_cookie_mutation_blocked(monkeypatch) -> None:
    monkeypatch.setattr(router_mod, "API_KEY", "csrf-secret-key")
    router_mod._LOGIN_FAILURES.clear()

    tc = TestClient(router_mod.app)
    r_login = tc.post("/api/auth/login", json={"api_key": "csrf-secret-key"})
    assert r_login.status_code == 200

    # Cookie-authenticated cross-origin POST must be blocked with 403
    r_evil = tc.post(
        "/api/control",
        json={"gaming_mode": True},
        headers={"Origin": "https://evil.example"},
    )
    assert r_evil.status_code == 403

    r_fetch_site = tc.post(
        "/api/control",
        json={"gaming_mode": True},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r_fetch_site.status_code == 403

    # Same-origin cookie POST succeeds
    r_same = tc.post(
        "/api/control",
        json={"gaming_mode": False},
        headers={"Origin": "http://testserver"},
    )
    assert r_same.status_code == 200

    # Explicit Bearer header auth is not cookie-only CSRF and succeeds
    r_bearer = tc.post(
        "/api/control",
        json={"gaming_mode": False},
        headers={
            "Authorization": "Bearer csrf-secret-key",
            "Origin": "https://evil.example",
        },
    )
    assert r_bearer.status_code == 200


def test_dns_ssrf_node_registration_blocked(monkeypatch) -> None:
    import socket

    monkeypatch.setattr(router_mod, "API_KEY", "ssrf-test-key")

    def fake_getaddrinfo(host: str, *_args, **_kwargs):
        if host == "evil-meta.internal":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
        if host == "evil-v6.internal":
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:169.254.169.254", 0, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.50", 0))]

    monkeypatch.setattr(router_mod.socket, "getaddrinfo", fake_getaddrinfo)
    tc = TestClient(router_mod.app)
    for host in ("evil-meta.internal", "evil-v6.internal"):
        r = tc.post(
            "/api/nodes",
            headers={"Authorization": "Bearer ssrf-test-key"},
            json={
                "id": "dns-evil",
                "display_name": "DNS Evil",
                "role": "cpu",
                "agent_url": f"http://{host}:8090",
                "engine_url": "http://127.0.0.1:8081",
            },
        )
        assert r.status_code == 400, f"Expected 400 for DNS SSRF host {host}"


def test_hmac_tool_approval_and_non_loopback_rejection(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import time

    import httpx

    from backend.shared.security import sign_tool_approval

    monkeypatch.setattr(agent_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(agent_mod, "ENABLE_TOOLS", True)
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "shared-node-secret")
    monkeypatch.setattr(agent_mod, "ALLOW_UNAUTH_TOOLS", False)

    tc = TestClient(agent_mod.app)
    payload = {
        "tool": "write_file",
        "arguments": {"path": "signed.txt", "content": "verified\n"},
        "workspace_subpath": "",
    }

    # Missing bearer token -> 401
    assert tc.post("/tools/execute", json=payload).status_code == 401

    # Valid bearer token + legacy forged header (no HMAC) -> 403
    r_forged = tc.post(
        "/tools/execute",
        headers={
            "Authorization": "Bearer shared-node-secret",
            "X-Splitch-Router-Approved": "1",
        },
        json=payload,
    )
    assert r_forged.status_code == 403

    # Valid HMAC signed for different arguments (tampering) -> 403
    ts_bad, sig_bad = sign_tool_approval(
        "shared-node-secret", "write_file", {"path": "other.txt", "content": "x"}, ""
    )
    r_tamper = tc.post(
        "/tools/execute",
        headers={
            "Authorization": "Bearer shared-node-secret",
            "X-Splitch-Approval-Ts": ts_bad,
            "X-Splitch-Approval-Sig": sig_bad,
        },
        json=payload,
    )
    assert r_tamper.status_code == 403

    # Expired HMAC (> 60s old) -> 403
    ts_old, sig_old = sign_tool_approval(
        "shared-node-secret",
        payload["tool"],
        payload["arguments"],
        payload["workspace_subpath"],
        timestamp=int(time.time()) - 180,
    )
    r_expired = tc.post(
        "/tools/execute",
        headers={
            "Authorization": "Bearer shared-node-secret",
            "X-Splitch-Approval-Ts": ts_old,
            "X-Splitch-Approval-Sig": sig_old,
        },
        json=payload,
    )
    assert r_expired.status_code == 403

    # Fresh valid HMAC -> 200
    ts_ok, sig_ok = sign_tool_approval(
        "shared-node-secret",
        payload["tool"],
        payload["arguments"],
        payload["workspace_subpath"],
    )
    r_ok = tc.post(
        "/tools/execute",
        headers={
            "Authorization": "Bearer shared-node-secret",
            "X-Splitch-Approval-Ts": ts_ok,
            "X-Splitch-Approval-Sig": sig_ok,
        },
        json=payload,
    )
    assert r_ok.status_code == 200 and r_ok.json()["ok"] is True
    assert (tmp_path / "signed.txt").read_text(encoding="utf-8") == "verified\n"

    # Without AGENT_TOKEN, non-loopback peer is blocked even with X-Splitch-Router-Approved: 1
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")

    async def _remote_call() -> int:
        transport = httpx.ASGITransport(app=agent_mod.app, client=("192.168.1.55", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.local") as client:
            resp = await client.post(
                "/tools/execute",
                headers={"X-Splitch-Router-Approved": "1"},
                json=payload,
            )
            return resp.status_code

    assert asyncio.run(_remote_call()) == 403


def test_run_command_process_group_kill_and_output_cap(tmp_path: Path, monkeypatch) -> None:
    import shutil
    import time

    from backend.agent import tools as agent_tools

    monkeypatch.setattr(agent_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(agent_mod, "ENABLE_TOOLS", True)
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_mod, "ROUTER_PUBLIC_KEY", None)
    monkeypatch.setattr(agent_mod, "ALLOW_UNAUTH_TOOLS", True)
    monkeypatch.setattr(agent_mod, "CMD_SANDBOX", "none")
    monkeypatch.setattr(agent_mod, "_CMD_TIMEOUT_S", 0.6)

    tc = TestClient(agent_mod.app)

    # 1. Bounded output cap (~100 KB stdout truncated cleanly without crashing)
    r_cap = tc.post(
        "/tools/execute",
        headers={"X-Splitch-Router-Approved": "1"},
        json={
            "tool": "run_command",
            "arguments": {"command": f'"{sys.executable}" -c "print(\'A\' * 100000)"'},
        },
    )
    assert r_cap.status_code == 200
    body_cap = r_cap.json()
    assert body_cap["ok"] is True and body_cap["exit_code"] == 0
    assert body_cap["sandbox"] == "host"
    assert "output truncated" in body_cap["output"]
    assert len(body_cap["output"]) <= 12_100

    # 2. POSIX process-group kill terminates background child processes on timeout
    if os.name != "nt":
        pid_file = tmp_path / "child.pid"
        r_to = tc.post(
            "/tools/execute",
            headers={"X-Splitch-Router-Approved": "1"},
            json={
                "tool": "run_command",
                "arguments": {
                    "command": f"sleep 30 & echo $! > '{pid_file}'; sleep 30",
                },
            },
        )
        assert r_to.status_code == 200
        assert r_to.json()["exit_code"] == 124
        assert pid_file.exists()
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        time.sleep(0.15)
        try:
            os.kill(child_pid, 0)
            raise AssertionError(f"Orphan child PID {child_pid} survived process-group kill")
        except ProcessLookupError:
            pass

    # 3. OS-level bwrap sandbox blocks host writes, isolates /tmp, and masks $HOME + SPLITCH_DATA_ROOT secrets
    if sys.platform.startswith("linux") and shutil.which("bwrap") and agent_tools._probe_bwrap_works():
        fake_home = tmp_path / "fake-home"
        ws_dir = tmp_path / "workspace"
        data_dir = tmp_path / "splitch-data"
        (fake_home / ".ssh").mkdir(parents=True)
        (fake_home / ".ssh" / "id_ed25519").write_text("PRIVATE_SSH_SECRET\n", encoding="utf-8")
        data_dir.mkdir(parents=True)
        (data_dir / "secrets.enc").write_text("VAULT_CIPHER_SECRET\n", encoding="utf-8")
        ws_dir.mkdir(parents=True)
        outside = tmp_path.parent / f"splitch-escape-{os.getpid()}.txt"

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setenv("SPLITCH_DATA_ROOT", str(data_dir))
        monkeypatch.setattr(agent_mod, "WORKSPACE_ROOT", str(ws_dir))
        monkeypatch.setattr(agent_mod, "CMD_SANDBOX", "required")
        monkeypatch.setattr(agent_mod, "_CMD_TIMEOUT_S", 5.0)
        r_sb = tc.post(
            "/tools/execute",
            headers={"X-Splitch-Router-Approved": "1"},
            json={
                "tool": "run_command",
                "arguments": {
                    "command": (
                        f"echo inside > ok.txt && "
                        f"(touch /etc/splitch-escape-test 2>/dev/null && echo RO_ESCAPED || echo BLOCKED_RO) && "
                        f"(cat '{fake_home / '.ssh' / 'id_ed25519'}' 2>/dev/null && echo HOME_LEAKED || echo BLOCKED_HOME) && "
                        f"(cat '{data_dir / 'secrets.enc'}' 2>/dev/null && echo DATA_LEAKED || echo BLOCKED_DATA) && "
                        f"touch '{outside}' 2>/dev/null || true"
                    ),
                },
            },
        )
        assert r_sb.status_code == 200
        sb_body = r_sb.json()
        assert sb_body["ok"] is True and sb_body["sandbox"] == "bwrap"
        assert "BLOCKED_RO" in sb_body["output"]
        assert "BLOCKED_HOME" in sb_body["output"]
        assert "BLOCKED_DATA" in sb_body["output"]
        assert "PRIVATE_SSH_SECRET" not in sb_body["output"]
        assert "VAULT_CIPHER_SECRET" not in sb_body["output"]
        assert (ws_dir / "ok.txt").read_text(encoding="utf-8").strip() == "inside"
        assert not outside.exists()  # private ephemeral /tmp inside bwrap cannot touch host /tmp


def test_command_sandbox_required_fail_closed_auto_fallback_and_seatbelt_profile(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    import shutil

    from backend.agent import tools as agent_tools

    # 1. Fail-closed when required sandbox is unavailable (covers CI / restricted containers / Windows)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(agent_tools, "_BWRAP_PROBE_CACHE", False)
    monkeypatch.setattr(agent_tools, "_find_seatbelt_bin", lambda: None)

    assert agent_tools.detect_effective_sandbox("required") == "unavailable"
    assert agent_tools.detect_effective_sandbox("auto") == "host"
    assert agent_tools.detect_effective_sandbox("none") == "host"

    res_fail = asyncio.run(
        agent_tools.run_sandboxed_shell_command(
            "echo should_not_run",
            tmp_path,
            tmp_path,
            sandbox_mode="required",
        )
    )
    assert res_fail.ok is False
    assert res_fail.sandbox == "error"
    assert "SPLITCH_CMD_SANDBOX=none" in res_fail.output

    # 2. Explicit `auto` falls back cleanly to host shell
    res_auto = asyncio.run(
        agent_tools.run_sandboxed_shell_command(
            f'"{sys.executable}" -c "print(42)"',
            tmp_path,
            tmp_path,
            sandbox_mode="auto",
        )
    )
    assert res_auto.ok is True
    assert res_auto.sandbox == "host"
    assert "42" in res_auto.output

    # 3. macOS Seatbelt profile restricts both file writes AND sensitive secret reads + outbound net
    fake_home = tmp_path / "mac-home"
    fake_data = tmp_path / "mac-data"
    ws = fake_home / "project"
    ws.mkdir(parents=True)
    fake_data.mkdir(parents=True)
    monkeypatch.setattr(agent_tools.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(fake_data))
    monkeypatch.setattr(agent_tools, "_find_seatbelt_bin", lambda: "/usr/bin/sandbox-exec")

    argv, label = agent_tools.build_sandboxed_command(
        "ls", ws, ws, mode="required", allow_net=False
    )
    assert label == "seatbelt"
    assert argv is not None and argv[0] == "/usr/bin/sandbox-exec"
    profile = argv[2]
    profile_posix = profile.replace("\\\\", "/")
    assert "(deny file-write*)" in profile
    assert "(deny network-outbound)" in profile
    assert "(deny file-read*" in profile
    assert ".ssh" in profile_posix
    assert "Library/Keychains" in profile_posix
    assert fake_data.resolve().as_posix() in profile_posix


def test_non_loopback_bind_requires_auth_unless_overridden(monkeypatch) -> None:
    import argparse

    import httpx

    from backend.cli import _cmd_agent, _cmd_router, _enforce_auth_on_exposed_bind

    for var in (
        "SPLITCH_API_KEY",
        "SPLITCH_API_KEY_HASH",
        "SPLITCH_AGENT_TOKEN",
        "SPLITCH_ROUTER_PUBLIC_KEY",
        "SPLITCH_ALLOW_UNAUTH_LAN",
    ):
        monkeypatch.delenv(var, raising=False)

    # Loopback always allowed without auth
    assert _enforce_auth_on_exposed_bind("splitch router", "127.0.0.1", require_api_key=True) is True
    assert _enforce_auth_on_exposed_bind("splitch agent", "127.0.0.1", require_agent_token=True) is True

    # Non-loopback refused by default without auth
    assert _enforce_auth_on_exposed_bind("splitch router", "0.0.0.0", require_api_key=True) is False
    assert _enforce_auth_on_exposed_bind("splitch agent", "0.0.0.0", require_agent_token=True) is False

    # CLI entrypoints refuse with exit code 2 BEFORE registering with --join or starting uvicorn
    join_called = {"count": 0}

    def _boom_post(*_a, **_kw):
        join_called["count"] += 1
        raise AssertionError("httpx.post must not run when non-loopback bind is refused")

    monkeypatch.setattr(httpx, "post", _boom_post)
    agent_args = argparse.Namespace(
        id="n1",
        name="Node 1",
        role="gpu",
        host="0.0.0.0",
        port=8090,
        engine_url="http://127.0.0.1:8081",
        media_url="",
        storage_path="",
        workspace="",
        enable_tools=False,
        yield_on_gaming=False,
        yield_gpus="",
        join="http://192.168.1.10:8080",
        join_token="tok",
        join_only=False,
        advertise_host="",
        ssl_certfile=None,
        ssl_keyfile=None,
        letsencrypt_domain=None,
        tls_self_signed=False,
        allow_unauthenticated=False,
    )
    assert _cmd_agent(agent_args) == 2
    assert join_called["count"] == 0

    router_args = argparse.Namespace(
        config=None,
        host="0.0.0.0",
        port=8080,
        ssl_certfile=None,
        ssl_keyfile=None,
        letsencrypt_domain=None,
        tls_self_signed=False,
        no_proxy_headers=False,
        forwarded_allow_ips=None,
        allow_unauthenticated=False,
    )
    assert _cmd_router(router_args) == 2

    # Explicit override permits LAN bind
    assert (
        _enforce_auth_on_exposed_bind(
            "splitch router", "0.0.0.0", require_api_key=True, allow_unauthenticated=True
        )
        is True
    )

    # Auth env vars satisfy non-loopback check
    monkeypatch.setenv("SPLITCH_API_KEY_HASH", "scrypt$16384$8$1$abc$def")
    monkeypatch.setenv("SPLITCH_ROUTER_PUBLIC_KEY", "ed25519:AAAA")
    assert _enforce_auth_on_exposed_bind("splitch router", "0.0.0.0", require_api_key=True) is True
    assert _enforce_auth_on_exposed_bind("splitch agent", "0.0.0.0", require_agent_token=True) is True


def test_streaming_first_chunk_failover_and_no_primary_retry_loop(monkeypatch) -> None:
    import httpx
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, StreamingResponse

    from backend.shared.models import AliasConfig, LoadedModel, NodeStatus, NodeTelemetry

    upstream_app = FastAPI()
    hits = {"primary_stream": 0, "standby_stream": 0, "primary_503": 0, "standby_503": 0}

    @upstream_app.post("/primary-empty/v1/chat/completions")
    async def primary_empty_stream():
        hits["primary_stream"] += 1

        async def _empty():
            if False:
                yield b""

        return StreamingResponse(_empty(), media_type="text/event-stream")

    @upstream_app.post("/standby-ok/v1/chat/completions")
    async def standby_ok_stream():
        hits["standby_stream"] += 1

        async def _ok():
            yield b'data: {"choices":[{"delta":{"content":"Recovered"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        return StreamingResponse(_ok(), media_type="text/event-stream")

    @upstream_app.post("/primary-503/v1/chat/completions")
    async def primary_503():
        hits["primary_503"] += 1
        return JSONResponse({"error": "primary down"}, status_code=503)

    @upstream_app.post("/standby-503/v1/chat/completions")
    async def standby_503():
        hits["standby_503"] += 1
        return JSONResponse({"error": "standby down"}, status_code=503)

    monkeypatch.setattr(router_mod, "API_KEY", "")
    old_nodes = dict(router_mod.cluster.nodes)
    old_aliases = dict(router_mod.cluster.aliases)
    old_client = router_mod.cluster._proxy_client
    router_mod.cluster._proxy_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream_app),
        base_url="http://cluster.mock",
    )
    try:
        def _mk_node(nid: str, url: str, model_id: str) -> NodeStatus:
            return NodeStatus(
                node_id=nid,
                display_name=nid,
                role="unified",
                agent_url=url,
                engine_url=url,
                online=True,
                telemetry=NodeTelemetry(
                    node_id=nid,
                    role="unified",
                    platform="linux",
                    timestamp=1.0,
                    vram_total_mb=24576.0,
                    vram_free_mb=20000.0,
                    storage_mounted=True,
                    engine="llama-server",
                    engine_healthy=True,
                    models=[LoadedModel(id=model_id, status="loaded")],
                ),
            )

        router_mod.cluster.nodes.clear()
        router_mod.cluster.nodes["gpu-rig"] = _mk_node(
            "gpu-rig", "http://cluster.mock/primary-empty", "m-primary"
        )
        router_mod.cluster.nodes["standby-mac"] = _mk_node(
            "standby-mac", "http://cluster.mock/standby-ok", "m-standby"
        )
        router_mod.cluster.aliases["heavy"] = AliasConfig(
            alias="heavy",
            task_kind="code",
            primary_node="gpu-rig",
            primary_model="m-primary",
            fallback_node="standby-mac",
            fallback_model="m-standby",
        )
        tc = TestClient(router_mod.app)
        r_stream = tc.post(
            "/v1/chat/completions",
            json={
                "model": "heavy",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r_stream.status_code == 200
        assert r_stream.headers.get("x-splitch-node") == "standby-mac"
        assert r_stream.headers.get("x-splitch-fallback") == "gpu-rig_upstream_error"
        assert "Recovered" in r_stream.text
        assert hits["primary_stream"] == 1 and hits["standby_stream"] == 1

        # Both primary and fallback returning 503 must NEVER loop back to primary
        router_mod.cluster.nodes["gpu-rig"].engine_url = "http://cluster.mock/primary-503"
        router_mod.cluster.nodes["standby-mac"].engine_url = "http://cluster.mock/standby-503"
        r_both = tc.post(
            "/v1/chat/completions",
            json={
                "model": "heavy",
                "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert r_both.status_code == 503
        assert hits["primary_503"] == 1 and hits["standby_503"] == 1
    finally:
        router_mod.cluster._proxy_client = old_client
        router_mod.cluster.nodes.clear()
        router_mod.cluster.nodes.update(old_nodes)
        router_mod.cluster.aliases.clear()
        router_mod.cluster.aliases.update(old_aliases)


def test_ui_update_cluster_security_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from backend.router import app as router_mod

    class _DummyClient:
        def __init__(self, host: str) -> None:
            self.host = host

    class _DummyReq:
        def __init__(self, host: str) -> None:
            self.client = _DummyClient(host)
            self.headers: dict[str, str] = {}

    monkeypatch.setattr(router_mod, "API_KEY", "")
    monkeypatch.setattr(router_mod, "API_KEY_HASH", "")
    monkeypatch.delenv("SPLITCH_ALLOW_UI_UPDATE", raising=False)

    # 1) Unauthenticated non-loopback LAN request is refused with 403
    with pytest.raises(HTTPException) as exc_info:
        router_mod._assert_ui_update_allowed(_DummyReq("198.51.100.42"))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 403

    # 2) Loopback request is allowed on open router
    router_mod._assert_ui_update_allowed(_DummyReq("127.0.0.1"))  # type: ignore[arg-type]
    router_mod._assert_ui_update_allowed(_DummyReq("::1"))  # type: ignore[arg-type]

    # 3) Explicit SPLITCH_ALLOW_UI_UPDATE=1 opts in non-loopback requests
    monkeypatch.setenv("SPLITCH_ALLOW_UI_UPDATE", "1")
    router_mod._assert_ui_update_allowed(_DummyReq("198.51.100.42"))  # type: ignore[arg-type]
    monkeypatch.delenv("SPLITCH_ALLOW_UI_UPDATE", raising=False)

    # 4) Authenticated router allows non-loopback (auth middleware enforces Bearer/cookie)
    monkeypatch.setattr(router_mod, "API_KEY", "test-secret-key")
    router_mod._assert_ui_update_allowed(_DummyReq("198.51.100.42"))  # type: ignore[arg-type]
