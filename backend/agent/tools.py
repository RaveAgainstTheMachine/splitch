"""
Workspace-sandboxed filesystem & shell tool execution for `splitch-agent`.

Security invariants:
  - Every filesystem path is resolved and confined to `workspace_root` via `Path.is_relative_to`.
  - `write_file` and `edit_file` run `_run_post_edit_verification` for immediate syntax/lint feedback.
  - `run_command` executes in an OS-level sandbox when available (`bwrap` on Linux with
    read-only root + writable workspace bind + unshared network; `sandbox-exec` Seatbelt on macOS),
    inside an isolated process group (`start_new_session=True`), with `os.killpg(..., SIGKILL)`
    reaping on timeout or 4 MB flood and a strict 64 KB stdout RAM cap.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import os
import py_compile
import re
import shutil
import signal
import subprocess
import sys
import tomllib
from pathlib import Path

from fastapi import HTTPException

from backend.shared.models import ToolExecRequest, ToolExecResponse, WorkspaceDirListing

IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "target"}
MUTATING_WORKSPACE_TOOLS = frozenset({"write_file", "edit_file", "run_command"})

MAX_CMD_STDOUT_BYTES = 64 * 1024  # 64 KB hard RAM cap
MAX_CMD_DRAIN_BYTES = 4 * 1024 * 1024  # 4 MB runaway flood fuse
MAX_CMD_OUTPUT_CHARS = 12_000

_BWRAP_PROBE_CACHE: bool | None = None


def resolve_workspace_path(
    workspace_root: str, subpath: str, rel: str = "."
) -> tuple[Path, Path]:
    base_root = Path(workspace_root).resolve()
    ws_root = (base_root / subpath.lstrip("/")).resolve() if subpath.strip() else base_root
    if not ws_root.is_relative_to(base_root):
        raise HTTPException(status_code=403, detail="Workspace subpath outside allowed root")
    target = (ws_root / rel.lstrip("/")).resolve() if rel and rel != "." else ws_root
    if not target.is_relative_to(ws_root):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    return ws_root, target


def run_post_edit_verification(target: Path, ws_root: Path) -> dict[str, str | bool]:
    """Fast (<3s) syntax/lint verification harness triggered after `write_file` / `edit_file`."""
    ext = target.suffix.lower()
    rel_str = str(target.relative_to(ws_root)) if target.is_relative_to(ws_root) else target.name

    if ext == ".py":
        try:
            py_compile.compile(str(target), doraise=True)
        except py_compile.PyCompileError as exc:
            return {
                "checked": True,
                "ok": False,
                "checker": "py_compile",
                "output": str(exc.msg)[:600],
            }
        ruff_bin = shutil.which("ruff")
        if ruff_bin:
            try:
                proc = subprocess.run(
                    [ruff_bin, "check", "--output-format=concise", str(target)],
                    cwd=str(ws_root),
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
                if proc.returncode != 0:
                    diag = (proc.stdout or proc.stderr).strip()[:600]
                    return {"checked": True, "ok": False, "checker": "ruff", "output": diag}
                return {
                    "checked": True,
                    "ok": True,
                    "checker": "py_compile+ruff",
                    "output": f"{rel_str} syntax & ruff lint clean",
                }
            except (OSError, subprocess.TimeoutExpired):
                pass
        return {
            "checked": True,
            "ok": True,
            "checker": "py_compile",
            "output": f"{rel_str} Python syntax valid",
        }

    if ext in (".sh", ".bash"):
        shell_bin = "bash" if ext == ".bash" and shutil.which("bash") else "sh"
        try:
            proc = subprocess.run(
                [shell_bin, "-n", str(target)],
                cwd=str(ws_root),
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if proc.returncode != 0:
                return {
                    "checked": True,
                    "ok": False,
                    "checker": f"{shell_bin} -n",
                    "output": (proc.stderr or proc.stdout).strip()[:600],
                }
            return {
                "checked": True,
                "ok": True,
                "checker": f"{shell_bin} -n",
                "output": f"{rel_str} shell syntax valid",
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"checked": True, "ok": False, "checker": shell_bin, "output": str(exc)[:300]}

    if ext == ".json":
        try:
            json.loads(target.read_text(encoding="utf-8"))
            return {"checked": True, "ok": True, "checker": "json", "output": f"{rel_str} valid JSON"}
        except ValueError as exc:
            return {"checked": True, "ok": False, "checker": "json", "output": str(exc)[:400]}

    if ext == ".toml":
        try:
            tomllib.loads(target.read_text(encoding="utf-8"))
            return {"checked": True, "ok": True, "checker": "tomllib", "output": f"{rel_str} valid TOML"}
        except ValueError as exc:
            return {"checked": True, "ok": False, "checker": "tomllib", "output": str(exc)[:400]}

    if ext in (".js", ".mjs", ".cjs") and shutil.which("node"):
        try:
            proc = subprocess.run(
                ["node", "--check", str(target)],
                cwd=str(ws_root),
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if proc.returncode != 0:
                return {
                    "checked": True,
                    "ok": False,
                    "checker": "node --check",
                    "output": (proc.stderr or proc.stdout).strip()[:600],
                }
            return {
                "checked": True,
                "ok": True,
                "checker": "node --check",
                "output": f"{rel_str} JS syntax valid",
            }
        except (OSError, subprocess.TimeoutExpired):
            pass

    return {"checked": False, "ok": True, "checker": "none", "output": ""}


def exec_fs_tool(req: ToolExecRequest, workspace_root: str) -> ToolExecResponse:
    ws_root, _ = resolve_workspace_path(workspace_root, req.workspace_subpath)
    args = req.arguments

    if req.tool == "list_dir":
        _, target = resolve_workspace_path(
            workspace_root, req.workspace_subpath, str(args.get("path", "."))
        )
        if not target.exists() or not target.is_dir():
            return ToolExecResponse(ok=False, output=f"Directory not found: {target}")
        entries = []
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if item.name in IGNORED_DIRS:
                continue
            suffix = "/" if item.is_dir() else f" ({item.stat().st_size} B)"
            entries.append(f"{item.name}{suffix}")
            if len(entries) >= 150:
                entries.append("... (truncated)")
                break
        return ToolExecResponse(ok=True, output="\n".join(entries) or "(empty directory)")

    if req.tool == "read_file":
        rel = str(args.get("path", ""))
        if not rel:
            return ToolExecResponse(ok=False, output="Missing 'path' argument")
        _, target = resolve_workspace_path(workspace_root, req.workspace_subpath, rel)
        if not target.is_file():
            return ToolExecResponse(ok=False, output=f"File not found: {rel}")
        raw_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = min(len(raw_lines), int(args.get("end_line") or (start + 249)))
        numbered = [f"{i}: {raw_lines[i - 1]}" for i in range(start, end + 1)]
        text = "\n".join(numbered)
        if len(text) > 30_000:
            text = text[:30_000] + "\n... (truncated)"
        return ToolExecResponse(ok=True, output=text or "(empty file)")

    if req.tool == "write_file":
        rel = str(args.get("path", ""))
        content = str(args.get("content", ""))
        if not rel:
            return ToolExecResponse(ok=False, output="Missing 'path' argument")
        _, target = resolve_workspace_path(workspace_root, req.workspace_subpath, rel)
        old_text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        diff_lines = list(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            )
        )
        diff_str = "".join(diff_lines)[:12_000] if diff_lines else "(no diff)"
        ver = run_post_edit_verification(target, ws_root)
        ver_note = ""
        if ver.get("checked"):
            mark = "✓" if ver.get("ok") else "✗"
            ver_note = f"\n[post-edit verification ({ver['checker']}): {mark} {ver['output']}]"
        return ToolExecResponse(
            ok=True,
            output=f"Wrote {len(content)} bytes to {rel}{ver_note}",
            diff=diff_str,
            verification=ver,
        )

    if req.tool == "edit_file":
        rel = str(args.get("path", ""))
        target_str = str(args.get("target", ""))
        replacement_str = str(args.get("replacement", ""))
        if not rel or not target_str:
            return ToolExecResponse(ok=False, output="Missing 'path' or 'target' argument")
        _, target = resolve_workspace_path(workspace_root, req.workspace_subpath, rel)
        if not target.is_file():
            return ToolExecResponse(ok=False, output=f"File not found: {rel}")
        old_text = target.read_text(encoding="utf-8", errors="replace")
        if target_str not in old_text:
            return ToolExecResponse(
                ok=False,
                output=f"Target string not found in {rel}. Read the file first to verify exact lines.",
            )
        count = old_text.count(target_str)
        new_text = old_text.replace(target_str, replacement_str)
        target.write_text(new_text, encoding="utf-8")
        diff_lines = list(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            )
        )
        diff_str = "".join(diff_lines)[:12_000] if diff_lines else "(no diff)"
        ver = run_post_edit_verification(target, ws_root)
        ver_note = ""
        if ver.get("checked"):
            mark = "✓" if ver.get("ok") else "✗"
            ver_note = f"\n[post-edit verification ({ver['checker']}): {mark} {ver['output']}]"
        return ToolExecResponse(
            ok=True,
            output=f"Replaced {count} occurrence(s) in {rel}{ver_note}",
            diff=diff_str,
            verification=ver,
        )

    if req.tool == "grep_search":
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolExecResponse(ok=False, output="Missing 'query' argument")
        _, target = resolve_workspace_path(
            workspace_root, req.workspace_subpath, str(args.get("path", "."))
        )
        matches: list[str] = []
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        for root_dir, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
            for fname in files:
                fpath = Path(root_dir) / fname
                try:
                    if fpath.stat().st_size > 512_000:
                        continue
                    for idx, line in enumerate(
                        fpath.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
                    ):
                        if pattern.search(line):
                            rel_p = fpath.relative_to(ws_root)
                            matches.append(f"{rel_p}:{idx}: {line.strip()[:160]}")
                            if len(matches) >= 40:
                                return ToolExecResponse(
                                    ok=True, output="\n".join(matches) + "\n... (capped at 40)"
                                )
                except OSError:
                    continue
        return ToolExecResponse(ok=True, output="\n".join(matches) or "No matches found.")

    return ToolExecResponse(ok=False, output=f"Unsupported filesystem tool: {req.tool}")


def list_workspace_dirs(workspace_root: str, subpath: str) -> WorkspaceDirListing:
    base_root = Path(workspace_root).resolve()
    ws_root, _ = resolve_workspace_path(workspace_root, subpath, ".")
    if not ws_root.exists() or not ws_root.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {subpath or '.'}")
    rel_sub = "" if ws_root == base_root else str(ws_root.relative_to(base_root))
    parent: str | None = None
    if ws_root != base_root:
        p = ws_root.parent
        parent = "" if p == base_root else str(p.relative_to(base_root))
    dirs: list[str] = []
    for item in sorted(ws_root.iterdir(), key=lambda entry: entry.name.lower()):
        if item.name.startswith(".") or item.name in IGNORED_DIRS:
            continue
        with contextlib.suppress(OSError):
            if item.is_dir():
                dirs.append(item.name)
                if len(dirs) >= 200:
                    break
    return WorkspaceDirListing(root=str(base_root), subpath=rel_sub, parent=parent, dirs=dirs)


# ── OS-Level Command Sandbox (`bwrap` on Linux, Seatbelt `sandbox-exec` on macOS) ──


def _probe_bwrap_works() -> bool:
    global _BWRAP_PROBE_CACHE
    if _BWRAP_PROBE_CACHE is not None:
        return _BWRAP_PROBE_CACHE
    bwrap = shutil.which("bwrap")
    if not bwrap or not sys.platform.startswith("linux"):
        _BWRAP_PROBE_CACHE = False
        return False
    try:
        proc = subprocess.run(
            [
                bwrap,
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
                "--unshare-net",
                "--",
                "/bin/sh",
                "-c",
                "true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
        )
        _BWRAP_PROBE_CACHE = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        _BWRAP_PROBE_CACHE = False
    return _BWRAP_PROBE_CACHE


def _seatbelt_escape(path_str: str) -> str:
    return path_str.replace("\\", "\\\\").replace('"', '\\"')


def build_sandboxed_command(
    cmd: str,
    ws_root: Path,
    cwd: Path,
    *,
    mode: str = "auto",
    allow_net: bool = False,
) -> tuple[list[str] | None, str]:
    """
    Build an OS-sandboxed command argv for `run_command`.
    Returns `(argv, sandbox_label)` where `argv is None` signals fallback to host shell.
    Raises `RuntimeError` if an explicit sandbox (`bwrap` / `seatbelt`) was requested but unavailable.
    """
    cleaned_mode = (mode or "auto").strip().lower()
    if cleaned_mode in ("none", "off", "host", "disabled"):
        return None, "host"

    if cleaned_mode == "bwrap" or (cleaned_mode == "auto" and sys.platform.startswith("linux")):
        bwrap = shutil.which("bwrap")
        if bwrap and (cleaned_mode == "bwrap" or _probe_bwrap_works()):
            argv = [
                bwrap,
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
                "--bind",
                str(ws_root),
                str(ws_root),
                "--chdir",
                str(cwd),
                "--die-with-parent",
            ]
            if not allow_net:
                argv.append("--unshare-net")
            argv.extend(["--", "/bin/sh", "-c", cmd])
            return argv, "bwrap"
        if cleaned_mode == "bwrap":
            raise RuntimeError("SPLITCH_CMD_SANDBOX=bwrap requested but `bwrap` is not installed")

    if cleaned_mode == "seatbelt" or (cleaned_mode == "auto" and sys.platform == "darwin"):
        sb_exec = shutil.which("sandbox-exec") or (
            "/usr/bin/sandbox-exec" if Path("/usr/bin/sandbox-exec").is_file() else None
        )
        if sb_exec:
            ws_esc = _seatbelt_escape(str(ws_root))
            net_rule = "" if allow_net else "(deny network-outbound)\n"
            profile = (
                "(version 1)\n"
                "(allow default)\n"
                "(deny file-write*)\n"
                f'(allow file-write* (subpath "{ws_esc}") '
                '(subpath "/tmp") (subpath "/private/tmp") (literal "/dev/null"))\n'
                f"{net_rule}"
            )
            return [sb_exec, "-p", profile, "/bin/sh", "-c", cmd], "seatbelt"
        if cleaned_mode == "seatbelt":
            raise RuntimeError(
                "SPLITCH_CMD_SANDBOX=seatbelt requested but `/usr/bin/sandbox-exec` is unavailable"
            )

    return None, "host"


def kill_proc_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
            return
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


async def run_sandboxed_shell_command(
    cmd: str,
    ws_root: Path,
    cwd: Path,
    *,
    timeout_s: float = 20.0,
    sandbox_mode: str = "auto",
    allow_net: bool = False,
) -> ToolExecResponse:
    try:
        argv, sandbox_label = build_sandboxed_command(
            cmd, ws_root, cwd, mode=sandbox_mode, allow_net=allow_net
        )
    except RuntimeError as exc:
        return ToolExecResponse(ok=False, output=str(exc), exit_code=1, sandbox="error")

    proc: asyncio.subprocess.Process | None = None
    buf = bytearray()
    truncated = False
    flood_killed = False
    try:
        if argv is not None:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
            )
        assert proc.stdout is not None

        async def _stream_bounded() -> None:
            nonlocal truncated, flood_killed
            total_read = 0
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                total_read += len(chunk)
                remaining = MAX_CMD_STDOUT_BYTES - len(buf)
                if remaining > 0:
                    buf.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                if total_read > MAX_CMD_DRAIN_BYTES:
                    flood_killed = True
                    kill_proc_tree(proc)
                    break
            await proc.wait()

        await asyncio.wait_for(_stream_bounded(), timeout=timeout_s)
        text = buf.decode("utf-8", errors="replace")
        if len(text) > MAX_CMD_OUTPUT_CHARS:
            text = text[:MAX_CMD_OUTPUT_CHARS]
            truncated = True
        if flood_killed:
            text += "\n... (output truncated; process terminated after exceeding 4 MB output limit)"
        elif truncated:
            text += "\n... (output truncated)"
        code = proc.returncode if proc.returncode is not None else -1
        return ToolExecResponse(
            ok=(code == 0 and not flood_killed),
            output=text or "(no output)",
            exit_code=code,
            sandbox=sandbox_label,
        )
    except TimeoutError:
        if proc is not None:
            kill_proc_tree(proc)
            with contextlib.suppress(TimeoutError, OSError):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
        return ToolExecResponse(
            ok=False,
            output=f"Command timed out after {int(timeout_s)}s",
            exit_code=124,
            sandbox=sandbox_label,
        )
    except OSError as exc:
        return ToolExecResponse(ok=False, output=str(exc), exit_code=1, sandbox=sandbox_label)
