"""Tests for workspace file tree, file reader, safe git actions, and AI commit message generation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.agent.workspace_git import (
    MAX_FILE_READ_BYTES,
    get_git_diff,
    get_git_status,
    git_commit,
    git_discard_paths,
    git_stage_paths,
    git_unstage_paths,
    list_workspace_tree,
    read_workspace_file,
)


def test_workspace_tree_and_file_reader_confinement(tmp_path: Path) -> None:
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    (tmp_path / "big.txt").write_text("a" * (MAX_FILE_READ_BYTES + 100), encoding="utf-8")

    listing = list_workspace_tree(str(tmp_path))
    names = [e.name for e in listing.entries]
    assert "node_modules" not in names
    assert names[0] == "src"  # directories sorted before files
    assert "README.md" in names

    with pytest.raises(HTTPException) as exc_info:
        list_workspace_tree(str(tmp_path), dir_path="../../etc")
    assert exc_info.value.status_code == 403

    py_file = read_workspace_file(str(tmp_path), "", "src/app.py")
    assert py_file.language == "python"
    assert py_file.is_binary is False
    assert py_file.truncated is False
    assert "print('hi')" in py_file.content

    bin_file = read_workspace_file(str(tmp_path), "", "bin.dat")
    assert bin_file.is_binary is True
    assert bin_file.content == ""

    big_file = read_workspace_file(str(tmp_path), "", "big.txt")
    assert big_file.truncated is True
    assert len(big_file.content) == MAX_FILE_READ_BYTES


def test_workspace_git_status_diff_stage_discard_and_commit(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)

    tracked = tmp_path / "hello.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "hello.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    # Modify tracked + add untracked
    tracked.write_text("x = 2\n", encoding="utf-8")
    untracked = tmp_path / "notes.txt"
    untracked.write_text("todo\n", encoding="utf-8")

    st = get_git_status(str(tmp_path))
    assert st.is_git_repo is True
    by_path = {c.path: c for c in st.changes}
    assert by_path["hello.py"].worktree_status == "M"
    assert by_path["notes.txt"].untracked is True

    diff_wt = get_git_diff(str(tmp_path), "", "hello.py", staged=False)
    assert "-x = 1" in diff_wt.diff
    assert "+x = 2" in diff_wt.diff

    diff_untracked = get_git_diff(str(tmp_path), "", "notes.txt", staged=False)
    assert "+todo" in diff_untracked.diff

    # Discard untracked removes it; discard tracked reverts it
    res_disc = git_discard_paths(str(tmp_path), "", ["notes.txt"])
    assert res_disc.ok is True
    assert not untracked.exists()

    # Stage, unstage, re-stage, commit
    assert git_stage_paths(str(tmp_path), "", ["hello.py"]).ok is True
    st2 = get_git_status(str(tmp_path))
    assert any(c.path == "hello.py" and c.staged for c in st2.changes)

    assert git_unstage_paths(str(tmp_path), "", ["hello.py"]).ok is True
    st3 = get_git_status(str(tmp_path))
    assert any(c.path == "hello.py" and not c.staged for c in st3.changes)

    assert git_stage_paths(str(tmp_path), "", ["hello.py"]).ok is True
    commit_res = git_commit(str(tmp_path), "", "feat: update x")
    assert commit_res.ok is True
    assert len(commit_res.commit_sha) >= 7

    st4 = get_git_status(str(tmp_path))
    assert st4.changes == []


def test_router_generate_commit_message(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from backend.router import app as router_mod
    from backend.router import workspace_api as ws_api_mod
    from backend.shared.models import NodeStatus

    old_nodes = dict(router_mod.cluster.nodes)
    try:
        router_mod.cluster.nodes.clear()
        router_mod.cluster.nodes["pc"] = NodeStatus(
            node_id="pc",
            display_name="PC",
            role="gpu",
            agent_url="http://pc.mock:8090",
            engine_url="http://pc.mock:8081",
            online=True,
        )

        class _FakeClient:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a) -> None:
                return None

            async def request(self, _method: str, url: str, **_kwargs):
                if url.endswith("/workspace/git/status"):
                    return httpx.Response(
                        200,
                        json={
                            "is_git_repo": True,
                            "branch": "main",
                            "changes": [
                                {
                                    "path": "app.py",
                                    "index_status": "M",
                                    "worktree_status": " ",
                                    "staged": True,
                                    "untracked": False,
                                }
                            ],
                        },
                    )
                if url.endswith("/workspace/git/diff"):
                    return httpx.Response(
                        200,
                        json={"path": "app.py", "staged": True, "diff": "+print('ok')"},
                    )
                return httpx.Response(404)

            async def post(self, url: str, **_kwargs):
                if url == "/v1/chat/completions":
                    return httpx.Response(
                        200,
                        json={"choices": [{"message": {"content": "feat: add ok print"}}]},
                    )
                return httpx.Response(404)

        monkeypatch.setattr(ws_api_mod.httpx, "AsyncClient", _FakeClient)
        tc = TestClient(router_mod.app)
        r = tc.post("/api/nodes/pc/git/generate-commit-message", json={"subpath": "", "model": "fast"})
        assert r.status_code == 200
        assert r.json()["message"] == "feat: add ok print"
    finally:
        router_mod.cluster.nodes.clear()
        router_mod.cluster.nodes.update(old_nodes)
