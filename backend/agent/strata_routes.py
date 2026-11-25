"""
Managed Strata Sparse-MoE lifecycle, Quick Tune calibration, and signed
streaming SSE proxy routes for `splitch-agent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from backend.shared.models import (
    NodeStrataStatus,
    StrataCalibrateRequest,
    StrataImportPlanRequest,
    StrataInstallRequest,
    StrataJobState,
    StrataLaunchRequest,
)

STRATA_PROXY_ALLOWLIST = frozenset(
    {
        "v1/chat/completions",
        "v1/completions",
        "v1/messages",
        "v1/responses",
        "v1/models",
        "health",
    }
)


async def evict_primary_engine_models(m: Any) -> None:
    """Unload any resident models on primary `ENGINE_URL` before Strata claims GPU VRAM."""
    st = m.state
    if not st.client or not st.snapshot.engine_healthy:
        return
    loaded = [mod.id for mod in st.snapshot.models if mod.status == "loaded"]
    for mid in loaded:
        with contextlib.suppress(httpx.HTTPError):
            if st.snapshot.engine == "ollama":
                await st.client.post(
                    f"{m.ENGINE_URL}/api/generate",
                    json={"model": mid, "prompt": "", "keep_alive": 0},
                    timeout=10.0,
                )
            else:
                await st.client.post(
                    f"{m.ENGINE_URL}/models/unload",
                    json={"model": mid},
                    timeout=10.0,
                )


def build_strata_status_snapshot(m: Any) -> NodeStrataStatus:
    """Build current Strata sidecar status using live `backend.agent.main` module state."""
    st = m.state
    strata_bin = m.resolve_strata_bin()
    meta = m.read_strata_engine_meta()
    storage_root = Path(m.STORAGE_PATH)
    installed_models = m.list_installed_strata_models(storage_root) if strata_bin else []
    active = st.snapshot.strata_active_model
    for im in installed_models:
        if active and im.id == active:
            im.is_active = True
    presets = m.list_strata_presets(storage_root) if strata_bin else []
    job = st.strata_job.model_copy()
    job.log_tail = list(st.strata_log_tail)
    return NodeStrataStatus(
        node_id=m.NODE_ID,
        installed=strata_bin is not None,
        version=str(meta.get("version") or ""),
        binary_path=str(strata_bin) if strata_bin else "",
        running=bool(
            st.snapshot.strata_healthy
            or (st.strata_proc is not None and st.strata_proc.returncode is None)
        ),
        healthy=st.snapshot.strata_healthy,
        port=m.STRATA_PORT,
        active_model=active,
        models=list(st.snapshot.strata_models),
        installed_models=installed_models,
        presets=presets,
        job=job,
        error=job.error,
    )


async def run_strata_cli_job(
    m: Any,
    *,
    action: Literal["launch", "calibrate"],
    cmd: list[str],
    step_label: str,
    timeout_s: float,
) -> None:
    st = m.state
    storage_root = Path(m.STORAGE_PATH)
    st.strata_log_tail.clear()
    st.strata_job = StrataJobState(
        job_id=uuid.uuid4().hex[:10],
        action=action,
        status="running",
        step=step_label,
        started_at=time.time(),
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=m.strata_env(storage_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        st.strata_proc = proc

        async def _drain() -> None:
            assert proc.stdout is not None
            while True:
                line_b = await proc.stdout.readline()
                if not line_b:
                    break
                line = line_b.decode("utf-8", errors="replace").rstrip()
                if line:
                    st.strata_log_tail.append(line)

        await asyncio.wait_for(_drain(), timeout=timeout_s)
        rc = await proc.wait()
        if rc == 0:
            await st.poll_once()
            st.strata_job.status = "completed"
            st.strata_job.step = (
                "Quick Tune complete" if action == "calibrate" else "Strata serving"
            )
        else:
            st.strata_job.status = "failed"
            tail = st.strata_log_tail[-1] if st.strata_log_tail else f"exit code {rc}"
            st.strata_job.error = tail
            st.strata_job.step = f"{action} failed"
    except TimeoutError:
        if st.strata_proc and st.strata_proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                st.strata_proc.terminate()
        st.strata_job.status = "failed"
        st.strata_job.error = f"Timed out after {int(timeout_s)}s"
        st.strata_job.step = f"{action} timed out"
    except (OSError, ValueError) as exc:
        st.strata_job.status = "failed"
        st.strata_job.error = str(exc)
        st.strata_job.step = f"{action} error"
    finally:
        st.strata_job.finished_at = time.time()
        st.strata_job.log_tail = list(st.strata_log_tail)


def build_strata_agent_router(main_getter: Callable[[], Any]) -> APIRouter:
    """Mount agent Strata sidecar lifecycle, Quick Tune, and streaming proxy routes."""
    router = APIRouter()

    @router.get("/strata/status", response_model=NodeStrataStatus)
    async def get_strata_status() -> NodeStrataStatus:
        m = main_getter()
        return await asyncio.to_thread(build_strata_status_snapshot, m)

    @router.post("/strata/install", response_model=NodeStrataStatus)
    async def install_strata_endpoint(req: StrataInstallRequest | None = None) -> NodeStrataStatus:
        m = main_getter()
        st = m.state
        if st.strata_job.status == "running":
            return await asyncio.to_thread(build_strata_status_snapshot, m)
        body = req or StrataInstallRequest()
        st.strata_log_tail.clear()
        st.strata_job = StrataJobState(
            job_id=uuid.uuid4().hex[:10],
            action="install",
            status="running",
            step="Downloading SHA-256-verified Strata CLI...",
            started_at=time.time(),
        )

        async def _worker() -> None:
            try:
                res = await asyncio.to_thread(
                    m.install_or_update_strata,
                    version=body.version,
                    force=body.force,
                )
                st.strata_log_tail.extend(res.steps)
                if res.ok:
                    st.strata_job.status = "completed"
                    st.strata_job.step = f"Strata v{res.current_tag} ready"
                else:
                    st.strata_job.status = "failed"
                    st.strata_job.error = res.error or "Install failed"
                    st.strata_job.step = "Install failed"
            except (OSError, RuntimeError, ValueError) as exc:
                st.strata_job.status = "failed"
                st.strata_job.error = str(exc)
                st.strata_job.step = "Install failed"
            finally:
                st.strata_job.finished_at = time.time()
                st.strata_job.log_tail = list(st.strata_log_tail)
                await st.poll_once()

        asyncio.create_task(_worker())
        return await asyncio.to_thread(build_strata_status_snapshot, m)

    @router.post("/strata/plan")
    async def plan_strata_endpoint(req: StrataLaunchRequest) -> dict:
        m = main_getter()
        strata_bin = m.resolve_strata_bin()
        if strata_bin is None:
            raise HTTPException(status_code=400, detail="Strata is not installed on this node")
        plan_req = req.model_copy(update={"plan_only": True})
        try:
            cmd = m.build_strata_launch_cmd(
                strata_bin, plan_req, storage_root=Path(m.STORAGE_PATH), port=m.STRATA_PORT
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def _run_plan() -> dict:
            proc = m.subprocess.run(
                cmd,
                env=m.strata_env(m.STORAGE_PATH),
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            if proc.returncode != 0:
                raise HTTPException(
                    status_code=400,
                    detail=(proc.stderr or proc.stdout or "strata launch --plan failed").strip()[:400],
                )
            try:
                return json.loads(proc.stdout or "{}")
            except ValueError:
                return {"raw": proc.stdout.strip()}

        return await asyncio.to_thread(_run_plan)

    @router.post("/strata/launch", response_model=NodeStrataStatus)
    async def launch_strata_endpoint(req: StrataLaunchRequest) -> NodeStrataStatus:
        m = main_getter()
        st = m.state
        if st.strata_job.status == "running":
            raise HTTPException(status_code=409, detail="Another Strata job is currently running")
        strata_bin = m.resolve_strata_bin()
        if strata_bin is None:
            raise HTTPException(status_code=400, detail="Strata is not installed on this node")
        launch_req = req.model_copy(update={"plan_only": False})
        try:
            cmd = m.build_strata_launch_cmd(
                strata_bin, launch_req, storage_root=Path(m.STORAGE_PATH), port=m.STRATA_PORT
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if req.evict_primary_vram:
            await evict_primary_engine_models(m)

        label = req.preset or (
            f"{req.family}/{req.model}" if req.family or req.model else Path(req.model_path).name
        )
        asyncio.create_task(
            run_strata_cli_job(
                m,
                action="launch",
                cmd=cmd,
                step_label=f"Launching Strata ({label})...",
                timeout_s=1800.0,
            )
        )
        return await asyncio.to_thread(build_strata_status_snapshot, m)

    @router.post("/strata/calibrate", response_model=NodeStrataStatus)
    async def calibrate_strata_endpoint(req: StrataCalibrateRequest) -> NodeStrataStatus:
        m = main_getter()
        st = m.state
        if st.strata_job.status == "running":
            raise HTTPException(status_code=409, detail="Another Strata job is currently running")
        strata_bin = m.resolve_strata_bin()
        if strata_bin is None:
            raise HTTPException(status_code=400, detail="Strata is not installed on this node")
        try:
            cmd = m.build_strata_calibrate_cmd(
                strata_bin, req, storage_root=Path(m.STORAGE_PATH), port=m.STRATA_PORT
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if req.evict_primary_vram:
            await evict_primary_engine_models(m)
        await asyncio.to_thread(m.stop_strata_server, Path(m.STORAGE_PATH))

        label = req.preset or f"{req.family}/{req.model}"
        timeout_s = 7800.0 if req.mode == "thorough" else 1800.0
        asyncio.create_task(
            run_strata_cli_job(
                m,
                action="calibrate",
                cmd=cmd,
                step_label=f"Running {req.mode} tune on {label} (exclusive GPU)...",
                timeout_s=timeout_s,
            )
        )
        return await asyncio.to_thread(build_strata_status_snapshot, m)

    @router.post("/strata/stop", response_model=NodeStrataStatus)
    async def stop_strata_endpoint() -> NodeStrataStatus:
        m = main_getter()
        st = m.state
        if st.strata_proc is not None and st.strata_proc.returncode is None:
            st.strata_proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(st.strata_proc.wait(), timeout=4.0)
        await asyncio.to_thread(m.stop_strata_server, Path(m.STORAGE_PATH))
        if st.strata_job.status == "running":
            st.strata_job.status = "failed"
            st.strata_job.error = "Stopped by user"
            st.strata_job.step = "Stopped"
            st.strata_job.finished_at = time.time()
        await st.poll_once()
        return await asyncio.to_thread(build_strata_status_snapshot, m)

    @router.post("/strata/import-plan")
    async def import_strata_plan_endpoint(req: StrataImportPlanRequest) -> dict:
        m = main_getter()
        try:
            return await asyncio.to_thread(
                m.import_strata_gguf_plan, req.model_path, Path(m.STORAGE_PATH)
            )
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.api_route("/strata/proxy/{path:path}", methods=["GET", "POST"])
    async def proxy_local_strata_engine(path: str, request: Request) -> Response:
        """Zero-copy streaming SSE proxy from router to local loopback `strata-server`."""
        m = main_getter()
        clean_path = path.lstrip("/")
        if clean_path not in STRATA_PROXY_ALLOWLIST:
            raise HTTPException(status_code=403, detail=f"Strata proxy path not allowlisted: {clean_path}")

        target = f"{m.STRATA_URL}/{clean_path}"
        if request.url.query:
            target = f"{target}?{request.url.query}"

        fwd_headers: dict[str, str] = {}
        for hname in ("content-type", "accept", "anthropic-version", "anthropic-beta"):
            val = request.headers.get(hname)
            if val:
                fwd_headers[hname] = val

        body = await request.body()
        client = m.httpx.AsyncClient(timeout=m.httpx.Timeout(600.0, connect=5.0))
        try:
            req_obj = client.build_request(
                request.method,
                target,
                content=body if body else None,
                headers=fwd_headers,
            )
            upstream = await client.send(req_obj, stream=True)
        except m.httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(status_code=502, detail=f"Local Strata server unreachable: {exc}") from exc

        async def _stream():
            try:
                async for chunk in upstream.aiter_bytes():
                    if chunk:
                        yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        ct = upstream.headers.get("content-type") or "application/json"
        return StreamingResponse(_stream(), status_code=upstream.status_code, media_type=ct)

    return router
