"""Unit tests for shared models and agent download speed tracking."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.agent import main as agent_mod
from backend.shared.models import DownloadJob


@contextmanager
def _loopback_hf_server(handler_cls: type[BaseHTTPRequestHandler]):
    """Run a real loopback HTTP server standing in for Hugging Face endpoints."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        host, port = srv.server_address
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2.0)


def _patch_hf_redirect(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    """Redirect https://huggingface.co requests to the local loopback server."""
    orig_async_client = httpx.AsyncClient

    class _LoopbackAsyncClient(orig_async_client):
        def build_request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> httpx.Request:
            url_str = str(url)
            if url_str.startswith("https://huggingface.co"):
                url = base_url + url_str.removeprefix("https://huggingface.co")
            return super().build_request(method, url, *args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _LoopbackAsyncClient)


def test_download_job_speed_default_and_roundtrip() -> None:
    job = DownloadJob(id="dl-1", node_id="n1", repo_id="org/repo", filename="model.gguf")
    assert job.speed_bytes_per_sec == 0.0

    updated = DownloadJob.model_validate({**job.model_dump(), "speed_bytes_per_sec": 42_500.5})
    assert updated.speed_bytes_per_sec == 42_500.5


def test_download_speed_tracker_updates_and_smooths() -> None:
    job = DownloadJob(id="dl-2", node_id="n1", repo_id="org/repo", filename="model.gguf")
    tracker = agent_mod._DownloadSpeedTracker()

    # Initial chunk before the 0.4s window still sets a positive initial speed.
    job.downloaded_bytes += 4096
    tracker.record(job, 4096)
    first_speed = job.speed_bytes_per_sec
    assert first_speed > 0.0

    # After >= 0.4s, speed is EMA-smoothed with the new window throughput.
    time.sleep(0.45)
    job.downloaded_bytes += 131_072
    tracker.record(job, 131_072)
    assert job.speed_bytes_per_sec > 0.0
    assert job.speed_bytes_per_sec != first_speed


async def test_run_hf_download_tracks_speed_and_resets_on_complete_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_mod, "STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(agent_mod, "ENGINE_URL", "http://127.0.0.1:1")

    chunk_sent = threading.Event()
    allow_finish = threading.Event()
    mode = {"fail": False}

    class _HfHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if mode["fail"]:
                # Send >1 MiB chunk with larger Content-Length so aiter_bytes yields before EOF.
                self.send_response(200)
                self.send_header("Content-Length", str(2 * 1024 * 1024))
                self.end_headers()
                self.wfile.write(b"x" * (1024 * 1024))
                self.wfile.flush()
                chunk_sent.set()
                allow_finish.wait(timeout=2.0)
                return

            payload_a = b"a" * (1024 * 1024)
            payload_b = b"b" * 4096
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload_a) + len(payload_b)))
            self.end_headers()
            self.wfile.write(payload_a)
            self.wfile.flush()
            chunk_sent.set()
            allow_finish.wait(timeout=2.0)
            self.wfile.write(payload_b)
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            return

    with _loopback_hf_server(_HfHandler) as base_url:
        _patch_hf_redirect(monkeypatch, base_url)

        # Completion path
        job = DownloadJob(id="dl-ok", node_id="n1", repo_id="org/repo", filename="m.safetensors")
        task = asyncio.create_task(agent_mod._run_hf_download(job))
        await asyncio.to_thread(chunk_sent.wait, 2.0)
        for _ in range(100):
            if job.speed_bytes_per_sec > 0.0:
                break
            await asyncio.sleep(0.01)
        assert job.speed_bytes_per_sec > 0.0
        allow_finish.set()
        await task
        assert job.status == "completed"
        assert job.percent == 100.0
        assert job.speed_bytes_per_sec == 0.0
        assert (tmp_path / "m.safetensors").stat().st_size == 1024 * 1024 + 4096

        # Failure path
        mode["fail"] = True
        chunk_sent.clear()
        allow_finish.clear()
        fail_job = DownloadJob(
            id="dl-err", node_id="n1", repo_id="org/repo", filename="bad.safetensors"
        )
        fail_task = asyncio.create_task(agent_mod._run_hf_download(fail_job))
        await asyncio.to_thread(chunk_sent.wait, 2.0)
        for _ in range(100):
            if fail_job.speed_bytes_per_sec > 0.0:
                break
            await asyncio.sleep(0.01)
        assert fail_job.speed_bytes_per_sec > 0.0
        allow_finish.set()
        await fail_task
        assert fail_job.status == "failed"
        assert fail_job.speed_bytes_per_sec == 0.0


async def test_run_hf_mlx_download_tracks_speed_and_resets_on_complete_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_mod, "STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(agent_mod, "ENGINE_URL", "http://127.0.0.1:1")

    chunk_sent = threading.Event()
    allow_finish = threading.Event()
    mode = {"fail_second": False}

    class _MlxHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/api/models/org/mlx-repo/tree/main"):
                tree = [
                    {"type": "file", "path": "model.safetensors", "size": 1024 * 1024 + 4096},
                    {"type": "file", "path": "config.json", "size": 12},
                ]
                body = json.dumps(tree).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path.endswith("/model.safetensors"):
                self.send_response(200)
                self.send_header("Content-Length", str(1024 * 1024 + 4096))
                self.end_headers()
                self.wfile.write(b"w" * (1024 * 1024))
                self.wfile.flush()
                chunk_sent.set()
                allow_finish.wait(timeout=2.0)
                self.wfile.write(b"w" * 4096)
                self.wfile.flush()
                return

            if self.path.endswith("/config.json"):
                if mode["fail_second"]:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                cfg_bytes = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(cfg_bytes)))
                self.end_headers()
                self.wfile.write(cfg_bytes)
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    with _loopback_hf_server(_MlxHandler) as base_url:
        _patch_hf_redirect(monkeypatch, base_url)

        # Completion path
        job = DownloadJob(id="mlx-ok", node_id="n1", repo_id="org/mlx-repo", filename="org--mlx")
        task = asyncio.create_task(agent_mod._run_hf_mlx_download(job))
        await asyncio.to_thread(chunk_sent.wait, 2.0)
        for _ in range(100):
            if job.speed_bytes_per_sec > 0.0:
                break
            await asyncio.sleep(0.01)
        assert job.speed_bytes_per_sec > 0.0
        allow_finish.set()
        await task
        assert job.status == "completed"
        assert job.percent == 100.0
        assert job.speed_bytes_per_sec == 0.0
        assert (tmp_path / "mlx" / "org--mlx-repo" / "model.safetensors").is_file()

        # Failure path (after weights streamed speed > 0.0, config.json returns 500)
        mode["fail_second"] = True
        chunk_sent.clear()
        allow_finish.clear()
        fail_job = DownloadJob(
            id="mlx-err", node_id="n1", repo_id="org/mlx-repo", filename="org--mlx"
        )
        fail_task = asyncio.create_task(agent_mod._run_hf_mlx_download(fail_job))
        await asyncio.to_thread(chunk_sent.wait, 2.0)
        for _ in range(100):
            if fail_job.speed_bytes_per_sec > 0.0:
                break
            await asyncio.sleep(0.01)
        assert fail_job.speed_bytes_per_sec > 0.0
        allow_finish.set()
        await fail_task
        assert fail_job.status == "failed"
        assert fail_job.speed_bytes_per_sec == 0.0
