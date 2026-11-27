"""
Managed Jeveration (arXiv:2611.24738) sidecar lifecycle, weight downloader, and
signed proxy routes for `splitch-agent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from backend.agent.downloads import run_hf_download
from backend.shared.models import (
    DownloadJob,
    NodeJeverationStatus,
    StartJeverationRequest,
)

JEVERATION_REPO_URL = "https://github.com/Carol-lyh/Jeveration.git"
JEVERATION_HF_REPO = "linlinlinlinsdada/Jeveration"
JEVERATION_WEIGHT_FILES = ("jeveration.pt", "decoder512.pt", "source_decoder512.pt")
JEVERATION_PROXY_ALLOWLIST = frozenset(
    {
        "health",
        "v1/text-to-image",
        "v1/image-to-image",
        "v1/dense-predict",
    }
)


def jeveration_engine_dir() -> Path:
    return Path.home() / ".local" / "share" / "splitch" / "engines" / "jeveration"


def jeveration_weights_dir(storage_root: Path) -> Path:
    return storage_root / "media" / "jeveration"


def jeveration_python_bin() -> Path:
    eng = jeveration_engine_dir()
    if sys.platform == "win32":
        return eng / ".venv" / "Scripts" / "python.exe"
    return eng / ".venv" / "bin" / "python"


def jeveration_is_installed() -> bool:
    eng = jeveration_engine_dir()
    return jeveration_python_bin().is_file() and (eng / "repo" / "jeveration").is_dir()


def jeveration_weights_ready(storage_root: Path) -> bool:
    wdir = jeveration_weights_dir(storage_root)
    return (wdir / "jeveration.pt").is_file() and (wdir / "decoder512.pt").is_file()


def _node_has_cuda(m: Any) -> bool:
    snap = getattr(m.state, "snapshot", None)
    if snap is not None and getattr(snap, "gpu_vendors", None):
        return "nvidia" in [v.lower() for v in snap.gpu_vendors]
    return shutil.which("nvidia-smi") is not None


def build_jeveration_status_snapshot(m: Any) -> NodeJeverationStatus:
    st = m.state
    storage_root = Path(m.STORAGE_PATH)
    proc = getattr(st, "jev_proc", None)
    running = proc is not None and proc.returncode is None
    if proc is not None and proc.returncode is not None and not getattr(st, "jev_last_error", None):
        st.jev_last_error = f"jeveration-worker exited with code {proc.returncode}"

    active_dl = [
        j
        for j in st.downloads.values()
        if j.repo_id == JEVERATION_HF_REPO and j.status == "downloading"
    ]
    return NodeJeverationStatus(
        node_id=m.NODE_ID,
        cuda_available=_node_has_cuda(m),
        installed=jeveration_is_installed(),
        weights_ready=jeveration_weights_ready(storage_root),
        running=running,
        healthy=bool(getattr(st, "jev_healthy", False)),
        port=int(getattr(st, "jev_port", m.JEVERATION_PORT)),
        install_in_progress=bool(getattr(st, "jev_install_in_progress", False)),
        install_step=str(getattr(st, "jev_install_step", "")),
        weights_downloading=bool(active_dl),
        error=getattr(st, "jev_last_error", None),
        vram_used_mb=float(getattr(st, "jev_vram_mb", 0.0)),
        active_downloads=active_dl,
    )


async def probe_jeveration_health(
    client: httpx.AsyncClient, port: int
) -> tuple[bool, float]:
    try:
        r = await client.get(f"http://127.0.0.1:{port}/health", timeout=1.5)
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, dict) and body.get("ready"):
                return True, float(body.get("vram_mb") or 0.0)
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return False, 0.0


def build_jeveration_agent_router(main_getter: Callable[[], Any]) -> APIRouter:
    """Mount agent Jeveration status, install, weight-pull, start/stop, and proxy routes."""
    router = APIRouter(tags=["jeveration"])

    @router.get("/jeveration/status", response_model=NodeJeverationStatus)
    async def get_jeveration_status() -> NodeJeverationStatus:
        m = main_getter()
        return await asyncio.to_thread(build_jeveration_status_snapshot, m)

    @router.post("/jeveration/install", response_model=NodeJeverationStatus)
    async def install_jeveration() -> NodeJeverationStatus:
        m = main_getter()
        st = m.state
        if not _node_has_cuda(m):
            raise HTTPException(
                status_code=400,
                detail="Jeveration requires an NVIDIA CUDA GPU on this node.",
            )
        if st.jev_install_in_progress:
            return await asyncio.to_thread(build_jeveration_status_snapshot, m)

        uv_bin = shutil.which("uv")
        git_bin = shutil.which("git")
        if not uv_bin or not git_bin:
            raise HTTPException(
                status_code=400,
                detail="Both `uv` and `git` are required on PATH to install Jeveration.",
            )

        st.jev_install_in_progress = True
        st.jev_install_step = "Preparing Python 3.12 environment..."
        st.jev_last_error = None

        async def _do_install() -> None:
            eng = jeveration_engine_dir()
            repo_dir = eng / "repo"
            venv_dir = eng / ".venv"
            try:
                eng.mkdir(parents=True, exist_ok=True)
                if not (repo_dir / ".git").is_dir():
                    st.jev_install_step = "Cloning Carol-lyh/Jeveration..."
                    proc = await asyncio.create_subprocess_exec(
                        git_bin,
                        "clone",
                        "--depth",
                        "1",
                        JEVERATION_REPO_URL,
                        str(repo_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, err = await asyncio.wait_for(proc.communicate(), timeout=120.0)
                    if proc.returncode != 0:
                        raise RuntimeError(
                            (err or b"").decode("utf-8", errors="replace")[:400]
                            or "git clone failed"
                        )

                if not jeveration_python_bin().is_file():
                    st.jev_install_step = "Creating Python 3.12 virtualenv via uv..."
                    proc = await asyncio.create_subprocess_exec(
                        uv_bin,
                        "venv",
                        "--python",
                        "3.12",
                        str(venv_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, err = await asyncio.wait_for(proc.communicate(), timeout=180.0)
                    if proc.returncode != 0:
                        raise RuntimeError(
                            (err or b"").decode("utf-8", errors="replace")[:400]
                            or "uv venv failed"
                        )

                st.jev_install_step = "Installing Jeveration CUDA dependencies (torch, transformers, flash-linear-attention)..."
                req_file = repo_dir / "requirements.txt"
                proc = await asyncio.create_subprocess_exec(
                    uv_bin,
                    "pip",
                    "install",
                    "--python",
                    str(jeveration_python_bin()),
                    "-r",
                    str(req_file),
                    "fastapi",
                    "uvicorn",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, err = await asyncio.wait_for(proc.communicate(), timeout=900.0)
                if proc.returncode != 0:
                    raise RuntimeError(
                        (err or b"").decode("utf-8", errors="replace")[:400]
                        or "uv pip install failed"
                    )
                st.jev_install_step = "Installed"
            except Exception as exc:  # noqa: BLE001
                st.jev_last_error = f"Jeveration install failed: {exc}"
                st.jev_install_step = "Failed"
            finally:
                st.jev_install_in_progress = False

        asyncio.create_task(_do_install())
        return await asyncio.to_thread(build_jeveration_status_snapshot, m)

    @router.post("/jeveration/pull-weights", response_model=NodeJeverationStatus)
    async def pull_jeveration_weights(request: Request) -> NodeJeverationStatus:
        m = main_getter()
        st = m.state
        wdir = jeveration_weights_dir(Path(m.STORAGE_PATH))
        wdir.mkdir(parents=True, exist_ok=True)
        hf_token = request.headers.get("x-splitch-hf-token", "").strip()

        async def _noop_refresh(_stem: str) -> None:
            return None

        for fname in JEVERATION_WEIGHT_FILES:
            if (wdir / fname).is_file():
                continue
            already = any(
                j.repo_id == JEVERATION_HF_REPO
                and j.filename == fname
                and j.status == "downloading"
                for j in st.downloads.values()
            )
            if already:
                continue
            job_id = f"dl_{uuid.uuid4().hex[:10]}"
            job = DownloadJob(
                id=job_id,
                node_id=m.NODE_ID,
                repo_id=JEVERATION_HF_REPO,
                filename=fname,
                status="downloading",
            )
            st.downloads[job_id] = job
            asyncio.create_task(
                run_hf_download(
                    job,
                    storage_path=str(wdir),
                    refresh_engine_cb=_noop_refresh,
                    poll_once_cb=st.poll_once,
                    hf_token_override=hf_token,
                )
            )
        return await asyncio.to_thread(build_jeveration_status_snapshot, m)

    @router.post("/jeveration/start", response_model=NodeJeverationStatus)
    async def start_jeveration(req: StartJeverationRequest) -> NodeJeverationStatus:
        m = main_getter()
        st = m.state
        if not jeveration_is_installed():
            raise HTTPException(
                status_code=400,
                detail="Jeveration is not installed on this node yet. Click Install Engine first.",
            )
        storage_root = Path(m.STORAGE_PATH)
        if not jeveration_weights_ready(storage_root):
            raise HTTPException(
                status_code=400,
                detail="Jeveration weights not found. Click Pull Weights (6.5 GB) first.",
            )

        if req.evict_primary_models:
            from backend.agent.strata_routes import evict_primary_engine_models

            await evict_primary_engine_models(m)
            if st.sd_proc and st.sd_proc.returncode is None:
                st.sd_proc.terminate()
                with contextlib.suppress(TimeoutError, ProcessLookupError):
                    await asyncio.wait_for(st.sd_proc.wait(), timeout=3.0)
                st.sd_proc = None

        if st.jev_proc and st.jev_proc.returncode is None:
            st.jev_proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(st.jev_proc.wait(), timeout=3.0)

        wdir = jeveration_weights_dir(storage_root)
        hf_home = wdir / "hf_cache"
        hf_home.mkdir(parents=True, exist_ok=True)
        worker_script = Path(__file__).resolve().parent / "jeveration_worker.py"
        cmd = [
            str(jeveration_python_bin()),
            str(worker_script),
            "--host",
            "127.0.0.1",
            "--port",
            str(req.port),
            "--repo-dir",
            str(jeveration_engine_dir() / "repo"),
            "--weights-dir",
            str(wdir),
            "--hf-home",
            str(hf_home),
        ]
        if not req.load_source_decoder:
            cmd.append("--no-source-decoder")

        env = os.environ.copy()
        env["HF_HOME"] = str(hf_home)
        st.jev_port = req.port
        st.jev_last_error = None
        log_path = wdir / "jeveration-worker.log"
        try:
            with log_path.open("wb") as log_fh:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    stdout=log_fh,
                    stderr=asyncio.subprocess.STDOUT,
                )
            st.jev_proc = proc
        except OSError as exc:
            st.jev_last_error = f"Failed to launch jeveration-worker: {exc}"
            raise HTTPException(status_code=500, detail=st.jev_last_error) from exc

        assert st.client is not None
        # Wait briefly (up to 5s) for fast warm starts or immediate crash;
        # cold HF downloads/loads continue asynchronously while status reports running=True, healthy=False.
        for _ in range(10):
            await asyncio.sleep(0.5)
            if proc.returncode is not None:
                err_tail = ""
                with contextlib.suppress(OSError):
                    err_tail = log_path.read_text("utf-8", errors="replace")[-500:]
                st.jev_last_error = err_tail or f"jeveration-worker exited with code {proc.returncode}"
                raise HTTPException(status_code=500, detail=st.jev_last_error)
            ok, vram_mb = await probe_jeveration_health(st.client, req.port)
            if ok:
                st.jev_healthy = True
                st.jev_vram_mb = vram_mb
                break

        await st.poll_once()
        return await asyncio.to_thread(build_jeveration_status_snapshot, m)

    @router.post("/jeveration/stop", response_model=NodeJeverationStatus)
    async def stop_jeveration() -> NodeJeverationStatus:
        m = main_getter()
        st = m.state
        if st.jev_proc and st.jev_proc.returncode is None:
            st.jev_proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(st.jev_proc.wait(), timeout=3.0)
        st.jev_proc = None
        st.jev_healthy = False
        st.jev_vram_mb = 0.0
        st.jev_last_error = None
        await st.poll_once()
        return await asyncio.to_thread(build_jeveration_status_snapshot, m)

    @router.api_route(
        "/jeveration/proxy/{path:path}",
        methods=["GET", "POST"],
    )
    async def proxy_local_jeveration(path: str, request: Request) -> Response:
        """Proxy router Jeveration requests to the node's local worker over the agent port."""
        m = main_getter()
        st = m.state
        clean = path.lstrip("/")
        if clean not in JEVERATION_PROXY_ALLOWLIST:
            raise HTTPException(status_code=403, detail=f"Jeveration proxy path '{clean}' is not allowlisted")

        assert st.client is not None
        port = int(getattr(st, "jev_port", m.JEVERATION_PORT))
        target = f"http://127.0.0.1:{port}/{clean}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        body = await request.body()
        req_headers = {}
        if ct := request.headers.get("content-type"):
            req_headers["content-type"] = ct
        try:
            resp = await st.client.request(
                request.method,
                target,
                content=body if body else None,
                headers=req_headers,
                timeout=httpx.Timeout(120.0, connect=5.0),
            )
            resp_headers = {}
            if lat := resp.headers.get("x-jeveration-latency-ms"):
                resp_headers["X-Jeveration-Latency-Ms"] = lat
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type"),
                headers=resp_headers,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Jeveration proxy error: {exc}") from exc

    return router
