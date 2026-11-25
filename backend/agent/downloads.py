"""
Hugging Face model download helpers (`hf_xet` accelerated + async HTTP fallback)
with live chunked throughput tracking for `splitch-agent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from backend.shared.models import DownloadJob

logger = logging.getLogger("splitch.agent")


class DownloadSpeedTracker:
    """Compute smoothed bytes/sec across chunked downloads."""

    def __init__(self) -> None:
        now = time.monotonic()
        self._start = now
        self._last_tick = now
        self._window_bytes = 0

    def record(self, job: DownloadJob, chunk_len: int) -> None:
        self._window_bytes += chunk_len
        now = time.monotonic()
        dt = now - self._last_tick
        if dt >= 0.4:
            instant = self._window_bytes / dt
            job.speed_bytes_per_sec = round(
                instant
                if job.speed_bytes_per_sec <= 0.0
                else (job.speed_bytes_per_sec * 0.4 + instant * 0.6),
                1,
            )
            self._last_tick = now
            self._window_bytes = 0
        elif job.speed_bytes_per_sec <= 0.0:
            elapsed = max(0.05, now - self._start)
            job.speed_bytes_per_sec = round(job.downloaded_bytes / elapsed, 1)


_DownloadSpeedTracker = DownloadSpeedTracker


def resolve_hf_protocol() -> str:
    raw = os.getenv("SPLITCH_HF_PROTOCOL", "").strip().lower()
    if raw in ("auto", "xet", "http"):
        return raw
    if os.getenv("PYTEST_CURRENT_TEST"):
        return "http"
    return "auto"


def make_job_tqdm(job: DownloadJob, tracker: DownloadSpeedTracker, *, cap_99: bool = False):
    from tqdm import tqdm as _BaseTqdm

    lock = threading.Lock()

    class _JobTqdm(_BaseTqdm):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("disable", True)
            super().__init__(*args, **kwargs)
            tot = int(getattr(self, "total", 0) or 0)
            if tot > 0:
                with lock:
                    if cap_99:
                        if tot > job.total_bytes and job.total_bytes == 0:
                            job.total_bytes = tot
                    else:
                        job.total_bytes = max(job.total_bytes, tot)

        def update(self, n: float = 1) -> bool | None:
            inc = int(n or 0)
            if inc > 0:
                with lock:
                    job.downloaded_bytes += inc
                    tracker.record(job, inc)
                    if job.total_bytes > 0:
                        pct = round((job.downloaded_bytes / job.total_bytes) * 100.0, 1)
                        job.percent = min(99.9, pct) if cap_99 else min(100.0, pct)
            return super().update(n)

    return _JobTqdm


def download_xet_single(
    job: DownloadJob,
    dest: Path,
    staging_dir: Path,
    hf_token: str,
    tracker: DownloadSpeedTracker,
) -> None:
    from huggingface_hub import hf_hub_download

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    tqdm_cls = make_job_tqdm(job, tracker, cap_99=False)
    try:
        downloaded = hf_hub_download(
            repo_id=job.repo_id,
            filename=job.filename,
            local_dir=str(staging_dir),
            token=hf_token or None,
            tqdm_class=tqdm_cls,
        )
        src = Path(downloaded)
        if not src.is_file():
            raise FileNotFoundError(f"hf_hub_download missing output: {src}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dest)
    finally:
        with contextlib.suppress(OSError):
            if staging_dir.exists():
                shutil.rmtree(staging_dir)


def download_xet_mlx(
    job: DownloadJob,
    dest_dir: Path,
    tmp_dir: Path,
    hf_token: str,
    tracker: DownloadSpeedTracker,
) -> None:
    from huggingface_hub import snapshot_download

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tqdm_cls = make_job_tqdm(job, tracker, cap_99=True)
    try:
        snapshot_download(
            repo_id=job.repo_id,
            local_dir=str(tmp_dir),
            allow_patterns=["*.safetensors", "*.json", "*.model", "*.tiktoken", "*.txt"],
            token=hf_token or None,
            tqdm_class=tqdm_cls,
        )
        if not any(tmp_dir.rglob("*.safetensors")):
            raise ValueError("Repository does not contain MLX .safetensors weights")
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        tmp_dir.replace(dest_dir)
    except Exception:
        with contextlib.suppress(OSError):
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
        raise


def _reset_for_http_fallback(job: DownloadJob) -> None:
    job.downloaded_bytes = 0
    job.total_bytes = 0
    job.percent = 0.0
    job.speed_bytes_per_sec = 0.0


async def run_hf_download(
    job: DownloadJob,
    *,
    storage_path: str,
    refresh_engine_cb: Callable[[str], Awaitable[None]],
    poll_once_cb: Callable[[], Awaitable[None]],
    async_client_cls: type[httpx.AsyncClient] | None = None,
    hf_token_override: str = "",
) -> None:
    """Download a single GGUF/safetensors/Strata model via Xet with HTTP fallback."""
    root = Path(storage_path)
    root.mkdir(parents=True, exist_ok=True)
    safe_name = Path(job.filename).name
    dest = root / safe_name
    part = root / f"{safe_name}.part"
    staging_dir = root / f".{safe_name}.xet"
    url = f"https://huggingface.co/{job.repo_id}/resolve/main/{job.filename}"
    headers: dict[str, str] = {}
    hf_token = hf_token_override.strip() or os.getenv("HF_TOKEN", "").strip()
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"

    proto = resolve_hf_protocol()
    if proto in ("auto", "xet"):
        xet_tracker = DownloadSpeedTracker()
        job.protocol = "xet"
        try:
            await asyncio.to_thread(
                download_xet_single, job, dest, staging_dir, hf_token, xet_tracker
            )
            job.percent = 100.0
            job.speed_bytes_per_sec = 0.0
            job.status = "completed"
            if safe_name.lower().endswith(".gguf"):
                asyncio.create_task(refresh_engine_cb(dest.stem))
            else:
                asyncio.create_task(poll_once_cb())
            return
        except Exception as exc:  # noqa: BLE001 — fall back to async httpx stream when xet fails
            if proto == "xet":
                job.speed_bytes_per_sec = 0.0
                job.status = "failed"
                job.error = str(exc)
                return
            logger.debug("hf_xet single download failed (%s); falling back to HTTP", exc)
            _reset_for_http_fallback(job)

    client_factory = async_client_cls or httpx.AsyncClient
    job.protocol = "http"
    tracker = DownloadSpeedTracker()
    try:
        async with (
            client_factory(
                follow_redirects=True,
                timeout=httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0),
            ) as dl_client,
            dl_client.stream("GET", url, headers=headers) as resp,
        ):
            resp.raise_for_status()
            job.total_bytes = int(resp.headers.get("content-length") or 0)
            with part.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    await asyncio.to_thread(f.write, chunk)
                    job.downloaded_bytes += len(chunk)
                    tracker.record(job, len(chunk))
                    if job.total_bytes > 0:
                        job.percent = round((job.downloaded_bytes / job.total_bytes) * 100.0, 1)
        part.replace(dest)
        job.percent = 100.0
        job.speed_bytes_per_sec = 0.0
        job.status = "completed"
        if safe_name.lower().endswith(".gguf"):
            asyncio.create_task(refresh_engine_cb(dest.stem))
        else:
            asyncio.create_task(poll_once_cb())
    except (httpx.HTTPError, OSError) as exc:
        job.speed_bytes_per_sec = 0.0
        job.status = "failed"
        job.error = str(exc)
        with contextlib.suppress(OSError):
            if part.exists():
                part.unlink()


async def run_hf_mlx_download(
    job: DownloadJob,
    *,
    storage_path: str,
    poll_once_cb: Callable[[], Awaitable[None]],
    async_client_cls: type[httpx.AsyncClient] | None = None,
    hf_token_override: str = "",
) -> None:
    """Download an MLX safetensors snapshot directory via Xet with HTTP tree fallback."""
    mlx_root = Path(storage_path) / "mlx"
    mlx_root.mkdir(parents=True, exist_ok=True)
    slug = job.repo_id.replace("/", "--")
    dest_dir = mlx_root / slug
    tmp_dir = mlx_root / f".{slug}.part"
    headers: dict[str, str] = {}
    hf_token = hf_token_override.strip() or os.getenv("HF_TOKEN", "").strip()
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"

    proto = resolve_hf_protocol()
    if proto in ("auto", "xet"):
        xet_tracker = DownloadSpeedTracker()
        job.protocol = "xet"
        try:
            await asyncio.to_thread(
                download_xet_mlx, job, dest_dir, tmp_dir, hf_token, xet_tracker
            )
            job.percent = 100.0
            job.speed_bytes_per_sec = 0.0
            job.status = "completed"
            asyncio.create_task(poll_once_cb())
            return
        except Exception as exc:  # noqa: BLE001 — fall back to async httpx stream when xet fails
            if proto == "xet":
                job.speed_bytes_per_sec = 0.0
                job.status = "failed"
                job.error = str(exc)
                return
            logger.debug("hf_xet MLX snapshot download failed (%s); falling back to HTTP", exc)
            _reset_for_http_fallback(job)

    client_factory = async_client_cls or httpx.AsyncClient
    job.protocol = "http"
    tracker = DownloadSpeedTracker()
    try:
        async with client_factory(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0),
        ) as dl_client:
            tree_resp = await dl_client.get(
                f"https://huggingface.co/api/models/{job.repo_id}/tree/main",
                params={"recursive": "true"},
                headers=headers,
            )
            tree_resp.raise_for_status()
            entries = tree_resp.json()
            if not isinstance(entries, list):
                raise TypeError("Unexpected HF tree response")

            wanted: list[tuple[str, int]] = []
            for item in entries:
                if not isinstance(item, dict) or item.get("type") != "file":
                    continue
                path_str = str(item.get("path") or "")
                if not path_str or ".." in path_str or path_str.startswith("."):
                    continue
                lo = path_str.lower()
                if lo.endswith((".safetensors", ".json", ".model", ".tiktoken", ".txt")):
                    size_b = int(
                        (item.get("lfs") or {}).get("size") or item.get("size") or 0
                    )
                    wanted.append((path_str, size_b))

            if not any(p.lower().endswith(".safetensors") for p, _ in wanted):
                raise ValueError("Repository does not contain MLX .safetensors weights")

            job.total_bytes = sum(sz for _, sz in wanted)
            if tmp_dir.exists():
                await asyncio.to_thread(shutil.rmtree, tmp_dir)
            tmp_dir.mkdir(parents=True, exist_ok=True)

            for rel_path, _ in wanted:
                out_file = (tmp_dir / rel_path).resolve()
                if not out_file.is_relative_to(tmp_dir.resolve()):
                    continue
                out_file.parent.mkdir(parents=True, exist_ok=True)
                url = f"https://huggingface.co/{job.repo_id}/resolve/main/{rel_path}"
                async with dl_client.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()
                    with out_file.open("wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            await asyncio.to_thread(f.write, chunk)
                            job.downloaded_bytes += len(chunk)
                            tracker.record(job, len(chunk))
                            if job.total_bytes > 0:
                                job.percent = min(
                                    99.9,
                                    round((job.downloaded_bytes / job.total_bytes) * 100.0, 1),
                                )

        if dest_dir.exists():
            await asyncio.to_thread(shutil.rmtree, dest_dir)
        tmp_dir.replace(dest_dir)
        job.percent = 100.0
        job.speed_bytes_per_sec = 0.0
        job.status = "completed"
        asyncio.create_task(poll_once_cb())
    except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
        job.speed_bytes_per_sec = 0.0
        job.status = "failed"
        job.error = str(exc)
        with contextlib.suppress(OSError):
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
