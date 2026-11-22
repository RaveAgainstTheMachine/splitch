"""
Cross-platform multi-GPU & heterogeneous GPU discovery and telemetry probing.

Supports homogeneous AND mixed-vendor combinations on a single host:
  - NVIDIA (1..N cards via `nvidia-smi` + per-GPU `pmon` PID attribution)
  - AMD (1..N cards via `rocm-smi --json` + Linux DRM sysfs fallback)
  - Intel (Arc / Data Center / iGPU via `xpu-smi` + Linux DRM sysfs fallback)
  - Apple Silicon (Unified memory via `sysctl`)
  - Any other GPU (Vulkan `vulkaninfo --summary` + Linux DRM sysfs + Windows CIM)

Deduplicates physical adapters across probes using normalized PCI bus IDs and
computes aggregate telemetry + optimal inference backend (auto-selecting Vulkan
for mixed-vendor multi-GPU hosts so llama-server spans all cards).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil

from backend.shared.models import GpuDevice, GpuProcess, GpuVendor

LLM_PROCESS_TOKENS = (
    "llama-server",
    "llama_server",
    "llama-cli",
    "ollama",
    "vllm",
    "lm-studio",
    "lmstudio",
    "koboldcpp",
    "exllama",
    "sd-server",
    "sd-cli",
)

PCI_VENDOR_MAP: dict[str, GpuVendor] = {
    "0x10de": "nvidia",
    "10de": "nvidia",
    "0x1002": "amd",
    "1002": "amd",
    "0x8086": "intel",
    "8086": "intel",
    "0x106b": "apple",
    "106b": "apple",
}


def normalize_bus_id(raw: str | None) -> str | None:
    """Normalize PCI bus IDs like `00000000:01:00.0` or `0000:01:00.0` -> `0000:01:00.0`."""
    if not raw:
        return None
    s = raw.strip().lower()
    m = re.search(r"([0-9a-f]{4,8}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-9a-f])", s)
    if not m:
        return None
    domain = m.group(1)[-4:].zfill(4)
    return f"{domain}:{m.group(2)}:{m.group(3)}.{m.group(4)}"


def classify_vendor(raw_vendor: str | None, name: str = "") -> GpuVendor:
    v = (raw_vendor or "").strip().lower()
    if v in PCI_VENDOR_MAP:
        return PCI_VENDOR_MAP[v]
    combo = f"{v} {name}".lower()
    if "nvidia" in combo or "geforce" in combo or "quadro" in combo or "tesla" in combo or "rtx" in combo:
        return "nvidia"
    if "amd" in combo or "radeon" in combo or "instinct" in combo or "ati " in combo:
        return "amd"
    if "intel" in combo or "arc " in combo or "iris" in combo or "uhd graphics" in combo:
        return "intel"
    if "apple" in combo:
        return "apple"
    return "other"


async def _run_async(args: list[str], timeout: float = 3.0) -> tuple[int, str]:
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace").strip()
    except TimeoutError:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        return -1, ""
    except (FileNotFoundError, OSError):
        return -1, ""


def _run_sync(args: list[str], timeout: float = 3.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return -1, ""


def parse_nvidia_pmon(output: str) -> list[GpuProcess]:
    """Parse `nvidia-smi pmon -c 1 -s m` with per-GPU index attribution."""
    processes: list[GpuProcess] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 5)
        if len(parts) < 6:
            continue
        gpu_s, pid_s, _ptype, fb_s, _ccpm, cmd = parts
        if pid_s == "-" or fb_s == "-":
            continue
        try:
            gpu_idx = int(gpu_s) if gpu_s.isdigit() else 0
            pid = int(pid_s)
            vram_mb = float(fb_s)
        except ValueError:
            continue
        cmd_lower = cmd.lower()
        is_llm = any(tok in cmd_lower for tok in LLM_PROCESS_TOKENS)
        processes.append(
            GpuProcess(
                pid=pid,
                name=cmd.strip(),
                vram_mb=vram_mb,
                is_llm=is_llm,
                gpu_index=gpu_idx,
            )
        )
    return processes


def parse_nvidia_csv(
    csv_out: str, pmon_out: str = ""
) -> tuple[list[GpuDevice], list[GpuProcess]]:
    """Parse multi-line `nvidia-smi --query-gpu=...` CSV and attribute `pmon` processes per card."""
    procs = parse_nvidia_pmon(pmon_out) if pmon_out else []
    gpus: list[GpuDevice] = []

    for row_num, raw_line in enumerate(csv_out.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 8:
            continue
        try:
            nv_idx = int(cols[0])
        except ValueError:
            nv_idx = row_num
        name = cols[1]
        bus_id = normalize_bus_id(cols[2])

        def _f(val: str) -> float:
            try:
                return float(val)
            except ValueError:
                return 0.0

        def _f_opt(val: str) -> float | None:
            try:
                return float(val)
            except ValueError:
                return None

        vram_total = _f(cols[3])
        vram_used = _f(cols[4])
        vram_free = _f(cols[5])
        if vram_free == 0.0 and vram_total > vram_used:
            vram_free = round(max(0.0, vram_total - vram_used), 1)
        util = _f(cols[6])
        temp = _f_opt(cols[7])

        card_procs = [p for p in procs if p.gpu_index == nv_idx]
        for p in card_procs:
            p.gpu_bus_id = bus_id
        non_llm = round(sum(p.vram_mb for p in card_procs if not p.is_llm), 1)

        gpus.append(
            GpuDevice(
                index=nv_idx,
                vendor="nvidia",
                name=name,
                bus_id=bus_id,
                vram_total_mb=round(vram_total, 1),
                vram_used_mb=round(vram_used, 1),
                vram_free_mb=round(vram_free, 1),
                util_percent=round(util, 1),
                temp_c=temp,
                non_llm_vram_mb=non_llm,
                is_unified=False,
                backend_hint="cuda",
            )
        )
    return gpus, procs


def parse_rocm_json(raw_json: str) -> list[GpuDevice]:
    """Parse ALL AMD ROCm cards from `rocm-smi ... --json` output."""
    if not raw_json:
        return []
    try:
        data = json.loads(raw_json)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []

    gpus: list[GpuDevice] = []
    for card_key, card in sorted(data.items()):
        if not isinstance(card, dict):
            continue
        name = str(
            card.get("Card Series")
            or card.get("Card model")
            or card.get("Card Model")
            or f"AMD GPU ({card_key})"
        )
        bus_id = normalize_bus_id(
            str(card.get("PCI Bus") or card.get("Bus") or card.get("PCI ID") or "")
        )
        try:
            total_b = float(card.get("VRAM Total Memory (B)", 0) or 0)
            used_b = float(card.get("VRAM Total Used Memory (B)", 0) or 0)
        except (ValueError, TypeError):
            total_b, used_b = 0.0, 0.0
        total_mb = round(total_b / (1024 * 1024), 1)
        used_mb = round(used_b / (1024 * 1024), 1)
        free_mb = round(max(0.0, total_mb - used_mb), 1)
        try:
            util = float(card.get("GPU use (%)", 0.0) or 0.0)
        except (ValueError, TypeError):
            util = 0.0
        temp_raw = (
            card.get("Temperature (Sensor edge) (C)")
            or card.get("Temperature (Sensor junction) (C)")
        )
        try:
            temp = float(temp_raw) if temp_raw is not None else None
        except (ValueError, TypeError):
            temp = None

        gpus.append(
            GpuDevice(
                index=len(gpus),
                vendor="amd",
                name=name,
                bus_id=bus_id,
                vram_total_mb=total_mb,
                vram_used_mb=used_mb,
                vram_free_mb=free_mb,
                util_percent=round(util, 1),
                temp_c=temp,
                non_llm_vram_mb=0.0,
                is_unified=False,
                backend_hint="rocm",
            )
        )
    return gpus


def parse_xpu_smi_json(raw_json: str) -> list[GpuDevice]:
    """Parse Intel Arc / Data Center GPUs from `xpu-smi discovery -j`."""
    if not raw_json:
        return []
    try:
        data = json.loads(raw_json)
    except ValueError:
        return []
    devices = data.get("device_list") if isinstance(data, dict) else None
    if not isinstance(devices, list):
        return []

    gpus: list[GpuDevice] = []
    for item in devices:
        if not isinstance(item, dict):
            continue
        name = str(item.get("device_name") or "Intel GPU").strip()
        bus_id = normalize_bus_id(str(item.get("pci_bdf_address") or ""))
        vram_mb = 0.0
        raw_mem = str(item.get("memory_physical_size_byte") or "")
        if raw_mem.isdigit():
            vram_mb = round(int(raw_mem) / (1024 * 1024), 1)
        gpus.append(
            GpuDevice(
                index=len(gpus),
                vendor="intel",
                name=name,
                bus_id=bus_id,
                vram_total_mb=vram_mb,
                vram_used_mb=0.0,
                vram_free_mb=vram_mb,
                util_percent=0.0,
                temp_c=None,
                non_llm_vram_mb=0.0,
                is_unified=False,
                backend_hint="vulkan",
            )
        )
    return gpus


def probe_linux_drm_sysfs(drm_root: Path | None = None) -> list[GpuDevice]:
    """
    Probe Linux `/sys/class/drm/card*/device` for AMD, Intel, NVIDIA, and generic PCI GPUs.
    Works without rocm-smi or xpu-smi installed; extracts PCI bus ID, VRAM, busy %, and temp.
    """
    root = drm_root or Path("/sys/class/drm")
    if not root.exists():
        return []

    gpus: list[GpuDevice] = []
    seen_bus: set[str] = set()
    for card_dir in sorted(root.glob("card[0-9]*")):
        if "-" in card_dir.name:  # skip connectors like card0-DP-1
            continue
        dev_dir = card_dir / "device"
        if not dev_dir.is_dir():
            continue

        def _read_txt(p: Path) -> str:
            try:
                return p.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                return ""

        vendor_hex = _read_txt(dev_dir / "vendor")
        device_hex = _read_txt(dev_dir / "device")
        uevent = _read_txt(dev_dir / "uevent")

        bus_id: str | None = None
        driver_name = ""
        for line in uevent.splitlines():
            if line.startswith("PCI_SLOT_NAME="):
                bus_id = normalize_bus_id(line.split("=", 1)[1])
            elif line.startswith("DRIVER="):
                driver_name = line.split("=", 1)[1].strip()

        if not vendor_hex and not bus_id:
            continue
        if bus_id and bus_id in seen_bus:
            continue
        if bus_id:
            seen_bus.add(bus_id)

        vendor = classify_vendor(vendor_hex, driver_name)

        # VRAM (amdgpu exposes mem_info_vram_total/used; intel xe/i915 may expose lmem_total_bytes)
        total_b_raw = _read_txt(dev_dir / "mem_info_vram_total") or _read_txt(
            dev_dir / "lmem_total_bytes"
        )
        used_b_raw = _read_txt(dev_dir / "mem_info_vram_used") or _read_txt(
            dev_dir / "lmem_avail_bytes"
        )
        total_mb = round(int(total_b_raw) / (1024 * 1024), 1) if total_b_raw.isdigit() else 0.0
        if (dev_dir / "mem_info_vram_used").exists() and used_b_raw.isdigit():
            used_mb = round(int(used_b_raw) / (1024 * 1024), 1)
        elif (dev_dir / "lmem_avail_bytes").exists() and used_b_raw.isdigit() and total_mb > 0:
            avail_mb = round(int(used_b_raw) / (1024 * 1024), 1)
            used_mb = round(max(0.0, total_mb - avail_mb), 1)
        else:
            used_mb = 0.0
        free_mb = round(max(0.0, total_mb - used_mb), 1)

        busy_raw = _read_txt(dev_dir / "gpu_busy_percent")
        util = float(busy_raw) if busy_raw.isdigit() else 0.0

        temp_c: float | None = None
        hwmon_root = dev_dir / "hwmon"
        if hwmon_root.is_dir():
            for hw in sorted(hwmon_root.glob("hwmon*")):
                t_raw = _read_txt(hw / "temp1_input")
                if t_raw.isdigit():
                    temp_c = round(int(t_raw) / 1000.0, 1)
                    break

        product_name = _read_txt(dev_dir / "product_name")
        if not product_name:
            v_label = {
                "nvidia": "NVIDIA",
                "amd": "AMD Radeon",
                "intel": "Intel GPU",
                "apple": "Apple",
                "other": "GPU",
            }[vendor]
            dev_suffix = f" ({device_hex})" if device_hex else ""
            product_name = f"{v_label}{dev_suffix}"

        backend_hint = {
            "nvidia": "cuda",
            "amd": "rocm" if Path("/dev/kfd").exists() else "vulkan",
            "intel": "vulkan",
            "apple": "metal",
            "other": "vulkan",
        }[vendor]

        gpus.append(
            GpuDevice(
                index=len(gpus),
                vendor=vendor,
                name=product_name,
                bus_id=bus_id,
                vram_total_mb=total_mb,
                vram_used_mb=used_mb,
                vram_free_mb=free_mb,
                util_percent=round(util, 1),
                temp_c=temp_c,
                non_llm_vram_mb=0.0,
                is_unified=False,
                backend_hint=backend_hint,
            )
        )
    return gpus


def parse_vulkaninfo_summary(output: str) -> list[GpuDevice]:
    """Parse physical GPUs from `vulkaninfo --summary` (skips software CPU devices like llvmpipe)."""
    if not output:
        return []

    blocks = re.split(r"(?m)^\s*GPU\d+:\s*$", output)
    gpus: list[GpuDevice] = []
    for block in blocks[1:]:
        name_m = re.search(r"deviceName\s*=\s*(.+)", block)
        type_m = re.search(r"deviceType\s*=\s*(\S+)", block)
        vend_m = re.search(r"vendorID\s*=\s*(0x[0-9a-fA-F]+)", block)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        dev_type = type_m.group(1).strip() if type_m else ""
        if "CPU" in dev_type.upper() or "llvmpipe" in name.lower() or "swiftshader" in name.lower():
            continue
        vendor = classify_vendor(vend_m.group(1) if vend_m else None, name)
        gpus.append(
            GpuDevice(
                index=len(gpus),
                vendor=vendor,
                name=name,
                bus_id=None,
                vram_total_mb=0.0,
                vram_used_mb=0.0,
                vram_free_mb=0.0,
                util_percent=0.0,
                temp_c=None,
                non_llm_vram_mb=0.0,
                is_unified="INTEGRATED" in dev_type.upper(),
                backend_hint="vulkan",
            )
        )
    return gpus


def merge_gpu_lists(*probe_lists: list[GpuDevice]) -> list[GpuDevice]:
    """
    Merge GPU lists ordered from highest-fidelity vendor probes (nvidia-smi, rocm-smi, xpu-smi)
    to generic fallbacks (DRM sysfs, vulkaninfo), deduplicating by PCI bus ID or vendor+name.
    """
    merged: list[GpuDevice] = []
    by_bus: dict[str, GpuDevice] = {}

    for g_list in probe_lists:
        for dev in g_list:
            bus = normalize_bus_id(dev.bus_id)
            if bus and bus in by_bus:
                existing = by_bus[bus]
                # Enrich missing fields on existing higher-priority record
                if existing.vram_total_mb == 0.0 and dev.vram_total_mb > 0:
                    existing.vram_total_mb = dev.vram_total_mb
                    existing.vram_used_mb = dev.vram_used_mb
                    existing.vram_free_mb = dev.vram_free_mb
                if existing.temp_c is None and dev.temp_c is not None:
                    existing.temp_c = dev.temp_c
                if existing.util_percent == 0.0 and dev.util_percent > 0:
                    existing.util_percent = dev.util_percent
                if existing.name.endswith(")") and not dev.name.endswith(")") and len(dev.name) > len(existing.name):
                    existing.name = dev.name
                continue

            # When a fallback probe lacks PCI bus ID (e.g. vulkaninfo), match against existing same-vendor card
            if not bus:
                norm_name = re.sub(r"[^a-z0-9]+", "", dev.name.lower())
                dup = next(
                    (
                        m
                        for m in merged
                        if m.vendor == dev.vendor
                        and (
                            norm_name in re.sub(r"[^a-z0-9]+", "", m.name.lower())
                            or re.sub(r"[^a-z0-9]+", "", m.name.lower()) in norm_name
                            or (m.bus_id is not None and m.vram_total_mb > 0 and dev.vram_total_mb == 0)
                        )
                    ),
                    None,
                )
                if dup is not None:
                    if dup.name.startswith(("AMD Radeon (", "Intel GPU (", "NVIDIA (", "GPU (")):
                        dup.name = dev.name
                    continue

            copy = dev.model_copy(update={"bus_id": bus})
            merged.append(copy)
            if bus:
                by_bus[bus] = copy

    # Re-index sequentially 0..N-1 for stable display and yield_gpu_indices mapping
    for idx, dev in enumerate(merged):
        dev.index = idx
    return merged


def recommend_backend_from_gpus(
    gpus: list[GpuDevice],
    *,
    os_name: str | None = None,
    arch: str | None = None,
) -> str:
    """
    Pick optimal inference backend for single, homogeneous multi-GPU, or mixed-vendor multi-GPU hosts.
    Mixed-vendor hosts (e.g. NVIDIA + AMD or NVIDIA + Intel) auto-select `vulkan` so `llama-server`
    can unify VRAM across all vendors simultaneously.
    """
    sys_os = (os_name or platform.system()).lower()
    sys_arch = (arch or platform.machine()).lower()
    if sys_os.startswith("darwin") or sys_os == "macos":
        return "metal" if sys_arch in ("arm64", "aarch64") else "cpu"

    if not gpus:
        return "cpu"

    # Separate discrete/compute GPUs from tiny iGPUs when deciding single vs mixed-vendor
    meaningful = [g for g in gpus if not g.is_unified or g.vram_total_mb >= 2048.0] or gpus
    vendors = {g.vendor for g in meaningful}

    if len(vendors) > 1:
        # Mixed-vendor multi-GPU (NVIDIA+AMD, NVIDIA+Intel, AMD+Intel, etc.) -> unified Vulkan
        return "vulkan"

    sole = next(iter(vendors))
    if sole == "nvidia":
        return "cuda"
    if sole == "amd":
        if shutil.which("rocm-smi") or Path("/dev/kfd").exists():
            return "rocm"
        return "vulkan"
    if sole == "apple":
        return "metal"
    return "vulkan"


def summarize_gpus(gpus: list[GpuDevice]) -> dict[str, Any]:
    """Compute backward-compatible aggregate telemetry fields from per-GPU devices."""
    if not gpus:
        return {
            "gpu_name": None,
            "vram_total_mb": 0.0,
            "vram_used_mb": 0.0,
            "vram_free_mb": 0.0,
            "gpu_util_percent": 0.0,
            "gpu_temp_c": None,
            "non_llm_vram_mb": 0.0,
            "gpu_count": 0,
            "gpu_vendors": [],
        }

    if len(gpus) == 1:
        g = gpus[0]
        label = g.name
    else:
        counts: dict[str, int] = {}
        for g in gpus:
            counts[g.name] = counts.get(g.name, 0) + 1
        parts = [f"{cnt}× {name}" if cnt > 1 else name for name, cnt in counts.items()]
        label = " + ".join(parts)

    temps = [g.temp_c for g in gpus if g.temp_c is not None]
    vendors = sorted({g.vendor for g in gpus})
    return {
        "gpu_name": label,
        "vram_total_mb": round(sum(g.vram_total_mb for g in gpus), 1),
        "vram_used_mb": round(sum(g.vram_used_mb for g in gpus), 1),
        "vram_free_mb": round(sum(g.vram_free_mb for g in gpus), 1),
        "gpu_util_percent": round(max((g.util_percent for g in gpus), default=0.0), 1),
        "gpu_temp_c": max(temps) if temps else None,
        "non_llm_vram_mb": round(sum(g.non_llm_vram_mb for g in gpus), 1),
        "gpu_count": len(gpus),
        "gpu_vendors": vendors,
    }


async def _collect_mac_gpu_async() -> list[GpuDevice]:
    sysctl_bin = "/usr/sbin/sysctl" if Path("/usr/sbin/sysctl").exists() else "sysctl"
    _, brand = await _run_async([sysctl_bin, "-n", "machdep.cpu.brand_string"])
    _, wired_cap = await _run_async([sysctl_bin, "-n", "iogpu.wired_limit_mb"])
    mem = psutil.virtual_memory()
    total_mb = round(mem.total / (1024 * 1024), 1)
    avail_mb = round(mem.available / (1024 * 1024), 1)
    try:
        cap_mb = float(wired_cap) if wired_cap and float(wired_cap) > 0 else round(total_mb * 0.75, 1)
    except ValueError:
        cap_mb = round(total_mb * 0.75, 1)
    vram_free = round(min(avail_mb, cap_mb), 1)
    vram_used = round(max(0.0, cap_mb - vram_free), 1)
    return [
        GpuDevice(
            index=0,
            vendor="apple",
            name=brand or "Apple Silicon",
            bus_id=None,
            vram_total_mb=cap_mb,
            vram_used_mb=vram_used,
            vram_free_mb=vram_free,
            util_percent=0.0,
            temp_c=None,
            non_llm_vram_mb=0.0,
            is_unified=True,
            backend_hint="metal",
        )
    ]


async def _collect_nvidia_async() -> tuple[list[GpuDevice], list[GpuProcess]]:
    if not shutil.which("nvidia-smi"):
        return [], []
    rc, csv_out = await _run_async(
        [
            "nvidia-smi",
            "--query-gpu=index,name,pci.bus_id,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
    )
    if rc != 0 or not csv_out:
        return [], []
    rc_pmon, pmon_out = await _run_async(["nvidia-smi", "pmon", "-c", "1", "-s", "m"])
    return parse_nvidia_csv(csv_out, pmon_out if rc_pmon == 0 else "")


async def _collect_rocm_async() -> list[GpuDevice]:
    if not shutil.which("rocm-smi"):
        return []
    rc, out = await _run_async(
        [
            "rocm-smi",
            "--showproductname",
            "--showbus",
            "--showmeminfo",
            "vram",
            "--showuse",
            "--showtemp",
            "--json",
        ]
    )
    return parse_rocm_json(out) if rc == 0 else []


async def _collect_intel_xpu_async() -> list[GpuDevice]:
    if not shutil.which("xpu-smi"):
        return []
    rc, out = await _run_async(["xpu-smi", "discovery", "-j"])
    return parse_xpu_smi_json(out) if rc == 0 else []


async def _collect_vulkan_async() -> list[GpuDevice]:
    if not shutil.which("vulkaninfo"):
        return []
    rc, out = await _run_async(["vulkaninfo", "--summary"], timeout=4.0)
    return parse_vulkaninfo_summary(out) if rc == 0 else []


async def _collect_windows_cim_async() -> list[GpuDevice]:
    if sys.platform != "win32":
        return []
    ps_bin = shutil.which("powershell") or shutil.which("pwsh")
    if not ps_bin:
        return []
    rc, out = await _run_async(
        [
            ps_bin,
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,PNPDeviceID | ConvertTo-Json -Compress",
        ],
        timeout=4.0,
    )
    if rc != 0 or not out:
        return []
    try:
        parsed = json.loads(out)
        items = parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        return []
    gpus: list[GpuDevice] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("Name"):
            continue
        name = str(item["Name"]).strip()
        if "microsoft basic" in name.lower() or "virtual" in name.lower():
            continue
        pnp = str(item.get("PNPDeviceID") or "")
        vend_m = re.search(r"VEN_([0-9A-Fa-f]{4})", pnp)
        vendor = classify_vendor(f"0x{vend_m.group(1)}" if vend_m else None, name)
        ram_b = item.get("AdapterRAM") or 0
        vram_mb = round(float(ram_b) / (1024 * 1024), 1) if isinstance(ram_b, (int, float)) and ram_b > 0 else 0.0
        gpus.append(
            GpuDevice(
                index=len(gpus),
                vendor=vendor,
                name=name,
                vram_total_mb=vram_mb,
                vram_free_mb=vram_mb,
                backend_hint="cuda" if vendor == "nvidia" else "vulkan",
            )
        )
    return gpus


# Static topology caches — never spawn expensive GPU-context probes (vulkaninfo, xpu-smi
# discovery, Windows CIM) on the 3s telemetry polling hot path.
_STATIC_XPU_CACHE: list[GpuDevice] | None = None
_STATIC_VULKAN_CACHE: list[GpuDevice] | None = None
_STATIC_WIN_CACHE: list[GpuDevice] | None = None


def reset_static_gpu_caches() -> None:
    global _STATIC_XPU_CACHE, _STATIC_VULKAN_CACHE, _STATIC_WIN_CACHE
    _STATIC_XPU_CACHE = None
    _STATIC_VULKAN_CACHE = None
    _STATIC_WIN_CACHE = None


def _needs_vulkan_name_enrichment(primary_gpus: list[GpuDevice]) -> bool:
    if not primary_gpus:
        return True
    return any(
        g.name.startswith(("AMD Radeon (", "Intel GPU (", "NVIDIA (", "GPU ("))
        for g in primary_gpus
    )


async def collect_all_gpus() -> tuple[list[GpuDevice], list[GpuProcess], str]:
    """
    Probe all GPU vendors in parallel on this host, deduplicate by PCI bus ID,
    and return `(gpus, gpu_processes, recommended_backend)`.

    Hot-path safe: only lightweight SMI + sysfs probes run every poll cycle;
    static fallback discovery (`vulkaninfo`, `xpu-smi discovery`, Windows CIM)
    runs at most once per process and `vulkaninfo` is skipped entirely when
    vendor SMI / DRM already identified all physical GPUs.
    """
    global _STATIC_XPU_CACHE, _STATIC_VULKAN_CACHE, _STATIC_WIN_CACHE

    if sys.platform == "darwin":
        mac_gpus = await _collect_mac_gpu_async()
        return mac_gpus, [], recommend_backend_from_gpus(mac_gpus)

    nv_task = _collect_nvidia_async()
    rocm_task = _collect_rocm_async()
    drm_task = (
        asyncio.to_thread(probe_linux_drm_sysfs)
        if sys.platform.startswith("linux")
        else asyncio.sleep(0, result=[])
    )
    (nv_gpus, nv_procs), rocm_gpus, drm_gpus = await asyncio.gather(
        nv_task, rocm_task, drm_task
    )

    if _STATIC_XPU_CACHE is None:
        _STATIC_XPU_CACHE = await _collect_intel_xpu_async()
    if _STATIC_WIN_CACHE is None:
        _STATIC_WIN_CACHE = await _collect_windows_cim_async()

    base_merged = merge_gpu_lists(
        nv_gpus, rocm_gpus, _STATIC_XPU_CACHE, drm_gpus, _STATIC_WIN_CACHE
    )

    if _STATIC_VULKAN_CACHE is None:
        if _needs_vulkan_name_enrichment(base_merged):
            _STATIC_VULKAN_CACHE = await _collect_vulkan_async()
        else:
            _STATIC_VULKAN_CACHE = []

    merged = (
        merge_gpu_lists(base_merged, _STATIC_VULKAN_CACHE)
        if _STATIC_VULKAN_CACHE
        else base_merged
    )

    # Map NVIDIA pmon process gpu_index onto final merged indices using bus_id when present
    bus_to_final = {g.bus_id: g.index for g in merged if g.bus_id}
    for proc in nv_procs:
        if proc.gpu_bus_id and proc.gpu_bus_id in bus_to_final:
            proc.gpu_index = bus_to_final[proc.gpu_bus_id]

    rec_backend = recommend_backend_from_gpus(merged)
    return merged, nv_procs, rec_backend


def detect_gpus_sync() -> tuple[list[GpuDevice], str | None]:
    """Synchronous multi-vendor GPU discovery used by CLI `splitch engine` and `splitch doctor`."""
    if sys.platform == "darwin":
        arch = platform.machine().lower()
        name = "Apple Silicon" if arch in ("arm64", "aarch64") else "macOS"
        if arch in ("arm64", "aarch64"):
            return [GpuDevice(index=0, vendor="apple", name=name, is_unified=True, backend_hint="metal")], None
        return [], None

    cuda_ver: str | None = None
    nv_gpus: list[GpuDevice] = []
    if shutil.which("nvidia-smi"):
        rc_info, out_info = _run_sync(["nvidia-smi"], timeout=5.0)
        if rc_info == 0 and out_info:
            m = re.search(r"CUDA(?:\s+\w+)?\s+Version:\s*(\d+\.\d+)", out_info, re.IGNORECASE)
            if m:
                cuda_ver = m.group(1)
        rc_csv, csv_out = _run_sync(
            [
                "nvidia-smi",
                "--query-gpu=index,name,pci.bus_id,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            timeout=5.0,
        )
        if rc_csv == 0 and csv_out:
            nv_gpus, _ = parse_nvidia_csv(csv_out)

    rocm_gpus: list[GpuDevice] = []
    if shutil.which("rocm-smi"):
        rc_r, out_r = _run_sync(
            ["rocm-smi", "--showproductname", "--showbus", "--showmeminfo", "vram", "--showuse", "--showtemp", "--json"],
            timeout=5.0,
        )
        if rc_r == 0:
            rocm_gpus = parse_rocm_json(out_r)

    xpu_gpus: list[GpuDevice] = []
    if shutil.which("xpu-smi"):
        rc_x, out_x = _run_sync(["xpu-smi", "discovery", "-j"], timeout=5.0)
        if rc_x == 0:
            xpu_gpus = parse_xpu_smi_json(out_x)

    drm_gpus = probe_linux_drm_sysfs() if sys.platform.startswith("linux") else []
    base_merged = merge_gpu_lists(nv_gpus, rocm_gpus, xpu_gpus, drm_gpus)

    vk_gpus: list[GpuDevice] = []
    if _needs_vulkan_name_enrichment(base_merged) and shutil.which("vulkaninfo"):
        rc_v, out_v = _run_sync(["vulkaninfo", "--summary"], timeout=4.0)
        if rc_v == 0:
            vk_gpus = parse_vulkaninfo_summary(out_v)

    merged = merge_gpu_lists(base_merged, vk_gpus) if vk_gpus else base_merged
    # Honor explicit env override if set during tests or containers
    if not merged and os.getenv("SPLITCH_FORCE_GPU_VENDOR"):
        v = classify_vendor(os.getenv("SPLITCH_FORCE_GPU_VENDOR"))
        merged = [GpuDevice(index=0, vendor=v, name=f"{v.upper()} GPU")]
    return merged, cuda_ver
