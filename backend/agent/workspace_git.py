"""
Workspace-confined File Tree, File Reader, and Safe Local Git Operations for `splitch-agent`.

Security invariants:
  - All paths are resolved and confined inside `WORKSPACE_ROOT` via `resolve_workspace_path()`.
  - All git commands execute via `subprocess.run(["git", ...], shell=False)` with explicit `--` separators.
  - No `git push`, `git reset --hard`, `git clean -fd`, or branch switching is exposed.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from fastapi import HTTPException

from backend.agent.tools import IGNORED_DIRS, resolve_workspace_path
from backend.shared.models import (
    GitActionResponse,
    GitDiffResponse,
    GitFileChange,
    GitStatusResponse,
    WorkspaceFileContent,
    WorkspaceTreeEntry,
    WorkspaceTreeListing,
)

MAX_FILE_READ_BYTES = 256 * 1024  # 256 KB cap
MAX_DIFF_BYTES = 200 * 1024  # 200 KB cap

_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".json": "json",
    ".md": "markdown",
    ".css": "css",
    ".html": "html",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".rs": "rust",
    ".go": "go",
    ".sql": "sql",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".java": "java",
    ".rb": "ruby",
    ".xml": "xml",
}


def _run_git(ws_root: Path, args: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(ws_root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _infer_language(path: Path) -> str:
    name_lo = path.name.lower()
    if name_lo == "dockerfile":
        return "dockerfile"
    if name_lo == "makefile":
        return "makefile"
    return _EXT_LANG.get(path.suffix.lower(), "text")


def list_workspace_tree(
    workspace_root: str, subpath: str = "", dir_path: str = ""
) -> WorkspaceTreeListing:
    base_root = Path(workspace_root).resolve()
    ws_root, target = resolve_workspace_path(workspace_root, subpath, dir_path or ".")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {dir_path or '.'}")

    rel_sub = "" if ws_root == base_root else str(ws_root.relative_to(base_root))
    rel_dir = "" if target == ws_root else str(target.relative_to(ws_root))

    items: list[WorkspaceTreeEntry] = []
    children = sorted(
        target.iterdir(),
        key=lambda p: (not p.is_dir(), p.name.lower()),
    )
    for child in children:
        if child.name in IGNORED_DIRS:
            continue
        with contextlib.suppress(OSError):
            is_d = child.is_dir()
            rel_p = str(child.relative_to(ws_root)).replace("\\", "/")
            sz = 0 if is_d else child.stat().st_size
            items.append(
                WorkspaceTreeEntry(
                    name=child.name,
                    path=rel_p,
                    is_dir=is_d,
                    size_bytes=sz,
                )
            )
            if len(items) >= 300:
                break

    return WorkspaceTreeListing(
        root=str(base_root),
        subpath=rel_sub,
        dir_path=rel_dir,
        entries=items,
    )


def read_workspace_file(
    workspace_root: str, subpath: str, rel_path: str
) -> WorkspaceFileContent:
    if not rel_path.strip():
        raise HTTPException(status_code=400, detail="File path is required")
    ws_root, target = resolve_workspace_path(workspace_root, subpath, rel_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {rel_path}")

    size_bytes = target.stat().st_size
    with target.open("rb") as f:
        raw = f.read(MAX_FILE_READ_BYTES + 1)

    truncated = len(raw) > MAX_FILE_READ_BYTES
    if truncated:
        raw = raw[:MAX_FILE_READ_BYTES]

    if b"\x00" in raw:
        return WorkspaceFileContent(
            path=str(target.relative_to(ws_root)).replace("\\", "/"),
            content="",
            language="binary",
            size_bytes=size_bytes,
            truncated=False,
            is_binary=True,
        )

    return WorkspaceFileContent(
        path=str(target.relative_to(ws_root)).replace("\\", "/"),
        content=raw.decode("utf-8", errors="replace"),
        language=_infer_language(target),
        size_bytes=size_bytes,
        truncated=truncated,
        is_binary=False,
    )


def get_git_status(workspace_root: str, subpath: str = "") -> GitStatusResponse:
    ws_root, _ = resolve_workspace_path(workspace_root, subpath, ".")
    rc_top, _, _ = _run_git(ws_root, ["rev-parse", "--is-inside-work-tree"])
    if rc_top != 0:
        return GitStatusResponse(is_git_repo=False)

    rc, out, err = _run_git(ws_root, ["status", "--porcelain=v1", "-b", "-uall"])
    if rc != 0:
        return GitStatusResponse(is_git_repo=True, error=err.strip() or "git status failed")

    branch = ""
    ahead = 0
    behind = 0
    changes: list[GitFileChange] = []

    for line in out.splitlines():
        if line.startswith("## "):
            header = line[3:].strip()
            if "..." in header:
                branch_part, rest = header.split("...", 1)
                branch = branch_part.strip()
                if "[ahead " in rest:
                    with contextlib.suppress(ValueError):
                        ahead = int(rest.split("[ahead ", 1)[1].split("]", 1)[0].split(",", 1)[0])
                if "behind " in rest:
                    with contextlib.suppress(ValueError):
                        behind = int(rest.split("behind ", 1)[1].split("]", 1)[0])
            else:
                branch = header.split(" ", 1)[0]
            continue

        if len(line) < 4:
            continue
        idx_st = line[0]
        wt_st = line[1]
        raw_path = line[3:]
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1]
        rel_p = raw_path.strip().strip('"')
        untracked = idx_st == "?" and wt_st == "?"
        staged = idx_st not in (" ", "?")
        changes.append(
            GitFileChange(
                path=rel_p,
                index_status=idx_st,
                worktree_status=wt_st,
                staged=staged,
                untracked=untracked,
            )
        )

    return GitStatusResponse(
        is_git_repo=True,
        branch=branch,
        ahead=ahead,
        behind=behind,
        changes=changes,
    )


def get_git_diff(
    workspace_root: str, subpath: str, rel_path: str, staged: bool = False
) -> GitDiffResponse:
    if not rel_path.strip():
        raise HTTPException(status_code=400, detail="File path is required")
    ws_root, target = resolve_workspace_path(workspace_root, subpath, rel_path)
    clean_rel = str(target.relative_to(ws_root)).replace("\\", "/")

    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    args.extend(["--", clean_rel])
    rc, out, err = _run_git(ws_root, args)
    if rc != 0:
        raise HTTPException(status_code=400, detail=err.strip() or "git diff failed")

    if not out and not staged and target.is_file():
        # Untracked file preview as unified diff additions
        rc_ls, ls_out, _ = _run_git(ws_root, ["ls-files", "--others", "--exclude-standard", "--", clean_rel])
        if rc_ls == 0 and ls_out.strip():
            file_data = read_workspace_file(workspace_root, subpath, clean_rel)
            if file_data.is_binary:
                out = f"--- /dev/null\n+++ b/{clean_rel}\n@@ -0,0 +1 @@\n+[Binary file ({file_data.size_bytes} bytes)]"
            else:
                lines = file_data.content.splitlines()
                body = "\n".join(f"+{ln}" for ln in lines)
                out = f"--- /dev/null\n+++ b/{clean_rel}\n@@ -0,0 +1,{len(lines)} @@\n{body}"

    raw_b = out.encode("utf-8", errors="replace")
    truncated = len(raw_b) > MAX_DIFF_BYTES
    if truncated:
        out = raw_b[:MAX_DIFF_BYTES].decode("utf-8", errors="replace")

    return GitDiffResponse(path=clean_rel, staged=staged, diff=out, truncated=truncated)


def _validate_rel_paths(workspace_root: str, subpath: str, paths: list[str]) -> tuple[Path, list[str]]:
    if not paths:
        raise HTTPException(status_code=400, detail="At least one path is required")
    ws_root, _ = resolve_workspace_path(workspace_root, subpath, ".")
    clean: list[str] = []
    for p in paths:
        if not p.strip():
            continue
        _, target = resolve_workspace_path(workspace_root, subpath, p)
        clean.append(str(target.relative_to(ws_root)).replace("\\", "/"))
    if not clean:
        raise HTTPException(status_code=400, detail="No valid paths provided")
    return ws_root, clean


def git_stage_paths(workspace_root: str, subpath: str, paths: list[str]) -> GitActionResponse:
    ws_root, clean = _validate_rel_paths(workspace_root, subpath, paths)
    rc, out, err = _run_git(ws_root, ["add", "--", *clean])
    return GitActionResponse(ok=(rc == 0), output=out.strip(), error=(err.strip() if rc != 0 else None))


def git_unstage_paths(workspace_root: str, subpath: str, paths: list[str]) -> GitActionResponse:
    ws_root, clean = _validate_rel_paths(workspace_root, subpath, paths)
    rc, out, err = _run_git(ws_root, ["restore", "--staged", "--", *clean])
    if rc != 0:
        rc, out, err = _run_git(ws_root, ["reset", "HEAD", "--", *clean])
    return GitActionResponse(ok=(rc == 0), output=out.strip(), error=(err.strip() if rc != 0 else None))


def git_discard_paths(workspace_root: str, subpath: str, paths: list[str]) -> GitActionResponse:
    ws_root, clean = _validate_rel_paths(workspace_root, subpath, paths)
    for rel in clean:
        target = (ws_root / rel).resolve()
        rc_ls, ls_out, _ = _run_git(ws_root, ["ls-files", "--others", "--exclude-standard", "--", rel])
        if rc_ls == 0 and ls_out.strip() and target.is_file():
            target.unlink()
            continue
        rc, _, err = _run_git(ws_root, ["checkout", "--", rel])
        if rc != 0:
            return GitActionResponse(ok=False, error=err.strip() or f"Failed to discard {rel}")
    return GitActionResponse(ok=True, output=f"Discarded {len(clean)} path(s)")


def git_commit(workspace_root: str, subpath: str, message: str) -> GitActionResponse:
    msg = message.strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Commit message cannot be empty")
    ws_root, _ = resolve_workspace_path(workspace_root, subpath, ".")
    rc, out, err = _run_git(ws_root, ["commit", "-m", msg], timeout=15.0)
    if rc != 0:
        return GitActionResponse(ok=False, output=out.strip(), error=err.strip() or "git commit failed")
    _, sha, _ = _run_git(ws_root, ["rev-parse", "--short", "HEAD"])
    return GitActionResponse(ok=True, output=out.strip(), commit_sha=sha.strip())
