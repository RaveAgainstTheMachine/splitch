"""
Splitch Environment Doctor & Self-Updater.

Powers:
  - `splitch doctor [--json]` and `GET /api/system/doctor`
  - `splitch update [--check] [--agent-only] [--rebuild-ui] [--no-restart] [--force]`
  - `GET /api/system/update-status`
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from backend.router.config import default_data_root
from backend.shared.engines import (
    EngineBackend,
    EngineTarget,
    get_engine_status,
    install_or_update_engine,
    read_engine_meta,
    read_mlx_engine_meta,
    read_sd_engine_meta,
)
from backend.shared.progress import TerminalProgress, null_step
from backend.version import __version__ as VERSION

CheckStatus = Literal["ok", "warn", "error", "info"]

DEFAULT_REPO_URL = "https://github.com/RaveAgainstTheMachine/splitch.git"
LEGACY_PLACEHOLDER_REPO_URLS = frozenset(
    {
        "https://github.com/splitch-ai/splitch.git",
        "https://github.com/splitch/splitch.git",
    }
)


class DoctorCheck(BaseModel):
    name: str
    status: CheckStatus
    summary: str
    detail: str | None = None
    fix: str | None = None


class DoctorReport(BaseModel):
    version: str = VERSION
    platform: str
    arch: str
    app_dir: str
    data_root: str
    install_mode: str = "source"
    ok: bool = True
    checks: list[DoctorCheck] = Field(default_factory=list)


class UpdateStatus(BaseModel):
    version: str = VERSION
    app_dir: str
    install_mode: str = "source"
    is_git_repo: bool = False
    branch: str = ""
    local_sha: str = ""
    remote_sha: str = ""
    remote_version: str = ""
    dirty: bool = False
    behind_count: int = 0
    ahead_count: int = 0
    update_available: bool = False
    recent_remote_commits: list[str] = Field(default_factory=list)
    error: str | None = None


class UpdateResult(BaseModel):
    ok: bool = True
    updated: bool = False
    rolled_back: bool = False
    previous_sha: str = ""
    current_sha: str = ""
    commits_pulled: int = 0
    ui_rebuilt: bool = False
    restarted_services: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    error: str | None = None


def _meta_path() -> Path:
    return default_data_root() / "install_meta.json"


def read_install_meta() -> dict:
    p = _meta_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_install_meta(meta: dict) -> None:
    p = _meta_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def resolve_app_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    meta = read_install_meta()
    raw = str(meta.get("install_dir") or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if (candidate / "pyproject.toml").exists():
            return candidate.resolve()
    return Path(__file__).resolve().parents[2]


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 20.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"Timed out after {timeout:.0f}s: {' '.join(cmd)}"


def _find_node_bin() -> tuple[str | None, str | None, str]:
    """Return (node_bin, npm_bin, version_str), checking PATH and Splitch's managed Node."""
    managed_bin = default_data_root() / "node" / "bin"
    candidates = [
        (shutil.which("node"), shutil.which("npm")),
        (
            str(managed_bin / "node") if (managed_bin / "node").exists() else None,
            str(managed_bin / "npm") if (managed_bin / "npm").exists() else None,
        ),
    ]
    for node_path, npm_path in candidates:
        if not node_path:
            continue
        code, out, _ = _run([node_path, "--version"], timeout=5.0)
        if code == 0 and out:
            return node_path, npm_path, out.lstrip("v")
    return None, None, ""


def _parse_major(ver: str) -> int:
    try:
        return int(ver.split(".", 1)[0])
    except ValueError:
        return 0


def _detect_gpu_summary() -> DoctorCheck:
    if sys.platform == "darwin" and platform.machine().lower() in ("arm64", "aarch64"):
        code, chip, _ = _run(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=3.0)
        return DoctorCheck(
            name="accelerator",
            status="ok",
            summary=f"Apple Silicon ({chip or 'arm64'})",
            detail="Unified Memory telemetry via sysctl/vm_stat",
        )
    if shutil.which("nvidia-smi"):
        code, out, _ = _run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            timeout=4.0,
        )
        if code == 0 and out:
            first = out.splitlines()[0].strip()
            return DoctorCheck(
                name="accelerator",
                status="ok",
                summary=f"NVIDIA CUDA ({first})",
                detail="Live Gaming-Mode VRAM preemption supported via nvidia-smi pmon",
            )
    if shutil.which("rocm-smi"):
        return DoctorCheck(
            name="accelerator",
            status="ok",
            summary="AMD ROCm GPU detected",
            detail="Telemetry supported via rocm-smi",
        )
    return DoctorCheck(
        name="accelerator",
        status="info",
        summary="CPU-only host (no nvidia-smi / rocm-smi / Apple Silicon detected)",
        detail="Splitch runs in CPU or router-only mode without a discrete GPU",
    )


def _probe_local_engines() -> DoctorCheck:
    probes = [
        ("llama-server", "http://127.0.0.1:8081", "/models"),
        ("Ollama", "http://127.0.0.1:11434", "/api/tags"),
        ("LM Studio", "http://127.0.0.1:1234", "/v1/models"),
        ("vLLM", "http://127.0.0.1:8000", "/v1/models"),
    ]
    found: list[str] = []
    for label, base, path in probes:
        try:
            r = httpx.get(f"{base}{path}", timeout=0.6)
            if r.status_code == 200:
                found.append(f"{label} ({base})")
        except httpx.HTTPError:
            continue
    if found:
        return DoctorCheck(
            name="local_engine",
            status="ok",
            summary=f"Reachable: {', '.join(found)}",
        )
    return DoctorCheck(
        name="local_engine",
        status="warn",
        summary="No local inference backend detected on default ports (8081/11434/1234/8000)",
        fix="Start llama-server, Ollama, LM Studio, or configure remote nodes in splitch.toml",
    )


def _probe_local_media_engines(installed_sd_path: str | None) -> DoctorCheck:
    probes = [
        ("sd-server", "http://127.0.0.1:8082", "/v1/models"),
        ("ComfyUI", "http://127.0.0.1:8188", "/system_stats"),
    ]
    found: list[str] = []
    for label, base, path in probes:
        try:
            r = httpx.get(f"{base}{path}", timeout=0.6)
            if r.status_code == 200:
                found.append(f"{label} ({base})")
        except httpx.HTTPError:
            continue
    if found:
        return DoctorCheck(
            name="media_engine",
            status="ok",
            summary=f"Reachable: {', '.join(found)}",
            detail=installed_sd_path,
        )
    if installed_sd_path:
        sd_cli_sibling = Path(installed_sd_path).parent / (
            "sd-cli.exe" if sys.platform == "win32" else "sd-cli"
        )
        vid_note = " + sd-cli video ready" if sd_cli_sibling.is_file() else ""
        return DoctorCheck(
            name="media_engine",
            status="info",
            summary=f"sd.cpp binary installed{vid_note} (start from Media Studio)",
            detail=installed_sd_path,
        )
    return DoctorCheck(
        name="media_engine",
        status="info",
        summary="Optional image/video engine not installed (sd-server / ComfyUI)",
        fix="Click 'Install sd-server' in Media Studio UI, or run `splitch engine install sd.cpp`",
    )


def run_doctor(
    app_dir: Path | None = None,
    *,
    check_engine_updates: bool = False,
) -> DoctorReport:
    resolved_app = resolve_app_dir(app_dir)
    meta = read_install_meta()
    data_root = default_data_root()
    checks: list[DoctorCheck] = []

    # 1. Python
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 11)
    checks.append(
        DoctorCheck(
            name="python",
            status="ok" if py_ok else "error",
            summary=f"Python {py_ver}",
            detail=sys.executable,
            fix=None if py_ok else "Install Python 3.11+ or run install.sh to bootstrap via uv",
        )
    )

    # 2. uv
    uv_bin = shutil.which("uv")
    if uv_bin:
        _, uv_ver, _ = _run([uv_bin, "--version"], timeout=4.0)
        checks.append(DoctorCheck(name="uv", status="ok", summary=uv_ver or "uv installed", detail=uv_bin))
    else:
        checks.append(
            DoctorCheck(
                name="uv",
                status="warn",
                summary="uv not found on PATH",
                fix="Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh",
            )
        )

    # 3. Git / Update capability
    git_bin = shutil.which("git")
    if git_bin and (resolved_app / ".git").exists():
        _, branch, _ = _run([git_bin, "rev-parse", "--abbrev-ref", "HEAD"], cwd=resolved_app)
        _, sha, _ = _run([git_bin, "rev-parse", "--short", "HEAD"], cwd=resolved_app)
        _, dirty_out, _ = _run([git_bin, "status", "--porcelain"], cwd=resolved_app)
        state = "dirty" if dirty_out else "clean"
        checks.append(
            DoctorCheck(
                name="git",
                status="ok" if not dirty_out else "info",
                summary=f"{branch}@{sha} ({state})",
                detail=str(resolved_app),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="git",
                status="info",
                summary="Non-git source installation" if git_bin else "git not installed",
                detail=str(resolved_app),
                fix="Re-run install.sh to update non-git installations",
            )
        )

    # 4. Node.js & Dashboard bundle
    agent_only = bool(meta.get("agent_only", False))
    node_bin, npm_bin, node_ver = _find_node_bin()
    if agent_only:
        checks.append(
            DoctorCheck(
                name="nodejs",
                status="info",
                summary="Skipped (installed in agent-only mode)",
            )
        )
    elif node_bin and _parse_major(node_ver) >= 20 and npm_bin:
        checks.append(
            DoctorCheck(name="nodejs", status="ok", summary=f"Node.js v{node_ver}", detail=node_bin)
        )
    else:
        checks.append(
            DoctorCheck(
                name="nodejs",
                status="warn",
                summary=f"Node.js v{node_ver} (< 20)" if node_ver else "Node.js 20+ not found",
                fix="Run install.sh to bootstrap a standalone Node.js LTS runtime for dashboard builds",
            )
        )

    dist_index = resolved_app / "dashboard" / "dist" / "index.html"
    if dist_index.exists():
        checks.append(
            DoctorCheck(
                name="dashboard_dist",
                status="ok",
                summary="Built dashboard bundle present",
                detail=str(dist_index),
            )
        )
    elif agent_only:
        checks.append(
            DoctorCheck(
                name="dashboard_dist",
                status="info",
                summary="Not built (agent-only node)",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="dashboard_dist",
                status="warn",
                summary="dashboard/dist/index.html missing",
                fix=f"Build UI: (cd {resolved_app / 'dashboard'} && npm install && npm run build)",
            )
        )

    # 5. Accelerator, Installed Engine Binary & Reachable Local Engine
    checks.append(_detect_gpu_summary())
    eng_status = get_engine_status(check_remote=check_engine_updates)
    if eng_status.installed:
        primary = eng_status.installed[0]
        parts = [f"{e.engine} ({e.build_tag or e.version or 'installed'})" for e in eng_status.installed]
        if eng_status.update_available and eng_status.latest_llama_tag:
            checks.append(
                DoctorCheck(
                    name="engine_binary",
                    status="warn",
                    summary=f"{', '.join(parts)} — update available ({eng_status.latest_llama_tag})",
                    detail=primary.path,
                    fix="Run: splitch engine update",
                )
            )
        else:
            tag_note = (
                f" • latest: {eng_status.latest_llama_tag}"
                if eng_status.latest_llama_tag
                else ""
            )
            checks.append(
                DoctorCheck(
                    name="engine_binary",
                    status="ok",
                    summary=f"{', '.join(parts)}{tag_note}",
                    detail=primary.path,
                )
            )
    else:
        checks.append(
            DoctorCheck(
                name="engine_binary",
                status="warn",
                summary=(
                    f"No local inference engine binary found (recommended backend: "
                    f"{eng_status.recommended_backend})"
                ),
                fix="Run: splitch engine install llama.cpp  (or `splitch doctor --fix`)",
            )
        )
    checks.append(_probe_local_engines())
    sd_inst = next((e for e in eng_status.installed if e.engine == "sd.cpp"), None)
    checks.append(_probe_local_media_engines(sd_inst.path if sd_inst else None))

    # 6. Data dir & SQLite WAL
    data_dir = Path(os.getenv("SPLITCH_DATA_DIR", str(data_root / "data")))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "chats.db"
        if db_path.exists():
            with sqlite3.connect(db_path, timeout=2.0) as conn:
                conv_cnt = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            checks.append(
                DoctorCheck(
                    name="data_dir",
                    status="ok",
                    summary=f"Writable ({conv_cnt} conversations in SQLite)",
                    detail=str(data_dir),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="data_dir",
                    status="ok",
                    summary="Writable (fresh data directory)",
                    detail=str(data_dir),
                )
            )
    except (OSError, sqlite3.Error) as exc:
        checks.append(
            DoctorCheck(
                name="data_dir",
                status="error",
                summary=f"Data directory error: {exc}",
                detail=str(data_dir),
            )
        )

    # 7. Security & TLS posture summary
    api_key_set = bool(
        os.getenv("SPLITCH_API_KEY", "").strip() or os.getenv("SPLITCH_API_KEY_HASH", "").strip()
    )
    agent_tok_set = bool(
        os.getenv("SPLITCH_AGENT_TOKEN", "").strip()
        or os.getenv("SPLITCH_ROUTER_PUBLIC_KEY", "").strip()
        or os.getenv("SPLITCH_ROUTER_PRIVATE_KEY", "").strip()
        or (data_dir / "router_ed25519.pem").is_file()
    )
    tools_on = os.getenv("SPLITCH_ENABLE_TOOLS", "0").strip().lower() in ("1", "true", "yes")
    tls_configured = bool(
        os.getenv("SPLITCH_TLS_CERT", "").strip()
        or os.getenv("SPLITCH_LETSENCRYPT_DOMAIN", "").strip()
        or os.getenv("SPLITCH_TLS_SELF_SIGNED", "").strip().lower() in ("1", "true", "yes")
    )
    tls_label = "ON" if tls_configured else "off (HTTP / external proxy)"
    sec_status: CheckStatus = "warn" if (tools_on and not (api_key_set or agent_tok_set)) else "ok"
    checks.append(
        DoctorCheck(
            name="security",
            status=sec_status,
            summary=(
                f"API Key: {'ON' if api_key_set else 'off'} • "
                f"Agent Token: {'ON' if agent_tok_set else 'off'} • "
                f"TLS: {tls_label} • "
                f"Workspace Tools: {'ON' if tools_on else 'off'}"
            ),
            fix=(
                "Set SPLITCH_API_KEY & SPLITCH_AGENT_TOKEN before exposing a tools-enabled node beyond localhost"
                if sec_status == "warn"
                else None
            ),
        )
    )

    # 8. Desktop / Tray router TLS & reachability (if desktop.json exists)
    if (data_root / "desktop.json").is_file():
        with contextlib.suppress(ImportError, OSError, ValueError):
            import ssl

            import httpx

            from backend.desktop.shortcuts import (
                _check_router_healthy,
                _resolve_ca_bundle,
                load_desktop_config,
            )

            dcfg = load_desktop_config()
            rurl = dcfg.router_url.rstrip("/")
            if _check_router_healthy(rurl, timeout=1.5):
                checks.append(
                    DoctorCheck(
                        name="desktop_router",
                        status="ok",
                        summary=f"Desktop router reachable ({rurl})",
                    )
                )
            elif rurl.startswith("http://") and _check_router_healthy(
                "https://" + rurl.removeprefix("http://"), timeout=1.5
            ):
                https_url = "https://" + rurl.removeprefix("http://")
                checks.append(
                    DoctorCheck(
                        name="desktop_router",
                        status="warn",
                        summary=f"desktop.json uses http:// while router serves {https_url}",
                        fix="Run: splitch doctor --fix (auto-upgrades desktop.json to https://)",
                    )
                )
            else:
                tls_untrusted = False
                if rurl.startswith("https://"):
                    try:
                        httpx.get(f"{rurl}/health", timeout=1.5, verify=_resolve_ca_bundle())
                    except httpx.HTTPError as exc:
                        if "CERTIFICATE_VERIFY_FAILED" in str(exc) or isinstance(
                            getattr(exc, "__cause__", None), ssl.SSLCertVerificationError
                        ):
                            tls_untrusted = True
                checks.append(
                    DoctorCheck(
                        name="desktop_router",
                        status="warn",
                        summary=(
                            f"Desktop router TLS cert untrusted ({rurl})"
                            if tls_untrusted
                            else f"Desktop router unreachable ({rurl})"
                        ),
                        fix=(
                            f"Copy cluster-ca.pem to {data_root / 'certs' / 'cluster-ca.pem'} or set SPLITCH_CA_BUNDLE"
                            if tls_untrusted
                            else "Ensure the target router is running or update router_url via `splitch desktop install --url ...`"
                        ),
                    )
                )

    overall_ok = all(c.status != "error" for c in checks)
    return DoctorReport(
        version=VERSION,
        platform=platform.system(),
        arch=platform.machine(),
        app_dir=str(resolved_app),
        data_root=str(data_root),
        install_mode=str(meta.get("mode") or "source"),
        ok=overall_ok,
        checks=checks,
    )


def _resolve_update_remote(resolved: Path, git_bin: str, meta: dict) -> tuple[str | None, str | None]:
    """Return (remote_name, error) wired to the GitHub repository (or configured upstream)."""
    override = os.getenv("SPLITCH_UPDATE_REMOTE", "").strip()
    if override:
        rc, _, _ = _run([git_bin, "remote", "get-url", override], cwd=resolved)
        if rc == 0:
            return override, None
        return None, f"Configured SPLITCH_UPDATE_REMOTE='{override}' does not exist"

    # If a dedicated 'github' remote exists (e.g., dual-remote dev workspace), prefer it
    rc_gh, gh_url, _ = _run([git_bin, "remote", "get-url", "github"], cwd=resolved)
    if rc_gh == 0 and gh_url:
        return "github", None

    rc_rem, origin_url, _ = _run([git_bin, "remote", "get-url", "origin"], cwd=resolved)
    if rc_rem != 0:
        target_url = str(meta.get("repo_url") or "").strip()
        if not target_url or target_url in LEGACY_PLACEHOLDER_REPO_URLS:
            target_url = DEFAULT_REPO_URL
        rc_add, _, err_add = _run([git_bin, "remote", "add", "origin", target_url], cwd=resolved)
        if rc_add != 0:
            return None, f"No 'origin' git remote configured ({err_add})"
        return "origin", None

    if origin_url in LEGACY_PLACEHOLDER_REPO_URLS:
        _run([git_bin, "remote", "set-url", "origin", DEFAULT_REPO_URL], cwd=resolved)
        if meta and meta.get("repo_url") in LEGACY_PLACEHOLDER_REPO_URLS:
            meta["repo_url"] = DEFAULT_REPO_URL
            write_install_meta(meta)

    return "origin", None


def check_update(
    app_dir: Path | None = None,
    branch: str | None = None,
    fetch_remote: bool = True,
) -> UpdateStatus:
    resolved = resolve_app_dir(app_dir)
    meta = read_install_meta()
    git_bin = shutil.which("git")
    is_git = bool(git_bin and (resolved / ".git").exists())

    status = UpdateStatus(
        version=VERSION,
        app_dir=str(resolved),
        install_mode=str(meta.get("mode") or "source"),
        is_git_repo=is_git,
    )
    if not is_git or not git_bin:
        status.error = "Not a git checkout; update by re-running install.sh or install.ps1"
        return status

    _, cur_branch, _ = _run([git_bin, "rev-parse", "--abbrev-ref", "HEAD"], cwd=resolved)
    target_branch = branch or str(meta.get("branch") or "") or cur_branch or "main"
    _, local_sha, _ = _run([git_bin, "rev-parse", "--short", "HEAD"], cwd=resolved)
    _, dirty_out, _ = _run([git_bin, "status", "--porcelain"], cwd=resolved)

    status.branch = target_branch
    status.local_sha = local_sha
    status.dirty = bool(dirty_out)

    remote_name, rem_err = _resolve_update_remote(resolved, git_bin, meta)
    if not remote_name:
        status.error = rem_err or "No 'origin' git remote configured"
        return status

    if fetch_remote:
        rc_fetch, _, err_fetch = _run(
            [git_bin, "fetch", "--quiet", remote_name, target_branch],
            cwd=resolved,
            timeout=15.0,
        )
        if rc_fetch != 0:
            status.error = f"git fetch failed: {err_fetch}"
            return status

    ref = f"{remote_name}/{target_branch}"
    rc_rev, remote_sha, _ = _run([git_bin, "rev-parse", "--short", ref], cwd=resolved)
    if rc_rev != 0:
        status.error = f"Remote ref '{ref}' not found (run with fetch enabled)"
        return status

    status.remote_sha = remote_sha
    rc_cnt, cnt_out, _ = _run(
        [git_bin, "rev-list", "--left-right", "--count", f"HEAD...{ref}"],
        cwd=resolved,
    )
    if rc_cnt == 0 and cnt_out:
        parts = cnt_out.split()
        if len(parts) == 2:
            status.ahead_count = int(parts[0])
            status.behind_count = int(parts[1])
            status.update_available = status.behind_count > 0

    if status.behind_count > 0:
        rc_rv, rv_out, _ = _run([git_bin, "show", f"{ref}:backend/version.py"], cwd=resolved)
        if rc_rv == 0 and rv_out:
            m_rv = re.search(r'__version__\s*=\s*"([^"]+)"', rv_out)
            if m_rv:
                status.remote_version = m_rv.group(1).strip()
        _, log_out, _ = _run(
            [git_bin, "log", "--oneline", "-n", "10", f"HEAD..{ref}"],
            cwd=resolved,
        )
        if log_out:
            status.recent_remote_commits = log_out.splitlines()

    return status


def _should_include_desktop_extra(resolved: Path, meta: dict) -> bool:
    """Preserve `--extra desktop` (dbus-next/pystray/pillow) during uv sync on desktop hosts."""
    if os.getenv("SPLITCH_NO_DESKTOP", "").strip().lower() in ("1", "true", "yes"):
        return False
    if meta.get("desktop_extra"):
        return True
    if (default_data_root() / "desktop.json").is_file():
        return True
    try:
        from backend.desktop.shortcuts import desktop_shortcuts_installed

        if desktop_shortcuts_installed():
            return True
    except (ImportError, OSError):
        pass
    venv_dir = resolved / ".venv"
    if venv_dir.is_dir():
        for pkg in ("dbus_next", "pystray"):
            if any(venv_dir.glob(f"**/site-packages/{pkg}")):
                return True
    return False


def _uv_sync_cmd(uv_bin: str, resolved: Path, meta: dict) -> list[str]:
    cmd = [uv_bin, "sync"]
    if _should_include_desktop_extra(resolved, meta):
        cmd.extend(["--extra", "desktop"])
    return cmd


def _restart_active_services() -> list[str]:
    restarted: list[str] = []
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        for unit in ("splitch-agent.service", "splitch-router.service"):
            rc, active, _ = _run(["systemctl", "--user", "is-active", unit], timeout=4.0)
            if rc == 0 and active == "active":
                rc2, _, _ = _run(["systemctl", "--user", "restart", unit], timeout=10.0)
                if rc2 == 0:
                    restarted.append(unit)
    elif sys.platform == "darwin" and shutil.which("launchctl"):
        uid = os.getuid()
        for label in ("com.splitch.agent", "com.splitch.router"):
            target = f"gui/{uid}/{label}"
            rc, _, _ = _run(["launchctl", "print", target], timeout=4.0)
            if rc == 0:
                rc2, _, _ = _run(["launchctl", "kickstart", "-k", target], timeout=10.0)
                if rc2 == 0:
                    restarted.append(label)

    # Also restart a running `splitch tray` process so it reloads the updated version & icon sync logic
    if not os.getenv("PYTEST_CURRENT_TEST"):
        try:
            import signal as _signal

            import psutil as _psutil

            from backend.desktop.shortcuts import read_live_pid, start_tray_detached

            pid_file = default_data_root() / "tray.pid"
            tpid = read_live_pid(pid_file)
            if tpid is not None and tpid != os.getpid():
                try:
                    proc = _psutil.Process(tpid)
                    proc.send_signal(_signal.SIGTERM)
                    proc.wait(timeout=3.0)
                except (_psutil.Error, OSError):
                    pass
                try:
                    pid_file.unlink(missing_ok=True)
                except OSError:
                    pass
                if start_tray_detached() is not None:
                    restarted.append("splitch-tray")
        except (ImportError, OSError):
            pass
    return restarted


def _rollback_git(
    resolved: Path,
    git_bin: str,
    target_sha: str,
    *,
    sync_deps: bool = True,
    meta: dict | None = None,
) -> tuple[bool, list[str], str | None]:
    steps: list[str] = []
    rc_rst, _, err_rst = _run([git_bin, "reset", "--hard", target_sha], cwd=resolved, timeout=30.0)
    if rc_rst != 0:
        return False, steps, f"git reset --hard {target_sha} failed: {err_rst}"
    steps.append(f"Rolled back git checkout to {target_sha[:8]}")
    if sync_deps:
        uv_bin = shutil.which("uv")
        if uv_bin:
            rc_uv, _, _ = _run(
                _uv_sync_cmd(uv_bin, resolved, meta or read_install_meta()),
                cwd=resolved,
                timeout=180.0,
            )
            if rc_uv == 0:
                steps.append("Restored Python environment via uv sync")
    return True, steps, None


def perform_update(
    *,
    app_dir: Path | None = None,
    branch: str | None = None,
    agent_only: bool = False,
    rebuild_ui: bool = False,
    restart_services: bool = True,
    force: bool = False,
    check_only: bool = False,
    sync_deps: bool = True,
    with_engine: bool = False,
    engine_backend: EngineBackend = "auto",
    progress: TerminalProgress | None = None,
) -> UpdateResult:
    step_ctx = progress.step if progress is not None else null_step
    resolved = resolve_app_dir(app_dir)
    meta = read_install_meta()
    effective_agent_only = agent_only or bool(meta.get("agent_only", False))

    with step_ctx("Checking upstream git status"):
        status = check_update(app_dir=resolved, branch=branch, fetch_remote=True)
    result = UpdateResult(
        ok=True,
        previous_sha=status.local_sha,
        current_sha=status.local_sha,
    )
    if status.error:
        result.ok = False
        result.error = status.error
        return result

    if check_only:
        result.commits_pulled = status.behind_count
        result.steps.append(
            f"Up to date at {status.branch}@{status.local_sha}"
            if not status.update_available
            else f"{status.behind_count} update commit(s) available: {status.local_sha} -> {status.remote_sha}"
        )
        if with_engine or read_engine_meta():
            with step_ctx("Checking upstream engine releases"):
                eng = get_engine_status(check_remote=True, backend_override=engine_backend)
            if eng.latest_llama_tag:
                if eng.update_available:
                    result.steps.append(
                        f"Engine update available: llama.cpp -> {eng.latest_llama_tag} "
                        f"({eng.matched_asset or eng.recommended_backend})"
                    )
                else:
                    result.steps.append(f"Engine up to date (llama.cpp {eng.latest_llama_tag})")
        return result

    if status.dirty and not force:
        result.ok = False
        result.error = (
            "Working tree has uncommitted changes. Commit/stash them first or re-run with --force."
        )
        return result

    git_bin = shutil.which("git") or "git"
    remote_name, _ = _resolve_update_remote(resolved, git_bin, meta)
    remote_name = remote_name or "origin"
    old_sha = status.local_sha
    _, prev_full_sha, _ = _run([git_bin, "rev-parse", "HEAD"], cwd=resolved)
    new_full_sha = prev_full_sha
    dashboard_changed = False
    pulled_new_commit = False

    def _fail_with_rollback(err_msg: str) -> UpdateResult:
        result.ok = False
        result.error = err_msg
        if pulled_new_commit and prev_full_sha:
            with step_ctx(f"Rolling back failed update to {old_sha}"):
                rb_ok, rb_steps, rb_err = _rollback_git(
                    resolved, git_bin, prev_full_sha, sync_deps=sync_deps
                )
            result.steps.extend(rb_steps)
            if rb_ok:
                result.rolled_back = True
                result.updated = False
                result.current_sha = old_sha
            elif rb_err:
                result.error = f"{err_msg} (rollback failed: {rb_err})"
        return result

    if status.update_available:
        rc_diff, diff_out, _ = _run(
            [git_bin, "diff", "--name-only", f"HEAD..{remote_name}/{status.branch}"],
            cwd=resolved,
        )
        if rc_diff == 0 and any(line.startswith("dashboard/") for line in diff_out.splitlines()):
            dashboard_changed = True

        with step_ctx(f"Pulling {status.behind_count} commit(s) from {remote_name}/{status.branch}"):
            rc_pull, _, err_pull = _run(
                [git_bin, "pull", "--ff-only", remote_name, status.branch],
                cwd=resolved,
                timeout=30.0,
            )
        if rc_pull != 0:
            result.ok = False
            result.error = f"Fast-forward pull failed: {err_pull}"
            return result

        _, new_sha, _ = _run([git_bin, "rev-parse", "--short", "HEAD"], cwd=resolved)
        _, new_full_sha, _ = _run([git_bin, "rev-parse", "HEAD"], cwd=resolved)
        pulled_new_commit = bool(new_full_sha and new_full_sha != prev_full_sha)
        result.updated = True
        result.current_sha = new_sha
        result.commits_pulled = status.behind_count
        result.steps.append(
            f"Pulled {status.behind_count} commit(s) on {status.branch}: {old_sha} -> {new_sha}"
        )
    else:
        result.steps.append(f"Git already up to date at {status.branch}@{old_sha}")

    if sync_deps and (result.updated or force):
        uv_bin = shutil.which("uv")
        if uv_bin:
            uv_cmd = _uv_sync_cmd(uv_bin, resolved, meta)
            if "--extra" in uv_cmd:
                meta["desktop_extra"] = True
            with step_ctx(f"Syncing Python environment ({' '.join(uv_cmd[1:])})"):
                rc_uv, _, err_uv = _run(uv_cmd, cwd=resolved, timeout=180.0)
            if rc_uv != 0:
                return _fail_with_rollback(f"uv sync failed: {err_uv}")
            result.steps.append(f"Synchronized Python environment via uv {' '.join(uv_cmd[1:])}")
        else:
            result.steps.append("Skipped uv sync (uv not found on PATH)")

    dist_missing = not (resolved / "dashboard" / "dist" / "index.html").exists()
    should_build_ui = (
        not effective_agent_only
        and (resolved / "dashboard" / "package.json").exists()
        and (rebuild_ui or dashboard_changed or dist_missing)
    )
    if should_build_ui:
        node_bin, npm_bin, node_ver = _find_node_bin()
        if not node_bin or not npm_bin or _parse_major(node_ver) < 20:
            return _fail_with_rollback(
                "Node.js 20+ required to rebuild dashboard (or pass --agent-only)"
            )
        env = os.environ.copy()
        env["PATH"] = f"{Path(node_bin).parent}{os.pathsep}{env.get('PATH', '')}"
        dash_dir = resolved / "dashboard"
        install_cmd = (
            [npm_bin, "ci", "--no-audit", "--no-fund"]
            if (dash_dir / "package-lock.json").exists()
            else [npm_bin, "install", "--no-audit", "--no-fund"]
        )
        with step_ctx("Installing dashboard dependencies"):
            rc_i, _, err_i = _run(install_cmd, cwd=dash_dir, timeout=180.0, env=env)
        if rc_i != 0:
            return _fail_with_rollback(f"npm install failed: {err_i}")
        with step_ctx("Building dashboard bundle"):
            rc_b, _, err_b = _run([npm_bin, "run", "build"], cwd=dash_dir, timeout=180.0, env=env)
        if rc_b != 0:
            return _fail_with_rollback(f"Dashboard build failed: {err_b}")
        result.ui_rebuilt = True
        result.steps.append("Rebuilt dashboard bundle (tsc -b && vite build)")

    if with_engine:
        eng_meta = read_engine_meta()
        eng_target: EngineTarget = (
            "ollama" if str(eng_meta.get("engine") or "") == "ollama" else "llama.cpp"
        )
        eng_res = install_or_update_engine(
            eng_target,
            backend=engine_backend,
            force=force,
            progress=progress,
        )
        result.steps.extend(eng_res.steps)
        if not eng_res.ok:
            result.ok = False
            result.error = f"Engine update failed: {eng_res.error}"
            return result
        if eng_res.action in ("install", "update"):
            result.updated = True
        if read_sd_engine_meta():
            sd_res = install_or_update_engine(
                "sd.cpp", backend=engine_backend, force=force, progress=progress
            )
            result.steps.extend(sd_res.steps)
            if sd_res.ok and sd_res.action in ("install", "update"):
                result.updated = True
        if read_mlx_engine_meta():
            mlx_res = install_or_update_engine(
                "mlx-lm", backend=engine_backend, force=force, progress=progress
            )
            result.steps.extend(mlx_res.steps)
            if mlx_res.ok and mlx_res.action in ("install", "update"):
                result.updated = True
        from backend.shared.strata_manager import read_strata_engine_meta

        if read_strata_engine_meta():
            st_res = install_or_update_engine(
                "strata", backend=engine_backend, force=force, progress=progress
            )
            result.steps.extend(st_res.steps)
            if st_res.ok and st_res.action in ("install", "update"):
                result.updated = True

    if restart_services and (result.updated or result.ui_rebuilt):
        with step_ctx("Restarting active services"):
            restarted = _restart_active_services()
        result.restarted_services = restarted
        if restarted:
            result.steps.append(f"Restarted active services: {', '.join(restarted)}")

    if meta or pulled_new_commit:
        if pulled_new_commit and prev_full_sha:
            meta["previous_sha"] = old_sha
            meta["previous_full_sha"] = prev_full_sha
            meta["current_full_sha"] = new_full_sha
        meta["updated_at"] = time.time()
        write_install_meta(meta)

    return result


def perform_rollback(
    *,
    app_dir: Path | None = None,
    to_sha: str | None = None,
    agent_only: bool = False,
    rebuild_ui: bool = False,
    restart_services: bool = True,
    sync_deps: bool = True,
    progress: TerminalProgress | None = None,
) -> UpdateResult:
    """Roll back Splitch to the previously recorded working git SHA (or an explicit `--to <sha>`)."""
    step_ctx = progress.step if progress is not None else null_step
    resolved = resolve_app_dir(app_dir)
    meta = read_install_meta()
    effective_agent_only = agent_only or bool(meta.get("agent_only", False))
    git_bin = shutil.which("git")
    if not git_bin or not (resolved / ".git").exists():
        return UpdateResult(
            ok=False,
            error="Not a git checkout; manual rollback requires a git installation.",
        )

    _, cur_short, _ = _run([git_bin, "rev-parse", "--short", "HEAD"], cwd=resolved)
    _, cur_full, _ = _run([git_bin, "rev-parse", "HEAD"], cwd=resolved)
    target = (
        (to_sha or "").strip()
        or str(meta.get("previous_full_sha") or "").strip()
        or str(meta.get("previous_sha") or "").strip()
    )
    if not target:
        return UpdateResult(
            ok=False,
            previous_sha=cur_short,
            current_sha=cur_short,
            error="No previous SHA recorded in install_meta.json; pass --to <commit-sha> explicitly.",
        )

    rc_ver, target_full, err_ver = _run(
        [git_bin, "rev-parse", "--verify", f"{target}^{{commit}}"],
        cwd=resolved,
    )
    if rc_ver != 0 or not target_full:
        return UpdateResult(
            ok=False,
            previous_sha=cur_short,
            current_sha=cur_short,
            error=f"Invalid rollback target '{target}': {err_ver or 'commit not found'}",
        )

    dashboard_changed = False
    rc_diff, diff_out, _ = _run(
        [git_bin, "diff", "--name-only", f"HEAD..{target_full}"],
        cwd=resolved,
    )
    if rc_diff == 0 and any(line.startswith("dashboard/") for line in diff_out.splitlines()):
        dashboard_changed = True

    with step_ctx(f"Rolling back git checkout to {target_full[:8]}"):
        rb_ok, rb_steps, rb_err = _rollback_git(
            resolved, git_bin, target_full, sync_deps=sync_deps
        )
    if not rb_ok:
        return UpdateResult(
            ok=False,
            previous_sha=cur_short,
            current_sha=cur_short,
            steps=rb_steps,
            error=rb_err,
        )

    _, new_short, _ = _run([git_bin, "rev-parse", "--short", "HEAD"], cwd=resolved)
    result = UpdateResult(
        ok=True,
        updated=True,
        rolled_back=True,
        previous_sha=cur_short,
        current_sha=new_short,
        steps=rb_steps,
    )

    should_build_ui = (
        not effective_agent_only
        and (resolved / "dashboard" / "package.json").exists()
        and (rebuild_ui or dashboard_changed)
    )
    if should_build_ui:
        node_bin, npm_bin, node_ver = _find_node_bin()
        if node_bin and npm_bin and _parse_major(node_ver) >= 20:
            env = os.environ.copy()
            env["PATH"] = f"{Path(node_bin).parent}{os.pathsep}{env.get('PATH', '')}"
            dash_dir = resolved / "dashboard"
            install_cmd = (
                [npm_bin, "ci", "--no-audit", "--no-fund"]
                if (dash_dir / "package-lock.json").exists()
                else [npm_bin, "install", "--no-audit", "--no-fund"]
            )
            with step_ctx("Rebuilding dashboard bundle for rolled-back revision"):
                rc_i, _, err_i = _run(install_cmd, cwd=dash_dir, timeout=180.0, env=env)
                if rc_i == 0:
                    rc_b, _, err_b = _run(
                        [npm_bin, "run", "build"], cwd=dash_dir, timeout=180.0, env=env
                    )
                else:
                    rc_b, err_b = rc_i, err_i
            if rc_b == 0:
                result.ui_rebuilt = True
                result.steps.append("Rebuilt dashboard bundle (tsc -b && vite build)")
            else:
                result.ok = False
                result.error = f"Rollback succeeded in git, but dashboard rebuild failed: {err_b}"
                return result

    if restart_services:
        with step_ctx("Restarting active services"):
            restarted = _restart_active_services()
        result.restarted_services = restarted
        if restarted:
            result.steps.append(f"Restarted active services: {', '.join(restarted)}")

    meta["previous_sha"] = cur_short
    meta["previous_full_sha"] = cur_full
    meta["current_full_sha"] = target_full
    meta["updated_at"] = time.time()
    write_install_meta(meta)
    return result


def fix_doctor_issues(
    *,
    app_dir: Path | None = None,
    engine: EngineTarget = "llama.cpp",
    backend: EngineBackend = "auto",
    with_media: bool = False,
    progress: TerminalProgress | None = None,
) -> list[str]:
    """Attempt safe local remediation for missing engine binary or unbuilt dashboard."""
    step_ctx = progress.step if progress is not None else null_step
    resolved = resolve_app_dir(app_dir)
    with step_ctx("Inspecting environment"):
        report = run_doctor(app_dir=resolved, check_engine_updates=True)
    actions: list[str] = []

    by_name = {c.name: c for c in report.checks}

    dist_chk = by_name.get("dashboard_dist")
    if dist_chk and dist_chk.status == "warn" and (resolved / "dashboard" / "package.json").exists():
        node_bin, npm_bin, node_ver = _find_node_bin()
        if node_bin and npm_bin and _parse_major(node_ver) >= 20:
            env = os.environ.copy()
            env["PATH"] = f"{Path(node_bin).parent}{os.pathsep}{env.get('PATH', '')}"
            dash_dir = resolved / "dashboard"
            with step_ctx("Installing dashboard dependencies"):
                rc_i, _, err_i = _run(
                    [npm_bin, "install", "--no-audit", "--no-fund"],
                    cwd=dash_dir,
                    timeout=180.0,
                    env=env,
                )
            if rc_i == 0:
                with step_ctx("Building dashboard bundle"):
                    rc_b, _, err_b = _run(
                        [npm_bin, "run", "build"], cwd=dash_dir, timeout=180.0, env=env
                    )
            else:
                rc_b, err_b = rc_i, err_i
            if rc_b == 0:
                actions.append("Rebuilt missing dashboard bundle (dashboard/dist)")
            else:
                actions.append(f"Dashboard rebuild failed: {err_b}")
        else:
            actions.append("Cannot rebuild dashboard: Node.js 20+ not found (re-run install.sh)")

    eng_chk = by_name.get("engine_binary")
    if eng_chk and eng_chk.status == "warn":
        res = install_or_update_engine(engine, backend=backend, progress=progress)
        if res.ok:
            actions.extend(res.steps)
        else:
            actions.append(f"Engine install/update failed: {res.error}")

    if with_media or engine == "sd.cpp":
        sd_res = install_or_update_engine("sd.cpp", backend=backend, progress=progress)
        if sd_res.ok:
            actions.extend(sd_res.steps)
        else:
            actions.append(f"Media engine (sd.cpp) install failed: {sd_res.error}")

    desk_chk = by_name.get("desktop_router")
    if desk_chk and desk_chk.status == "warn" and "uses http://" in desk_chk.summary:
        with contextlib.suppress(ImportError, OSError, ValueError):
            from backend.desktop.shortcuts import (
                _check_router_healthy,
                load_desktop_config,
                save_desktop_config,
            )

            dcfg = load_desktop_config()
            if dcfg.router_url.startswith("http://"):
                https_url = "https://" + dcfg.router_url.removeprefix("http://").rstrip("/")
                if _check_router_healthy(https_url, timeout=1.5):
                    dcfg.router_url = https_url
                    save_desktop_config(dcfg)
                    actions.append(f"Upgraded desktop.json router_url to {https_url}")

    if not actions:
        actions.append("Nothing to fix — all auto-remediable checks are already healthy")
    return actions
