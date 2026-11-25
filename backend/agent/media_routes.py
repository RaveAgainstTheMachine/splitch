"""
Managed local media engine (`sd-server`, `sd-cli`, ComfyUI) lifecycle and
streaming proxy routes for `splitch-agent`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from backend.shared.models import (
    InstallMediaEngineRequest,
    LocalModelFile,
    NodeMediaEngineStatus,
    SdCliVideoRequest,
    StartMediaEngineRequest,
)


def scan_local_media_models(storage_path: str) -> list[LocalModelFile]:
    """Scan local media checkpoints and diffusion models under storage roots."""
    files: list[LocalModelFile] = []
    seen: set[str] = set()
    roots = [Path(storage_path), Path(storage_path) / "media"]
    for root in roots:
        if not root.exists():
            continue
        for pat in ("*.safetensors", "*.ckpt", "*.gguf"):
            for entry in sorted(root.glob(pat)):
                if not entry.is_file():
                    continue
                resolved = str(entry.resolve())
                if resolved in seen:
                    continue
                try:
                    size_gb = round(entry.stat().st_size / (1024**3), 2)
                except OSError:
                    continue
                seen.add(resolved)
                files.append(
                    LocalModelFile(
                        filename=entry.name,
                        path=str(entry),
                        size_gb=size_gb,
                        managed=True,
                    )
                )
    return files


def resolve_media_file(storage_root: Path, filename: str) -> Path | None:
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return None
    for candidate in (storage_root / filename, storage_root / "media" / filename):
        resolved = candidate.resolve()
        if resolved.is_file() and resolved.is_relative_to(storage_root):
            return resolved
    return None


def find_first_media_match(storage_root: Path, patterns: tuple[str, ...]) -> Path | None:
    for root in (storage_root, storage_root / "media"):
        if not root.exists():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_file():
                continue
            low = entry.name.lower()
            if any(pat in low for pat in patterns):
                resolved = entry.resolve()
                if resolved.is_relative_to(storage_root):
                    return resolved
    return None


def build_sd_model_args(
    storage_root: Path,
    model_path: Path,
    *,
    vae: str = "",
    t5xxl: str = "",
    clip_l: str = "",
    llm: str = "",
) -> tuple[list[str], str | None]:
    """Build sd-server / sd-cli model flags, auto-wiring multi-file DiT bundles."""
    low = model_path.name.lower()

    vae_path = resolve_media_file(storage_root, vae) if vae else None
    t5_path = resolve_media_file(storage_root, t5xxl) if t5xxl else None
    clip_l_path = resolve_media_file(storage_root, clip_l) if clip_l else None
    llm_path = resolve_media_file(storage_root, llm) if llm else None

    if "z-image" in low or "z_image" in low:
        llm_path = llm_path or find_first_media_match(storage_root, ("qwen3-4b", "qwen_3_4b"))
        vae_path = vae_path or find_first_media_match(storage_root, ("ae.safetensors",))
        missing = []
        if not llm_path:
            missing.append("Qwen3-4B text encoder (Qwen3-4B-Q4_K_M.gguf)")
        if not vae_path:
            missing.append("FLUX VAE (ae.safetensors)")
        if missing:
            return [], f"Z-Image-Turbo requires companion file(s): {', '.join(missing)}. Click Pull on the Z-Image-Turbo bundle."
        return [
            "--diffusion-model",
            str(model_path),
            "--llm",
            str(llm_path),
            "--vae",
            str(vae_path),
            "--flash-attn",
        ], None

    if "flux" in low:
        t5_path = t5_path or find_first_media_match(storage_root, ("t5-v1_1-xxl", "t5xxl"))
        clip_l_path = clip_l_path or find_first_media_match(storage_root, ("clip_l",))
        vae_path = vae_path or find_first_media_match(storage_root, ("ae.safetensors",))
        missing = []
        if not t5_path:
            missing.append("T5-XXL encoder (t5-v1_1-xxl-encoder-Q4_K_M.gguf)")
        if not clip_l_path:
            missing.append("CLIP-L encoder (clip_l.safetensors)")
        if not vae_path:
            missing.append("FLUX VAE (ae.safetensors)")
        if missing:
            return [], f"FLUX requires companion file(s): {', '.join(missing)}. Click Pull on the FLUX.1-schnell bundle."
        return [
            "--diffusion-model",
            str(model_path),
            "--t5xxl",
            str(t5_path),
            "--clip_l",
            str(clip_l_path),
            "--vae",
            str(vae_path),
            "--flash-attn",
        ], None

    if "wan2." in low or "wan_" in low:
        t5_path = t5_path or find_first_media_match(storage_root, ("umt5_xxl", "umt5-xxl", "umt5"))
        vae_path = vae_path or find_first_media_match(storage_root, ("wan_2.1_vae", "wan2.1_vae", "wan_vae"))
        missing = []
        if not t5_path:
            missing.append("UMT5-XXL encoder (umt5_xxl_fp8_e4m3fn_scaled.safetensors)")
        if not vae_path:
            missing.append("Wan 2.1 VAE (wan_2.1_vae.safetensors)")
        if missing:
            return [], f"Wan 2.1 requires companion file(s): {', '.join(missing)}. Click Pull on a Wan 2.1 video bundle."
        return [
            "--diffusion-model",
            str(model_path),
            "--t5xxl",
            str(t5_path),
            "--vae",
            str(vae_path),
            "--flow-shift",
            "3.0",
            "--flash-attn",
        ], None

    if any((vae_path, t5_path, clip_l_path, llm_path)):
        args = ["--diffusion-model", str(model_path)]
        if vae_path:
            args.extend(["--vae", str(vae_path)])
        if t5_path:
            args.extend(["--t5xxl", str(t5_path)])
        if clip_l_path:
            args.extend(["--clip_l", str(clip_l_path)])
        if llm_path:
            args.extend(["--llm", str(llm_path)])
        args.append("--flash-attn")
        return args, None

    return ["-m", str(model_path)], None


def _media_lib_env(binary: Path) -> dict[str, str]:
    env = os.environ.copy()
    lib_dir = str(binary.parent)
    if sys.platform == "darwin":
        env["DYLD_LIBRARY_PATH"] = f"{lib_dir}:{env.get('DYLD_LIBRARY_PATH', '')}"
    elif sys.platform.startswith("linux"):
        env["LD_LIBRARY_PATH"] = f"{lib_dir}:{env.get('LD_LIBRARY_PATH', '')}"
    return env


def build_media_status_snapshot(m: Any) -> NodeMediaEngineStatus:
    """Build current media engine status from live `backend.agent.main` module state."""
    installed = m.detect_installed_engines()
    sd_info = next((e for e in installed if e.engine == "sd.cpp"), None)
    st = m.state
    running = bool(st.sd_proc is not None and st.sd_proc.returncode is None)
    if st.sd_proc is not None and st.sd_proc.returncode is not None and not st.sd_last_error:
        st.sd_last_error = f"sd-server exited with code {st.sd_proc.returncode}"
    return NodeMediaEngineStatus(
        node_id=m.NODE_ID,
        sd_installed=sd_info is not None,
        sd_binary=sd_info.path if sd_info else "",
        sd_version=sd_info.build_tag or sd_info.version if sd_info else "",
        sd_backend=sd_info.backend if sd_info else "",
        sd_managed_running=running,
        sd_running_model=st.sd_running_model if running else "",
        sd_error=st.sd_last_error,
        install_in_progress=st.sd_install_busy,
        install_step=st.sd_install_step,
        media_engine=st.snapshot.media_engine,
        media_healthy=st.snapshot.media_healthy,
        media_url=st.snapshot.media_url,
        local_media_models=scan_local_media_models(m.STORAGE_PATH),
        active_downloads=list(st.downloads.values()),
    )


def build_media_agent_router(main_getter: Callable[[], Any]) -> APIRouter:
    """Mount agent media engine status, lifecycle, video gen, and proxy routes."""
    router = APIRouter()

    @router.get("/media/engine/status", response_model=NodeMediaEngineStatus)
    async def get_media_engine_status() -> NodeMediaEngineStatus:
        m = main_getter()
        return await asyncio.to_thread(build_media_status_snapshot, m)

    @router.post("/media/engine/install", response_model=NodeMediaEngineStatus)
    async def install_media_engine(req: InstallMediaEngineRequest) -> NodeMediaEngineStatus:
        m = main_getter()
        st = m.state
        if st.sd_install_busy:
            return await asyncio.to_thread(build_media_status_snapshot, m)
        st.sd_install_busy = True
        st.sd_last_error = None
        st.sd_install_step = f"Downloading hardware-matched stable-diffusion.cpp ({req.backend})..."

        async def _worker() -> None:
            try:
                res = await asyncio.to_thread(
                    m.install_or_update_engine,
                    "sd.cpp",
                    backend=req.backend,
                    force=req.force,
                )
                if res.ok:
                    st.sd_install_step = res.steps[-1] if res.steps else "Installed stable-diffusion.cpp"
                    st.sd_last_error = None
                else:
                    st.sd_last_error = res.error or "Failed to install stable-diffusion.cpp"
                    st.sd_install_step = "Install failed"
            except (OSError, RuntimeError, ValueError) as exc:
                st.sd_last_error = str(exc)
                st.sd_install_step = "Install failed"
            finally:
                st.sd_install_busy = False

        asyncio.create_task(_worker())
        return await asyncio.to_thread(build_media_status_snapshot, m)

    @router.post("/media/engine/start", response_model=NodeMediaEngineStatus)
    async def start_media_engine(req: StartMediaEngineRequest) -> NodeMediaEngineStatus:
        m = main_getter()
        st = m.state
        if ".." in req.model_file or "/" in req.model_file or "\\" in req.model_file:
            raise HTTPException(status_code=400, detail="Invalid model_file name")
        storage_root = Path(m.STORAGE_PATH).resolve()
        model_path = resolve_media_file(storage_root, req.model_file)
        if model_path is None:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{req.model_file}' not found in {m.STORAGE_PATH}",
            )

        model_args, bundle_err = build_sd_model_args(
            storage_root,
            model_path,
            vae=req.vae,
            t5xxl=req.t5xxl,
            clip_l=req.clip_l,
            llm=req.llm,
        )
        if bundle_err:
            raise HTTPException(status_code=400, detail=bundle_err)

        installed = await asyncio.to_thread(m.detect_installed_engines)
        sd_info = next((e for e in installed if e.engine == "sd.cpp"), None)
        if not sd_info:
            raise HTTPException(
                status_code=400,
                detail="stable-diffusion.cpp (sd-server) is not installed on this node yet.",
            )

        if st.sd_proc is not None and st.sd_proc.returncode is None:
            st.sd_proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(st.sd_proc.wait(), timeout=3.0)

        sd_bin = Path(sd_info.path)
        if sd_bin.name.startswith("sd-cli"):
            sibling = sd_bin.parent / ("sd-server.exe" if sys.platform == "win32" else "sd-server")
            if sibling.is_file():
                sd_bin = sibling

        cmd = [
            str(sd_bin),
            "--listen-ip",
            "127.0.0.1",
            "--listen-port",
            str(req.port),
            *model_args,
        ]
        st.sd_last_error = None
        st.sd_running_model = req.model_file
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=_media_lib_env(sd_bin),
            )
            st.sd_proc = proc
        except OSError as exc:
            st.sd_last_error = f"Failed to launch sd-server: {exc}"
            raise HTTPException(status_code=500, detail=st.sd_last_error) from exc

        for _ in range(12):
            await asyncio.sleep(0.4)
            if proc.returncode is not None:
                err_tail = ""
                if proc.stderr:
                    raw_err = await proc.stderr.read()
                    err_tail = raw_err.decode("utf-8", errors="replace").strip()[-500:]
                st.sd_last_error = err_tail or f"sd-server exited with code {proc.returncode}"
                st.sd_running_model = ""
                raise HTTPException(status_code=500, detail=st.sd_last_error)
            if st.client is not None:
                ok, _, _ = await m._probe_single_media_url(st.client, f"http://127.0.0.1:{req.port}")
                if ok:
                    break

        await st.poll_once()
        return await asyncio.to_thread(build_media_status_snapshot, m)

    @router.post("/media/engine/stop", response_model=NodeMediaEngineStatus)
    async def stop_media_engine() -> NodeMediaEngineStatus:
        m = main_getter()
        st = m.state
        if st.sd_proc is not None and st.sd_proc.returncode is None:
            st.sd_proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(st.sd_proc.wait(), timeout=4.0)
        st.sd_proc = None
        st.sd_running_model = ""
        st.sd_last_error = None
        await st.poll_once()
        return await asyncio.to_thread(build_media_status_snapshot, m)

    @router.post("/media/video/generate")
    async def generate_video_via_sd_cli(req: SdCliVideoRequest) -> Response:
        """Run native stable-diffusion.cpp `sd-cli -M vid_gen` for Wan 2.1/2.2 video generation."""
        m = main_getter()
        st = m.state
        storage_root = Path(m.STORAGE_PATH).resolve()
        model_path: Path | None = None
        if req.model_file and req.model_file not in ("auto", "sd-cpp-local"):
            model_path = resolve_media_file(storage_root, req.model_file)
        if model_path is None:
            model_path = find_first_media_match(
                storage_root, ("wan2.1_t2v", "wan2.1-t2v", "wan2.2", "ltx-video")
            )
        if model_path is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No video model found on this node. Pull a Wan 2.1 video bundle in "
                    "Media Studio → Engine & Models (or connect ComfyUI)."
                ),
            )

        model_args, bundle_err = build_sd_model_args(storage_root, model_path)
        if bundle_err:
            raise HTTPException(status_code=400, detail=bundle_err)

        installed = await asyncio.to_thread(m.detect_installed_engines)
        sd_info = next((e for e in installed if e.engine == "sd.cpp"), None)
        if not sd_info:
            raise HTTPException(
                status_code=400,
                detail="stable-diffusion.cpp (sd-cli) is not installed on this node.",
            )

        sd_cli = Path(sd_info.path)
        if sd_cli.name.startswith("sd-server"):
            sibling = sd_cli.parent / ("sd-cli.exe" if sys.platform == "win32" else "sd-cli")
            if sibling.is_file():
                sd_cli = sibling
        if not sd_cli.is_file():
            found = shutil.which("sd-cli")
            if not found:
                raise HTTPException(status_code=400, detail="sd-cli binary not found alongside sd-server")
            sd_cli = Path(found)

        was_running_model = (
            st.sd_running_model
            if (st.sd_proc is not None and st.sd_proc.returncode is None)
            else ""
        )
        if st.sd_proc is not None and st.sd_proc.returncode is None:
            st.sd_proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(st.sd_proc.wait(), timeout=4.0)
            st.sd_proc = None
            st.sd_running_model = ""

        with tempfile.TemporaryDirectory(prefix="splitch-vid-") as tmpdir:
            out_path = Path(tmpdir) / "out.webm"
            cmd = [
                str(sd_cli),
                "-M",
                "vid_gen",
                *model_args,
                "-W",
                str(req.width),
                "-H",
                str(req.height),
                "--video-frames",
                str(req.frames),
                "--fps",
                str(req.fps),
                "--steps",
                str(req.steps),
                "--cfg-scale",
                str(req.cfg_scale),
                "-s",
                str(req.seed),
                "-p",
                req.prompt,
                "-o",
                str(out_path),
            ]
            if req.negative_prompt.strip():
                cmd.extend(["-n", req.negative_prompt.strip()])

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=_media_lib_env(sd_cli),
                )
                _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=600.0)
            except TimeoutError as exc:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                raise HTTPException(status_code=504, detail="sd-cli video generation timed out") from exc
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"Failed to launch sd-cli: {exc}") from exc

            if proc.returncode != 0 or not out_path.is_file():
                err_tail = stderr_b.decode("utf-8", errors="replace").strip()[-500:]
                raise HTTPException(
                    status_code=500,
                    detail=err_tail or f"sd-cli exited with code {proc.returncode}",
                )
            raw = await asyncio.to_thread(out_path.read_bytes)

        if was_running_model:
            with contextlib.suppress(HTTPException, OSError):
                await start_media_engine(StartMediaEngineRequest(model_file=was_running_model))

        return Response(content=raw, media_type="video/webm")

    @router.api_route("/media/proxy/{path:path}", methods=["GET", "POST"])
    async def proxy_local_media_engine(path: str, request: Request) -> Response:
        """Proxy router media requests to the node's local sd-server/ComfyUI over the agent port."""
        m = main_getter()
        st = m.state
        base = (st.snapshot.media_url or m.MEDIA_URL or "http://127.0.0.1:8082").rstrip("/")
        if not base:
            raise HTTPException(status_code=503, detail="No local media engine running on this node")
        target = f"{base}/{path.lstrip('/')}"
        if request.url.query:
            target = f"{target}?{request.url.query}"

        fwd_headers: dict[str, str] = {}
        ct = request.headers.get("content-type")
        if ct:
            fwd_headers["content-type"] = ct
        accept = request.headers.get("accept")
        if accept:
            fwd_headers["accept"] = accept

        body = await request.body()
        try:
            async with m.httpx.AsyncClient(timeout=m.httpx.Timeout(600.0, connect=10.0)) as client:
                resp = await client.request(
                    request.method,
                    target,
                    content=body if body else None,
                    headers=fwd_headers,
                )
        except m.httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Local media engine error: {exc}") from exc

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )

    return router
