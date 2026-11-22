"""
splitch-agent (:8090)
Cross-platform per-node hardware + inference engine telemetry, GGUF storage,
client config/skills sync, and opt-in workspace-sandboxed tool runner.
Strictly non-blocking on HTTP hot paths: background loop refreshes cached snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hmac
import json
import logging
import os
import platform
import re
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
import psutil
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from backend.router.config import default_data_root
from backend.shared.engines import detect_installed_engines, install_or_update_engine
from backend.shared.gpu_probe import (
    LLM_PROCESS_TOKENS,  # noqa: F401
    collect_all_gpus,
    summarize_gpus,
)
from backend.shared.gpu_probe import (
    parse_nvidia_pmon as _parse_pmon,  # noqa: F401
)
from backend.shared.models import (
    ClientConfigSyncRequest,
    DownloadJob,
    EngineKind,
    InstallMediaEngineRequest,
    LoadedModel,
    LocalModelFile,
    McpScriptSyncRequest,
    MediaEngineKind,
    ModelActionRequest,
    NodeMediaEngineStatus,
    NodeModelInventory,
    NodeRole,
    NodeTelemetry,
    PullModelRequest,
    SkillSyncPayload,
    StartMediaEngineRequest,
    ToolExecRequest,
    ToolExecResponse,
    WorkspaceDirListing,
)

logger = logging.getLogger("splitch.agent")


def _slugify(raw: str) -> str:
    s = re.sub(r"[^a-z0-9_\-]+", "-", raw.lower()).strip("-")[:32]
    return s or "node"


NODE_ID = os.getenv("SPLITCH_NODE_ID", _slugify(platform.node().split(".")[0] or "local"))
_RAW_ROLE = os.getenv("SPLITCH_NODE_ROLE", "").strip().lower()
NODE_ROLE: NodeRole = (
    _RAW_ROLE  # type: ignore[assignment]
    if _RAW_ROLE in ("gpu", "unified", "cpu")
    else ("unified" if sys.platform == "darwin" else "gpu")
)
ENGINE_URL = os.getenv("SPLITCH_ENGINE_URL", "http://127.0.0.1:8081").rstrip("/")
MEDIA_URL = os.getenv("SPLITCH_MEDIA_URL", "").strip().rstrip("/")
STORAGE_PATH = os.getenv("SPLITCH_STORAGE_PATH", str(default_data_root() / "models"))
WORKSPACE_ROOT = os.getenv("SPLITCH_WORKSPACE_ROOT", str(Path.home()))
EXTRA_MODEL_DIRS = [
    p.strip()
    for p in os.getenv("SPLITCH_EXTRA_MODEL_DIRS", "").split(os.pathsep)
    if p.strip()
]
POLL_INTERVAL_S = float(os.getenv("SPLITCH_AGENT_POLL_S", "3.0"))
ENABLE_TOOLS = os.getenv("SPLITCH_ENABLE_TOOLS", "0").strip().lower() in ("1", "true", "yes")
AGENT_TOKEN = os.getenv("SPLITCH_AGENT_TOKEN", "").strip()


def _collect_storage(path_str: str) -> tuple[bool, float, float]:
    p = Path(path_str)
    if sys.platform == "darwin" and path_str.startswith("/Volumes/"):
        parts = p.parts
        if len(parts) >= 3:
            vol_root = Path(parts[0]) / parts[1] / parts[2]
            if not vol_root.exists() or not os.path.ismount(vol_root):
                return False, 0.0, 0.0
    check_path = p if p.exists() else p.parent
    while not check_path.exists() and check_path != check_path.parent:
        check_path = check_path.parent
    try:
        usage = shutil.disk_usage(check_path)
        return (
            True,
            round(usage.total / (1024**3), 1),
            round(usage.free / (1024**3), 1),
        )
    except OSError:
        return False, 0.0, 0.0


def _normalize_models(payload: dict) -> list[LoadedModel]:
    models: list[LoadedModel] = []
    for item in payload.get("data", []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        raw_status = item.get("status")
        status_val = "loaded"
        if isinstance(raw_status, dict):
            val = str(raw_status.get("value", "unloaded")).lower()
            if val in ("loaded", "loading", "unloaded"):
                status_val = val
        elif isinstance(raw_status, str):
            val = raw_status.lower()
            if val in ("loaded", "loading", "unloaded"):
                status_val = val
        models.append(LoadedModel(id=str(item["id"]), status=status_val))  # type: ignore[arg-type]
    return models


def _normalize_ollama_models(tags_payload: dict, ps_payload: dict | None = None) -> list[LoadedModel]:
    running: set[str] = set()
    if isinstance(ps_payload, dict):
        for item in ps_payload.get("models") or []:
            if isinstance(item, dict) and item.get("name"):
                running.add(str(item["name"]))
    models: list[LoadedModel] = []
    for item in tags_payload.get("models") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        mid = str(item["name"])
        models.append(LoadedModel(id=mid, status="loaded" if mid in running else "unloaded"))
    return models


async def _collect_engine_models(
    client: httpx.AsyncClient,
) -> tuple[bool, EngineKind, list[LoadedModel], int | None]:
    # 1. llama-server router mode (/models + optional /props for n_ctx)
    try:
        resp = await client.get(f"{ENGINE_URL}/models")
        if resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("data"), list):
                ctx_len: int | None = None
                with contextlib.suppress(httpx.HTTPError, ValueError, TypeError):
                    pr = await client.get(f"{ENGINE_URL}/props")
                    if pr.status_code == 200:
                        dgs = pr.json().get("default_generation_settings") or {}
                        if isinstance(dgs.get("n_ctx"), int) and dgs["n_ctx"] > 0:
                            ctx_len = dgs["n_ctx"]
                return True, "llama-server", _normalize_models(body), ctx_len
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("llama-server /models probe skipped: %s", exc)

    # 2. Ollama (/api/tags + /api/ps)
    try:
        tags_resp = await client.get(f"{ENGINE_URL}/api/tags")
        if tags_resp.status_code == 200:
            tags_json = tags_resp.json()
            if isinstance(tags_json, dict) and isinstance(tags_json.get("models"), list):
                ps_json = None
                with contextlib.suppress(httpx.HTTPError, ValueError):
                    ps_resp = await client.get(f"{ENGINE_URL}/api/ps")
                    if ps_resp.status_code == 200:
                        ps_json = ps_resp.json()
                return True, "ollama", _normalize_ollama_models(tags_json, ps_json), None
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("ollama /api/tags probe skipped: %s", exc)

    # 3. Generic OpenAI-compatible server (LM Studio, vLLM, KoboldCpp, SGLang, llama-server)
    try:
        resp = await client.get(f"{ENGINE_URL}/v1/models")
        if resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("data"), list):
                return True, "openai", _normalize_models(body), None
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("openai /v1/models probe skipped: %s", exc)

    return False, "unknown", [], None


async def _probe_single_media_url(
    client: httpx.AsyncClient, base: str
) -> tuple[bool, MediaEngineKind, list[str]]:
    # 1. ComfyUI (/system_stats + /object_info/CheckpointLoaderSimple)
    try:
        r = await client.get(f"{base}/system_stats", timeout=1.2)
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, dict) and ("system" in body or "devices" in body):
                models: list[str] = []
                with contextlib.suppress(httpx.HTTPError, ValueError, TypeError):
                    obj = await client.get(f"{base}/object_info/CheckpointLoaderSimple", timeout=1.5)
                    if obj.status_code == 200:
                        req_inputs = (
                            obj.json()
                            .get("CheckpointLoaderSimple", {})
                            .get("input", {})
                            .get("required", {})
                        )
                        ckpts = req_inputs.get("ckpt_name", [[]])[0]
                        if isinstance(ckpts, list):
                            models.extend(str(c) for c in ckpts[:60])
                with contextlib.suppress(httpx.HTTPError, ValueError, TypeError):
                    uobj = await client.get(f"{base}/object_info/UNETLoader", timeout=1.5)
                    if uobj.status_code == 200:
                        req_inputs = (
                            uobj.json().get("UNETLoader", {}).get("input", {}).get("required", {})
                        )
                        unets = req_inputs.get("unet_name", [[]])[0]
                        if isinstance(unets, list):
                            for u in unets[:40]:
                                if str(u) not in models:
                                    models.append(str(u))
                return True, "comfyui", models
    except (httpx.HTTPError, ValueError):
        pass

    # 2. stable-diffusion.cpp `sd-server` (/sdapi/v1/sd-models or /v1/models or /health)
    try:
        r = await client.get(f"{base}/v1/models", timeout=1.2)
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, dict) and isinstance(body.get("data"), list):
                m_ids = [str(m.get("id")) for m in body["data"] if isinstance(m, dict) and m.get("id")]
                # Distinguish sd-server vs generic OpenAI images server
                srv_hdr = (r.headers.get("server") or "").lower()
                is_sd = "sd" in srv_hdr or "stable-diffusion" in srv_hdr or base.endswith(":8082")
                return True, ("sd-server" if is_sd else "openai-images"), m_ids
    except (httpx.HTTPError, ValueError):
        pass

    try:
        r = await client.get(f"{base}/sdapi/v1/sd-models", timeout=1.2)
        if r.status_code == 200 and isinstance(r.json(), list):
            m_ids = [
                str(m.get("title") or m.get("model_name"))
                for m in r.json()
                if isinstance(m, dict) and (m.get("title") or m.get("model_name"))
            ]
            return True, "sd-server", m_ids[:60]
    except (httpx.HTTPError, ValueError):
        pass

    return False, "none", []


async def _collect_media_engine(
    client: httpx.AsyncClient,
) -> tuple[bool, MediaEngineKind, str, list[str]]:
    candidates: list[str] = []
    if MEDIA_URL:
        candidates.append(MEDIA_URL)
    else:
        for default_url in ("http://127.0.0.1:8082", "http://127.0.0.1:8188"):
            if default_url != ENGINE_URL:
                candidates.append(default_url)

    for base in candidates:
        ok, kind, models = await _probe_single_media_url(client, base)
        if ok:
            return True, kind, base, models
    return False, "none", MEDIA_URL, []


def _scan_local_ggufs() -> list[LocalModelFile]:
    files: list[LocalModelFile] = []
    seen_paths: set[str] = set()
    primary_root = Path(STORAGE_PATH)
    if primary_root.exists():
        for entry in sorted(primary_root.glob("*.gguf")):
            if not entry.is_file():
                continue
            try:
                size_gb = round(entry.stat().st_size / (1024**3), 2)
            except OSError:
                continue
            resolved = str(entry.resolve())
            seen_paths.add(resolved)
            files.append(
                LocalModelFile(
                    filename=entry.name, path=str(entry), size_gb=size_gb, managed=True
                )
            )

    for extra in EXTRA_MODEL_DIRS:
        root = Path(extra)
        if not root.exists():
            continue
        for entry in sorted(root.rglob("*.gguf")):
            try:
                if not entry.is_file():
                    continue
                resolved = str(entry.resolve())
                if resolved in seen_paths:
                    continue
                size_bytes = entry.stat().st_size
                if size_bytes < 50 * 1024 * 1024:
                    continue
                seen_paths.add(resolved)
                files.append(
                    LocalModelFile(
                        filename=entry.name,
                        path=str(entry),
                        size_gb=round(size_bytes / (1024**3), 2),
                        managed=False,
                    )
                )
            except OSError:
                continue
    return files


class AgentState:
    def __init__(self) -> None:
        self.client: httpx.AsyncClient | None = None
        self.downloads: dict[str, DownloadJob] = {}
        self.sd_proc: asyncio.subprocess.Process | None = None
        self.sd_running_model: str = ""
        self.sd_last_error: str | None = None
        self.sd_install_busy: bool = False
        self.sd_install_step: str = ""
        self.snapshot: NodeTelemetry = NodeTelemetry(
            node_id=NODE_ID,
            role=NODE_ROLE,
            platform=platform.system().lower(),
            timestamp=time.time(),
            storage_path=STORAGE_PATH,
            engine_url=ENGINE_URL,
            tools_enabled=ENABLE_TOOLS,
            workspace_root=WORKSPACE_ROOT if ENABLE_TOOLS else "",
        )
        self._task: asyncio.Task | None = None

    async def poll_once(self) -> None:
        assert self.client is not None
        mem = psutil.virtual_memory()
        cpu_pct = psutil.cpu_percent(interval=None)
        ram_total_mb = round(mem.total / (1024 * 1024), 1)
        ram_used_mb = round(mem.used / (1024 * 1024), 1)
        ram_avail_mb = round(mem.available / (1024 * 1024), 1)

        storage_mounted, storage_total_gb, storage_free_gb = await asyncio.to_thread(
            _collect_storage, STORAGE_PATH
        )

        effective_role: NodeRole = NODE_ROLE
        gpus, procs, rec_backend = await collect_all_gpus()
        gpu_summary = summarize_gpus(gpus)
        if sys.platform == "darwin":
            effective_role = "unified"
        elif not gpus and _RAW_ROLE == "":
            effective_role = "cpu"

        engine_healthy, engine_kind, models, context_length = await _collect_engine_models(
            self.client
        )
        media_healthy, media_engine, media_url, media_models = await _collect_media_engine(
            self.client
        )

        self.snapshot = NodeTelemetry(
            node_id=NODE_ID,
            role=effective_role,
            platform=platform.system().lower(),
            timestamp=time.time(),
            online=True,
            cpu_percent=round(cpu_pct, 1),
            ram_total_mb=ram_total_mb,
            ram_used_mb=ram_used_mb,
            ram_available_mb=ram_avail_mb,
            gpu_name=gpu_summary["gpu_name"],
            vram_total_mb=gpu_summary["vram_total_mb"],
            vram_used_mb=gpu_summary["vram_used_mb"],
            vram_free_mb=gpu_summary["vram_free_mb"],
            gpu_util_percent=gpu_summary["gpu_util_percent"],
            gpu_temp_c=gpu_summary["gpu_temp_c"],
            gpu_processes=procs,
            non_llm_vram_mb=gpu_summary["non_llm_vram_mb"],
            gpus=gpus,
            gpu_count=gpu_summary["gpu_count"],
            gpu_vendors=gpu_summary["gpu_vendors"],
            recommended_backend=rec_backend,
            storage_path=STORAGE_PATH,
            storage_mounted=storage_mounted,
            storage_total_gb=storage_total_gb,
            storage_free_gb=storage_free_gb,
            engine=engine_kind,
            engine_healthy=engine_healthy,
            engine_url=ENGINE_URL,
            media_engine=media_engine,
            media_healthy=media_healthy,
            media_url=media_url,
            media_models=media_models,
            context_length=context_length,
            tools_enabled=ENABLE_TOOLS,
            workspace_root=WORKSPACE_ROOT if ENABLE_TOOLS else "",
            models=models,
        )

    async def _loop(self) -> None:
        psutil.cpu_percent(interval=None)
        while True:
            try:
                await self.poll_once()
            except (OSError, httpx.HTTPError, ValueError) as exc:
                logger.warning("agent poll failed: %s", exc)
            await asyncio.sleep(POLL_INTERVAL_S)


state = AgentState()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    await state.poll_once()
    state._task = asyncio.create_task(state._loop())
    yield
    if state.sd_proc and state.sd_proc.returncode is None:
        state.sd_proc.terminate()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(state.sd_proc.wait(), timeout=3.0)
    if state._task:
        state._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state._task
    if state.client:
        await state.client.aclose()


app = FastAPI(title="Splitch Node Agent", version="0.3.0", lifespan=lifespan)


@app.middleware("http")
async def _agent_auth_middleware(request: Request, call_next):
    if AGENT_TOKEN and request.url.path != "/health":
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token or not hmac.compare_digest(token, AGENT_TOKEN):
            response = JSONResponse(status_code=401, content={"detail": "Invalid agent bearer token"})
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/health")
async def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "node_id": NODE_ID,
        "engine": state.snapshot.engine,
        "engine_healthy": state.snapshot.engine_healthy,
        "tools_enabled": ENABLE_TOOLS,
    }


@app.get("/metrics", response_model=NodeTelemetry)
async def get_metrics() -> NodeTelemetry:
    return state.snapshot


@app.post("/models/{action}")
async def model_action(action: Literal["load", "unload"], req: ModelActionRequest) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._:/\-]+", req.model):
        raise HTTPException(status_code=400, detail="Invalid model identifier")
    assert state.client is not None
    try:
        if state.snapshot.engine == "ollama":
            resp = await state.client.post(
                f"{ENGINE_URL}/api/generate",
                json={"model": req.model, "prompt": "", "keep_alive": "10m" if action == "load" else 0},
                timeout=60.0 if action == "load" else 15.0,
            )
        else:
            resp = await state.client.post(
                f"{ENGINE_URL}/models/{action}",
                json={"model": req.model},
                timeout=60.0 if action == "load" else 15.0,
            )
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
        asyncio.create_task(state.poll_once())
        return {"ok": True, "action": action, "model": req.model}
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Inference engine unreachable: {exc}") from exc


# ── Local GGUF Storage Management ────────────────────────────────────────────


@app.get("/files/models", response_model=NodeModelInventory)
async def list_model_files() -> NodeModelInventory:
    files = await asyncio.to_thread(_scan_local_ggufs)
    return NodeModelInventory(
        node_id=NODE_ID,
        storage_path=STORAGE_PATH,
        files=files,
        downloads=list(state.downloads.values()),
    )


async def _run_hf_download(job: DownloadJob) -> None:
    root = Path(STORAGE_PATH)
    root.mkdir(parents=True, exist_ok=True)
    safe_name = Path(job.filename).name
    dest = root / safe_name
    part = root / f"{safe_name}.part"
    url = f"https://huggingface.co/{job.repo_id}/resolve/main/{job.filename}"
    headers = {}
    hf_token = os.getenv("HF_TOKEN")
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"

    try:
        async with (
            httpx.AsyncClient(
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
                    if job.total_bytes > 0:
                        job.percent = round((job.downloaded_bytes / job.total_bytes) * 100.0, 1)
        part.replace(dest)
        job.percent = 100.0
        job.status = "completed"
        asyncio.create_task(state.poll_once())
    except (httpx.HTTPError, OSError) as exc:
        job.status = "failed"
        job.error = str(exc)
        with contextlib.suppress(OSError):
            if part.exists():
                part.unlink()


@app.post("/files/models/pull", response_model=DownloadJob)
async def pull_model_file(req: PullModelRequest) -> DownloadJob:
    if not re.fullmatch(r"[A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+", req.repo_id):
        raise HTTPException(status_code=400, detail="Invalid Hugging Face repo_id")
    if ".." in req.filename or not req.filename.lower().endswith((".gguf", ".safetensors")):
        raise HTTPException(
            status_code=400, detail="Only safe .gguf or .safetensors filenames are allowed"
        )
    mounted, _, _ = await asyncio.to_thread(_collect_storage, STORAGE_PATH)
    if not mounted:
        raise HTTPException(status_code=503, detail=f"Storage not mounted: {STORAGE_PATH}")

    job = DownloadJob(
        id=uuid.uuid4().hex[:10],
        node_id=NODE_ID,
        repo_id=req.repo_id,
        filename=req.filename,
        status="downloading",
    )
    state.downloads[job.id] = job
    asyncio.create_task(_run_hf_download(job))
    return job


# ── Managed Local Media Engine (`sd-server` / ComfyUI) Lifecycle ─────────────


def _scan_local_media_models() -> list[LocalModelFile]:
    files: list[LocalModelFile] = []
    seen: set[str] = set()
    roots = [Path(STORAGE_PATH), Path(STORAGE_PATH) / "media"]
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


def _build_media_engine_status() -> NodeMediaEngineStatus:
    installed = detect_installed_engines()
    sd_info = next((e for e in installed if e.engine == "sd.cpp"), None)
    running = bool(state.sd_proc is not None and state.sd_proc.returncode is None)
    if state.sd_proc is not None and state.sd_proc.returncode is not None and not state.sd_last_error:
        state.sd_last_error = f"sd-server exited with code {state.sd_proc.returncode}"
    return NodeMediaEngineStatus(
        node_id=NODE_ID,
        sd_installed=sd_info is not None,
        sd_binary=sd_info.path if sd_info else "",
        sd_version=sd_info.build_tag or sd_info.version if sd_info else "",
        sd_backend=sd_info.backend if sd_info else "",
        sd_managed_running=running,
        sd_running_model=state.sd_running_model if running else "",
        sd_error=state.sd_last_error,
        install_in_progress=state.sd_install_busy,
        install_step=state.sd_install_step,
        media_engine=state.snapshot.media_engine,
        media_healthy=state.snapshot.media_healthy,
        media_url=state.snapshot.media_url,
        local_media_models=_scan_local_media_models(),
        active_downloads=list(state.downloads.values()),
    )


@app.get("/media/engine/status", response_model=NodeMediaEngineStatus)
async def get_media_engine_status() -> NodeMediaEngineStatus:
    return await asyncio.to_thread(_build_media_engine_status)


@app.post("/media/engine/install", response_model=NodeMediaEngineStatus)
async def install_media_engine(req: InstallMediaEngineRequest) -> NodeMediaEngineStatus:
    if state.sd_install_busy:
        return await asyncio.to_thread(_build_media_engine_status)
    state.sd_install_busy = True
    state.sd_last_error = None
    state.sd_install_step = f"Downloading hardware-matched stable-diffusion.cpp ({req.backend})..."

    async def _worker() -> None:
        try:
            res = await asyncio.to_thread(
                install_or_update_engine,
                "sd.cpp",
                backend=req.backend,
                force=req.force,
            )
            if res.ok:
                state.sd_install_step = res.steps[-1] if res.steps else "Installed stable-diffusion.cpp"
                state.sd_last_error = None
            else:
                state.sd_last_error = res.error or "Failed to install stable-diffusion.cpp"
                state.sd_install_step = "Install failed"
        except (OSError, RuntimeError, ValueError) as exc:
            state.sd_last_error = str(exc)
            state.sd_install_step = "Install failed"
        finally:
            state.sd_install_busy = False

    asyncio.create_task(_worker())
    return await asyncio.to_thread(_build_media_engine_status)


@app.post("/media/engine/start", response_model=NodeMediaEngineStatus)
async def start_media_engine(req: StartMediaEngineRequest) -> NodeMediaEngineStatus:
    if ".." in req.model_file or "/" in req.model_file or "\\" in req.model_file:
        raise HTTPException(status_code=400, detail="Invalid model_file name")

    # Resolve model file strictly inside STORAGE_PATH or STORAGE_PATH/media
    storage_root = Path(STORAGE_PATH).resolve()
    candidates = [storage_root / req.model_file, storage_root / "media" / req.model_file]
    model_path = next(
        (p.resolve() for p in candidates if p.is_file() and p.resolve().is_relative_to(storage_root)),
        None,
    )
    if model_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{req.model_file}' not found in {STORAGE_PATH}",
        )

    installed = await asyncio.to_thread(detect_installed_engines)
    sd_info = next((e for e in installed if e.engine == "sd.cpp"), None)
    if not sd_info:
        raise HTTPException(
            status_code=400,
            detail="stable-diffusion.cpp (sd-server) is not installed on this node yet.",
        )

    # Stop existing managed sd-server before restarting with new model
    if state.sd_proc is not None and state.sd_proc.returncode is None:
        state.sd_proc.terminate()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(state.sd_proc.wait(), timeout=3.0)

    sd_bin = Path(sd_info.path)
    if sd_bin.name.startswith("sd-cli"):
        sibling = sd_bin.parent / ("sd-server.exe" if sys.platform == "win32" else "sd-server")
        if sibling.is_file():
            sd_bin = sibling

    env = os.environ.copy()
    lib_dir = str(sd_bin.parent)
    if sys.platform == "darwin":
        env["DYLD_LIBRARY_PATH"] = f"{lib_dir}:{env.get('DYLD_LIBRARY_PATH', '')}"
    elif sys.platform.startswith("linux"):
        env["LD_LIBRARY_PATH"] = f"{lib_dir}:{env.get('LD_LIBRARY_PATH', '')}"

    cmd = [
        str(sd_bin),
        "--listen-ip",
        "127.0.0.1",
        "--listen-port",
        str(req.port),
        "-m",
        str(model_path),
    ]
    state.sd_last_error = None
    state.sd_running_model = req.model_file
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        state.sd_proc = proc
    except OSError as exc:
        state.sd_last_error = f"Failed to launch sd-server: {exc}"
        raise HTTPException(status_code=500, detail=state.sd_last_error) from exc

    # Wait briefly to detect immediate crash or readiness
    for _ in range(12):
        await asyncio.sleep(0.4)
        if proc.returncode is not None:
            err_tail = ""
            if proc.stderr:
                raw_err = await proc.stderr.read()
                err_tail = raw_err.decode("utf-8", errors="replace").strip()[-500:]
            state.sd_last_error = err_tail or f"sd-server exited with code {proc.returncode}"
            state.sd_running_model = ""
            raise HTTPException(status_code=500, detail=state.sd_last_error)
        if state.client is not None:
            ok, _, _ = await _probe_single_media_url(state.client, f"http://127.0.0.1:{req.port}")
            if ok:
                break

    await state.poll_once()
    return await asyncio.to_thread(_build_media_engine_status)


@app.post("/media/engine/stop", response_model=NodeMediaEngineStatus)
async def stop_media_engine() -> NodeMediaEngineStatus:
    if state.sd_proc is not None and state.sd_proc.returncode is None:
        state.sd_proc.terminate()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(state.sd_proc.wait(), timeout=4.0)
    state.sd_proc = None
    state.sd_running_model = ""
    state.sd_last_error = None
    await state.poll_once()
    return await asyncio.to_thread(_build_media_engine_status)


@app.api_route("/media/proxy/{path:path}", methods=["GET", "POST"])
async def proxy_local_media_engine(path: str, request: Request) -> Response:
    """Proxy router media requests to the node's local sd-server/ComfyUI over the agent port."""
    base = (state.snapshot.media_url or MEDIA_URL or "http://127.0.0.1:8082").rstrip("/")
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            resp = await client.request(
                request.method,
                target,
                content=body if body else None,
                headers=fwd_headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Local media engine error: {exc}") from exc

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


@app.delete("/files/models/{filename}")
async def delete_model_file(filename: str) -> dict:
    if Path(filename).name != filename or not filename.lower().endswith(".gguf"):
        raise HTTPException(status_code=400, detail="Invalid GGUF filename")
    root = Path(STORAGE_PATH).resolve()
    target = (root / filename).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Model file not found")
    await asyncio.to_thread(target.unlink)
    asyncio.create_task(state.poll_once())
    return {"ok": True, "deleted": filename}


# ── Skills & Client Config Sync ──────────────────────────────────────────────


def _sync_skills_to_dir(target_root: Path, skills: list[dict[str, str]]) -> int:
    target_root.mkdir(parents=True, exist_ok=True)
    manifest_file = target_root / ".splitch-managed.json"
    prior_managed: list[str] = []
    if manifest_file.exists():
        try:
            prior_managed = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior_managed = []

    new_ids: list[str] = []
    for item in skills:
        skill_id = item.get("id", "")
        content = item.get("content", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,63}", skill_id):
            continue
        skill_dir = (target_root / skill_id).resolve()
        if not skill_dir.is_relative_to(target_root.resolve()):
            continue
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        new_ids.append(skill_id)

    for old_id in set(prior_managed):
        if old_id not in new_ids and re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,63}", old_id):
            old_dir = (target_root / old_id).resolve()
            if old_dir.is_relative_to(target_root.resolve()) and old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)

    manifest_file.write_text(json.dumps(sorted(new_ids), indent=2), encoding="utf-8")
    return len(new_ids)


@app.post("/sync/skills")
async def sync_skills(payload: SkillSyncPayload) -> dict:
    home = Path.home().resolve()
    if payload.target_dirs:
        dirs: list[Path] = []
        for raw_d in payload.target_dirs:
            resolved = Path(os.path.expanduser(raw_d)).resolve()
            if not resolved.is_relative_to(home):
                raise HTTPException(
                    status_code=403,
                    detail=f"Skill sync target must be inside user home directory: {raw_d}",
                )
            dirs.append(resolved)
    else:
        dirs = [home / ".config" / "opencode" / "skills", home / ".agents" / "skills"]
    synced_paths: list[str] = []
    count = 0
    for d in dirs:
        count = await asyncio.to_thread(_sync_skills_to_dir, d, payload.skills)
        synced_paths.append(str(d))
    return {"ok": True, "synced_count": count, "paths": synced_paths}


def _merge_client_config(client: str, incoming: dict, raw_text: str | None = None) -> str:
    home = Path.home()
    if client in ("hermes", "codex", "aider"):
        targets = {
            "hermes": home / ".hermes" / "config.yaml",
            "codex": home / ".codex" / "config.toml",
            "aider": home / ".aider.conf.yml",
        }
        path = targets[client]
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (raw_text or "").strip()
        if not body:
            raise ValueError(f"Missing config text for client '{client}'")
        if client == "codex" and path.exists():
            # Preserve unrelated codex settings while replacing/appending [model_providers.splitch]
            prior = path.read_text(encoding="utf-8", errors="replace")
            marker_start = "# --- splitch-managed-start ---"
            marker_end = "# --- splitch-managed-end ---"
            managed = f"{marker_start}\n{body}\n{marker_end}\n"
            if marker_start in prior and marker_end in prior:
                pre = prior.split(marker_start, 1)[0].rstrip()
                post = prior.split(marker_end, 1)[1].lstrip()
                combined = (f"{pre}\n\n" if pre else "") + managed + (f"\n{post}" if post else "")
            else:
                combined = prior.rstrip() + ("\n\n" if prior.strip() else "") + managed
            path.write_text(combined, encoding="utf-8")
        else:
            path.write_text(body + "\n", encoding="utf-8")
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                path.chmod(0o600)
        return str(path)

    if client == "claude_code":
        path = home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
        env_map = existing.setdefault("env", {})
        if isinstance(env_map, dict) and isinstance(incoming.get("env"), dict):
            env_map.update(incoming["env"])
        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                path.chmod(0o600)
        return str(path)

    if client == "opencode":
        path = home / ".config" / "opencode" / "opencode.json"
    else:
        path = home / ".config" / "zed" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}

    if client == "opencode":
        existing.setdefault("$schema", "https://opencode.ai/config.json")
        providers = existing.setdefault("provider", {})
        if isinstance(providers, dict) and "provider" in incoming:
            providers.update(incoming["provider"])
        mcp = existing.setdefault("mcp", {})
        if isinstance(mcp, dict) and "mcp" in incoming:
            mcp.update(incoming["mcp"])
        if "model" in incoming and "model" not in existing:
            existing["model"] = incoming["model"]
        if "small_model" in incoming and "small_model" not in existing:
            existing["small_model"] = incoming["small_model"]
    else:
        lm = existing.setdefault("language_models", {})
        if isinstance(lm, dict) and "language_models" in incoming:
            oai = lm.setdefault("openai_compatible", {})
            oai.update(incoming["language_models"].get("openai_compatible", {}))
        cs = existing.setdefault("context_servers", {})
        if isinstance(cs, dict) and "context_servers" in incoming:
            cs.update(incoming["context_servers"])

    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return str(path)


@app.post("/sync/client-config")
async def sync_client_config(req: ClientConfigSyncRequest) -> dict:
    try:
        written_path = await asyncio.to_thread(_merge_client_config, req.client, req.config, req.raw_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "client": req.client, "path": written_path}


@app.post("/sync/mcp-script")
async def sync_mcp_script(req: McpScriptSyncRequest) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,63}", req.id):
        raise HTTPException(status_code=400, detail="Invalid MCP script id")
    scripts_root = Path(STORAGE_PATH).parent / "mcp_servers"

    def _write() -> str:
        scripts_root.mkdir(parents=True, exist_ok=True)
        target = (scripts_root / f"{req.id}.py").resolve()
        if not target.is_relative_to(scripts_root.resolve()):
            raise HTTPException(status_code=403, detail="Path traversal blocked")
        target.write_text(req.code, encoding="utf-8")
        if sys.platform != "win32":
            target.chmod(0o700)
        return str(target)

    path = await asyncio.to_thread(_write)
    return {"ok": True, "id": req.id, "path": path}


# ── Workspace-Sandboxed Agent Tool Execution (Opt-In) ────────────────────────

_IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "target"}


def _resolve_workspace_path(subpath: str, rel: str = ".") -> tuple[Path, Path]:
    base_root = Path(WORKSPACE_ROOT).resolve()
    ws_root = (base_root / subpath.lstrip("/")).resolve() if subpath.strip() else base_root
    if not ws_root.is_relative_to(base_root):
        raise HTTPException(status_code=403, detail="Workspace subpath outside allowed root")
    target = (ws_root / rel.lstrip("/")).resolve() if rel and rel != "." else ws_root
    if not target.is_relative_to(ws_root):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    return ws_root, target


def _exec_fs_tool(req: ToolExecRequest) -> ToolExecResponse:
    ws_root, _ = _resolve_workspace_path(req.workspace_subpath)
    args = req.arguments

    if req.tool == "list_dir":
        _, target = _resolve_workspace_path(req.workspace_subpath, str(args.get("path", ".")))
        if not target.exists() or not target.is_dir():
            return ToolExecResponse(ok=False, output=f"Directory not found: {target}")
        entries = []
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if item.name in _IGNORED_DIRS:
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
        _, target = _resolve_workspace_path(req.workspace_subpath, rel)
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
        _, target = _resolve_workspace_path(req.workspace_subpath, rel)
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
        return ToolExecResponse(
            ok=True,
            output=f"Wrote {len(content)} bytes to {rel}",
            diff=diff_str,
        )

    if req.tool == "grep_search":
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolExecResponse(ok=False, output="Missing 'query' argument")
        _, target = _resolve_workspace_path(req.workspace_subpath, str(args.get("path", ".")))
        matches: list[str] = []
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        for root_dir, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS and not d.startswith(".")]
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


def _list_workspace_dirs(subpath: str) -> WorkspaceDirListing:
    base_root = Path(WORKSPACE_ROOT).resolve()
    ws_root, _ = _resolve_workspace_path(subpath, ".")
    if not ws_root.exists() or not ws_root.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {subpath or '.'}")
    rel_sub = "" if ws_root == base_root else str(ws_root.relative_to(base_root))
    parent: str | None = None
    if ws_root != base_root:
        p = ws_root.parent
        parent = "" if p == base_root else str(p.relative_to(base_root))
    dirs: list[str] = []
    for item in sorted(ws_root.iterdir(), key=lambda entry: entry.name.lower()):
        if item.name.startswith(".") or item.name in _IGNORED_DIRS:
            continue
        with contextlib.suppress(OSError):
            if item.is_dir():
                dirs.append(item.name)
                if len(dirs) >= 200:
                    break
    return WorkspaceDirListing(root=str(base_root), subpath=rel_sub, parent=parent, dirs=dirs)


@app.get("/workspace/dirs", response_model=WorkspaceDirListing)
async def browse_workspace_dirs(subpath: str = "") -> WorkspaceDirListing:
    if not ENABLE_TOOLS:
        raise HTTPException(
            status_code=403,
            detail="Workspace tools are disabled on this node (start with --enable-tools or SPLITCH_ENABLE_TOOLS=1)",
        )
    return await asyncio.to_thread(_list_workspace_dirs, subpath)


@app.post("/tools/execute", response_model=ToolExecResponse)
async def execute_workspace_tool(req: ToolExecRequest) -> ToolExecResponse:
    if not ENABLE_TOOLS:
        raise HTTPException(
            status_code=403,
            detail="Workspace tools are disabled on this node (start with --enable-tools or SPLITCH_ENABLE_TOOLS=1)",
        )
    if req.tool != "run_command":
        return await asyncio.to_thread(_exec_fs_tool, req)

    cmd = str(req.arguments.get("command", "")).strip()
    if not cmd:
        return ToolExecResponse(ok=False, output="Missing 'command' argument", exit_code=1)
    _, cwd = _resolve_workspace_path(req.workspace_subpath, str(req.arguments.get("cwd", ".")))
    if not cwd.exists() or not cwd.is_dir():
        return ToolExecResponse(ok=False, output=f"Working dir not found: {cwd}", exit_code=1)

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        raw_out, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        text = raw_out.decode("utf-8", errors="replace")
        if len(text) > 12_000:
            text = text[:12_000] + "\n... (output truncated)"
        code = proc.returncode if proc.returncode is not None else -1
        return ToolExecResponse(ok=code == 0, output=text or "(no output)", exit_code=code)
    except TimeoutError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        return ToolExecResponse(ok=False, output="Command timed out after 20s", exit_code=124)
    except OSError as exc:
        return ToolExecResponse(ok=False, output=str(exc), exit_code=1)
