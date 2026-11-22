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

    # POST /api/nodes rejects non-HTTP(S) URL schemes
    r_bad_url = tc.post(
        "/api/nodes",
        headers={"Authorization": "Bearer super-secret-test-key"},
        json={
            "id": "evil-node",
            "display_name": "Evil",
            "role": "cpu",
            "agent_url": "file:///etc/passwd",
            "engine_url": "http://127.0.0.1:8081",
        },
    )
    assert r_bad_url.status_code == 400


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
