"""Unit tests for Splitch Agent Chat store, API, opt-in tool security, and sandboxed tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.agent import main as agent_main
from backend.router.chat_api import ChatStore, build_chat_router
from backend.router.config import SplitchConfig
from backend.router.telemetry import ClusterState
from backend.router.web_search import _is_public_url
from backend.shared.models import ConversationCreate, ToolExecRequest


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
