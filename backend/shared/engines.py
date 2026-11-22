"""
Splitch Hardware-Aware Inference Engine Manager (`llama.cpp` / `ollama`).

Powers:
  - `splitch engine status | install | update`
  - `splitch doctor [--check-updates] [--fix]`
  - `splitch update --with-engine`
  - `install.sh --engine ...` & `install.ps1 -Engine ...`
  - `GET /api/system/engine`
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from backend.router.config import default_data_root
from backend.shared.gpu_probe import detect_gpus_sync, recommend_backend_from_gpus
from backend.shared.models import GpuDevice
from backend.shared.progress import TerminalProgress, null_download, null_step

EngineBackend = Literal["auto", "cuda", "rocm", "vulkan", "metal", "cpu"]
EngineTarget = Literal["llama.cpp", "ollama", "sd.cpp"]

LLAMA_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
SD_RELEASES_API = "https://api.github.com/repos/leejet/stable-diffusion.cpp/releases"


class EngineBinaryInfo(BaseModel):
    engine: str
    path: str
    version: str = ""
    build_tag: str = ""
    managed: bool = False
    backend: str = ""


class EngineStatus(BaseModel):
    platform: str
    arch: str
    recommended_backend: str
    cuda_version: str | None = None
    gpu_vendors: list[str] = Field(default_factory=list)
    detected_gpus: list[GpuDevice] = Field(default_factory=list)
    installed: list[EngineBinaryInfo] = Field(default_factory=list)
    managed_meta: dict = Field(default_factory=dict)
    latest_llama_tag: str | None = None
    matched_asset: str | None = None
    matched_cudart_asset: str | None = None
    update_available: bool = False
    error: str | None = None


class EngineActionResult(BaseModel):
    ok: bool = True
    engine: str
    action: Literal["install", "update", "noop", "dry-run"] = "install"
    previous_tag: str = ""
    current_tag: str = ""
    backend: str = ""
    binary_path: str = ""
    shim_path: str = ""
    steps: list[str] = Field(default_factory=list)
    error: str | None = None


def _engine_meta_path() -> Path:
    return default_data_root() / "engine_meta.json"


def _managed_llama_dir() -> Path:
    return default_data_root() / "engines" / "llama.cpp"


def _managed_sd_dir() -> Path:
    return default_data_root() / "engines" / "sd.cpp"


def _sd_meta_path() -> Path:
    return default_data_root() / "sd_engine_meta.json"


def read_sd_engine_meta() -> dict:
    p = _sd_meta_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_sd_engine_meta(meta: dict) -> None:
    p = _sd_meta_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def read_engine_meta() -> dict:
    p = _engine_meta_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_engine_meta(meta: dict) -> None:
    p = _engine_meta_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _run_cmd(
    cmd: list[str],
    *,
    timeout: float = 15.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
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
        return 124, "", f"Timed out after {timeout:.0f}s"


def _norm_os(raw: str | None = None) -> str:
    s = (raw or platform.system()).lower()
    if s.startswith("darwin") or s == "macos":
        return "macos"
    if s.startswith("win"):
        return "win"
    return "linux"


def _norm_arch(raw: str | None = None) -> str:
    m = (raw or platform.machine()).lower()
    if m in ("x86_64", "amd64", "x64"):
        return "x64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m


def detect_engine_hardware() -> tuple[str, str | None]:
    """Return (recommended_backend, cuda_version_or_none) across single & multi-vendor GPUs."""
    os_name = _norm_os()
    arch = _norm_arch()

    if os_name == "macos":
        return ("metal" if arch == "arm64" else "cpu"), None

    gpus, cuda_ver = detect_gpus_sync()
    if gpus:
        return recommend_backend_from_gpus(gpus, os_name=os_name, arch=arch), cuda_ver

    if shutil.which("nvidia-smi"):
        rc, out, _ = _run_cmd(["nvidia-smi"], timeout=5.0)
        if rc == 0 and out:
            m = re.search(r"CUDA(?:\s+\w+)?\s+Version:\s*(\d+\.\d+)", out, re.IGNORECASE)
            if m:
                cuda_ver = m.group(1)
        return "cuda", cuda_ver

    if shutil.which("rocm-smi"):
        return "rocm", None

    if shutil.which("vulkaninfo") or Path("/usr/lib/libvulkan.so.1").exists():
        return "vulkan", None

    return "cpu", None


def parse_llama_build_tag(version_output: str) -> str:
    """Extract canonical `bNNNNN` tag from `llama-server --version` output."""
    if not version_output:
        return ""
    m = re.search(r"build\s+(\d+)", version_output, re.IGNORECASE)
    if m:
        return f"b{m.group(1)}"
    m2 = re.search(r"version:\s*(\d+)\s*\(", version_output, re.IGNORECASE)
    if m2:
        return f"b{m2.group(1)}"
    m3 = re.search(r"\b(b\d{3,})\b", version_output)
    return m3.group(1) if m3 else ""


def _parse_ver_tuple(raw: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", raw)
    return tuple(int(x) for x in nums) if nums else (0,)


def _find_binary_in_dir(root: Path, names: tuple[str, ...]) -> Path | None:
    if not root.exists():
        return None
    for name in names:
        direct = root / name
        if direct.is_file():
            return direct
    for candidate in sorted(root.rglob("*")):
        if candidate.is_file() and candidate.name in names:
            return candidate
    return None


def detect_installed_engines() -> list[EngineBinaryInfo]:
    found: list[EngineBinaryInfo] = []
    meta = read_engine_meta()
    managed_dir = _managed_llama_dir()
    seen_paths: set[str] = set()

    # 1. Managed llama-server first
    managed_bin = _find_binary_in_dir(managed_dir, ("llama-server", "llama-server.exe"))
    if managed_bin:
        env = os.environ.copy()
        lib_dir = str(managed_bin.parent)
        if sys.platform == "darwin":
            env["DYLD_LIBRARY_PATH"] = f"{lib_dir}:{env.get('DYLD_LIBRARY_PATH', '')}"
        elif sys.platform.startswith("linux"):
            env["LD_LIBRARY_PATH"] = f"{lib_dir}:{env.get('LD_LIBRARY_PATH', '')}"
        _, out, err = _run_cmd([str(managed_bin), "--version"], timeout=5.0, env=env)
        text = out or err
        first_line = text.splitlines()[0] if text else ""
        tag = parse_llama_build_tag(text) or str(meta.get("tag") or "")
        seen_paths.add(str(managed_bin.resolve()))
        found.append(
            EngineBinaryInfo(
                engine="llama.cpp",
                path=str(managed_bin),
                version=first_line,
                build_tag=tag,
                managed=True,
                backend=str(meta.get("backend") or ""),
            )
        )

    # 2. System PATH llama-server
    sys_llama = shutil.which("llama-server")
    if sys_llama:
        resolved = str(Path(sys_llama).resolve())
        # Avoid duplicating if PATH shim simply points to managed binary
        is_managed_shim = str(meta.get("shim_path") or "") == sys_llama
        if resolved not in seen_paths and not is_managed_shim:
            _, out, err = _run_cmd([sys_llama, "--version"], timeout=5.0)
            text = out or err
            first_line = text.splitlines()[0] if text else ""
            seen_paths.add(resolved)
            found.append(
                EngineBinaryInfo(
                    engine="llama.cpp",
                    path=sys_llama,
                    version=first_line,
                    build_tag=parse_llama_build_tag(text),
                    managed=False,
                )
            )

    # 3. Ollama
    ollama_bin = shutil.which("ollama")
    if ollama_bin:
        _, out, err = _run_cmd([ollama_bin, "--version"], timeout=5.0)
        text = out or err
        first_line = text.splitlines()[0] if text else ""
        found.append(
            EngineBinaryInfo(
                engine="ollama",
                path=ollama_bin,
                version=first_line,
                managed=False,
            )
        )

    # 4. Managed or system stable-diffusion.cpp (`sd-server`)
    sd_meta = read_sd_engine_meta()
    managed_sd = _find_binary_in_dir(_managed_sd_dir(), ("sd-server", "sd-server.exe", "sd-cli", "sd-cli.exe"))
    if managed_sd:
        seen_paths.add(str(managed_sd.resolve()))
        found.append(
            EngineBinaryInfo(
                engine="sd.cpp",
                path=str(managed_sd),
                version=str(sd_meta.get("tag") or "managed"),
                build_tag=str(sd_meta.get("tag") or ""),
                managed=True,
                backend=str(sd_meta.get("backend") or ""),
            )
        )
    else:
        sys_sd = shutil.which("sd-server")
        if sys_sd and str(Path(sys_sd).resolve()) not in seen_paths:
            found.append(
                EngineBinaryInfo(
                    engine="sd.cpp",
                    path=sys_sd,
                    version="system",
                    managed=False,
                )
            )

    return found


def select_llama_assets(
    assets: list[dict],
    *,
    os_name: str,
    arch: str,
    backend: str,
    cuda_version: str | None = None,
) -> tuple[dict | None, dict | None]:
    """
    Choose the best `(llama_asset, cudart_asset)` pair from a `ggml-org/llama.cpp` release.
    Returns `(None, None)` if no compatible prebuilt binary exists.
    """
    os_key = _norm_os(os_name)
    arch_key = _norm_arch(arch)
    ext = ".zip" if os_key == "win" else ".tar.gz"

    os_tokens = {
        "macos": ("-macos-",),
        "linux": ("-ubuntu-", "-linux-"),
        "win": ("-win-",),
    }[os_key]

    def _is_main_candidate(name: str) -> bool:
        lo = name.lower()
        if not lo.startswith("llama-") or not lo.endswith(ext):
            return False
        if "-bin-" not in lo or "android" in lo or "snapdragon" in lo:
            return False
        if not any(tok in lo for tok in os_tokens):
            return False
        return f"-{arch_key}{ext}" in lo

    mains = [a for a in assets if _is_main_candidate(str(a.get("name", "")))]
    if not mains:
        return None, None

    cudarts = [
        a
        for a in assets
        if str(a.get("name", "")).lower().startswith("cudart-")
        and str(a.get("name", "")).lower().endswith(ext)
        and any(tok in str(a.get("name", "")).lower() for tok in os_tokens)
        and f"-{arch_key}{ext}" in str(a.get("name", "")).lower()
    ]

    def _extract_cuda_ver(name: str) -> tuple[int, ...]:
        m = re.search(r"cuda-(\d+(?:\.\d+)?)", name.lower())
        return _parse_ver_tuple(m.group(1)) if m else (0,)

    def _pick_cuda() -> tuple[dict | None, dict | None]:
        cuda_mains = [a for a in mains if "-cuda-" in str(a["name"]).lower()]
        if not cuda_mains:
            return None, None
        target_v = _parse_ver_tuple(cuda_version) if cuda_version else None
        if target_v:
            compatible = [a for a in cuda_mains if _extract_cuda_ver(a["name"]) <= target_v]
            chosen = (
                max(compatible, key=lambda a: _extract_cuda_ver(a["name"]))
                if compatible
                else min(cuda_mains, key=lambda a: _extract_cuda_ver(a["name"]))
            )
        else:
            chosen = max(cuda_mains, key=lambda a: _extract_cuda_ver(a["name"]))
        chosen_ver = _extract_cuda_ver(chosen["name"])
        matching_rt = next(
            (c for c in cudarts if _extract_cuda_ver(c["name"]) == chosen_ver),
            cudarts[0] if cudarts else None,
        )
        return chosen, matching_rt

    def _pick_token(token: str) -> dict | None:
        hits = [a for a in mains if token in str(a["name"]).lower()]
        return hits[0] if hits else None

    def _pick_cpu() -> dict | None:
        gpu_markers = ("-cuda-", "-rocm-", "-vulkan-", "-sycl-", "-openvino-", "-opencl-", "-zdnn-")
        plain = [a for a in mains if not any(g in str(a["name"]).lower() for g in gpu_markers)]
        if plain:
            return plain[0]
        return _pick_token("-cpu-")

    if os_key == "macos":
        return mains[0], None

    if backend == "cuda":
        main, rt = _pick_cuda()
        if main:
            return main, rt
        return _pick_token("-vulkan-") or _pick_cpu(), None

    if backend == "rocm":
        return _pick_token("-rocm-") or _pick_token("-vulkan-") or _pick_cpu(), None

    if backend == "vulkan":
        return _pick_token("-vulkan-") or _pick_cpu(), None

    return _pick_cpu() or mains[0], None


def fetch_latest_llama_release(version: str = "latest") -> tuple[str, list[dict]]:
    """Fetch release `(tag_name, assets)` from `ggml-org/llama.cpp` GitHub API."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "splitch-updater"}
    gh_tok = os.getenv("GITHUB_TOKEN", "").strip()
    if gh_tok:
        headers["Authorization"] = f"Bearer {gh_tok}"

    with httpx.Client(timeout=15.0, headers=headers, follow_redirects=True) as client:
        if version and version != "latest":
            r = client.get(f"{LLAMA_RELEASES_API}/tags/{version}")
            r.raise_for_status()
            rel = r.json()
            return str(rel["tag_name"]), list(rel.get("assets") or [])

        r = client.get(LLAMA_RELEASES_API, params={"per_page": 10})
        r.raise_for_status()
        for rel in r.json():
            assets = list(rel.get("assets") or [])
            if any(str(a.get("name", "")).startswith("llama-") and "-bin-" in str(a.get("name", "")) for a in assets):
                return str(rel["tag_name"]), assets
        raise RuntimeError("No prebuilt llama.cpp binary release found in recent GitHub releases")


def _compare_build_tags(current: str, latest: str) -> bool:
    """Return True if `latest` represents a newer build than `current`."""
    if not latest:
        return False
    if not current:
        return True
    cur_nums = re.findall(r"\d+", current)
    lat_nums = re.findall(r"\d+", latest)
    if cur_nums and lat_nums:
        return int(lat_nums[0]) > int(cur_nums[0])
    return current != latest


def get_engine_status(
    *,
    check_remote: bool = False,
    backend_override: EngineBackend = "auto",
) -> EngineStatus:
    os_name = _norm_os()
    arch = _norm_arch()
    detected_gpus, cuda_ver = detect_gpus_sync()
    rec_backend, fallback_cuda = detect_engine_hardware()
    cuda_ver = cuda_ver or fallback_cuda
    eff_backend = rec_backend if backend_override == "auto" else backend_override
    installed = detect_installed_engines()
    meta = read_engine_meta()

    st = EngineStatus(
        platform=platform.system(),
        arch=platform.machine(),
        recommended_backend=eff_backend,
        cuda_version=cuda_ver,
        gpu_vendors=sorted({g.vendor for g in detected_gpus}),
        detected_gpus=detected_gpus,
        installed=installed,
        managed_meta=meta,
    )

    if check_remote:
        try:
            tag, assets = fetch_latest_llama_release("latest")
            main_a, rt_a = select_llama_assets(
                assets,
                os_name=os_name,
                arch=arch,
                backend=eff_backend,
                cuda_version=cuda_ver,
            )
            st.latest_llama_tag = tag
            st.matched_asset = str(main_a["name"]) if main_a else None
            st.matched_cudart_asset = str(rt_a["name"]) if rt_a else None
            llama_inst = next((e for e in installed if e.engine == "llama.cpp"), None)
            if llama_inst:
                st.update_available = _compare_build_tags(llama_inst.build_tag, tag)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            st.error = f"Failed to check upstream llama.cpp releases: {exc}"

    return st


def _safe_extract_archive(archive_path: Path, dest_dir: Path) -> None:
    """Safely extract `.tar.gz` or `.zip` archive with path-traversal protection."""
    dest_resolved = dest_dir.resolve()
    name_lo = archive_path.name.lower()
    if name_lo.endswith((".tar.gz", ".tgz", ".tar.xz")):
        with tarfile.open(archive_path, "r:*") as tf:
            for member in tf.getmembers():
                target = (dest_resolved / member.name).resolve()
                if not target.is_relative_to(dest_resolved):
                    raise ValueError(f"Unsafe archive member path: {member.name}")
                if member.issym() or member.islnk():
                    link_target = (target.parent / member.linkname).resolve()
                    if not link_target.is_relative_to(dest_resolved):
                        raise ValueError(f"Unsafe archive symlink: {member.name} -> {member.linkname}")
            tf.extractall(dest_resolved, filter="data")
    elif name_lo.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                target = (dest_resolved / info.filename).resolve()
                if not target.is_relative_to(dest_resolved):
                    raise ValueError(f"Unsafe zip member path: {info.filename}")
            zf.extractall(dest_resolved)
    else:
        raise ValueError(f"Unsupported archive format: {archive_path.name}")


def _write_llama_shim(bin_dir: Path, binary_path: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    lib_dir = binary_path.parent
    if sys.platform == "win32":
        shim = bin_dir / "llama-server.cmd"
        shim.write_text(
            f'@echo off\r\nset "PATH={lib_dir};%PATH%"\r\n"{binary_path}" %*\r\n',
            encoding="ascii",
        )
        return shim

    shim = bin_dir / "llama-server"
    lib_var = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
    shim.write_text(
        "#!/usr/bin/env sh\n"
        f'export {lib_var}="{lib_dir}:${{{lib_var}:-}}"\n'
        f'exec "{binary_path}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def install_llama_from_archives(
    *,
    tag: str,
    main_archive: Path,
    cudart_archive: Path | None = None,
    backend: str = "cpu",
    bin_dir: Path | None = None,
) -> EngineActionResult:
    """Install/replace Splitch's managed llama.cpp directory atomically from local archives."""
    managed_dir = _managed_llama_dir()
    managed_dir.parent.mkdir(parents=True, exist_ok=True)
    prev_meta = read_engine_meta()
    prev_tag = str(prev_meta.get("tag") or "")

    staging = Path(tempfile.mkdtemp(prefix=".llama-staging-", dir=managed_dir.parent))
    backup: Path | None = None
    try:
        _safe_extract_archive(main_archive, staging)
        if cudart_archive is not None:
            _safe_extract_archive(cudart_archive, staging)

        bin_in_stage = _find_binary_in_dir(staging, ("llama-server", "llama-server.exe"))
        if not bin_in_stage:
            raise RuntimeError(f"No llama-server binary found inside {main_archive.name}")
        if sys.platform != "win32":
            bin_in_stage.chmod(bin_in_stage.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # If cudart libs unpacked into a sibling/root dir, copy them beside llama-server so rpath/LD_LIBRARY_PATH finds them
        if cudart_archive is not None:
            for lib in staging.rglob("*"):
                if lib.is_file() and (
                    ".so" in lib.name or lib.name.lower().endswith((".dll", ".dylib"))
                ):
                    dst = bin_in_stage.parent / lib.name
                    if not dst.exists():
                        shutil.copy2(lib, dst)

        rel_bin = bin_in_stage.relative_to(staging)
        if managed_dir.exists():
            backup = managed_dir.parent / f".llama-backup-{int(time.time())}"
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            managed_dir.rename(backup)
        staging.rename(managed_dir)
        if backup and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

        final_bin = managed_dir / rel_bin
        target_bin_dir = bin_dir or (
            default_data_root() / "bin"
            if sys.platform == "win32"
            else Path.home() / ".local" / "bin"
        )
        shim = _write_llama_shim(target_bin_dir, final_bin)

        now = time.time()
        write_engine_meta(
            {
                "engine": "llama.cpp",
                "tag": tag,
                "backend": backend,
                "asset": main_archive.name,
                "cudart_asset": cudart_archive.name if cudart_archive else None,
                "binary_path": str(final_bin),
                "shim_path": str(shim),
                "installed_at": prev_meta.get("installed_at", now),
                "updated_at": now,
            }
        )
        return EngineActionResult(
            ok=True,
            engine="llama.cpp",
            action="update" if prev_tag else "install",
            previous_tag=prev_tag,
            current_tag=tag,
            backend=backend,
            binary_path=str(final_bin),
            shim_path=str(shim),
            steps=[
                f"Extracted {main_archive.name}"
                + (f" + {cudart_archive.name}" if cudart_archive else ""),
                f"Installed managed llama-server ({tag}, {backend}) -> {final_bin}",
                f"Installed CLI shim -> {shim}",
            ],
        )
    except (OSError, RuntimeError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        if backup and backup.exists() and not managed_dir.exists():
            backup.rename(managed_dir)
        shutil.rmtree(staging, ignore_errors=True)
        return EngineActionResult(
            ok=False,
            engine="llama.cpp",
            action="update" if prev_tag else "install",
            previous_tag=prev_tag,
            backend=backend,
            error=str(exc),
        )


def _download_file(
    url: str,
    dest: Path,
    *,
    progress: TerminalProgress | None = None,
    label: str | None = None,
) -> None:
    headers = {"User-Agent": "splitch-updater"}
    with (
        httpx.Client(timeout=300.0, headers=headers, follow_redirects=True) as client,
        client.stream("GET", url) as resp,
    ):
        resp.raise_for_status()
        raw_len = resp.headers.get("content-length")
        total = int(raw_len) if (raw_len and raw_len.isdigit()) else None
        bar_ctx = (
            progress.download(label or dest.name, total)
            if progress is not None
            else null_download(label or dest.name, total)
        )
        with bar_ctx as bar, dest.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                fh.write(chunk)
                bar.advance(len(chunk))


def select_sd_assets(
    assets: list[dict],
    *,
    os_name: str,
    arch: str,
    backend: str,
) -> tuple[dict | None, dict | None]:
    """Choose `(sd_asset, cudart_asset)` from `leejet/stable-diffusion.cpp` GitHub releases."""
    os_key = _norm_os(os_name)
    arch_key = _norm_arch(arch)
    arch_tokens = ("x86_64", "x64") if arch_key == "x64" else ("arm64", "aarch64")
    os_tokens = {
        "macos": ("darwin", "macos"),
        "linux": ("linux", "ubuntu"),
        "win": ("win",),
    }[os_key]

    def _is_main(name: str) -> bool:
        lo = name.lower()
        if not lo.startswith("sd-") or not lo.endswith((".zip", ".tar.gz")):
            return False
        if "-bin-" not in lo:
            return False
        if not any(tok in lo for tok in os_tokens):
            return False
        return any(tok in lo for tok in arch_tokens)

    mains = [a for a in assets if _is_main(str(a.get("name", "")))]
    if not mains:
        return None, None

    cudarts = [
        a
        for a in assets
        if str(a.get("name", "")).lower().startswith("cudart-sd-")
        and any(tok in str(a.get("name", "")).lower() for tok in os_tokens)
        and any(tok in str(a.get("name", "")).lower() for tok in arch_tokens)
    ]

    def _pick_token(token: str) -> dict | None:
        hits = [a for a in mains if token in str(a["name"]).lower()]
        return hits[0] if hits else None

    def _pick_cpu() -> dict | None:
        gpu_markers = ("-cuda", "-rocm", "-vulkan", "-sycl", "-opencl")
        plain = [a for a in mains if not any(g in str(a["name"]).lower() for g in gpu_markers)]
        if plain:
            return plain[0]
        return _pick_token("-cpu")

    if os_key == "macos":
        return mains[0], None
    if backend == "cuda":
        cu = _pick_token("-cuda")
        if cu:
            return cu, (cudarts[0] if cudarts else None)
        # Linux prebuilts ship Vulkan (full NVIDIA GPU acceleration) when no Linux CUDA zip exists
        return _pick_token("-vulkan") or _pick_cpu() or mains[0], None
    if backend == "rocm":
        return _pick_token("-rocm") or _pick_token("-vulkan") or _pick_cpu(), None
    if backend == "vulkan":
        return _pick_token("-vulkan") or _pick_cpu(), None
    return _pick_cpu() or mains[0], None


def fetch_latest_sd_release(version: str = "latest") -> tuple[str, list[dict]]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "splitch-updater"}
    gh_tok = os.getenv("GITHUB_TOKEN", "").strip()
    if gh_tok:
        headers["Authorization"] = f"Bearer {gh_tok}"

    with httpx.Client(timeout=15.0, headers=headers, follow_redirects=True) as client:
        if version and version != "latest":
            r = client.get(f"{SD_RELEASES_API}/tags/{version}")
            r.raise_for_status()
            rel = r.json()
            return str(rel["tag_name"]), list(rel.get("assets") or [])
        r = client.get(SD_RELEASES_API, params={"per_page": 10})
        r.raise_for_status()
        for rel in r.json():
            assets = list(rel.get("assets") or [])
            if any(str(a.get("name", "")).startswith("sd-") and "-bin-" in str(a.get("name", "")) for a in assets):
                return str(rel["tag_name"]), assets
        raise RuntimeError("No prebuilt stable-diffusion.cpp release found on GitHub")


def _write_named_shim(bin_dir: Path, name: str, binary_path: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    lib_dir = binary_path.parent
    if sys.platform == "win32":
        shim = bin_dir / f"{name}.cmd"
        shim.write_text(
            f'@echo off\r\nset "PATH={lib_dir};%PATH%"\r\n"{binary_path}" %*\r\n',
            encoding="ascii",
        )
        return shim
    shim = bin_dir / name
    lib_var = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
    shim.write_text(
        "#!/usr/bin/env sh\n"
        f'export {lib_var}="{lib_dir}:${{{lib_var}:-}}"\n'
        f'exec "{binary_path}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def install_sd_from_archives(
    *,
    tag: str,
    main_archive: Path,
    cudart_archive: Path | None = None,
    backend: str = "cpu",
    bin_dir: Path | None = None,
) -> EngineActionResult:
    """Install/replace Splitch's managed stable-diffusion.cpp directory atomically."""
    managed_dir = _managed_sd_dir()
    managed_dir.parent.mkdir(parents=True, exist_ok=True)
    prev_meta = read_sd_engine_meta()
    prev_tag = str(prev_meta.get("tag") or "")

    staging = Path(tempfile.mkdtemp(prefix=".sd-staging-", dir=managed_dir.parent))
    backup: Path | None = None
    try:
        _safe_extract_archive(main_archive, staging)
        if cudart_archive is not None:
            _safe_extract_archive(cudart_archive, staging)

        srv_bin = _find_binary_in_dir(staging, ("sd-server", "sd-server.exe"))
        cli_bin = _find_binary_in_dir(staging, ("sd-cli", "sd-cli.exe", "sd", "sd.exe"))
        primary = srv_bin or cli_bin
        if not primary:
            raise RuntimeError(f"No sd-server or sd-cli binary found inside {main_archive.name}")
        for b in (srv_bin, cli_bin):
            if b and sys.platform != "win32":
                b.chmod(b.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        if cudart_archive is not None:
            for lib in staging.rglob("*"):
                if lib.is_file() and (
                    ".so" in lib.name or lib.name.lower().endswith((".dll", ".dylib"))
                ):
                    dst = primary.parent / lib.name
                    if not dst.exists():
                        shutil.copy2(lib, dst)

        rel_srv = srv_bin.relative_to(staging) if srv_bin else None
        rel_cli = cli_bin.relative_to(staging) if cli_bin else None
        if managed_dir.exists():
            backup = managed_dir.parent / f".sd-backup-{int(time.time())}"
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            managed_dir.rename(backup)
        staging.rename(managed_dir)
        if backup and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

        target_bin_dir = bin_dir or (
            default_data_root() / "bin"
            if sys.platform == "win32"
            else Path.home() / ".local" / "bin"
        )
        final_srv = (managed_dir / rel_srv) if rel_srv else None
        final_cli = (managed_dir / rel_cli) if rel_cli else None
        final_primary = final_srv or final_cli
        assert final_primary is not None
        shims: list[str] = []
        primary_shim = ""
        if final_srv:
            s = _write_named_shim(target_bin_dir, "sd-server", final_srv)
            primary_shim = str(s)
            shims.append(str(s))
        if final_cli:
            s = _write_named_shim(target_bin_dir, "sd-cli", final_cli)
            if not primary_shim:
                primary_shim = str(s)
            shims.append(str(s))

        now = time.time()
        write_sd_engine_meta(
            {
                "engine": "sd.cpp",
                "tag": tag,
                "backend": backend,
                "asset": main_archive.name,
                "cudart_asset": cudart_archive.name if cudart_archive else None,
                "binary_path": str(final_primary),
                "shim_path": primary_shim,
                "installed_at": prev_meta.get("installed_at", now),
                "updated_at": now,
            }
        )
        return EngineActionResult(
            ok=True,
            engine="sd.cpp",
            action="update" if prev_tag else "install",
            previous_tag=prev_tag,
            current_tag=tag,
            backend=backend,
            binary_path=str(final_primary),
            shim_path=primary_shim,
            steps=[
                f"Extracted {main_archive.name}"
                + (f" + {cudart_archive.name}" if cudart_archive else ""),
                f"Installed managed stable-diffusion.cpp ({tag}, {backend}) -> {final_primary}",
                f"Installed CLI shim(s) -> {', '.join(shims)}",
            ],
        )
    except (OSError, RuntimeError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        if backup and backup.exists() and not managed_dir.exists():
            backup.rename(managed_dir)
        shutil.rmtree(staging, ignore_errors=True)
        return EngineActionResult(
            ok=False,
            engine="sd.cpp",
            action="update" if prev_tag else "install",
            previous_tag=prev_tag,
            backend=backend,
            error=str(exc),
        )


def install_or_update_engine(
    engine: EngineTarget = "llama.cpp",
    *,
    backend: EngineBackend = "auto",
    version: str = "latest",
    bin_dir: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
    progress: TerminalProgress | None = None,
) -> EngineActionResult:
    step_ctx = progress.step if progress is not None else null_step
    if engine == "ollama":
        return _install_or_update_ollama(dry_run=dry_run, progress=progress)
    if engine == "sd.cpp":
        os_name = _norm_os()
        arch = _norm_arch()
        rec_backend, _ = detect_engine_hardware()
        eff_backend = rec_backend if backend == "auto" else backend
        prev_meta = read_sd_engine_meta()
        prev_tag = str(prev_meta.get("tag") or "")
        try:
            with step_ctx("Resolving stable-diffusion.cpp release"):
                tag, assets = fetch_latest_sd_release(version)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return EngineActionResult(
                ok=False,
                engine="sd.cpp",
                backend=eff_backend,
                previous_tag=prev_tag,
                error=f"Failed to resolve stable-diffusion.cpp release '{version}': {exc}",
            )
        if prev_tag and not force and prev_tag == tag:
            if progress:
                progress.ok(f"stable-diffusion.cpp already up to date ({prev_tag})")
            return EngineActionResult(
                ok=True,
                engine="sd.cpp",
                action="noop",
                previous_tag=prev_tag,
                current_tag=prev_tag,
                backend=eff_backend,
                binary_path=str(prev_meta.get("binary_path") or ""),
                shim_path=str(prev_meta.get("shim_path") or ""),
                steps=[f"Managed stable-diffusion.cpp is already up to date ({prev_tag})"],
            )
        main_a, rt_a = select_sd_assets(
            assets, os_name=os_name, arch=arch, backend=eff_backend
        )
        if not main_a:
            return EngineActionResult(
                ok=False,
                engine="sd.cpp",
                backend=eff_backend,
                previous_tag=prev_tag,
                error=f"No prebuilt stable-diffusion.cpp binary found for {os_name}/{arch} ({eff_backend}) in {tag}",
            )
        if dry_run:
            steps = [
                f"Resolved stable-diffusion.cpp {tag} for {os_name}/{arch} (backend={eff_backend})",
                f"Would download: {main_a['name']}",
            ]
            if rt_a:
                steps.append(f"Would download CUDA runtime: {rt_a['name']}")
            return EngineActionResult(
                ok=True,
                engine="sd.cpp",
                action="dry-run",
                previous_tag=prev_tag,
                current_tag=tag,
                backend=eff_backend,
                steps=steps,
            )
        with tempfile.TemporaryDirectory(prefix="splitch-sd-dl-") as td:
            tmp_dir = Path(td)
            main_path = tmp_dir / str(main_a["name"])
            rt_path: Path | None = None
            try:
                _download_file(
                    str(main_a["browser_download_url"]),
                    main_path,
                    progress=progress,
                    label=str(main_a["name"]),
                )
                if rt_a:
                    rt_path = tmp_dir / str(rt_a["name"])
                    _download_file(
                        str(rt_a["browser_download_url"]),
                        rt_path,
                        progress=progress,
                        label=str(rt_a["name"]),
                    )
            except httpx.HTTPError as exc:
                return EngineActionResult(
                    ok=False,
                    engine="sd.cpp",
                    backend=eff_backend,
                    previous_tag=prev_tag,
                    error=f"Download failed: {exc}",
                )
            with step_ctx(
                f"Extracting stable-diffusion.cpp ({tag}, {eff_backend})",
                done_label=f"Installed stable-diffusion.cpp ({tag}, {eff_backend})",
            ):
                return install_sd_from_archives(
                    tag=tag,
                    main_archive=main_path,
                    cudart_archive=rt_path,
                    backend=eff_backend,
                    bin_dir=bin_dir,
                )

    os_name = _norm_os()
    arch = _norm_arch()
    rec_backend, cuda_ver = detect_engine_hardware()
    eff_backend = rec_backend if backend == "auto" else backend
    prev_meta = read_engine_meta()
    prev_tag = str(prev_meta.get("tag") or "")

    try:
        with step_ctx("Resolving llama.cpp release"):
            tag, assets = fetch_latest_llama_release(version)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return EngineActionResult(
            ok=False,
            engine="llama.cpp",
            backend=eff_backend,
            previous_tag=prev_tag,
            error=f"Failed to resolve llama.cpp release '{version}': {exc}",
        )

    if prev_tag and not force and not _compare_build_tags(prev_tag, tag):
        if progress:
            progress.ok(f"llama.cpp already up to date ({prev_tag})")
        return EngineActionResult(
            ok=True,
            engine="llama.cpp",
            action="noop",
            previous_tag=prev_tag,
            current_tag=prev_tag,
            backend=eff_backend,
            binary_path=str(prev_meta.get("binary_path") or ""),
            shim_path=str(prev_meta.get("shim_path") or ""),
            steps=[f"Managed llama.cpp is already up to date ({prev_tag})"],
        )

    main_a, rt_a = select_llama_assets(
        assets,
        os_name=os_name,
        arch=arch,
        backend=eff_backend,
        cuda_version=cuda_ver,
    )
    if not main_a:
        return EngineActionResult(
            ok=False,
            engine="llama.cpp",
            backend=eff_backend,
            previous_tag=prev_tag,
            error=f"No prebuilt llama.cpp binary found for {os_name}/{arch} ({eff_backend}) in {tag}",
        )

    if dry_run:
        steps = [
            f"Resolved llama.cpp {tag} for {os_name}/{arch} (backend={eff_backend})",
            f"Would download: {main_a['name']}",
        ]
        if rt_a:
            steps.append(f"Would download CUDA runtime: {rt_a['name']}")
        return EngineActionResult(
            ok=True,
            engine="llama.cpp",
            action="dry-run",
            previous_tag=prev_tag,
            current_tag=tag,
            backend=eff_backend,
            steps=steps,
        )

    with tempfile.TemporaryDirectory(prefix="splitch-llama-dl-") as td:
        tmp_dir = Path(td)
        main_path = tmp_dir / str(main_a["name"])
        rt_path: Path | None = None
        try:
            _download_file(
                str(main_a["browser_download_url"]),
                main_path,
                progress=progress,
                label=str(main_a["name"]),
            )
            if rt_a:
                rt_path = tmp_dir / str(rt_a["name"])
                _download_file(
                    str(rt_a["browser_download_url"]),
                    rt_path,
                    progress=progress,
                    label=str(rt_a["name"]),
                )
        except httpx.HTTPError as exc:
            return EngineActionResult(
                ok=False,
                engine="llama.cpp",
                backend=eff_backend,
                previous_tag=prev_tag,
                error=f"Download failed: {exc}",
            )

        with step_ctx(
            f"Extracting llama.cpp ({tag}, {eff_backend})",
            done_label=f"Installed llama.cpp ({tag}, {eff_backend})",
        ):
            return install_llama_from_archives(
                tag=tag,
                main_archive=main_path,
                cudart_archive=rt_path,
                backend=eff_backend,
                bin_dir=bin_dir,
            )


def _install_or_update_ollama(
    *, dry_run: bool = False, progress: TerminalProgress | None = None
) -> EngineActionResult:
    step_ctx = progress.step if progress is not None else null_step
    os_name = _norm_os()
    existing = shutil.which("ollama")
    action: Literal["install", "update", "dry-run"] = (
        "dry-run" if dry_run else ("update" if existing else "install")
    )

    if os_name == "macos":
        if shutil.which("brew"):
            cmd = ["brew", "upgrade" if existing else "install", "ollama"]
            if dry_run:
                return EngineActionResult(
                    ok=True, engine="ollama", action="dry-run", steps=[f"Would run: {' '.join(cmd)}"]
                )
            with step_ctx(f"Running {' '.join(cmd)}"):
                rc, _, err = _run_cmd(cmd, timeout=300.0)
            if rc != 0:
                return EngineActionResult(ok=False, engine="ollama", action=action, error=err)
            return EngineActionResult(
                ok=True,
                engine="ollama",
                action=action,
                binary_path=shutil.which("ollama") or "",
                steps=[f"Completed: {' '.join(cmd)}"],
            )
        return EngineActionResult(
            ok=False,
            engine="ollama",
            action=action,
            error="Install Homebrew or download Ollama from https://ollama.com/download/mac",
        )

    if os_name == "win":
        if shutil.which("winget"):
            cmd = [
                "winget",
                "upgrade" if existing else "install",
                "--id",
                "Ollama.Ollama",
                "-e",
                "--accept-source-agreements",
                "--accept-package-agreements",
            ]
            if dry_run:
                return EngineActionResult(
                    ok=True, engine="ollama", action="dry-run", steps=[f"Would run: {' '.join(cmd)}"]
                )
            with step_ctx("Installing Ollama via winget"):
                rc, _, err = _run_cmd(cmd, timeout=300.0)
            if rc != 0:
                return EngineActionResult(ok=False, engine="ollama", action=action, error=err)
            return EngineActionResult(
                ok=True,
                engine="ollama",
                action=action,
                binary_path=shutil.which("ollama") or "",
                steps=[f"Completed: {' '.join(cmd)}"],
            )
        return EngineActionResult(
            ok=False,
            engine="ollama",
            action=action,
            error="winget not found; download Ollama from https://ollama.com/download/windows",
        )

    # Linux
    if dry_run:
        return EngineActionResult(
            ok=True,
            engine="ollama",
            action="dry-run",
            steps=["Would run: curl -fsSL https://ollama.com/install.sh | sh"],
        )
    with step_ctx("Installing Ollama"):
        rc, _, err = _run_cmd(["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"], timeout=300.0)
    if rc != 0:
        return EngineActionResult(ok=False, engine="ollama", action=action, error=err)
    return EngineActionResult(
        ok=True,
        engine="ollama",
        action=action,
        binary_path=shutil.which("ollama") or "",
        steps=["Installed/updated Ollama via official install script"],
    )
