"""
splitch-agent (:8090)
Cross-platform per-node hardware + inference engine telemetry, GGUF storage,
client config/skills sync, and opt-in workspace-sandboxed tool runner.
Strictly non-blocking on HTTP hot paths: background loop refreshes cached snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.agent.downloads import (
    DownloadSpeedTracker as _DownloadSpeedTracker,  # noqa: F401
)
from backend.agent.downloads import (
    download_xet_mlx as _download_xet_mlx,  # noqa: F401
)
from backend.agent.downloads import (
    download_xet_single as _download_xet_single,  # noqa: F401
)
from backend.agent.downloads import (
    make_job_tqdm as _make_job_tqdm,  # noqa: F401
)
from backend.agent.downloads import (
    resolve_hf_protocol as _resolve_hf_protocol,  # noqa: F401
)
from backend.agent.downloads import (
    run_hf_download as _run_hf_download_impl,
)
from backend.agent.downloads import (
    run_hf_mlx_download as _run_hf_mlx_download_impl,
)
from backend.agent.media_routes import (
    build_media_agent_router,
)
from backend.agent.media_routes import (
    build_media_status_snapshot as _build_media_status_snapshot,
)
from backend.agent.media_routes import (
    build_sd_model_args as _build_sd_model_args,  # noqa: F401
)
from backend.agent.media_routes import (
    find_first_media_match as _find_first_media_match,  # noqa: F401
)
from backend.agent.media_routes import (
    resolve_media_file as _resolve_media_file,  # noqa: F401
)
from backend.agent.media_routes import (
    scan_local_media_models as _scan_local_media_models_impl,
)
from backend.agent.strata_routes import (
    build_strata_agent_router,
)
from backend.agent.strata_routes import (
    build_strata_status_snapshot as _build_strata_status_snapshot,
)
from backend.agent.sync import (
    merge_client_config as _merge_client_config,
)
from backend.agent.sync import (
    sync_skills_to_dir as _sync_skills_to_dir,
)
from backend.agent.sync import (
    write_mcp_script,
)
from backend.agent.tools import (
    MUTATING_WORKSPACE_TOOLS as _MUTATING_WORKSPACE_TOOLS,
)
from backend.agent.tools import (
    detect_effective_sandbox,
    exec_fs_tool,
    list_workspace_dirs,
    resolve_workspace_path,
    run_post_edit_verification,
    run_sandboxed_shell_command,
)
from backend.agent.workspace_git import (
    get_git_diff,
    get_git_status,
    git_commit,
    git_discard_paths,
    git_stage_paths,
    git_unstage_paths,
    list_workspace_tree,
    read_workspace_file,
)
from backend.router.config import default_data_root
from backend.shared.engines import (
    detect_installed_engines,  # noqa: F401
    install_or_update_engine,  # noqa: F401
)
from backend.shared.gpu_probe import (
    LLM_PROCESS_TOKENS,  # noqa: F401
    collect_all_gpus,
    summarize_gpus,
)
from backend.shared.gpu_probe import (
    parse_nvidia_pmon as _parse_pmon,  # noqa: F401
)
from backend.shared.models import (
    AgentRollbackRequest,
    AgentsMdPayload,
    AgentsMdWriteRequest,
    AgentUpdateRequest,
    ClientConfigSyncRequest,
    DownloadJob,
    EngineKind,
    EngineRuntimeConfig,
    GitActionResponse,
    GitCommitRequest,
    GitDiffResponse,
    GitPathsRequest,
    GitStatusResponse,
    LoadedModel,
    LocalModelFile,
    McpScriptSyncRequest,
    MediaEngineKind,
    ModelActionRequest,
    NodeMediaEngineStatus,
    NodeModelInventory,
    NodeRole,
    NodeStrataStatus,
    NodeTelemetry,
    PullModelRequest,
    SkillSyncPayload,
    StorageCleanResponse,
    StorageUsageResponse,
    StrataJobState,
    ToolExecRequest,
    ToolExecResponse,
    UiThemeConfig,
    WorkspaceDirListing,
    WorkspaceFileContent,
    WorkspaceTreeListing,
)
from backend.shared.secrets import (
    load_ed25519_public_key,
    resolve_secret_value,
    verify_agent_request_ed25519,
    verify_tool_approval_ed25519,
)
from backend.shared.security import (
    DEFAULT_AGENT_NONCE_CACHE,
    DEFAULT_TOOL_NONCE_CACHE,
    is_allowed_host_header,
    is_loopback_host,
    verify_tool_approval,
)
from backend.shared.strata_manager import (
    build_strata_calibrate_cmd,  # noqa: F401
    build_strata_launch_cmd,  # noqa: F401
    import_strata_gguf_plan,  # noqa: F401
    install_or_update_strata,  # noqa: F401
    list_installed_strata_models,  # noqa: F401
    list_strata_presets,  # noqa: F401
    read_strata_engine_meta,  # noqa: F401
    resolve_strata_bin,
    stop_strata_server,  # noqa: F401
    strata_env,  # noqa: F401
)
from backend.shared.updater import (
    UpdateResult,
    _restart_active_services,
    perform_rollback,
    perform_update,
)
from backend.version import __version__ as VERSION

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
STRATA_PORT = int(os.getenv("SPLITCH_STRATA_PORT", "8085"))
STRATA_URL = os.getenv("SPLITCH_STRATA_URL", f"http://127.0.0.1:{STRATA_PORT}").rstrip("/")
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
try:
    AGENT_TOKEN = resolve_secret_value(os.getenv("SPLITCH_AGENT_TOKEN", ""), check_permissions=True)
except (OSError, PermissionError, ValueError):
    AGENT_TOKEN = ""
ROUTER_PUBLIC_KEY = None
_RAW_ROUTER_PUB = os.getenv("SPLITCH_ROUTER_PUBLIC_KEY", "").strip()
if _RAW_ROUTER_PUB:
    with contextlib.suppress(OSError, ValueError, TypeError):
        ROUTER_PUBLIC_KEY = load_ed25519_public_key(_RAW_ROUTER_PUB)
ALLOW_UNAUTH_TOOLS = os.getenv("SPLITCH_ALLOW_UNAUTH_TOOLS", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
CMD_SANDBOX = os.getenv("SPLITCH_CMD_SANDBOX", "required").strip().lower()
CMD_SANDBOX_ALLOW_NET = os.getenv("SPLITCH_CMD_SANDBOX_ALLOW_NET", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
_CMD_TIMEOUT_S = float(os.getenv("SPLITCH_CMD_TIMEOUT_S", "20.0"))


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
    # 0. Splitch managed MLX-LM or Strata server (/health engine/server tag + /v1/models)
    try:
        h_resp = await client.get(f"{ENGINE_URL}/health")
        if h_resp.status_code == 200:
            h_body = h_resp.json()
            if isinstance(h_body, dict):
                h_eng = str(h_body.get("engine") or "").lower()
                h_srv = str(h_body.get("server") or h_body.get("name") or "").lower()
                detected_kind: EngineKind | None = (
                    "mlx-lm"
                    if h_eng == "mlx-lm"
                    else ("strata" if (h_eng == "strata" or "strata" in h_srv) else None)
                )
                if detected_kind is not None:
                    m_resp = await client.get(f"{ENGINE_URL}/v1/models")
                    if m_resp.status_code == 200:
                        m_body = m_resp.json()
                        if isinstance(m_body, dict) and isinstance(m_body.get("data"), list):
                            return True, detected_kind, _normalize_models(m_body), None
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("mlx-lm / strata /health probe skipped: %s", exc)

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

    # 3. Generic OpenAI-compatible server (LM Studio, vLLM, KoboldCpp, SGLang, MLX, Strata, llama-server)
    try:
        resp = await client.get(f"{ENGINE_URL}/v1/models")
        if resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("data"), list):
                data_list = body.get("data") or []
                is_mlx = any(
                    isinstance(m, dict) and str(m.get("owned_by") or "").lower() == "mlx-lm"
                    for m in data_list
                )
                is_strata = any(
                    isinstance(m, dict)
                    and (
                        str(m.get("owned_by") or "").lower() == "strata"
                        or str(m.get("id") or "").lower() == "strata"
                        or str(m.get("id") or "").lower().startswith("strata-")
                    )
                    for m in data_list
                )
                kind: EngineKind = "mlx-lm" if is_mlx else ("strata" if is_strata else "openai")
                return True, kind, _normalize_models(body), None
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("openai /v1/models probe skipped: %s", exc)

    return False, "unknown", [], None


async def _probe_strata_sidecar(
    client: httpx.AsyncClient,
) -> tuple[bool, str, list[LoadedModel]]:
    """Probe the managed Strata sidecar (`127.0.0.1:8085`) without blocking hot paths."""
    if STRATA_URL == ENGINE_URL:
        return False, "", []
    try:
        r = await client.get(f"{STRATA_URL}/v1/models", timeout=1.0)
        if r.status_code == 200:
            body = r.json()
            if isinstance(body, dict) and isinstance(body.get("data"), list):
                models = _normalize_models(body)
                active = next((m.id for m in models if m.status == "loaded"), models[0].id if models else "")
                return True, active, models
    except (httpx.HTTPError, ValueError):
        pass
    try:
        hr = await client.get(f"{STRATA_URL}/health", timeout=0.8)
        if hr.status_code == 200:
            hb = hr.json()
            active = str(hb.get("model") or hb.get("active_model") or "") if isinstance(hb, dict) else ""
            models = [LoadedModel(id=active, status="loaded")] if active else []
            return True, active, models
    except (httpx.HTTPError, ValueError):
        pass
    return False, "", []


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
        for pat in ("*.gguf", "*.strata"):
            for entry in sorted(primary_root.glob(pat)):
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
        for pat in ("*.gguf", "*.strata"):
            for entry in sorted(root.rglob(pat)):
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
        self.strata_proc: asyncio.subprocess.Process | None = None
        self.strata_log_tail: deque[str] = deque(maxlen=80)
        self.strata_job: StrataJobState = StrataJobState()
        self.snapshot: NodeTelemetry = NodeTelemetry(
            node_id=NODE_ID,
            role=NODE_ROLE,
            platform=platform.system().lower(),
            timestamp=time.time(),
            version=VERSION,
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
        strata_installed = await asyncio.to_thread(lambda: resolve_strata_bin() is not None)
        strata_healthy, strata_active_model, strata_models = await _probe_strata_sidecar(
            self.client
        )

        effective_sandbox = (
            await asyncio.to_thread(
                detect_effective_sandbox, CMD_SANDBOX, allow_net=CMD_SANDBOX_ALLOW_NET
            )
            if ENABLE_TOOLS
            else ""
        )

        self.snapshot = NodeTelemetry(
            node_id=NODE_ID,
            role=effective_role,
            platform=platform.system().lower(),
            timestamp=time.time(),
            online=True,
            version=VERSION,
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
            cmd_sandbox=effective_sandbox,
            models=models,
            strata_installed=strata_installed,
            strata_healthy=strata_healthy,
            strata_active_model=strata_active_model,
            strata_models=strata_models,
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


app = FastAPI(title="Splitch Node Agent", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def _agent_auth_middleware(request: Request, call_next):
    if not is_allowed_host_header(request.headers.get("host")):
        return JSONResponse(status_code=421, content={"detail": "Invalid Host header"})
    if (AGENT_TOKEN or ROUTER_PUBLIC_KEY is not None) and request.url.path != "/health":
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        ok_bearer = bool(AGENT_TOKEN and token and hmac.compare_digest(token, AGENT_TOKEN))
        ok_ed25519 = False
        if ROUTER_PUBLIC_KEY is not None and request.headers.get("x-splitch-auth-sig"):
            body_sha_hdr = request.headers.get("x-splitch-auth-body-sha256", "")
            req_body = await request.body() if body_sha_hdr else b""
            ok_ed25519 = verify_agent_request_ed25519(
                ROUTER_PUBLIC_KEY,
                request.headers.get("x-splitch-auth-ts", ""),
                request.headers.get("x-splitch-auth-sig", ""),
                request.method,
                request.url.path,
                nonce_header=request.headers.get("x-splitch-auth-nonce", ""),
                body=req_body,
                body_sha256_header=body_sha_hdr,
                replay_cache=DEFAULT_AGENT_NONCE_CACHE,
            )
        if not (ok_bearer or ok_ed25519):
            response = JSONResponse(
                status_code=401, content={"detail": "Invalid agent bearer token or Ed25519 signature"}
            )
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
        "version": VERSION,
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


def _scan_local_mlx_models() -> list[LocalModelFile]:
    files: list[LocalModelFile] = []
    mlx_root = Path(STORAGE_PATH) / "mlx"
    if not mlx_root.exists() or not mlx_root.is_dir():
        return files
    for entry in sorted(mlx_root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "config.json").is_file() or not any(entry.glob("*.safetensors")):
            continue
        total_bytes = 0
        for child in entry.rglob("*"):
            if child.is_file():
                with contextlib.suppress(OSError):
                    total_bytes += child.stat().st_size
        files.append(
            LocalModelFile(
                filename=entry.name,
                path=str(entry),
                size_gb=round(total_bytes / (1024**3), 2),
                managed=True,
                format="mlx",
            )
        )
    return files


@app.get("/files/models", response_model=NodeModelInventory)
async def list_model_files() -> NodeModelInventory:
    ggufs = await asyncio.to_thread(_scan_local_ggufs)
    mlxs = await asyncio.to_thread(_scan_local_mlx_models)
    return NodeModelInventory(
        node_id=NODE_ID,
        storage_path=STORAGE_PATH,
        files=[*ggufs, *mlxs],
        downloads=list(state.downloads.values()),
    )


async def _refresh_engine_model_index(expected_stem: str | None = None) -> None:
    """Refresh llama-server --models-dir index after a GGUF is added or removed."""
    await state.poll_once()
    if state.snapshot.engine != "llama-server":
        return
    if expected_stem:
        known = {m.id.lower() for m in state.snapshot.models}
        if expected_stem.lower() in known:
            return

    cmd: list[str] | None = None
    custom_cmd = os.getenv("SPLITCH_ENGINE_RELOAD_CMD", "").strip()
    if custom_cmd:
        cmd = shlex.split(custom_cmd)
    elif sys.platform == "linux" and shutil.which("systemctl"):
        for unit in ("splitch-llama.service", "llama-server.service"):
            try:
                chk = await asyncio.create_subprocess_exec(
                    "systemctl",
                    "--user",
                    "is-active",
                    "--quiet",
                    unit,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await asyncio.wait_for(chk.wait(), timeout=3.0)
                if rc == 0:
                    cmd = ["systemctl", "--user", "restart", unit]
                    break
            except (OSError, TimeoutError):
                continue
    elif sys.platform == "darwin" and shutil.which("launchctl"):
        uid = os.getuid()
        for label in ("com.splitch.llama", "ai.splitch.llama"):
            try:
                chk = await asyncio.create_subprocess_exec(
                    "launchctl",
                    "print",
                    f"gui/{uid}/{label}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await asyncio.wait_for(chk.wait(), timeout=3.0)
                if rc == 0:
                    cmd = ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]
                    break
            except (OSError, TimeoutError):
                continue

    if not cmd:
        return

    try:
        logger.info("Refreshing llama-server model index via: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=15.0)
    except (OSError, TimeoutError) as exc:
        logger.warning("Engine index refresh command failed: %s", exc)
        return

    for _ in range(6):
        await asyncio.sleep(1.0)
        await state.poll_once()
        if state.snapshot.engine_healthy and (
            not expected_stem
            or any(m.id.lower() == expected_stem.lower() for m in state.snapshot.models)
        ):
            break


async def _run_hf_download(job: DownloadJob, *, hf_token_override: str = "") -> None:
    await _run_hf_download_impl(
        job,
        storage_path=STORAGE_PATH,
        refresh_engine_cb=_refresh_engine_model_index,
        poll_once_cb=state.poll_once,
        async_client_cls=httpx.AsyncClient,
        hf_token_override=hf_token_override,
    )


async def _run_hf_mlx_download(job: DownloadJob, *, hf_token_override: str = "") -> None:
    await _run_hf_mlx_download_impl(
        job,
        storage_path=STORAGE_PATH,
        poll_once_cb=state.poll_once,
        async_client_cls=httpx.AsyncClient,
        hf_token_override=hf_token_override,
    )


@app.post("/files/models/pull", response_model=DownloadJob)
async def pull_model_file(req: PullModelRequest, request: Request) -> DownloadJob:
    if not re.fullmatch(r"[A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+", req.repo_id):
        raise HTTPException(status_code=400, detail="Invalid Hugging Face repo_id")
    hf_token_hdr = request.headers.get("x-splitch-hf-token", "").strip()
    if req.format == "mlx":
        mounted, _, _ = await asyncio.to_thread(_collect_storage, STORAGE_PATH)
        if not mounted:
            raise HTTPException(status_code=503, detail=f"Storage not mounted: {STORAGE_PATH}")
        slug = req.repo_id.replace("/", "--")
        job = DownloadJob(
            id=uuid.uuid4().hex[:10],
            node_id=NODE_ID,
            repo_id=req.repo_id,
            filename=slug,
            status="downloading",
        )
        state.downloads[job.id] = job
        asyncio.create_task(_run_hf_mlx_download(job, hf_token_override=hf_token_hdr))
        return job

    if ".." in req.filename or not req.filename.lower().endswith((".gguf", ".safetensors", ".strata")):
        raise HTTPException(
            status_code=400, detail="Only safe .gguf, .safetensors, or .strata filenames are allowed"
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
    asyncio.create_task(_run_hf_download(job, hf_token_override=hf_token_hdr))
    return job


def _engine_runtime_state_file() -> Path:
    return default_data_root() / "engine_runtime.json"


def _resolve_llama_service_target() -> tuple[str, str, Path | None]:
    if sys.platform.startswith("linux"):
        systemd_dir = Path.home() / ".config" / "systemd" / "user"
        for unit in ("splitch-llama.service", "llama-server.service"):
            unit_file = systemd_dir / unit
            if unit_file.is_file():
                return "systemd", unit, systemd_dir / f"{unit}.d" / "99-splitch-runtime.conf"
    elif sys.platform == "darwin":
        agents_dir = Path.home() / "Library" / "LaunchAgents"
        for label in ("com.splitch.llama", "ai.splitch.llama"):
            plist_file = agents_dir / f"{label}.plist"
            if plist_file.is_file():
                return "launchd", label, plist_file
    return "none", "", None


def _read_engine_runtime_config() -> EngineRuntimeConfig:
    kind, name, target_path = _resolve_llama_service_target()
    data: dict[str, object] = {}
    p = _engine_runtime_state_file()
    if p.is_file():
        with contextlib.suppress(OSError, ValueError):
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
    data["supported"] = kind != "none"
    data["unit_name"] = name
    data["override_path"] = str(target_path) if target_path else ""
    data["apply_restart"] = False
    return EngineRuntimeConfig.model_validate(data)


def _strip_llama_managed_flags(args: list[str]) -> list[str]:
    """Strip flags managed by EngineRuntimeConfig so updated values replace cleanly."""
    valued = {
        "--ctx-size",
        "-c",
        "--cache-type-k",
        "-ctk",
        "--cache-type-v",
        "-ctv",
        "--flash-attn",
        "-fa",
        "--models-max",
        "--sleep-idle-seconds",
        "--parallel",
        "-np",
    }
    out: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        flag_name = tok.split("=", 1)[0]
        if flag_name in valued:
            if "=" not in tok and i + 1 < len(args):
                i += 2
            else:
                i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _apply_engine_runtime_config(cfg: EngineRuntimeConfig) -> EngineRuntimeConfig:
    kind, name, target_path = _resolve_llama_service_target()
    state_file = _engine_runtime_state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    saved = cfg.model_dump()
    saved["apply_restart"] = False
    saved["supported"] = kind != "none"
    saved["unit_name"] = name
    saved["override_path"] = str(target_path) if target_path else ""
    state_file.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")

    managed_flags = [
        "--ctx-size",
        str(cfg.ctx_size),
        "--cache-type-k",
        cfg.cache_type_k,
        "--cache-type-v",
        cfg.cache_type_v,
        "--flash-attn",
        "on" if cfg.flash_attn else "off",
        "--models-max",
        str(cfg.models_max),
        "--sleep-idle-seconds",
        str(cfg.sleep_idle_seconds),
        "--parallel",
        str(cfg.parallel),
    ]

    if kind == "systemd" and target_path is not None:
        unit_file = target_path.parent.parent / name
        base_exec: list[str] = []
        with contextlib.suppress(OSError):
            raw_unit = unit_file.read_text(encoding="utf-8")
            # Collapse line-continued ExecStart= blocks
            joined = re.sub(r"\\\s*\n\s*", " ", raw_unit)
            m = re.search(r"^ExecStart=(.+)$", joined, re.MULTILINE)
            if m:
                base_exec = shlex.split(m.group(1).strip())
        if not base_exec:
            llama_bin = shutil.which("llama-server") or "/usr/bin/llama-server"
            base_exec = [
                llama_bin,
                "--host",
                "0.0.0.0",
                "--port",
                "8081",
                "--models-dir",
                STORAGE_PATH,
            ]
        final_args = _strip_llama_managed_flags(base_exec) + managed_flags
        target_path.parent.mkdir(parents=True, exist_ok=True)
        escaped_cmd = " ".join(shlex.quote(a) for a in final_args)
        target_path.write_text(f"[Service]\nExecStart=\nExecStart={escaped_cmd}\n", encoding="utf-8")
        if cfg.apply_restart and shutil.which("systemctl"):
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True,
                timeout=8.0,
                check=False,
            )
            subprocess.run(
                ["systemctl", "--user", "restart", name],
                capture_output=True,
                timeout=12.0,
                check=False,
            )
    elif kind == "launchd" and target_path is not None:
        with contextlib.suppress(OSError, plistlib.InvalidFileException, ValueError):
            plist_data = plistlib.loads(target_path.read_bytes())
            prog = list(plist_data.get("ProgramArguments") or [])
            if prog:
                plist_data["ProgramArguments"] = _strip_llama_managed_flags(
                    [str(x) for x in prog]
                ) + managed_flags
                target_path.write_bytes(plistlib.dumps(plist_data))
                if cfg.apply_restart and shutil.which("launchctl"):
                    uid = os.getuid()
                    subprocess.run(
                        ["launchctl", "kickstart", "-k", f"gui/{uid}/{name}"],
                        capture_output=True,
                        timeout=10.0,
                        check=False,
                    )

    return EngineRuntimeConfig.model_validate(saved)


def _storage_cache_dirs() -> list[Path]:
    dirs: list[Path] = []
    llama_cache = os.getenv("LLAMA_CACHE", "").strip()
    if llama_cache:
        dirs.append(Path(llama_cache).expanduser())
    sib_cache = Path(STORAGE_PATH).expanduser().parent / "cache"
    if sib_cache.is_dir():
        dirs.append(sib_cache)
    hf_hub = Path.home() / ".cache" / "huggingface" / "hub"
    if hf_hub.is_dir():
        dirs.append(hf_hub)
    seen: set[Path] = set()
    uniq: list[Path] = []
    for d in dirs:
        res = d.resolve()
        if res not in seen and res != Path(STORAGE_PATH).expanduser().resolve():
            seen.add(res)
            uniq.append(d)
    return uniq


def _scan_storage_usage() -> StorageUsageResponse:
    root = Path(STORAGE_PATH).expanduser()
    free_gb = 0.0
    if root.exists():
        with contextlib.suppress(OSError):
            free_gb = round(shutil.disk_usage(root).free / (1024**3), 2)

    models_bytes = 0
    partial_bytes = 0
    partial_count = 0
    if root.is_dir():
        for entry in root.rglob("*"):
            with contextlib.suppress(OSError):
                rel_parts = entry.relative_to(root).parts
                if any(p.endswith(".part") for p in rel_parts[:-1]):
                    continue
                if entry.name.endswith(".part"):
                    partial_count += 1
                    if entry.is_file():
                        partial_bytes += entry.stat().st_size
                    elif entry.is_dir():
                        partial_bytes += sum(
                            p.stat().st_size for p in entry.rglob("*") if p.is_file()
                        )
                elif entry.is_file() and entry.suffix.lower() in (
                    ".gguf",
                    ".strata",
                    ".safetensors",
                    ".ckpt",
                ):
                    models_bytes += entry.stat().st_size

    cache_bytes = 0
    cache_entries = 0
    for cdir in _storage_cache_dirs():
        if not cdir.is_dir():
            continue
        with contextlib.suppress(OSError):
            for entry in cdir.rglob("*"):
                if entry.is_file():
                    cache_entries += 1
                    cache_bytes += entry.stat().st_size

    return StorageUsageResponse(
        storage_path=str(root),
        free_gb=free_gb,
        models_gb=round(models_bytes / (1024**3), 2),
        partial_gb=round(partial_bytes / (1024**3), 2),
        cache_gb=round(cache_bytes / (1024**3), 2),
        partial_count=partial_count,
        cache_entries=cache_entries,
    )


def _clean_storage_cache() -> StorageCleanResponse:
    root = Path(STORAGE_PATH).expanduser()
    removed = 0
    freed_bytes = 0
    if root.is_dir():
        for entry in list(root.rglob("*.part")):
            with contextlib.suppress(OSError):
                if entry.is_file():
                    sz = entry.stat().st_size
                    entry.unlink()
                    removed += 1
                    freed_bytes += sz
                elif entry.is_dir():
                    sz = sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
                    shutil.rmtree(entry)
                    removed += 1
                    freed_bytes += sz

    for cdir in _storage_cache_dirs():
        if not cdir.is_dir():
            continue
        for child in list(cdir.iterdir()):
            with contextlib.suppress(OSError):
                if child.is_file():
                    sz = child.stat().st_size
                    child.unlink()
                    removed += 1
                    freed_bytes += sz
                elif child.is_dir():
                    sz = sum(p.stat().st_size for p in child.rglob("*") if p.is_file())
                    shutil.rmtree(child)
                    removed += 1
                    freed_bytes += sz

    return StorageCleanResponse(removed_files=removed, freed_gb=round(freed_bytes / (1024**3), 2))


@app.get("/engine/runtime-config", response_model=EngineRuntimeConfig)
async def get_engine_runtime_config() -> EngineRuntimeConfig:
    return await asyncio.to_thread(_read_engine_runtime_config)


@app.post("/engine/runtime-config", response_model=EngineRuntimeConfig)
async def set_engine_runtime_config(cfg: EngineRuntimeConfig) -> EngineRuntimeConfig:
    updated = await asyncio.to_thread(_apply_engine_runtime_config, cfg)
    if cfg.apply_restart:
        asyncio.create_task(state.poll_once())
    return updated


@app.get("/files/storage/usage", response_model=StorageUsageResponse)
async def get_storage_usage() -> StorageUsageResponse:
    return await asyncio.to_thread(_scan_storage_usage)


@app.post("/files/storage/clean-cache", response_model=StorageCleanResponse)
async def clean_storage_cache() -> StorageCleanResponse:
    res = await asyncio.to_thread(_clean_storage_cache)
    asyncio.create_task(state.poll_once())
    return res


# ── Managed Local Media Engine (`sd-server` / ComfyUI) Lifecycle ─────────────


def _scan_local_media_models() -> list[LocalModelFile]:
    return _scan_local_media_models_impl(STORAGE_PATH)


def _build_media_engine_status() -> NodeMediaEngineStatus:
    return _build_media_status_snapshot(sys.modules[__name__])


# ── Managed Strata Sparse-MoE Lifecycle, Quick Tune & Signed Streaming Proxy ─


def _build_strata_status() -> NodeStrataStatus:
    return _build_strata_status_snapshot(sys.modules[__name__])


app.include_router(build_media_agent_router(lambda: sys.modules[__name__]))
app.include_router(build_strata_agent_router(lambda: sys.modules[__name__]))


@app.delete("/files/models/{filename}")
async def delete_model_file(filename: str) -> dict:
    if Path(filename).name != filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid model filename")
    root = Path(STORAGE_PATH).resolve()
    if filename.lower().endswith(".gguf"):
        target = (root / filename).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(status_code=403, detail="Path traversal blocked")
        if not target.exists():
            raise HTTPException(status_code=404, detail="Model file not found")
        await asyncio.to_thread(target.unlink)
        asyncio.create_task(_refresh_engine_model_index())
        return {"ok": True, "deleted": filename}

    mlx_root = (root / "mlx").resolve()
    mlx_target = (mlx_root / filename).resolve()
    if not mlx_target.is_relative_to(mlx_root) or mlx_target == mlx_root:
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    if not mlx_target.is_dir():
        raise HTTPException(status_code=404, detail="Model file or MLX directory not found")
    await asyncio.to_thread(shutil.rmtree, mlx_target)
    asyncio.create_task(state.poll_once())
    return {"ok": True, "deleted": filename}


# ── Skills & Client Config Sync ──────────────────────────────────────────────


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


@app.post("/sync/client-config")
async def sync_client_config(req: ClientConfigSyncRequest) -> dict:
    try:
        written_path = await asyncio.to_thread(
            _merge_client_config, req.client, req.config, req.raw_text
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "client": req.client, "path": written_path}


@app.post("/sync/mcp-script")
async def sync_mcp_script(req: McpScriptSyncRequest) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,63}", req.id):
        raise HTTPException(status_code=400, detail="Invalid MCP script id")
    path = await asyncio.to_thread(write_mcp_script, STORAGE_PATH, req.id, req.code)
    return {"ok": True, "id": req.id, "path": path}


# ── Workspace-Sandboxed Agent Tool Execution (Opt-In) ────────────────────────


def _resolve_workspace_path(subpath: str, rel: str = ".") -> tuple[Path, Path]:
    return resolve_workspace_path(WORKSPACE_ROOT, subpath, rel)


def _run_post_edit_verification(target: Path, ws_root: Path) -> dict[str, str | bool]:
    return run_post_edit_verification(target, ws_root)


def _exec_fs_tool(req: ToolExecRequest) -> ToolExecResponse:
    return exec_fs_tool(req, WORKSPACE_ROOT)


@app.get("/workspace/dirs", response_model=WorkspaceDirListing)
async def browse_workspace_dirs(subpath: str = "") -> WorkspaceDirListing:
    if not ENABLE_TOOLS:
        raise HTTPException(
            status_code=403,
            detail="Workspace tools are disabled on this node (start with --enable-tools or SPLITCH_ENABLE_TOOLS=1)",
        )
    return await asyncio.to_thread(list_workspace_dirs, WORKSPACE_ROOT, subpath)


def _require_tools_enabled() -> None:
    if not ENABLE_TOOLS:
        raise HTTPException(
            status_code=403,
            detail="Workspace tools are disabled on this node (start with --enable-tools or SPLITCH_ENABLE_TOOLS=1)",
        )


@app.get("/workspace/tree", response_model=WorkspaceTreeListing)
async def get_workspace_tree(subpath: str = "", dir_path: str = "") -> WorkspaceTreeListing:
    _require_tools_enabled()
    return await asyncio.to_thread(list_workspace_tree, WORKSPACE_ROOT, subpath, dir_path)


@app.get("/workspace/file", response_model=WorkspaceFileContent)
async def get_workspace_file(subpath: str = "", path: str = "") -> WorkspaceFileContent:
    _require_tools_enabled()
    return await asyncio.to_thread(read_workspace_file, WORKSPACE_ROOT, subpath, path)


@app.post("/workspace/agents-md", response_model=AgentsMdPayload)
async def write_workspace_agents_md(req: AgentsMdWriteRequest) -> AgentsMdPayload:
    _require_tools_enabled()

    def _write() -> AgentsMdPayload:
        base = Path(WORKSPACE_ROOT).expanduser().resolve()
        ws = (base / req.subpath).resolve() if req.subpath.strip() else base
        if not ws.is_relative_to(base) or not ws.is_dir():
            raise HTTPException(status_code=400, detail="Invalid workspace subpath")
        target = (ws / "AGENTS.md").resolve()
        if not target.is_relative_to(ws):
            raise HTTPException(status_code=400, detail="Invalid AGENTS.md path")
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(req.content)
        return AgentsMdPayload(content=req.content, exists=True, path="AGENTS.md")

    return await asyncio.to_thread(_write)


@app.get("/workspace/git/status", response_model=GitStatusResponse)
async def get_workspace_git_status(subpath: str = "") -> GitStatusResponse:
    _require_tools_enabled()
    return await asyncio.to_thread(get_git_status, WORKSPACE_ROOT, subpath)


@app.get("/workspace/git/diff", response_model=GitDiffResponse)
async def get_workspace_git_diff(
    subpath: str = "", path: str = "", staged: bool = False
) -> GitDiffResponse:
    _require_tools_enabled()
    return await asyncio.to_thread(get_git_diff, WORKSPACE_ROOT, subpath, path, staged)


@app.post("/workspace/git/stage", response_model=GitActionResponse)
async def post_workspace_git_stage(req: GitPathsRequest) -> GitActionResponse:
    _require_tools_enabled()
    return await asyncio.to_thread(git_stage_paths, WORKSPACE_ROOT, req.subpath, req.paths)


@app.post("/workspace/git/unstage", response_model=GitActionResponse)
async def post_workspace_git_unstage(req: GitPathsRequest) -> GitActionResponse:
    _require_tools_enabled()
    return await asyncio.to_thread(git_unstage_paths, WORKSPACE_ROOT, req.subpath, req.paths)


@app.post("/workspace/git/discard", response_model=GitActionResponse)
async def post_workspace_git_discard(req: GitPathsRequest) -> GitActionResponse:
    _require_tools_enabled()
    return await asyncio.to_thread(git_discard_paths, WORKSPACE_ROOT, req.subpath, req.paths)


@app.post("/workspace/git/commit", response_model=GitActionResponse)
async def post_workspace_git_commit(req: GitCommitRequest) -> GitActionResponse:
    _require_tools_enabled()
    return await asyncio.to_thread(git_commit, WORKSPACE_ROOT, req.subpath, req.message)


@app.post("/tools/execute", response_model=ToolExecResponse)
async def execute_workspace_tool(req: ToolExecRequest, request: Request) -> ToolExecResponse:
    if not ENABLE_TOOLS:
        raise HTTPException(
            status_code=403,
            detail="Workspace tools are disabled on this node (start with --enable-tools or SPLITCH_ENABLE_TOOLS=1)",
        )
    if req.tool in _MUTATING_WORKSPACE_TOOLS:
        if AGENT_TOKEN or ROUTER_PUBLIC_KEY is not None:
            ts_hdr = request.headers.get("x-splitch-approval-ts", "")
            sig_hdr = request.headers.get("x-splitch-approval-sig", "")
            nonce_hdr = request.headers.get("x-splitch-approval-nonce", "")
            ok_ed25519 = bool(
                ROUTER_PUBLIC_KEY is not None
                and verify_tool_approval_ed25519(
                    ROUTER_PUBLIC_KEY,
                    ts_hdr,
                    sig_hdr,
                    req.tool,
                    req.arguments,
                    req.workspace_subpath,
                    nonce=nonce_hdr,
                    replay_cache=DEFAULT_TOOL_NONCE_CACHE,
                )
            )
            ok_hmac = bool(
                AGENT_TOKEN
                and verify_tool_approval(
                    AGENT_TOKEN,
                    ts_hdr,
                    sig_hdr,
                    req.tool,
                    req.arguments,
                    req.workspace_subpath,
                    nonce=nonce_hdr,
                    replay_cache=DEFAULT_TOOL_NONCE_CACHE,
                )
            )
            if not (ok_ed25519 or ok_hmac):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Execution of mutating tool '{req.tool}' requires a valid "
                        "router-signed Ed25519 or HMAC approval assertion "
                        "(X-Splitch-Approval-Ts / X-Splitch-Approval-Sig)."
                    ),
                )
        else:
            peer_host = request.client.host if request.client else ""
            has_router_approval = (
                request.headers.get("x-splitch-router-approved", "").strip() == "1"
            )
            if not (ALLOW_UNAUTH_TOOLS and has_router_approval and is_loopback_host(peer_host)):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Execution of mutating tool '{req.tool}' requires signed router auth "
                        "(configure SPLITCH_ROUTER_PUBLIC_KEY via `splitch secret init-agent-keys` "
                        "or SPLITCH_AGENT_TOKEN; local dev override: SPLITCH_ALLOW_UNAUTH_TOOLS=1)."
                    ),
                )
    if req.tool != "run_command":
        return await asyncio.to_thread(_exec_fs_tool, req)

    cmd = str(req.arguments.get("command", "")).strip()
    if not cmd:
        return ToolExecResponse(ok=False, output="Missing 'command' argument", exit_code=1)
    ws_root, cwd = _resolve_workspace_path(req.workspace_subpath, str(req.arguments.get("cwd", ".")))
    if not cwd.exists() or not cwd.is_dir():
        return ToolExecResponse(ok=False, output=f"Working dir not found: {cwd}", exit_code=1)

    return await run_sandboxed_shell_command(
        cmd,
        ws_root,
        cwd,
        timeout_s=_CMD_TIMEOUT_S,
        sandbox_mode=CMD_SANDBOX,
        allow_net=CMD_SANDBOX_ALLOW_NET,
    )


# ── Signed Remote Self-Update & Rollback (Protected by Agent Auth Middleware) ─


def _schedule_delayed_restart(delay_s: float = 1.5) -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return

    async def _do_restart() -> None:
        await asyncio.sleep(delay_s)
        with contextlib.suppress(OSError, RuntimeError):
            await asyncio.to_thread(_restart_active_services)

    asyncio.create_task(_do_restart())


@app.post("/system/update", response_model=UpdateResult)
async def agent_system_update(req: AgentUpdateRequest | None = None) -> UpdateResult:
    body = req or AgentUpdateRequest()
    res = await asyncio.to_thread(
        perform_update,
        agent_only=True,
        restart_services=False,
        with_engine=body.with_engine,
        force=body.force,
    )
    if res.ok and (res.updated or res.ui_rebuilt):
        _schedule_delayed_restart(1.5)
    return res


@app.post("/desktop/sync-theme")
async def agent_sync_desktop_theme(theme: UiThemeConfig) -> dict:
    """
    Signed router -> agent theme push so worker nodes sync desktop/app-menu icons
    even when the system tray is closed or unauthenticated.
    """
    if os.getenv("SPLITCH_NO_DESKTOP", "").strip().lower() in ("1", "true", "yes"):
        return {"ok": True, "synced": False, "reason": "desktop_disabled"}

    def _do_sync() -> dict:
        from backend.desktop.shortcuts import desktop_shortcuts_installed, sync_desktop_icons

        if not desktop_shortcuts_installed():
            return {"ok": True, "synced": False, "reason": "no_shortcuts_installed"}
        res = sync_desktop_icons(theme)
        return {
            "ok": True,
            "synced": True,
            "tag": res["tag"],
            "updated_paths": res["updated_paths"],
        }

    return await asyncio.to_thread(_do_sync)


@app.post("/system/rollback", response_model=UpdateResult)
async def agent_system_rollback(req: AgentRollbackRequest | None = None) -> UpdateResult:
    body = req or AgentRollbackRequest()
    res = await asyncio.to_thread(
        perform_rollback,
        agent_only=True,
        to_sha=body.to_sha,
        restart_services=False,
    )
    if res.ok and res.updated:
        _schedule_delayed_restart(1.5)
    return res
