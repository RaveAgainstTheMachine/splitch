"""Tests for salted secret hashing, AES-256-GCM SecretVault, config indirection, MCP masking, and Ed25519 agent auth."""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent import main as agent_mod
from backend.router import app as router_mod
from backend.router.config import SplitchConfig
from backend.router.mcp_api import build_mcp_router
from backend.router.provision_api import JoinTokenManager
from backend.router.telemetry import ClusterState
from backend.shared.secrets import (
    MASKED_SECRET_SENTINEL,
    SecretVault,
    generate_ed25519_keypair,
    hash_secret_scrypt,
    hash_secret_sha256,
    load_ed25519_private_key,
    load_ed25519_public_key,
    resolve_env_map,
    resolve_secret_value,
    sign_agent_request_ed25519,
    sign_tool_approval_ed25519,
    verify_secret_hash,
)


def test_salted_scrypt_and_sha256_hashing() -> None:
    h1 = hash_secret_scrypt("correct-horse-battery")
    h2 = hash_secret_scrypt("correct-horse-battery")
    assert h1.startswith("scrypt$") and h2.startswith("scrypt$")
    assert h1 != h2  # unique random salt per hash
    assert verify_secret_hash("correct-horse-battery", h1) is True
    assert verify_secret_hash("correct-horse-battery", h1) is True  # hits verified cache cleanly
    assert verify_secret_hash("wrong-password", h1) is False
    assert verify_secret_hash("correct-horse-battery", "scrypt$bad$format") is False

    s1 = hash_secret_sha256("spj_high_entropy_token_12345")
    s2 = hash_secret_sha256("spj_high_entropy_token_12345")
    assert s1.startswith("sha256$") and s1 != s2
    assert verify_secret_hash("spj_high_entropy_token_12345", s1) is True
    assert verify_secret_hash("spj_other_token", s1) is False


def test_router_api_key_hash_and_independent_session_secret(monkeypatch) -> None:
    hashed = hash_secret_scrypt("hunter2-super-secret")
    monkeypatch.setattr(router_mod, "API_KEY", "")
    monkeypatch.setattr(router_mod, "API_KEY_HASH", hashed)
    monkeypatch.setattr(router_mod, "SESSION_SECRET", "independent-random-session-secret-0123456789")
    router_mod._LOGIN_FAILURES.clear()

    tc = TestClient(router_mod.app)

    # Unauthenticated blocked
    assert tc.get("/api/status").status_code == 401
    assert tc.get("/api/auth/status").json() == {"auth_required": True, "authenticated": False}

    # Wrong Bearer & wrong login rejected
    assert tc.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert tc.post("/api/auth/login", json={"api_key": "nope"}).status_code == 401

    # Valid Bearer against salted scrypt hash succeeds
    assert (
        tc.get("/api/status", headers={"Authorization": "Bearer hunter2-super-secret"}).status_code
        == 200
    )

    # Login sets independent HttpOnly session cookie (not derived from API_KEY)
    r_login = tc.post("/api/auth/login", json={"api_key": "hunter2-super-secret"})
    assert r_login.status_code == 200
    assert tc.get("/api/status").status_code == 200


def test_join_tokens_stored_hashed_only_in_memory() -> None:
    jtm = JoinTokenManager()
    minted = jtm.create(ttl_seconds=120, node_hint="worker-1")
    assert minted.secret.startswith("spj_")

    stored = jtm._tokens[minted.id]
    assert not hasattr(stored, "secret")
    assert stored.secret_hash.startswith("sha256$")
    assert minted.secret not in stored.secret_hash

    assert jtm.is_valid_unconsumed(minted.secret) is True
    assert jtm.is_valid_unconsumed("spj_forged") is False
    assert jtm.validate_and_consume(minted.secret) is True
    assert jtm.validate_and_consume(minted.secret) is False  # single-use


def test_secret_indirection_and_vault_aes256gcm_aad(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPLITCH_TEST_ENV_SECRET", "from-environment-42")
    assert resolve_secret_value("env:SPLITCH_TEST_ENV_SECRET") == "from-environment-42"
    assert resolve_secret_value("${SPLITCH_TEST_ENV_SECRET}") == "from-environment-42"

    sec_file = tmp_path / "api.key"
    sec_file.write_text("from-file-99\n", encoding="utf-8")
    if sys.platform != "win32":
        sec_file.chmod(0o644)
        with pytest.raises(PermissionError):
            resolve_secret_value(f"file:{sec_file}", check_permissions=True)
        sec_file.chmod(0o600)
    assert resolve_secret_value(f"file:{sec_file}", check_permissions=True) == "from-file-99"

    # AES-256-GCM SecretVault round-trip + permissions + AAD tamper detection
    vault = SecretVault(tmp_path / "data")
    ref = vault.set("brave_api_key", "BSA_top_secret_123")
    assert ref == "vault:brave_api_key"
    assert resolve_secret_value(ref, vault=vault) == "BSA_top_secret_123"
    assert vault.list_names() == ["brave_api_key"]

    raw_enc = vault.vault_path.read_text(encoding="utf-8")
    assert "BSA_top_secret_123" not in raw_enc
    if sys.platform != "win32":
        assert stat.S_IMODE(vault.vault_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(vault.key_path.stat().st_mode) == 0o600

    # Tampering: copying encrypted entry under a different secret name MUST fail AAD check
    env = json.loads(raw_enc)
    env["entries"]["swapped_name"] = env["entries"]["brave_api_key"]
    vault.vault_path.write_text(json.dumps(env), encoding="utf-8")
    assert vault.get("swapped_name") is None
    assert vault.get("brave_api_key") == "BSA_top_secret_123"

    assert vault.delete("brave_api_key") is True
    assert vault.get("brave_api_key") is None


def test_mcp_auto_vaulting_masking_sentinel_preservation_and_export_redaction(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BRAVE_RUNTIME_KEY", "brave-live-value")
    cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
    app = FastAPI()
    app.include_router(build_mcp_router(cluster, tmp_path))
    tc = TestClient(app)

    # Save MCP server containing a raw secret token + safe env pointer + plain config var
    r_save = tc.post(
        "/api/mcp",
        json={
            "id": "gh-srv",
            "name": "GitHub",
            "description": "GitHub MCP",
            "transport": "stdio",
            "scope": "shared",
            "enabled": True,
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {
                "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_real_secret_token_98765",
                "BRAVE_API_KEY": "env:BRAVE_RUNTIME_KEY",
                "LOG_LEVEL": "info",
            },
        },
    )
    assert r_save.status_code == 200
    saved = r_save.json()
    assert saved["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == MASKED_SECRET_SENTINEL
    assert saved["env"]["BRAVE_API_KEY"] == "env:BRAVE_RUNTIME_KEY"
    assert saved["env"]["LOG_LEVEL"] == "info"

    # Disk JSON MUST NOT contain plaintext token — only vault pointer
    disk_raw = (tmp_path / "mcp_servers.json").read_text(encoding="utf-8")
    assert "ghp_real_secret_token_98765" not in disk_raw
    assert "vault:mcp.gh-srv.GITHUB_PERSONAL_ACCESS_TOKEN" in disk_raw

    # GET /api/mcp returns masked sentinel, never plaintext
    listed = tc.get("/api/mcp").json()
    assert len(listed) == 1
    assert listed[0]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == MASKED_SECRET_SENTINEL

    # Re-saving via UI with masked sentinel preserves existing vault secret cleanly
    saved["description"] = "Updated description"
    r_upd = tc.post("/api/mcp", json=saved)
    assert r_upd.status_code == 200
    vault = SecretVault(tmp_path)
    assert vault.get("mcp.gh-srv.GITHUB_PERSONAL_ACCESS_TOKEN") == "ghp_real_secret_token_98765"

    # Runtime env resolution decrypts vault + resolves env: pointer for child process
    disk_items = json.loads((tmp_path / "mcp_servers.json").read_text(encoding="utf-8"))
    resolved = resolve_env_map(disk_items[0]["env"], vault=vault)
    assert resolved["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_real_secret_token_98765"
    assert resolved["BRAVE_API_KEY"] == "brave-live-value"
    assert resolved["LOG_LEVEL"] == "info"

    # Exported OpenCode/Zed config redacts vaulted secret to ${VAR} placeholder
    exp = tc.get("/api/mcp/export/opencode").json()
    assert exp["mcp"]["gh-srv"]["environment"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == (
        "${GITHUB_PERSONAL_ACCESS_TOKEN}"
    )

    # Deleting MCP server purges its encrypted entries from SecretVault
    assert tc.delete("/api/mcp/gh-srv").status_code == 200
    assert vault.get("mcp.gh-srv.GITHUB_PERSONAL_ACCESS_TOKEN") is None


def test_ed25519_asymmetric_agent_auth_and_tool_approval(tmp_path: Path, monkeypatch) -> None:
    priv_path, pub_compact = generate_ed25519_keypair(
        tmp_path / "router_ed25519.pem",
        tmp_path / "router_ed25519.pub",
    )
    assert pub_compact.startswith("ed25519:")
    if sys.platform != "win32":
        assert stat.S_IMODE(priv_path.stat().st_mode) == 0o600

    priv = load_ed25519_private_key(str(priv_path))
    pub = load_ed25519_public_key(pub_compact)

    # Agent holds ONLY router's public key — zero shared symmetric secret!
    monkeypatch.setattr(agent_mod, "WORKSPACE_ROOT", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(agent_mod, "ENABLE_TOOLS", True)
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_mod, "ROUTER_PUBLIC_KEY", pub)

    tc = TestClient(agent_mod.app)

    # /health remains public; /metrics requires valid Ed25519 request signature
    assert tc.get("/health").status_code == 200
    assert tc.get("/metrics").status_code == 401
    assert tc.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401

    # Valid Ed25519 signature for GET /metrics succeeds
    hdrs_metrics = sign_agent_request_ed25519(priv, "GET", "/metrics")
    assert tc.get("/metrics", headers=hdrs_metrics).status_code == 200

    # Replaying GET /metrics signature against POST /tools/execute fails with 401 (method+path bound)
    tool_payload = {
        "tool": "write_file",
        "arguments": {"path": "ed.txt", "content": "asymmetric-ok\n"},
        "workspace_subpath": "",
    }
    assert tc.post("/tools/execute", headers=hdrs_metrics, json=tool_payload).status_code == 401

    # Valid request auth for POST /tools/execute WITHOUT Ed25519 tool approval -> 403
    hdrs_exec = sign_agent_request_ed25519(priv, "POST", "/tools/execute")
    assert tc.post("/tools/execute", headers=hdrs_exec, json=tool_payload).status_code == 403

    # Expired tool approval -> 403
    ts_exp, sig_exp = sign_tool_approval_ed25519(
        priv,
        tool_payload["tool"],
        tool_payload["arguments"],
        tool_payload["workspace_subpath"],
        timestamp=int(time.time()) - 180,
    )
    assert (
        tc.post(
            "/tools/execute",
            headers={**hdrs_exec, "X-Splitch-Approval-Ts": ts_exp, "X-Splitch-Approval-Sig": sig_exp},
            json=tool_payload,
        ).status_code
        == 403
    )

    # Tampered arguments -> 403
    ts_bad, sig_bad = sign_tool_approval_ed25519(
        priv, "write_file", {"path": "other.txt", "content": "nope"}, ""
    )
    assert (
        tc.post(
            "/tools/execute",
            headers={**hdrs_exec, "X-Splitch-Approval-Ts": ts_bad, "X-Splitch-Approval-Sig": sig_bad},
            json=tool_payload,
        ).status_code
        == 403
    )

    # Fresh valid Ed25519 request auth + Ed25519 tool approval -> 200
    ts_ok, sig_ok = sign_tool_approval_ed25519(
        priv,
        tool_payload["tool"],
        tool_payload["arguments"],
        tool_payload["workspace_subpath"],
    )
    r_ok = tc.post(
        "/tools/execute",
        headers={**hdrs_exec, "X-Splitch-Approval-Ts": ts_ok, "X-Splitch-Approval-Sig": sig_ok},
        json=tool_payload,
    )
    assert r_ok.status_code == 200 and r_ok.json()["ok"] is True
    assert (tmp_path / "ws" / "ed.txt").read_text(encoding="utf-8") == "asymmetric-ok\n"


def test_cli_secret_subcommands_end_to_end(tmp_path: Path, monkeypatch, capsys) -> None:
    import argparse

    from backend.cli import _cmd_secret

    monkeypatch.setenv("SPLITCH_DATA_DIR", str(tmp_path))

    assert _cmd_secret(argparse.Namespace(secret_cmd="hash", algo="scrypt", value="pw123")) == 0
    out_hash = capsys.readouterr().out.strip()
    assert out_hash.startswith("scrypt$") and verify_secret_hash("pw123", out_hash)

    assert _cmd_secret(argparse.Namespace(secret_cmd="set", name="tavily_api_key", value="tvly-123")) == 0
    assert "vault:tavily_api_key" in capsys.readouterr().out

    assert _cmd_secret(argparse.Namespace(secret_cmd="list")) == 0
    assert "vault:tavily_api_key" in capsys.readouterr().out

    assert _cmd_secret(argparse.Namespace(secret_cmd="delete", name="tavily_api_key")) == 0
    capsys.readouterr()

    assert (
        _cmd_secret(
            argparse.Namespace(secret_cmd="init-agent-keys", out_dir=str(tmp_path), force=False)
        )
        == 0
    )
    out_keys = capsys.readouterr().out
    assert "SPLITCH_ROUTER_PUBLIC_KEY=ed25519:" in out_keys
    assert (tmp_path / "router_ed25519.pem").is_file()
