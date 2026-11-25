"""Adversarial and edge-case parity tests for Phase 3 structural refactor."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.agent import downloads as downloads_mod
from backend.agent import main as agent_mod
from backend.agent import media_routes as media_routes_mod
from backend.agent import strata_routes as strata_routes_mod
from backend.router import chat_api as chat_api_mod
from backend.router import chat_context as chat_ctx_mod
from backend.router.chat_store import ChatStore
from backend.router.config import RouterSettings, SplitchConfig
from backend.router.telemetry import ClusterState
from backend.shared.models import (
    ChatMessageRecord,
    ChatRulesConfig,
    ConversationCreate,
    DownloadJob,
    ScopedExtensionsConfig,
    ScopeRule,
)


def test_agent_mounted_media_and_strata_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_mod, "ROUTER_PUBLIC_KEY", None)
    monkeypatch.setattr(agent_mod, "STORAGE_PATH", str(tmp_path))

    # Populate media files in both storage root and media/ subdir, plus non-media noise
    (tmp_path / "model-a.safetensors").write_bytes(b"sa")
    media_sub = tmp_path / "media"
    media_sub.mkdir()
    (media_sub / "model-b.ckpt").write_bytes(b"cb")
    (media_sub / "z-image-turbo.safetensors").write_bytes(b"zt")
    (media_sub / "ignored.bin").write_bytes(b"no")

    captured_proxy_urls: list[str] = []

    def _strata_handler(request: httpx.Request) -> httpx.Response:
        captured_proxy_urls.append(str(request.url))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "engine": "strata"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "demo-moe"}]})
        return httpx.Response(404, json={"detail": "not found"})

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = httpx.MockTransport(_strata_handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(agent_mod.httpx, "AsyncClient", _MockAsyncClient)

    tc = TestClient(agent_mod.app)

    # Mounted media status & local media scan parity
    r_media = tc.get("/media/engine/status")
    assert r_media.status_code == 200
    media_data = r_media.json()
    assert media_data["node_id"] == agent_mod.NODE_ID
    scanned_names = {f["filename"] for f in media_data["local_media_models"]}
    assert scanned_names == {"model-a.safetensors", "model-b.ckpt", "z-image-turbo.safetensors"}
    assert "ignored.bin" not in scanned_names

    # Backwards-compatible aliases on backend.agent.main
    assert agent_mod._scan_local_media_models() == media_routes_mod.scan_local_media_models(
        str(tmp_path)
    )
    assert agent_mod._build_media_engine_status().node_id == agent_mod.NODE_ID
    assert agent_mod._build_strata_status().node_id == agent_mod.NODE_ID

    # Path traversal & missing model/companion errors in /media/engine/start
    for bad_name in ("../evil.safetensors", "sub/evil.safetensors", "sub\\evil.safetensors"):
        r_trav = tc.post("/media/engine/start", json={"model_file": bad_name})
        assert r_trav.status_code == 400
        assert "Invalid model_file" in r_trav.json()["detail"]

    r_missing_model = tc.post("/media/engine/start", json={"model_file": "nonexistent.safetensors"})
    assert r_missing_model.status_code == 404

    r_missing_companion = tc.post(
        "/media/engine/start", json={"model_file": "z-image-turbo.safetensors"}
    )
    assert r_missing_companion.status_code == 400
    assert "companion file(s)" in r_missing_companion.json()["detail"]

    # Mounted strata status
    r_strata = tc.get("/strata/status")
    assert r_strata.status_code == 200
    assert r_strata.json()["node_id"] == agent_mod.NODE_ID

    # Strata proxy allowlist enforcement (403 before contacting upstream)
    for blocked in ("not-allowlisted", "v1/embeddings", "v1/chat/completions/extra"):
        r_blk = tc.get(f"/strata/proxy/{blocked}")
        assert r_blk.status_code == 403
        assert "not allowlisted" in r_blk.json()["detail"]
    assert captured_proxy_urls == []

    # Allowlisted paths pass through to upstream
    assert "health" in strata_routes_mod.STRATA_PROXY_ALLOWLIST
    r_ok_health = tc.get("/strata/proxy/health")
    assert r_ok_health.status_code == 200
    assert r_ok_health.json()["engine"] == "strata"
    r_ok_models = tc.get("/strata/proxy/v1/models")
    assert r_ok_models.status_code == 200
    assert r_ok_models.json()["data"][0]["id"] == "demo-moe"
    assert len(captured_proxy_urls) == 2

    # Missing Strata binary / import-plan errors in /strata/import-plan
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    r_missing_strata = tc.post(
        "/strata/import-plan", json={"model_path": str(tmp_path / "does-not-exist.gguf")}
    )
    assert r_missing_strata.status_code == 400
    assert "not installed" in r_missing_strata.json()["detail"].lower()

    # Installed executable Strata stub: outside-storage-root fails 400, failing exit fails 500
    fake_strata_dir = tmp_path / "data" / "engines" / "strata" / "bin"
    fake_strata_dir.mkdir(parents=True)
    fake_strata_bin = fake_strata_dir / "strata"
    fake_strata_bin.write_text("#!/usr/bin/env sh\necho 'missing gguf' >&2\nexit 2\n", encoding="utf-8")
    fake_strata_bin.chmod(0o755)

    r_outside_root = tc.post("/strata/import-plan", json={"model_path": "/etc/passwd"})
    assert r_outside_root.status_code == 400
    assert "storage root" in r_outside_root.json()["detail"]

    r_bad_gguf = tc.post(
        "/strata/import-plan", json={"model_path": str(tmp_path / "missing.gguf")}
    )
    assert r_bad_gguf.status_code == 500
    assert "missing gguf" in r_bad_gguf.json()["detail"]


def test_downloads_tracker_and_protocol_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    assert agent_mod._DownloadSpeedTracker is downloads_mod.DownloadSpeedTracker
    assert agent_mod._resolve_hf_protocol is downloads_mod.resolve_hf_protocol
    assert agent_mod._make_job_tqdm is downloads_mod.make_job_tqdm
    assert agent_mod._download_xet_single is downloads_mod.download_xet_single
    assert agent_mod._download_xet_mlx is downloads_mod.download_xet_mlx

    # resolve_hf_protocol env handling
    monkeypatch.delenv("SPLITCH_HF_PROTOCOL", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_downloads_tracker_and_protocol_parity")
    assert downloads_mod.resolve_hf_protocol() == "http"
    assert agent_mod._resolve_hf_protocol() == "http"

    monkeypatch.setenv("SPLITCH_HF_PROTOCOL", "  XET ")
    assert downloads_mod.resolve_hf_protocol() == "xet"

    monkeypatch.setenv("SPLITCH_HF_PROTOCOL", "auto")
    assert downloads_mod.resolve_hf_protocol() == "auto"

    monkeypatch.setenv("SPLITCH_HF_PROTOCOL", "invalid-proto")
    assert downloads_mod.resolve_hf_protocol() == "http"

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert downloads_mod.resolve_hf_protocol() == "auto"

    # DownloadSpeedTracker initial fast-path & 0-byte guard
    tracker = downloads_mod.DownloadSpeedTracker()
    job = DownloadJob(id="j1", node_id="n1", repo_id="org/repo", filename="m.gguf")
    tracker.record(job, 0)
    assert job.speed_bytes_per_sec == 0.0

    job.downloaded_bytes = 5000
    tracker.record(job, 5000)
    assert job.speed_bytes_per_sec > 0.0

    # Window smoothing after >= 0.4s
    first_speed = job.speed_bytes_per_sec
    time.sleep(0.42)
    job.downloaded_bytes += 40000
    tracker.record(job, 40000)
    assert job.speed_bytes_per_sec != first_speed
    assert job.speed_bytes_per_sec > 0.0

    # make_job_tqdm percent capping (cap_99=True vs False)
    job_cap = DownloadJob(
        id="j2", node_id="n1", repo_id="org/repo", filename="mlx", total_bytes=100
    )
    tqdm_cap = downloads_mod.make_job_tqdm(
        job_cap, downloads_mod.DownloadSpeedTracker(), cap_99=True
    )
    bar_cap = tqdm_cap(total=100)
    bar_cap.update(100)
    bar_cap.close()
    assert job_cap.downloaded_bytes == 100
    assert job_cap.percent == 99.9

    job_full = DownloadJob(
        id="j3", node_id="n1", repo_id="org/repo", filename="m.gguf", total_bytes=100
    )
    tqdm_full = downloads_mod.make_job_tqdm(
        job_full, downloads_mod.DownloadSpeedTracker(), cap_99=False
    )
    bar_full = tqdm_full(total=100)
    bar_full.update(100)
    bar_full.close()
    assert job_full.percent == 100.0


def test_chat_context_approval_and_reasoning_parity() -> None:
    assert chat_api_mod._requires_tool_approval is chat_ctx_mod.requires_tool_approval
    assert (
        chat_api_mod._adjust_reasoning_effort_on_error
        is chat_ctx_mod.adjust_reasoning_effort_on_error
    )
    assert chat_api_mod._sse is chat_ctx_mod.sse_event
    assert (
        chat_api_mod._matches_cmd_prefix_or_pattern is chat_ctx_mod.matches_cmd_prefix_or_pattern
    )

    # Denylist always wins, even with full_auto + conv_auto_approve
    full_cfg = RouterSettings(
        approval_policy="full_auto",
        shell_allowlist=["git", "ls", "pytest"],
        shell_denylist=["rm -rf", "sudo", "| sh", "curl "],
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "run_command",
            {"command": "echo hi && rm -rf /tmp/x"},
            is_external_mcp=False,
            conv_auto_approve=True,
            router_cfg=full_cfg,
        )
        is True
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "run_command",
            {"command": "  SUDO ls -la"},
            is_external_mcp=False,
            conv_auto_approve=True,
            router_cfg=full_cfg,
        )
        is True
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "run_command",
            {"command": "git status"},
            is_external_mcp=False,
            conv_auto_approve=False,
            router_cfg=full_cfg,
        )
        is False
    )

    # turbo_safe allowlist prefix boundaries vs non-allowlisted vs empty
    turbo_cfg = RouterSettings(
        approval_policy="turbo_safe",
        shell_allowlist=["git", "ls", "pytest"],
        shell_denylist=["rm -rf", "sudo"],
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "run_command",
            {"command": "git diff --stat"},
            is_external_mcp=False,
            conv_auto_approve=False,
            router_cfg=turbo_cfg,
        )
        is False
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "run_command",
            {"command": "pytest\t-q"},
            is_external_mcp=False,
            conv_auto_approve=False,
            router_cfg=turbo_cfg,
        )
        is False
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "run_command",
            {"command": "ls"},
            is_external_mcp=False,
            conv_auto_approve=False,
            router_cfg=turbo_cfg,
        )
        is False
    )
    # Prefix trap: "git_evil" must NOT match allowlist entry "git"
    assert (
        chat_ctx_mod.requires_tool_approval(
            "run_command",
            {"command": "git_evil --flag"},
            is_external_mcp=False,
            conv_auto_approve=False,
            router_cfg=turbo_cfg,
        )
        is True
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "run_command",
            {"command": "   "},
            is_external_mcp=False,
            conv_auto_approve=False,
            router_cfg=turbo_cfg,
        )
        is True
    )

    # External MCP ignores turbo_safe/auto_edits, only skips on conv_auto_approve or full_auto
    assert (
        chat_ctx_mod.requires_tool_approval(
            "mcp__ext__fetch",
            {},
            is_external_mcp=True,
            conv_auto_approve=False,
            router_cfg=turbo_cfg,
        )
        is True
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "mcp__ext__fetch",
            {},
            is_external_mcp=True,
            conv_auto_approve=True,
            router_cfg=turbo_cfg,
        )
        is False
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "mcp__ext__fetch",
            {},
            is_external_mcp=True,
            conv_auto_approve=False,
            router_cfg=full_cfg,
        )
        is False
    )

    # Read-only vs mutating file tools under ask vs auto_edits
    ask_cfg = RouterSettings(approval_policy="ask")
    auto_edits_cfg = RouterSettings(approval_policy="auto_edits")
    assert (
        chat_ctx_mod.requires_tool_approval(
            "read_file",
            {"path": "README.md"},
            is_external_mcp=False,
            conv_auto_approve=False,
            router_cfg=ask_cfg,
        )
        is False
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "write_file",
            {"path": "a.py"},
            is_external_mcp=False,
            conv_auto_approve=False,
            router_cfg=ask_cfg,
        )
        is True
    )
    assert (
        chat_ctx_mod.requires_tool_approval(
            "write_file",
            {"path": "a.py"},
            is_external_mcp=False,
            conv_auto_approve=False,
            router_cfg=auto_edits_cfg,
        )
        is False
    )

    # adjust_reasoning_effort_on_error edge variants
    assert chat_ctx_mod.adjust_reasoning_effort_on_error({"model": "x"}, "reasoning_effort") is None
    assert (
        chat_ctx_mod.adjust_reasoning_effort_on_error(
            {"reasoning_effort": "high"}, "context length exceeded"
        )
        is None
    )

    orig_high = {"reasoning_effort": "high", "model": "gpt"}
    remapped_xhigh = chat_ctx_mod.adjust_reasoning_effort_on_error(
        orig_high, "unexpected reasoning effort 'high'; supported: low, medium, xhigh"
    )
    assert remapped_xhigh == {"reasoning_effort": "xhigh", "model": "gpt"}
    assert orig_high["reasoning_effort"] == "high"  # input not mutated

    remapped_high = chat_ctx_mod.adjust_reasoning_effort_on_error(
        {"reasoning_effort": "xhigh"}, "unexpected reasoning effort; supported: low, high"
    )
    assert remapped_high == {"reasoning_effort": "high"}

    stripped_xhigh = chat_ctx_mod.adjust_reasoning_effort_on_error(
        {"reasoning_effort": "xhigh"}, "reasoning_effort invalid; supported: low, high, xhigh"
    )
    assert stripped_xhigh == {}

    remapped_off = chat_ctx_mod.adjust_reasoning_effort_on_error(
        {"reasoning_effort": "none"}, "unexpected reasoning effort; supported: off, low"
    )
    assert remapped_off == {"reasoning_effort": "off"}

    stripped_med = chat_ctx_mod.adjust_reasoning_effort_on_error(
        {"reasoning_effort": "medium"}, "reasoning_effort not supported by template"
    )
    assert stripped_med == {}


def _mk_msg(cid: str, idx: int, role: str, content: str = "x") -> ChatMessageRecord:
    return ChatMessageRecord(
        id=f"m{idx}",
        conversation_id=cid,
        role=role,  # type: ignore[arg-type]
        content=content,
        created_at=float(idx + 1),
    )


def test_split_for_compaction_turn_boundaries() -> None:
    short = [_mk_msg("c", i, "user" if i % 2 == 0 else "assistant") for i in range(6)]
    older_s, recent_s = chat_ctx_mod.split_for_compaction(short, keep_recent=6)
    assert older_s == [] and recent_s == short

    # Tool-call turn in the middle: split_idx=4 lands on tool, walks back to user at index 2
    msgs = [
        _mk_msg("c", 0, "user", "u0"),
        _mk_msg("c", 1, "assistant", "a0"),
        _mk_msg("c", 2, "user", "u1"),
        _mk_msg("c", 3, "assistant", "a1"),
        _mk_msg("c", 4, "tool", "t1"),
        _mk_msg("c", 5, "assistant", "a2"),
        _mk_msg("c", 6, "user", "u2"),
        _mk_msg("c", 7, "assistant", "a3"),
    ]
    older, recent = chat_ctx_mod.split_for_compaction(msgs, keep_recent=4)
    assert [m.content for m in older] == ["u0", "a0"]
    assert [m.content for m in recent] == ["u1", "a1", "t1", "a2", "u2", "a3"]
    assert recent[0].role == "user"

    # When fewer than 2 messages precede the boundary, compaction skips safely
    too_short_prefix = [_mk_msg("c", i, "user" if i == 0 else "assistant") for i in range(7)]
    older_none, recent_all = chat_ctx_mod.split_for_compaction(too_short_prefix, keep_recent=6)
    assert older_none == [] and recent_all == too_short_prefix


def test_discovery_tool_and_prepare_chat_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    skills_dir = tmp_path / "skills"
    data_dir.mkdir()
    skills_dir.mkdir()

    # Skills: one valid, one global, one inactive
    for sid, desc in (
        ("alpha-skill", "Alpha debugging workflow"),
        ("beta-skill", "Beta security scanner"),
        ("gamma-skill", "Gamma inactive skill"),
    ):
        sdir = skills_dir / sid
        sdir.mkdir()
        (sdir / "SKILL.md").write_text(f"# {sid}\n{desc}\n", encoding="utf-8")

    # MCP configs: active, inactive, disabled
    (data_dir / "mcp_servers.json").write_text(
        json.dumps(
            [
                {
                    "id": "local-git",
                    "name": "Local Git",
                    "description": "Git tools",
                    "transport": "stdio",
                    "command": "echo",
                    "enabled": True,
                },
                {
                    "id": "docs-search",
                    "name": "Docs Search",
                    "description": "Search API docs",
                    "transport": "http",
                    "url": "https://example.invalid/mcp",
                    "enabled": True,
                },
                {
                    "id": "disabled-srv",
                    "name": "Disabled Server",
                    "description": "Disabled search tool",
                    "transport": "stdio",
                    "command": "echo",
                    "enabled": False,
                },
            ]
        ),
        encoding="utf-8",
    )

    # Scopes and global rules
    scopes_file = data_dir / "workspace_scopes.json"
    chat_ctx_mod.save_scopes(
        data_dir,
        scopes_file,
        ScopedExtensionsConfig(
            global_scope=ScopeRule(skills=["alpha-skill"], mcps=["local-git"]),
            workspaces={"subproj": ScopeRule(skills=["beta-skill"], mcps=[])},
        ),
    )
    chat_ctx_mod.save_chat_rules(
        data_dir, ChatRulesConfig(global_rules="Always run preflight checks.")
    )

    # Intercept outbound registry HTTP calls cleanly via httpx.MockTransport
    def _registry_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"servers": [], "skills": []})

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = httpx.MockTransport(_registry_handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _MockAsyncClient)

    async def _run_async_checks() -> None:
        # Missing query errors
        ok_bad_mcp, msg_bad_mcp = await chat_ctx_mod.execute_discovery_tool(
            "suggest_mcp",
            {"query": "   "},
            data_dir=data_dir,
            skills_dir=skills_dir,
            mcp_map={},
            eff_skills=[],
        )
        assert ok_bad_mcp is False
        assert "Missing 'query'" in msg_bad_mcp

        ok_bad_sk, msg_bad_sk = await chat_ctx_mod.execute_discovery_tool(
            "suggest_skill",
            {},
            data_dir=data_dir,
            skills_dir=skills_dir,
            mcp_map={},
            eff_skills=[],
        )
        assert ok_bad_sk is False
        assert "Missing 'query'" in msg_bad_sk

        # Local matching for suggest_mcp
        mcp_map = chat_ctx_mod.load_mcp_configs(data_dir, ["local-git"])
        ok_mcp, out_mcp = await chat_ctx_mod.execute_discovery_tool(
            "suggest_mcp",
            {"query": "DOCS", "reason": "need docs"},
            data_dir=data_dir,
            skills_dir=skills_dir,
            mcp_map=mcp_map,
            eff_skills=["alpha-skill"],
        )
        assert ok_mcp is True
        parsed_mcp = json.loads(out_mcp)
        assert [m["id"] for m in parsed_mcp["local_matches"]] == ["docs-search"]
        assert parsed_mcp["local_matches"][0]["enabled_in_chat"] is False

        # Local matching for suggest_skill
        ok_sk, out_sk = await chat_ctx_mod.execute_discovery_tool(
            "suggest_skill",
            {"query": "alpha", "reason": "debugging"},
            data_dir=data_dir,
            skills_dir=skills_dir,
            mcp_map=mcp_map,
            eff_skills=["alpha-skill"],
        )
        assert ok_sk is True
        parsed_sk = json.loads(out_sk)
        assert [s["id"] for s in parsed_sk["local_matches"]] == ["alpha-skill"]
        assert parsed_sk["local_matches"][0]["enabled_in_chat"] is True

        # prepare_chat_turn with real SQLite ChatStore + scopes + rules + compacted watermark
        store = ChatStore(data_dir / "chats.db")
        conv = store.create_conversation(
            ConversationCreate(
                title="Parity Chat",
                workspace_subpath="/subproj/",
                enabled_skills=["../escape", "alpha-skill"],
                enabled_mcps=[],
                web_search_enabled=True,
                swarm_enabled=True,
                system_prompt="Custom prompt here.",
            )
        )
        store.add_message(
            ChatMessageRecord(
                id="old1",
                conversation_id=conv.id,
                role="user",
                content="old compacted message",
                created_at=10.0,
            )
        )
        conv = store.save_compaction(conv.id, "- Prior goal: refactor", 15.0) or conv
        store.add_message(
            ChatMessageRecord(
                id="new1",
                conversation_id=conv.id,
                role="user",
                content="inspect image",
                images=["data:image/png;base64,AA=="],
                created_at=20.0,
            )
        )

        cluster = ClusterState(SplitchConfig())
        events, turn = await chat_ctx_mod.prepare_chat_turn(
            conv=conv,
            cluster=cluster,
            store=store,
            data_dir=data_dir,
            skills_dir=skills_dir,
            scopes_file=scopes_file,
            media_service=None,
        )
        assert events == []
        assert turn.eff_skills == ["alpha-skill", "beta-skill", "../escape"]
        assert list(turn.mcp_map.keys()) == ["local-git"]

        sys_msg = turn.openai_msgs[0]["content"]
        assert "Always run preflight checks." in sys_msg
        assert "Custom prompt here." in sys_msg
        assert '<skill id="alpha-skill">' in sys_msg
        assert '<skill id="beta-skill">' in sys_msg
        assert "../escape" not in sys_msg
        assert "<conversation_summary>\n- Prior goal: refactor\n</conversation_summary>" in sys_msg
        assert "gamma-skill" in sys_msg  # listed under Installed-but-Inactive Skills
        assert "docs-search" in sys_msg  # listed under Installed-but-Inactive MCP Servers
        assert "Swarm orchestration is enabled." in sys_msg
        assert "Web search is enabled." in sys_msg

        # Older message <= compacted_through_ts (15.0) is excluded; new message with image included
        assert len(turn.openai_msgs) == 2
        user_content = turn.openai_msgs[1]["content"]
        assert isinstance(user_content, list)
        assert user_content[0] == {"type": "text", "text": "inspect image"}
        assert user_content[1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AA=="},
        }

        tool_names = {t["function"]["name"] for t in turn.tools_payload}
        assert {"read_file", "suggest_mcp", "suggest_skill", "web_search", "spawn_agent"} <= tool_names

    asyncio.run(_run_async_checks())


def test_dashboard_css_imports_cascade_order_and_no_pills() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    index_css = repo_root / "dashboard" / "src" / "index.css"
    styles_dir = repo_root / "dashboard" / "src" / "styles"

    imports = re.findall(r'@import\s+"([^"]+)";', index_css.read_text(encoding="utf-8"))
    expected = [
        "./styles/base.css",
        "./styles/panels.css",
        "./styles/chat.css",
        "./styles/studio-settings.css",
    ]
    assert imports == expected

    actual_partials = sorted(p.name for p in styles_dir.glob("*.css"))
    expected_names = sorted(Path(p).name for p in expected)
    assert actual_partials == expected_names

    pill_re = re.compile(r"999px|9999px|rounded-full")
    for p in (repo_root / "dashboard" / "src").rglob("*"):
        if p.is_file() and p.suffix in {".css", ".ts", ".tsx"}:
            txt = p.read_text(encoding="utf-8")
            assert not pill_re.search(txt), f"Pill border-radius found in {p}"
            if p.parent == styles_dir:
                assert len(txt.strip()) > 0
