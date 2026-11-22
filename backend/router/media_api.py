"""
Unified Image & Video Generation Control Plane (`sd-server`, `ComfyUI`, `openai-images`).

Endpoints:
  - GET    /api/media/backends
  - GET    /api/media/jobs
  - GET    /api/media/jobs/{job_id}
  - POST   /api/media/generate
  - DELETE /api/media/jobs/{job_id}
  - GET    /api/media/files/{filename}
  - POST   /v1/images/generations
  - POST   /v1/videos/generations
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import mimetypes
import random
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.router.policy import check_node_eligibility
from backend.router.telemetry import ClusterState
from backend.shared.models import (
    DownloadJob,
    InstallMediaEngineRequest,
    MediaBackendNode,
    MediaEngineKind,
    MediaGenerateRequest,
    MediaJob,
    NodeMediaEngineStatus,
    NodeStatus,
    OpenAIImageRequest,
    OpenAIVideoRequest,
    PullModelRequest,
    StartMediaEngineRequest,
)

SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_JOBS_KEPT = 120


def _resolve_node_media_url(node: NodeStatus) -> str:
    """Resolve router-reachable URL for a node's media engine (via agent proxy for local engines)."""
    explicit = (node.media_url or "").strip().rstrip("/")
    if explicit:
        parsed_exp = urlparse(explicit)
        if parsed_exp.hostname not in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
            return explicit
    reported = (node.telemetry.media_url if node.telemetry else "") or explicit
    reported = reported.strip().rstrip("/")
    if not reported:
        return ""
    parsed_rep = urlparse(reported)
    if parsed_rep.hostname in ("127.0.0.1", "localhost", "0.0.0.0", "::1") and node.agent_url:
        return f"{node.agent_url.rstrip('/')}/media/proxy"
    return reported


def _parse_size(raw: str, default_w: int = 1024, default_h: int = 1024) -> tuple[int, int]:
    m = re.match(r"^\s*(\d{3,4})\s*[xX×]\s*(\d{3,4})\s*$", raw or "")
    if not m:
        return default_w, default_h
    w = max(128, min(2048, int(m.group(1))))
    h = max(128, min(2048, int(m.group(2))))
    return w, h


def _decode_data_url_or_b64(raw: str) -> bytes:
    payload = raw.strip()
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    return base64.b64decode(payload)


def _guess_ext_and_mime(raw: bytes, fallback_kind: str = "image") -> tuple[str, str]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if raw[:4] == b"GIF8":
        return ".gif", "image/gif"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        return ".mp4", "video/mp4"
    if raw.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm", "video/webm"
    if fallback_kind == "video":
        return ".mp4", "video/mp4"
    return ".png", "image/png"


def _build_default_comfy_image_graph(
    *,
    prompt: str,
    negative_prompt: str,
    model: str,
    width: int,
    height: int,
    steps: int,
    cfg_scale: float,
    seed: int,
    uploaded_image: str | None = None,
    strength: float = 0.7,
) -> dict[str, Any]:
    graph: dict[str, Any] = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["1", 1]},
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "splitch", "images": ["6", 0]}},
    }
    if uploaded_image:
        graph["4a"] = {"class_type": "LoadImage", "inputs": {"image": uploaded_image}}
        graph["4"] = {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["4a", 0], "vae": ["1", 2]},
        }
        denoise = max(0.05, min(1.0, strength))
    else:
        graph["4"] = {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        }
        denoise = 1.0

    graph["5"] = {
        "class_type": "KSampler",
        "inputs": {
            "seed": seed,
            "steps": steps,
            "cfg": cfg_scale,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": denoise,
            "model": ["1", 0],
            "positive": ["2", 0],
            "negative": ["3", 0],
            "latent_image": ["4", 0],
        },
    }
    return graph


def _build_default_comfy_video_graph(
    *,
    prompt: str,
    negative_prompt: str,
    model: str,
    width: int,
    height: int,
    steps: int,
    cfg_scale: float,
    seed: int,
    frames: int,
    fps: int,
) -> dict[str, Any]:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": frames},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg_scale,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {
            "class_type": "SaveAnimatedWEBP",
            "inputs": {
                "filename_prefix": "splitch_vid",
                "fps": float(fps),
                "lossless": False,
                "quality": 85,
                "method": "default",
                "images": ["6", 0],
            },
        },
    }


def _apply_workflow_placeholders(
    raw_json: str,
    *,
    prompt: str,
    negative_prompt: str,
    model: str,
    width: int,
    height: int,
    steps: int,
    cfg_scale: float,
    seed: int,
    frames: int,
    fps: int,
    uploaded_image: str | None,
) -> dict[str, Any]:
    parsed = json.loads(raw_json)
    if not isinstance(parsed, dict):
        raise TypeError("Custom ComfyUI workflow JSON must be a JSON object mapping node IDs to nodes")

    def _walk(val: Any) -> Any:
        if isinstance(val, dict):
            return {k: _walk(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_walk(v) for v in val]
        if isinstance(val, str):
            exact_map: dict[str, Any] = {
                "{{WIDTH}}": width,
                "{{HEIGHT}}": height,
                "{{STEPS}}": steps,
                "{{CFG}}": cfg_scale,
                "{{SEED}}": seed,
                "{{FRAMES}}": frames,
                "{{FPS}}": fps,
            }
            if val in exact_map:
                return exact_map[val]
            return (
                val.replace("{{PROMPT}}", prompt)
                .replace("{{NEGATIVE_PROMPT}}", negative_prompt)
                .replace("{{MODEL}}", model)
                .replace("{{IMAGE}}", uploaded_image or "")
                .replace("{{WIDTH}}", str(width))
                .replace("{{HEIGHT}}", str(height))
                .replace("{{STEPS}}", str(steps))
                .replace("{{CFG}}", str(cfg_scale))
                .replace("{{SEED}}", str(seed))
                .replace("{{FRAMES}}", str(frames))
                .replace("{{FPS}}", str(fps))
            )
        return val

    return _walk(parsed)


class MediaService:
    def __init__(self, cluster: ClusterState, data_dir: Path) -> None:
        self.cluster = cluster
        self.media_dir = data_dir / "media"
        self.jobs_path = self.media_dir / "jobs.json"
        self.jobs: dict[str, MediaJob] = {}
        self._tasks: set[asyncio.Task] = set()
        self._load_jobs()

    def _load_jobs(self) -> None:
        if not self.jobs_path.exists():
            return
        try:
            raw = json.loads(self.jobs_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    job = MediaJob.model_validate(item)
                    if job.status in ("queued", "evicting_vram", "running"):
                        job.status = "error"
                        job.error = "Interrupted by router restart"
                    self.jobs[job.id] = job
        except (OSError, ValueError):
            return

    def _save_jobs_sync(self) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        ordered = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)[:MAX_JOBS_KEPT]
        self.jobs = {j.id: j for j in ordered}
        self.jobs_path.write_text(
            json.dumps([j.model_dump(mode="json") for j in ordered], indent=2),
            encoding="utf-8",
        )
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                self.jobs_path.chmod(0o600)

    async def _save_jobs(self) -> None:
        await asyncio.to_thread(self._save_jobs_sync)

    def list_backends(self) -> list[MediaBackendNode]:
        out: list[MediaBackendNode] = []
        for n in self.cluster.nodes.values():
            t = n.telemetry
            m_url = _resolve_node_media_url(n)
            m_engine: MediaEngineKind = t.media_engine if t else "none"
            m_healthy = bool(t.media_healthy) if t else False
            if not m_healthy and n.media_url and m_engine == "none":
                m_engine = "comfyui" if ":8188" in n.media_url else "sd-server"
            out.append(
                MediaBackendNode(
                    node_id=n.node_id,
                    display_name=n.display_name,
                    role=n.role,
                    online=n.online,
                    media_engine=m_engine,
                    media_healthy=m_healthy,
                    media_url=m_url,
                    media_models=list(t.media_models) if t else [],
                    vram_free_mb=t.vram_free_mb if t else 0.0,
                    vram_total_mb=t.vram_total_mb if t else 0.0,
                    busy_reason=n.busy_reason,
                )
            )
        return out

    def has_healthy_backend(self) -> bool:
        return any(b.online and b.media_healthy and b.media_url for b in self.list_backends())

    def resolve_media_node(self, preferred_node_id: str = "auto") -> tuple[NodeStatus, MediaEngineKind, str]:
        candidates: list[NodeStatus] = []
        if preferred_node_id and preferred_node_id != "auto":
            n = self.cluster.nodes.get(preferred_node_id)
            if not n:
                raise HTTPException(status_code=404, detail=f"Node '{preferred_node_id}' not found")
            candidates = [n]
        else:
            # Prefer GPU nodes first, then unified, then CPU
            role_rank = {"gpu": 0, "unified": 1, "cpu": 2}
            candidates = sorted(
                self.cluster.nodes.values(),
                key=lambda x: (
                    role_rank.get(x.role, 3),
                    -(x.telemetry.vram_total_mb if x.telemetry else 0.0),
                ),
            )

        skip_reasons: list[str] = []
        for n in candidates:
            if not n.online:
                skip_reasons.append(f"{n.node_id}: offline")
                continue
            m_url = _resolve_node_media_url(n)
            t = n.telemetry
            if not m_url or not t or not t.media_healthy:
                skip_reasons.append(f"{n.node_id}: no healthy media engine (sd-server / ComfyUI)")
                continue
            # Respect gaming/GPU-yield guards (ignore engine_down since LLM engine is separate from media engine)
            reason = check_node_eligibility(n, "", self.cluster.control, self.cluster.config.guards)
            if reason and not reason.endswith("_engine_down") and not reason.endswith("_vram_low"):
                skip_reasons.append(f"{n.node_id}: {reason}")
                continue
            return n, t.media_engine, m_url

        raise HTTPException(
            status_code=503,
            detail=(
                "No healthy image/video engine available. Start `sd-server` (:8082) via "
                "`splitch engine install sd.cpp` or start ComfyUI (:8188) on a node. "
                f"Reasons: {', '.join(skip_reasons) or 'none'}"
            ),
        )

    async def _evict_llm_vram_before_media(self, node: NodeStatus, job: MediaJob) -> None:
        t = node.telemetry
        if not t or node.role != "gpu":
            return
        loaded = [m for m in t.models if m.status == "loaded"]
        if not loaded:
            return
        job.status = "evicting_vram"
        job.stage = f"Unloading {len(loaded)} LLM model(s) on {node.display_name} to free GPU VRAM"
        job.progress = 8
        for m in loaded:
            with contextlib.suppress(Exception):
                await self.cluster.proxy_model_action(node.node_id, "unload", m.id)
        await asyncio.sleep(0.4)

    def _media_client_kwargs(self, base_url: str) -> dict[str, Any]:
        is_agent_proxy = base_url.rstrip("/").endswith("/media/proxy")
        if not is_agent_proxy:
            return {}
        ca = self.cluster.config.tls.ca_bundle.strip()
        return {"headers": self.cluster.agent_headers(), "verify": ca if ca else True}

    async def _run_sd_server(
        self,
        base_url: str,
        req: MediaGenerateRequest,
        seed: int,
        job: MediaJob,
    ) -> tuple[bytes, str, str]:
        job.stage = f"Generating {req.kind} via sd-server ({req.width}x{req.height}, {req.steps} steps)"
        job.progress = 25
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=10.0),
            **self._media_client_kwargs(base_url),
        ) as client:
            if req.kind == "video":
                payload: dict[str, Any] = {
                    "prompt": req.prompt,
                    "negative_prompt": req.negative_prompt,
                    "size": f"{req.width}x{req.height}",
                    "frames": req.frames,
                    "fps": req.fps,
                    "steps": req.steps,
                    "cfg_scale": req.cfg_scale,
                    "seed": seed,
                    "response_format": "b64_json",
                }
                if req.model:
                    payload["model"] = req.model
                if req.init_image:
                    payload["image"] = req.init_image
                resp = await client.post(f"{base_url}/v1/videos/generations", json=payload)
                if resp.status_code == 404:
                    raise RuntimeError(
                        "Connected sd-server does not expose /v1/videos/generations. "
                        "Use a ComfyUI backend or a video-enabled sd-server build (Wan 2.1/2.2)."
                    )
                resp.raise_for_status()
                body = resp.json()
            elif req.init_image:
                img_bytes = _decode_data_url_or_b64(req.init_image)
                data_fields: dict[str, str] = {
                    "prompt": req.prompt,
                    "size": f"{req.width}x{req.height}",
                    "n": "1",
                    "response_format": "b64_json",
                }
                if req.model:
                    data_fields["model"] = req.model
                resp = await client.post(
                    f"{base_url}/v1/images/edits",
                    data=data_fields,
                    files={"image": ("init.png", img_bytes, "image/png")},
                )
                resp.raise_for_status()
                body = resp.json()
            else:
                payload = {
                    "prompt": req.prompt,
                    "negative_prompt": req.negative_prompt,
                    "n": 1,
                    "size": f"{req.width}x{req.height}",
                    "response_format": "b64_json",
                    "steps": req.steps,
                    "cfg_scale": req.cfg_scale,
                    "seed": seed,
                }
                if req.model:
                    payload["model"] = req.model
                resp = await client.post(f"{base_url}/v1/images/generations", json=payload)
                resp.raise_for_status()
                body = resp.json()

            items = body.get("data") if isinstance(body, dict) else None
            if not isinstance(items, list) or not items:
                raise RuntimeError("Media backend returned empty data list")
            first = items[0]
            if isinstance(first, dict) and first.get("b64_json"):
                raw = base64.b64decode(first["b64_json"])
            elif isinstance(first, dict) and first.get("url"):
                dl = await client.get(str(first["url"]))
                dl.raise_for_status()
                raw = dl.content
            else:
                raise RuntimeError("Media backend response missing b64_json or url")
            ext, mime = _guess_ext_and_mime(raw, req.kind)
            return raw, ext, mime

    async def _run_comfyui(
        self,
        base_url: str,
        node: NodeStatus,
        req: MediaGenerateRequest,
        seed: int,
        job: MediaJob,
    ) -> tuple[bytes, str, str]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            **self._media_client_kwargs(base_url),
        ) as client:
            uploaded_name: str | None = None
            if req.init_image:
                job.stage = "Uploading reference image to ComfyUI"
                job.progress = 15
                img_bytes = _decode_data_url_or_b64(req.init_image)
                up = await client.post(
                    f"{base_url}/upload/image",
                    files={"image": (f"splitch_{job.id}.png", img_bytes, "image/png")},
                    data={"overwrite": "true"},
                )
                up.raise_for_status()
                uploaded_name = str(up.json().get("name") or f"splitch_{job.id}.png")

            chosen_model = req.model.strip()
            if not chosen_model and node.telemetry and node.telemetry.media_models:
                chosen_model = node.telemetry.media_models[0]

            if req.workflow_json and req.workflow_json.strip():
                graph = _apply_workflow_placeholders(
                    req.workflow_json,
                    prompt=req.prompt,
                    negative_prompt=req.negative_prompt,
                    model=chosen_model,
                    width=req.width,
                    height=req.height,
                    steps=req.steps,
                    cfg_scale=req.cfg_scale,
                    seed=seed,
                    frames=req.frames,
                    fps=req.fps,
                    uploaded_image=uploaded_name,
                )
            elif req.kind == "video":
                if not chosen_model:
                    raise RuntimeError(
                        "Select a checkpoint or supply a custom ComfyUI workflow JSON for video generation."
                    )
                graph = _build_default_comfy_video_graph(
                    prompt=req.prompt,
                    negative_prompt=req.negative_prompt,
                    model=chosen_model,
                    width=req.width,
                    height=req.height,
                    steps=req.steps,
                    cfg_scale=req.cfg_scale,
                    seed=seed,
                    frames=req.frames,
                    fps=req.fps,
                )
            else:
                if not chosen_model:
                    raise RuntimeError(
                        "No checkpoint detected in ComfyUI. Install a checkpoint or pass a model name."
                    )
                graph = _build_default_comfy_image_graph(
                    prompt=req.prompt,
                    negative_prompt=req.negative_prompt,
                    model=chosen_model,
                    width=req.width,
                    height=req.height,
                    steps=req.steps,
                    cfg_scale=req.cfg_scale,
                    seed=seed,
                    uploaded_image=uploaded_name,
                    strength=req.strength,
                )

            job.stage = "Queued workflow in ComfyUI"
            job.progress = 20
            q_resp = await client.post(
                f"{base_url}/prompt",
                json={"prompt": graph, "client_id": f"splitch-{job.id}"},
            )
            if q_resp.status_code >= 400:
                raise RuntimeError(f"ComfyUI rejected workflow: {q_resp.text[:400]}")
            prompt_id = str(q_resp.json().get("prompt_id") or "")
            if not prompt_id:
                raise RuntimeError("ComfyUI did not return a prompt_id")

            # Poll ComfyUI history until complete (max 10 min)
            deadline = time.monotonic() + 600.0
            output_entry: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                await asyncio.sleep(0.8)
                h_resp = await client.get(f"{base_url}/history/{prompt_id}")
                if h_resp.status_code != 200:
                    continue
                hist = h_resp.json()
                record = hist.get(prompt_id) if isinstance(hist, dict) else None
                if not isinstance(record, dict):
                    job.progress = min(85, job.progress + 3)
                    job.stage = "Rendering in ComfyUI..."
                    continue

                status_obj = record.get("status") or {}
                if status_obj.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI execution error: {status_obj}")

                outputs = record.get("outputs") or {}
                for node_out in outputs.values():
                    if not isinstance(node_out, dict):
                        continue
                    for key in ("gifs", "videos", "images"):
                        arr = node_out.get(key)
                        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                            output_entry = arr[0]
                            break
                    if output_entry:
                        break
                if output_entry:
                    break

            if not output_entry or not output_entry.get("filename"):
                raise RuntimeError("ComfyUI workflow completed without producing an image or video output")

            job.stage = "Downloading generated output from ComfyUI"
            job.progress = 92
            v_resp = await client.get(
                f"{base_url}/view",
                params={
                    "filename": str(output_entry["filename"]),
                    "subfolder": str(output_entry.get("subfolder") or ""),
                    "type": str(output_entry.get("type") or "output"),
                },
            )
            v_resp.raise_for_status()
            raw = v_resp.content
            ext, mime = _guess_ext_and_mime(raw, req.kind)
            return raw, ext, mime

    async def _execute_job(
        self,
        job: MediaJob,
        req: MediaGenerateRequest,
        node: NodeStatus,
        engine: MediaEngineKind,
        base_url: str,
    ) -> MediaJob:
        started = time.perf_counter()
        seed = req.seed if req.seed is not None else random.randint(1, 2**31 - 1)
        job.seed = seed
        node.in_flight += 1
        try:
            if req.evict_llm_vram:
                await self._evict_llm_vram_before_media(node, job)

            job.status = "running"
            if engine == "comfyui":
                raw, ext, mime = await self._run_comfyui(base_url, node, req, seed, job)
            else:
                raw, ext, mime = await self._run_sd_server(base_url, req, seed, job)

            self.media_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{job.id}{ext}"
            out_path = self.media_dir / filename
            await asyncio.to_thread(out_path.write_bytes, raw)
            if sys.platform != "win32":
                with contextlib.suppress(OSError):
                    out_path.chmod(0o600)

            job.filename = filename
            job.file_url = f"/api/media/files/{filename}"
            job.mime_type = mime
            job.status = "completed"
            job.progress = 100
            job.stage = "Done"
            job.completed_at = time.time()
            job.duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError, TypeError) as exc:
            job.status = "error"
            job.error = str(exc)
            job.stage = "Failed"
            job.completed_at = time.time()
            job.duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        finally:
            node.in_flight = max(0, node.in_flight - 1)
            await self._save_jobs()
        return job

    async def start_job(self, req: MediaGenerateRequest) -> MediaJob:
        node, engine, base_url = self.resolve_media_node(req.node_id)
        job_id = f"med_{uuid.uuid4().hex[:10]}"
        chosen_model = req.model.strip() or (
            node.telemetry.media_models[0]
            if node.telemetry and node.telemetry.media_models
            else ""
        )
        job = MediaJob(
            id=job_id,
            kind=req.kind,
            status="queued",
            progress=2,
            stage=f"Scheduled on {node.display_name} ({engine})",
            prompt=req.prompt,
            negative_prompt=req.negative_prompt,
            node_id=node.node_id,
            media_engine=engine,
            model=chosen_model,
            width=req.width,
            height=req.height,
            steps=req.steps,
            cfg_scale=req.cfg_scale,
            seed=req.seed,
            frames=req.frames,
            fps=req.fps,
            created_at=time.time(),
        )
        self.jobs[job.id] = job
        await self._save_jobs()
        task = asyncio.create_task(self._execute_job(job, req, node, engine, base_url))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def generate_sync(self, req: MediaGenerateRequest) -> MediaJob:
        node, engine, base_url = self.resolve_media_node(req.node_id)
        job_id = f"med_{uuid.uuid4().hex[:10]}"
        chosen_model = req.model.strip() or (
            node.telemetry.media_models[0]
            if node.telemetry and node.telemetry.media_models
            else ""
        )
        job = MediaJob(
            id=job_id,
            kind=req.kind,
            status="queued",
            progress=2,
            stage=f"Scheduled on {node.display_name} ({engine})",
            prompt=req.prompt,
            negative_prompt=req.negative_prompt,
            node_id=node.node_id,
            media_engine=engine,
            model=chosen_model,
            width=req.width,
            height=req.height,
            steps=req.steps,
            cfg_scale=req.cfg_scale,
            seed=req.seed,
            frames=req.frames,
            fps=req.fps,
            created_at=time.time(),
        )
        self.jobs[job.id] = job
        await self._save_jobs()
        return await self._execute_job(job, req, node, engine, base_url)

    def resolve_file_path(self, filename: str) -> Path:
        if not SAFE_FILENAME_RE.fullmatch(filename):
            raise HTTPException(status_code=400, detail="Invalid media filename")
        root = self.media_dir.resolve()
        target = (root / filename).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=404, detail="Media file not found")
        return target

    async def delete_job(self, job_id: str) -> bool:
        job = self.jobs.pop(job_id, None)
        if not job:
            return False
        if job.filename and SAFE_FILENAME_RE.fullmatch(job.filename):
            p = (self.media_dir / job.filename).resolve()
            if p.is_relative_to(self.media_dir.resolve()) and p.is_file():
                with contextlib.suppress(OSError):
                    p.unlink()
        await self._save_jobs()
        return True


def build_media_router(cluster: ClusterState, data_dir: Path) -> tuple[APIRouter, MediaService]:
    service = MediaService(cluster, data_dir)
    router = APIRouter(tags=["media"])

    @router.get("/api/media/backends", response_model=list[MediaBackendNode])
    async def list_media_backends() -> list[MediaBackendNode]:
        return service.list_backends()

    def _require_online_node(node_id: str) -> NodeStatus:
        node = cluster.nodes.get(node_id)
        if not node or not node.online:
            raise HTTPException(status_code=503, detail=f"Node '{node_id}' is offline")
        return node

    @router.get("/api/media/nodes/{node_id}/engine", response_model=NodeMediaEngineStatus)
    async def get_node_media_engine(node_id: str) -> NodeMediaEngineStatus:
        node = _require_online_node(node_id)
        try:
            async with httpx.AsyncClient(
                timeout=8.0, headers=cluster.agent_headers("GET", "/media/engine/status")
            ) as client:
                r = await client.get(f"{node.agent_url}/media/engine/status")
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.text[:300])
                return NodeMediaEngineStatus.model_validate(r.json())
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/api/media/nodes/{node_id}/install", response_model=NodeMediaEngineStatus)
    async def install_node_media_engine(
        node_id: str, req: InstallMediaEngineRequest
    ) -> NodeMediaEngineStatus:
        node = _require_online_node(node_id)
        try:
            async with httpx.AsyncClient(
                timeout=20.0, headers=cluster.agent_headers("POST", "/media/engine/install")
            ) as client:
                r = await client.post(
                    f"{node.agent_url}/media/engine/install", json=req.model_dump()
                )
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.text[:300])
                return NodeMediaEngineStatus.model_validate(r.json())
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/api/media/nodes/{node_id}/start", response_model=NodeMediaEngineStatus)
    async def start_node_media_engine(
        node_id: str, req: StartMediaEngineRequest
    ) -> NodeMediaEngineStatus:
        node = _require_online_node(node_id)
        try:
            async with httpx.AsyncClient(
                timeout=30.0, headers=cluster.agent_headers("POST", "/media/engine/start")
            ) as client:
                r = await client.post(
                    f"{node.agent_url}/media/engine/start", json=req.model_dump()
                )
                if r.status_code >= 400:
                    detail = r.text[:400]
                    with contextlib.suppress(ValueError):
                        detail = str(r.json().get("detail", detail))
                    raise HTTPException(status_code=r.status_code, detail=detail)
                await cluster.poll_all_nodes()
                return NodeMediaEngineStatus.model_validate(r.json())
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/api/media/nodes/{node_id}/stop", response_model=NodeMediaEngineStatus)
    async def stop_node_media_engine(node_id: str) -> NodeMediaEngineStatus:
        node = _require_online_node(node_id)
        try:
            async with httpx.AsyncClient(
                timeout=15.0, headers=cluster.agent_headers("POST", "/media/engine/stop")
            ) as client:
                r = await client.post(f"{node.agent_url}/media/engine/stop")
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.text[:300])
                await cluster.poll_all_nodes()
                return NodeMediaEngineStatus.model_validate(r.json())
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/api/media/nodes/{node_id}/pull", response_model=DownloadJob)
    async def pull_node_media_model(node_id: str, req: PullModelRequest) -> DownloadJob:
        node = _require_online_node(node_id)
        payload = req.model_dump()
        payload["node_id"] = node_id
        try:
            async with httpx.AsyncClient(
                timeout=15.0, headers=cluster.agent_headers("POST", "/files/models/pull")
            ) as client:
                r = await client.post(f"{node.agent_url}/files/models/pull", json=payload)
                if r.status_code >= 400:
                    detail = r.text[:400]
                    with contextlib.suppress(ValueError):
                        detail = str(r.json().get("detail", detail))
                    raise HTTPException(status_code=r.status_code, detail=detail)
                return DownloadJob.model_validate(r.json())
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/api/media/jobs", response_model=list[MediaJob])
    async def list_media_jobs() -> list[MediaJob]:
        return sorted(service.jobs.values(), key=lambda j: j.created_at, reverse=True)

    @router.get("/api/media/jobs/{job_id}", response_model=MediaJob)
    async def get_media_job(job_id: str) -> MediaJob:
        job = service.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Media job not found")
        return job

    @router.post("/api/media/generate", response_model=MediaJob)
    async def create_media_job(req: MediaGenerateRequest) -> MediaJob:
        return await service.start_job(req)

    @router.delete("/api/media/jobs/{job_id}")
    async def delete_media_job(job_id: str) -> dict:
        if not await service.delete_job(job_id):
            raise HTTPException(status_code=404, detail="Media job not found")
        return {"ok": True, "deleted": job_id}

    @router.get("/api/media/files/{filename}")
    async def get_media_file(filename: str):
        p = service.resolve_file_path(filename)
        mime, _ = mimetypes.guess_type(str(p))
        return FileResponse(str(p), media_type=mime or "application/octet-stream")

    @router.post("/v1/images/generations")
    async def openai_images_generations(req: OpenAIImageRequest) -> dict:
        w, h = _parse_size(req.size, 1024, 1024)
        model_name = "" if req.model in ("auto", "image", "") else req.model
        results: list[dict[str, Any]] = []
        for idx in range(req.n):
            job_req = MediaGenerateRequest(
                kind="image",
                prompt=req.prompt,
                negative_prompt=req.negative_prompt,
                node_id=req.node_id,
                model=model_name,
                width=w,
                height=h,
                steps=req.steps,
                cfg_scale=req.cfg_scale,
                seed=(req.seed + idx) if req.seed is not None else None,
                evict_llm_vram=req.evict_llm_vram,
            )
            job = await service.generate_sync(job_req)
            if job.status != "completed" or not job.filename:
                raise HTTPException(status_code=502, detail=job.error or "Image generation failed")
            if req.response_format == "url":
                results.append(
                    {
                        "url": job.file_url,
                        "revised_prompt": req.prompt,
                        "seed": job.seed,
                        "node": job.node_id,
                    }
                )
            else:
                raw = await asyncio.to_thread(service.resolve_file_path(job.filename).read_bytes)
                results.append(
                    {
                        "b64_json": base64.b64encode(raw).decode("ascii"),
                        "url": job.file_url,
                        "revised_prompt": req.prompt,
                        "seed": job.seed,
                        "node": job.node_id,
                    }
                )
        return {"created": int(time.time()), "data": results}

    @router.post("/v1/videos/generations")
    async def openai_videos_generations(req: OpenAIVideoRequest) -> dict:
        w, h = _parse_size(req.size, 768, 512)
        model_name = "" if req.model in ("auto", "video", "") else req.model
        job = await service.generate_sync(
            MediaGenerateRequest(
                kind="video",
                prompt=req.prompt,
                negative_prompt=req.negative_prompt,
                node_id=req.node_id,
                model=model_name,
                width=w,
                height=h,
                steps=req.steps,
                cfg_scale=req.cfg_scale,
                seed=req.seed,
                frames=req.frames,
                fps=req.fps,
                init_image=req.init_image,
                evict_llm_vram=req.evict_llm_vram,
                workflow_json=req.workflow_json,
            )
        )
        if job.status != "completed" or not job.filename:
            raise HTTPException(status_code=502, detail=job.error or "Video generation failed")
        return {
            "created": int(time.time()),
            "data": [
                {
                    "url": job.file_url,
                    "mime_type": job.mime_type,
                    "seed": job.seed,
                    "node": job.node_id,
                    "duration_ms": job.duration_ms,
                }
            ],
        }

    return router, service
