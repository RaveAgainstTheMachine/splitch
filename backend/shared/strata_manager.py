"""
Splitch Managed Strata (`Niko1221/Strata`) Engine Manager, 4-Tier Fit Estimator & CLI Wrapper.

Supports:
  - SHA-256-verified `strata-control` installation from official `Niko1221/Strata` GitHub Releases
    (`linux-x86_64`, `linux-aarch64`, `macos-arm64`, `windows-x86_64`).
  - Curated MoE catalog + 4-tier fit estimation (VRAM + Host RAM + Unified Memory + NVMe/SSD).
  - Safe argument-validated command construction for `strata launch`, `strata models`,
    and 1-click Quick Tune calibration (`--tune quick` / `--tune thorough`).
"""

from __future__ import annotations

import hashlib
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
from typing import Any

import httpx

from backend.router.config import default_data_root
from backend.shared.models import (
    NodeStatus,
    StrataCalibrateRequest,
    StrataFitAssessment,
    StrataFitTier,
    StrataInstalledModel,
    StrataLaunchRequest,
    StrataModelSpec,
)
from backend.shared.progress import TerminalProgress, null_download, null_step

STRATA_RELEASES_BASE = "https://github.com/Niko1221/Strata/releases"
STRATA_MANIFEST_LATEST_URL = f"{STRATA_RELEASES_BASE}/latest/download/install-manifest.txt"

_SAFE_FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_SAFE_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
_SAFE_PRESET_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
_SAFE_CONTEXT_RE = re.compile(r"^(8k|16k|32k|64k|128k|256k|512k|[0-9]{4,6})$", re.IGNORECASE)


STRATA_CATALOG: tuple[StrataModelSpec, ...] = (
    StrataModelSpec(
        family="inkling",
        model="inkling-2-mini",
        label="Inkling-2-Mini (Compact MoE)",
        provider="Strata",
        download_gb=8.5,
        experts_ram_gb=8.5,
        min_vram_gb=6.0,
        ssd_shard_gb=0.0,
        supports_vision=False,
        supports_dflash2=False,
        notes="Fastest entry MoE; fits 8–24 GB GPUs or 16 GB Apple Silicon.",
    ),
    StrataModelSpec(
        family="qwen",
        model="coder",
        label="Qwen3.8-Flash-Next Coder (IQ1_M)",
        provider="ISTA-DASLab",
        download_gb=23.0,
        experts_ram_gb=23.0,
        min_vram_gb=10.0,
        ssd_shard_gb=29.0,
        supports_vision=True,
        supports_dflash2=True,
        notes="Coding-focused pack; runs on 32 GB RAM + 12 GB GPU with SSD lookup shard.",
    ),
    StrataModelSpec(
        family="qwen",
        model="q2_0",
        label="Qwen3.8-Flash-Next (Q2_0)",
        provider="ISTA-DASLab",
        download_gb=37.6,
        experts_ram_gb=34.0,
        min_vram_gb=10.0,
        ssd_shard_gb=29.0,
        supports_vision=True,
        supports_dflash2=True,
        notes="Fastest general-purpose Qwen3.8 MoE tier; 48+ GB RAM recommended.",
    ),
    StrataModelSpec(
        family="qwen",
        model="iq2_xs",
        label="Qwen3.8-Flash-Next (IQ2_XS)",
        provider="ISTA-DASLab",
        download_gb=39.2,
        experts_ram_gb=35.5,
        min_vram_gb=10.0,
        ssd_shard_gb=29.0,
        supports_vision=True,
        supports_dflash2=True,
        notes="Balanced speed & quality on 48–64 GB RAM + 12+ GB GPU.",
    ),
    StrataModelSpec(
        family="qwen",
        model="iq3_xxs",
        label="Qwen3.8-Flash-Next (IQ3_XXS)",
        provider="ISTA-DASLab",
        download_gb=47.0,
        experts_ram_gb=43.0,
        min_vram_gb=12.0,
        ssd_shard_gb=29.0,
        supports_vision=True,
        supports_dflash2=True,
        notes="High-accuracy pack for 64+ GB RAM workstations.",
    ),
    StrataModelSpec(
        family="qwen",
        model="iq3_s",
        label="Qwen3.8-Flash-Next (IQ3_S)",
        provider="ISTA-DASLab",
        download_gb=54.8,
        experts_ram_gb=50.0,
        min_vram_gb=12.0,
        ssd_shard_gb=29.0,
        supports_vision=True,
        supports_dflash2=True,
        notes="Highest ISTA-DASLab quality; needs 64+ GB RAM.",
    ),
    StrataModelSpec(
        family="qwen36",
        model="ud-q4_k_xl",
        label="Qwen3.6-35B-A3B (Unsloth UD-Q4_K_XL)",
        provider="Unsloth",
        download_gb=24.0,
        experts_ram_gb=22.0,
        min_vram_gb=12.0,
        ssd_shard_gb=0.0,
        supports_vision=False,
        supports_dflash2=False,
        notes="35B-A3B sparse MoE; fits 32 GB Unified Memory or 32+ GB RAM + GPU.",
    ),
    StrataModelSpec(
        family="dsv41",
        model="iq2_xs",
        label="DeepSeek-V4.1-Flash (IQ2_XS)",
        provider="Strata",
        download_gb=64.0,
        experts_ram_gb=58.0,
        min_vram_gb=16.0,
        ssd_shard_gb=0.0,
        supports_vision=False,
        supports_dflash2=False,
        notes="Large DeepSeek MoE; requires 64–96+ GB RAM or Apple Silicon Ultra.",
    ),
    StrataModelSpec(
        family="hy4",
        model="q4_k_m",
        label="Tencent Hy4-Flash (Q4_K_M)",
        provider="Strata",
        download_gb=70.0,
        experts_ram_gb=64.0,
        min_vram_gb=12.0,
        ssd_shard_gb=16.0,
        supports_vision=False,
        supports_dflash2=False,
        notes="Native sparse MoE + Engram SSD shard; needs 64–128 GB RAM.",
    ),
)


def managed_strata_dir() -> Path:
    return default_data_root() / "engines" / "strata"


def strata_meta_path() -> Path:
    return default_data_root() / "strata_engine_meta.json"


def read_strata_engine_meta() -> dict[str, Any]:
    p = strata_meta_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_strata_engine_meta(meta: dict[str, Any]) -> None:
    p = strata_meta_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def strata_platform_key(os_name: str | None = None, arch: str | None = None) -> str | None:
    """Map host OS/arch to Strata `install-manifest.txt` platform key."""
    sys_s = (os_name or platform.system()).lower()
    mach_s = (arch or platform.machine()).lower()
    if sys_s.startswith("darwin") or sys_s == "macos":
        if mach_s in ("arm64", "aarch64"):
            return "macos-arm64"
        return None
    if sys_s.startswith("win"):
        return "windows-x86_64" if mach_s in ("x86_64", "amd64", "x64") else None
    if sys_s.startswith("linux"):
        if mach_s in ("x86_64", "amd64", "x64"):
            return "linux-x86_64"
        if mach_s in ("arm64", "aarch64"):
            return "linux-aarch64"
    return None


def parse_install_manifest(text: str) -> dict[str, dict[str, Any]]:
    """Parse `install-manifest.txt` lines (`<platform> <version> <archive> <sha256> <size>`)."""
    out: dict[str, dict[str, Any]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        plat, ver, archive, sha256, size_s = parts[:5]
        try:
            size_b = int(size_s)
        except ValueError:
            size_b = 0
        out[plat] = {
            "platform": plat,
            "version": ver,
            "archive": archive,
            "sha256": sha256.lower(),
            "size": size_b,
        }
    return out


def resolve_strata_bin() -> Path | None:
    """Locate managed `<data_root>/engines/strata/bin/strata` or system `strata`."""
    meta = read_strata_engine_meta()
    exe_name = "strata.exe" if sys.platform == "win32" else "strata"
    candidates: list[Path] = []
    if meta.get("binary_path"):
        candidates.append(Path(str(meta["binary_path"])))
    if meta.get("shim_path"):
        candidates.append(Path(str(meta["shim_path"])))
    base = managed_strata_dir()
    candidates.append(base / "bin" / exe_name)
    term_dir = base / "terminal"
    if term_dir.is_dir():
        for child in sorted(term_dir.iterdir(), reverse=True):
            if child.is_dir():
                for rel in ("strata", "strata.cmd", "strata.bat", "strata.py"):
                    cand = child / rel
                    if cand.is_file():
                        candidates.append(cand)
    for c in candidates:
        if c.is_file():
            return c
    sys_bin = shutil.which("strata")
    return Path(sys_bin) if sys_bin else None


def strata_env(storage_root: str | Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["STRATA_DATA_DIR"] = str(managed_strata_dir())
    if storage_root:
        env["STRATA_MODELS_DIR"] = str(Path(storage_root) / "strata")
    return env


def _write_strata_shim(target_entry: Path, bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    data_dir = str(managed_strata_dir())
    if sys.platform == "win32":
        shim = bin_dir / "strata.cmd"
        if target_entry.suffix == ".py":
            py_exe = target_entry.parent / "python.exe"
            runner = f'"{py_exe if py_exe.is_file() else sys.executable}" "{target_entry}"'
        else:
            runner = f'"{target_entry}"'
        shim.write_text(
            f'@echo off\r\nset "STRATA_DATA_DIR={data_dir}"\r\n{runner} %*\r\n',
            encoding="utf-8",
        )
        return shim

    shim = bin_dir / "strata"
    if target_entry.suffix == ".py":
        py_cand = target_entry.parent / "python3"
        if not py_cand.is_file():
            py_cand = target_entry.parent / "python"
        runner = f'"{py_cand if py_cand.is_file() else sys.executable}" "{target_entry}"'
    else:
        runner = f'"{target_entry}"'
    shim.write_text(
        "#!/bin/sh\n"
        f'export STRATA_DATA_DIR="{data_dir}"\n'
        f'exec {runner} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _find_extracted_entry(unpack_dir: Path) -> Path | None:
    for name in ("strata", "strata.exe", "strata.cmd", "strata.py"):
        direct = unpack_dir / name
        if direct.is_file():
            return direct
    for name in ("strata", "strata.exe", "strata.cmd", "strata.py"):
        for hit in sorted(unpack_dir.rglob(name)):
            if hit.is_file():
                return hit
    return None


def install_or_update_strata(
    *,
    version: str = "latest",
    bin_dir: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
    progress: TerminalProgress | None = None,
):
    from backend.shared.engines import EngineActionResult

    step_ctx = progress.step if progress is not None else null_step
    plat_key = strata_platform_key()
    if not plat_key:
        return EngineActionResult(
            ok=False,
            engine="strata",
            error=f"Strata does not ship binaries for {platform.system()}/{platform.machine()} (requires Linux x86_64/arm64, Apple Silicon macOS, or Windows x64).",
        )

    base_url = (
        f"{STRATA_RELEASES_BASE}/latest/download"
        if not version or version == "latest"
        else f"{STRATA_RELEASES_BASE}/download/{version if version.startswith('v') else 'v' + version}"
    )
    manifest_url = f"{base_url}/install-manifest.txt"
    prev_meta = read_strata_engine_meta()
    prev_ver = str(prev_meta.get("version") or "")

    try:
        with (
            step_ctx("Fetching Strata release manifest"),
            httpx.Client(timeout=20.0, follow_redirects=True) as client,
        ):
            r = client.get(manifest_url)
            r.raise_for_status()
            manifest = parse_install_manifest(r.text)
    except (httpx.HTTPError, ValueError) as exc:
        return EngineActionResult(
            ok=False,
            engine="strata",
            previous_tag=prev_ver,
            error=f"Failed to fetch Strata manifest: {exc}",
        )

    entry = manifest.get(plat_key)
    if not entry:
        return EngineActionResult(
            ok=False,
            engine="strata",
            previous_tag=prev_ver,
            error=f"Platform '{plat_key}' missing from Strata install-manifest.txt",
        )

    target_ver = str(entry["version"])
    archive_name = str(entry["archive"])
    expected_sha = str(entry["sha256"])
    archive_url = f"{base_url}/{archive_name}"
    existing_bin = resolve_strata_bin()
    resolved_bin_dir = bin_dir or (Path.home() / ".local" / "bin")

    if prev_ver == target_ver and existing_bin and existing_bin.is_file() and not force:
        return EngineActionResult(
            ok=True,
            engine="strata",
            action="noop",
            previous_tag=prev_ver,
            current_tag=prev_ver,
            backend="strata",
            binary_path=str(existing_bin),
            shim_path=str(prev_meta.get("shim_path") or ""),
            steps=[f"Managed Strata is already up to date (v{prev_ver})"],
        )

    if dry_run:
        return EngineActionResult(
            ok=True,
            engine="strata",
            action="dry-run",
            previous_tag=prev_ver,
            current_tag=target_ver,
            backend="strata",
            steps=[
                f"Resolved Strata {target_ver} ({plat_key}): {archive_name}",
                f"Expected SHA-256: {expected_sha}",
                f"Would install into {managed_strata_dir() / 'terminal' / target_ver}",
            ],
        )

    steps: list[str] = []
    dest_ver_dir = managed_strata_dir() / "terminal" / target_ver
    dest_ver_dir.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="splitch-strata-") as tmp_str:
        tmp_dir = Path(tmp_str)
        archive_path = tmp_dir / archive_name
        try:
            with (
                httpx.Client(
                    timeout=httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0),
                    follow_redirects=True,
                ) as client,
                client.stream("GET", archive_url) as resp,
            ):
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or entry.get("size") or 0)
                dl_ctx = (
                    progress.download(f"Downloading {archive_name}", total=total)
                    if progress is not None
                    else null_download(f"Downloading {archive_name}", total)
                )
                hasher = hashlib.sha256()
                with dl_ctx as bar, archive_path.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        hasher.update(chunk)
                        bar.advance(len(chunk))
                actual_sha = hasher.hexdigest().lower()
        except (httpx.HTTPError, OSError) as exc:
            return EngineActionResult(
                ok=False,
                engine="strata",
                previous_tag=prev_ver,
                error=f"Strata download failed: {exc}",
            )

        if actual_sha != expected_sha:
            return EngineActionResult(
                ok=False,
                engine="strata",
                previous_tag=prev_ver,
                error=f"SHA-256 mismatch for {archive_name}: expected {expected_sha}, got {actual_sha}",
            )
        steps.append(f"Verified SHA-256 for {archive_name}")

        stage_dir = tmp_dir / "unpacked"
        stage_dir.mkdir(parents=True, exist_ok=True)
        try:
            if archive_name.endswith(".zip"):
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(stage_dir)
            else:
                with tarfile.open(archive_path, mode="r:gz") as tf:
                    tf.extractall(stage_dir, filter="data")
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            return EngineActionResult(
                ok=False,
                engine="strata",
                previous_tag=prev_ver,
                error=f"Failed to extract {archive_name}: {exc}",
            )

        if dest_ver_dir.exists():
            shutil.rmtree(dest_ver_dir, ignore_errors=True)
        shutil.move(str(stage_dir), str(dest_ver_dir))

    entry_bin = _find_extracted_entry(dest_ver_dir)
    if entry_bin is None:
        return EngineActionResult(
            ok=False,
            engine="strata",
            previous_tag=prev_ver,
            error=f"Strata entry binary not found inside {dest_ver_dir}",
        )

    shim_path = _write_strata_shim(entry_bin, resolved_bin_dir)
    steps.append(f"Installed Strata v{target_ver} -> {dest_ver_dir}")
    steps.append(f"Wrote shim: {shim_path}")

    write_strata_engine_meta(
        {
            "engine": "strata",
            "version": target_ver,
            "platform": plat_key,
            "binary_path": str(entry_bin),
            "shim_path": str(shim_path),
            "install_dir": str(dest_ver_dir),
            "updated_at": time.time(),
        }
    )

    return EngineActionResult(
        ok=True,
        engine="strata",
        action="update" if prev_ver else "install",
        previous_tag=prev_ver,
        current_tag=target_ver,
        backend="strata",
        binary_path=str(entry_bin),
        shim_path=str(shim_path),
        steps=steps,
    )


def assess_strata_fit(spec: StrataModelSpec, node: NodeStatus) -> StrataFitAssessment:
    """
    4-tier hardware fit assessment for Strata's VRAM + Host RAM + Unified + SSD expert split.
    """
    t = node.telemetry
    if t is None:
        return StrataFitAssessment(
            node_id=node.node_id,
            tier="insufficient",
            reason="Node offline (no hardware telemetry)",
        )

    vram_gb = round(t.vram_total_mb / 1024.0, 1)
    ram_gb = round(t.ram_total_mb / 1024.0, 1)
    ssd_free_gb = round(t.storage_free_gb, 1)
    is_unified = bool(node.role == "unified" or (t.gpus and any(g.is_unified for g in t.gpus)))

    total_download = spec.download_gb + spec.ssd_shard_gb
    if ssd_free_gb > 0 and ssd_free_gb < (total_download + 4.0):
        return StrataFitAssessment(
            node_id=node.node_id,
            tier="insufficient",
            reason=f"Needs {total_download + 4:.0f} GB SSD free ({ssd_free_gb:.1f} GB available)",
            vram_gb=vram_gb,
            ram_gb=ram_gb,
            ssd_free_gb=ssd_free_gb,
            is_unified=is_unified,
        )

    if is_unified:
        # Unified memory counts once (Apple Silicon / AMD Strix Halo)
        pool_gb = max(vram_gb, ram_gb)
        usable = pool_gb * 0.85
        needed = spec.experts_ram_gb + 4.0
        if usable >= needed + 6.0:
            tier: StrataFitTier = "optimal"
            reason = f"Unified {pool_gb:.0f} GB fits weights + KV cleanly"
        elif usable >= needed:
            tier = "tight"
            reason = f"Tight on {pool_gb:.0f} GB unified memory (keep context ≤64k)"
        else:
            tier = "insufficient"
            reason = f"Needs ~{needed:.0f} GB unified memory ({pool_gb:.0f} GB total)"
        return StrataFitAssessment(
            node_id=node.node_id,
            tier=tier,
            reason=reason,
            vram_gb=vram_gb,
            ram_gb=ram_gb,
            ssd_free_gb=ssd_free_gb,
            is_unified=True,
        )

    if vram_gb < spec.min_vram_gb:
        return StrataFitAssessment(
            node_id=node.node_id,
            tier="insufficient",
            reason=f"Needs ≥{spec.min_vram_gb:.0f} GB VRAM ({vram_gb:.1f} GB detected)",
            vram_gb=vram_gb,
            ram_gb=ram_gb,
            ssd_free_gb=ssd_free_gb,
            is_unified=False,
        )

    ram_needed_optimal = spec.experts_ram_gb + 10.0
    if ram_gb >= ram_needed_optimal:
        return StrataFitAssessment(
            node_id=node.node_id,
            tier="optimal",
            reason=f"{vram_gb:.0f} GB VRAM + {ram_gb:.0f} GB RAM holds all hot & warm experts",
            vram_gb=vram_gb,
            ram_gb=ram_gb,
            ssd_free_gb=ssd_free_gb,
            is_unified=False,
        )
    if (ram_gb + vram_gb) >= ram_needed_optimal:
        return StrataFitAssessment(
            node_id=node.node_id,
            tier="low_ram",
            reason=f"Low-RAM split: {vram_gb:.0f} GB VRAM + {ram_gb:.0f} GB RAM (partial SSD paging)",
            vram_gb=vram_gb,
            ram_gb=ram_gb,
            ssd_free_gb=ssd_free_gb,
            is_unified=False,
        )
    return StrataFitAssessment(
        node_id=node.node_id,
        tier="tight" if ram_gb >= 28.0 and vram_gb >= 16.0 else "insufficient",
        reason=(
            f"Heavy SSD expert streaming ({ram_gb:.0f} GB RAM < {ram_needed_optimal:.0f} GB)"
            if ram_gb >= 28.0 and vram_gb >= 16.0
            else f"Needs ≥{ram_needed_optimal:.0f} GB RAM or RAM+VRAM ({ram_gb:.0f} GB RAM)"
        ),
        vram_gb=vram_gb,
        ram_gb=ram_gb,
        ssd_free_gb=ssd_free_gb,
        is_unified=False,
    )


def _validate_strata_args(
    *,
    family: str = "",
    model: str = "",
    preset: str = "",
    model_path: str = "",
    context: str = "64k",
    storage_root: Path | None = None,
) -> Path | None:
    if family and not _SAFE_FAMILY_RE.fullmatch(family):
        raise ValueError(f"Invalid Strata family: {family!r}")
    if model and not _SAFE_MODEL_RE.fullmatch(model):
        raise ValueError(f"Invalid Strata model: {model!r}")
    if preset and not _SAFE_PRESET_RE.fullmatch(preset):
        raise ValueError(f"Invalid Strata preset: {preset!r}")
    if context and not _SAFE_CONTEXT_RE.fullmatch(context):
        raise ValueError(f"Invalid Strata context length: {context!r}")
    if model_path:
        if storage_root is None:
            raise ValueError("storage_root required when specifying model_path")
        root_res = storage_root.resolve()
        cand = Path(model_path).expanduser().resolve()
        if not cand.is_relative_to(root_res):
            raise ValueError(f"model_path must reside inside storage root ({root_res})")
        return cand
    return None


def build_strata_launch_cmd(
    strata_bin: Path,
    req: StrataLaunchRequest,
    *,
    storage_root: Path,
    port: int,
) -> list[str]:
    resolved_path = _validate_strata_args(
        family=req.family.strip(),
        model=req.model.strip(),
        preset=req.preset.strip(),
        model_path=req.model_path.strip(),
        context=req.context.strip(),
        storage_root=storage_root,
    )
    cmd = [str(strata_bin), "launch"]
    if req.preset.strip():
        cmd.extend(["--preset", req.preset.strip()])
    elif resolved_path is not None:
        cmd.extend(["--model-path", str(resolved_path)])
    elif req.family.strip() and req.model.strip():
        cmd.extend(["--family", req.family.strip(), "--model", req.model.strip()])
    else:
        raise ValueError("Specify either preset, family+model, or model_path")

    data_dir = storage_root / "strata"
    data_dir.mkdir(parents=True, exist_ok=True)
    cmd.extend(
        [
            "--data-dir",
            str(data_dir),
            "--context",
            req.context.strip(),
            "--kv",
            req.kv,
            "--access",
            "local",
            "--port",
            str(port),
            "--no-api-key",
            "--yes",
            "--no-tui",
        ]
    )
    if req.vision != "auto":
        cmd.extend(["--vision", req.vision])
    if req.drafting != "auto":
        cmd.extend(["--drafting", req.drafting])
    if req.parallel is not None:
        cmd.extend(["--parallel", str(req.parallel)])
    if req.plan_only:
        cmd.extend(["--plan", "--output", "json"])
    else:
        cmd.append("--restart")
    return cmd


def build_strata_calibrate_cmd(
    strata_bin: Path,
    req: StrataCalibrateRequest,
    *,
    storage_root: Path,
    port: int,
) -> list[str]:
    _validate_strata_args(
        family=req.family.strip(),
        model=req.model.strip(),
        preset=req.preset.strip(),
        context=req.context.strip(),
        storage_root=storage_root,
    )
    cmd = [str(strata_bin), "launch"]
    if req.preset.strip():
        cmd.extend(["--preset", req.preset.strip()])
    elif req.family.strip() and req.model.strip():
        cmd.extend(["--family", req.family.strip(), "--model", req.model.strip()])
    else:
        raise ValueError("Specify preset or family+model for Quick Tune calibration")

    data_dir = storage_root / "strata"
    data_dir.mkdir(parents=True, exist_ok=True)
    cmd.extend(
        [
            "--data-dir",
            str(data_dir),
            "--context",
            req.context.strip(),
            "--kv",
            req.kv,
            "--tune",
            req.mode,
            "--access",
            "local",
            "--port",
            str(port),
            "--no-api-key",
            "--yes",
            "--no-tui",
            "--restart",
        ]
    )
    return cmd


def list_installed_strata_models(storage_root: Path | None = None) -> list[StrataInstalledModel]:
    strata_bin = resolve_strata_bin()
    if strata_bin is None:
        return []
    try:
        proc = subprocess.run(
            [str(strata_bin), "models", "list", "--installed", "--output", "json"],
            env=strata_env(storage_root),
            capture_output=True,
            text=True,
            timeout=8.0,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        payload = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []

    raw_items = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        return []
    out: list[StrataInstalledModel] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or item.get("model_id") or item.get("name") or "").strip()
        if not mid:
            continue
        out.append(
            StrataInstalledModel(
                id=mid,
                family=str(item.get("family") or ""),
                config_path=str(item.get("config_path") or item.get("path") or ""),
                component_status=str(item.get("component_status") or item.get("status") or ""),
            )
        )
    return out


def list_strata_presets(storage_root: Path | None = None) -> list[str]:
    strata_bin = resolve_strata_bin()
    if strata_bin is None:
        return []
    try:
        proc = subprocess.run(
            [str(strata_bin), "launch", "--list-presets", "--output", "json"],
            env=strata_env(storage_root),
            capture_output=True,
            text=True,
            timeout=6.0,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        payload = json.loads(proc.stdout)
        items = payload.get("presets") if isinstance(payload, dict) else payload
        if isinstance(items, list):
            return [
                str(p.get("name") if isinstance(p, dict) else p).strip()
                for p in items
                if (isinstance(p, dict) and p.get("name")) or isinstance(p, str)
            ]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return []


def stop_strata_server(storage_root: Path | None = None) -> tuple[bool, str]:
    strata_bin = resolve_strata_bin()
    if strata_bin is None:
        return True, "Strata not installed"
    try:
        proc = subprocess.run(
            [str(strata_bin), "launch", "--stop"],
            env=strata_env(storage_root),
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, out
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def import_strata_gguf_plan(model_path: str, storage_root: Path) -> dict[str, Any]:
    strata_bin = resolve_strata_bin()
    if strata_bin is None:
        raise FileNotFoundError("Strata is not installed on this node")
    resolved = _validate_strata_args(model_path=model_path, storage_root=storage_root)
    assert resolved is not None
    proc = subprocess.run(
        [str(strata_bin), "models", "import", str(resolved), "--plan", "--output", "json"],
        env=strata_env(storage_root),
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "strata models import failed").strip())
    return json.loads(proc.stdout or "{}")
