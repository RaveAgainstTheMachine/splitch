"""
Runtime Settings persistence, Live Client Protocol Probe, and Direct Node Client Config Push.

Powers the Settings Modal (`GET/PATCH /api/settings`, `POST /api/clients/probe`, `POST /api/clients/push`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request

from backend.router.mcp_api import generate_client_config
from backend.router.telemetry import ClusterState
from backend.shared.models import (
    ClientProbeRequest,
    ClientProbeResponse,
    ClientPushRequest,
    McpServerConfig,
    RuntimeSettings,
    RuntimeSettingsPatch,
)


def _validate_optional_http_url(raw: str, field_name: str) -> str:
    cleaned = raw.strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must be a valid http:// or https:// URL"
        )
    return cleaned


def _build_client_payload(
    client: Literal["opencode", "zed", "hermes", "codex", "claude_code", "aider"],
    router_url: str,
    model: str,
    cluster: ClusterState,
    mcp_servers: list[McpServerConfig],
) -> tuple[dict, str | None]:
    base = router_url.rstrip("/")
    base_v1 = f"{base}/v1"
    api_key_placeholder = "splitch-local"

    if client in ("opencode", "zed"):
        cfg = generate_client_config(client, base, cluster, mcp_servers)
        return cfg, None

    if client == "claude_code":
        return (
            {
                "env": {
                    "ANTHROPIC_BASE_URL": base,
                    "ANTHROPIC_AUTH_TOKEN": api_key_placeholder,
                    "ANTHROPIC_MODEL": model,
                }
            },
            None,
        )

    if client == "hermes":
        yaml_text = (
            "model:\n"
            "  provider: custom\n"
            f"  default: {model}\n"
            f"  base_url: {base_v1}\n"
            f"  api_key: {api_key_placeholder}\n"
            "  context_length: 65536\n"
        )
        return {}, yaml_text

    if client == "codex":
        toml_text = (
            f'model = "{model}"\n'
            'model_provider = "splitch"\n\n'
            "[model_providers.splitch]\n"
            'name = "Splitch"\n'
            f'base_url = "{base_v1}"\n'
            'wire_api = "responses"\n'
            'env_key = "SPLITCH_API_KEY"\n'
        )
        return {}, toml_text

    # aider
    weak = "fast" if "fast" in cluster.aliases else model
    aider_yaml = (
        f"openai-api-base: {base_v1}\n"
        f"openai-api-key: {api_key_placeholder}\n"
        f"model: openai/{model}\n"
        f"weak-model: openai/{weak}\n"
    )
    return {}, aider_yaml


def apply_persisted_settings(cluster: ClusterState, data_dir: Path) -> None:
    """Load `data_dir/settings.json` on router startup and overlay onto live `cluster.config`."""
    p = data_dir / "settings.json"
    if not p.exists():
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
    except (OSError, ValueError):
        return

    g = cluster.config.guards
    r = cluster.config.router
    s = cluster.config.security

    if isinstance(raw.get("non_llm_vram_threshold_mb"), (int, float)):
        g.non_llm_vram_threshold_mb = float(raw["non_llm_vram_threshold_mb"])
    if isinstance(raw.get("min_vram_free_mb"), (int, float)):
        g.min_vram_free_mb = float(raw["min_vram_free_mb"])
    if isinstance(raw.get("max_external_gpu_util_percent"), (int, float)):
        g.max_external_gpu_util_percent = float(raw["max_external_gpu_util_percent"])
    if isinstance(raw.get("yield_cooldown_s"), (int, float)):
        g.yield_cooldown_s = float(raw["yield_cooldown_s"])
    if isinstance(raw.get("auto_evict_on_yield"), bool):
        g.auto_evict_on_yield = raw["auto_evict_on_yield"]
    if isinstance(raw.get("poll_interval_s"), (int, float)):
        r.poll_interval_s = max(1.0, float(raw["poll_interval_s"]))
    if isinstance(raw.get("auto_heavy_char_threshold"), int):
        r.auto_heavy_char_threshold = max(500, raw["auto_heavy_char_threshold"])
    if isinstance(raw.get("max_traces"), int):
        r.max_traces = max(10, min(1000, raw["max_traces"]))
        cluster.traces = deque(cluster.traces, maxlen=r.max_traces)
    if isinstance(raw.get("public_url"), str):
        r.public_url = raw["public_url"].strip().rstrip("/")
    if isinstance(raw.get("searxng_url"), str) and raw["searxng_url"].strip():
        os.environ["SPLITCH_SEARXNG_URL"] = raw["searxng_url"].strip().rstrip("/")
    if isinstance(raw.get("hsts_enabled"), bool):
        s.hsts_enabled = raw["hsts_enabled"]
    if isinstance(raw.get("trusted_proxies"), str):
        s.trusted_proxies = raw["trusted_proxies"].strip()
    if isinstance(raw.get("allowed_origins"), list):
        s.allowed_origins = [str(x).strip().rstrip("/") for x in raw["allowed_origins"] if str(x).strip()]
    if isinstance(raw.get("login_max_attempts"), int):
        s.login_max_attempts = max(1, min(100, raw["login_max_attempts"]))
    if isinstance(raw.get("login_window_s"), (int, float)):
        s.login_window_s = max(5.0, min(3600.0, float(raw["login_window_s"])))


def build_settings_router(
    cluster: ClusterState,
    data_dir: Path,
    *,
    api_key_getter,
    auth_enabled_getter=None,
    internal_auth_headers_getter=None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["settings"])
    settings_file = data_dir / "settings.json"
    mcp_file = data_dir / "mcp_servers.json"

    apply_persisted_settings(cluster, data_dir)

    def _current_settings() -> RuntimeSettings:
        api_key = str(api_key_getter() or "")
        auth_on = bool(auth_enabled_getter()) if auth_enabled_getter is not None else bool(api_key)
        tls = cluster.config.tls
        tls_active = bool(
            tls.enabled
            or tls.self_signed
            or tls.certfile
            or tls.letsencrypt_domain
            or os.getenv("SPLITCH_TLS_CERT", "").strip()
            or os.getenv("SPLITCH_LETSENCRYPT_DOMAIN", "").strip()
            or os.getenv("SPLITCH_TLS_SELF_SIGNED", "").strip().lower() in ("1", "true", "yes")
        )
        remote_prov = auth_on or (
            os.getenv("SPLITCH_ENABLE_REMOTE_PROVISION", "").strip().lower()
            in ("1", "true", "yes")
        )
        return RuntimeSettings(
            non_llm_vram_threshold_mb=cluster.config.guards.non_llm_vram_threshold_mb,
            min_vram_free_mb=cluster.config.guards.min_vram_free_mb,
            max_external_gpu_util_percent=cluster.config.guards.max_external_gpu_util_percent,
            yield_cooldown_s=cluster.config.guards.yield_cooldown_s,
            auto_evict_on_yield=cluster.config.guards.auto_evict_on_yield,
            poll_interval_s=cluster.config.router.poll_interval_s,
            auto_heavy_char_threshold=cluster.config.router.auto_heavy_char_threshold,
            max_traces=cluster.config.router.max_traces,
            public_url=cluster.config.router.public_url,
            searxng_url=os.getenv("SPLITCH_SEARXNG_URL", "").strip(),
            brave_api_key_set=bool(
                os.getenv("BRAVE_API_KEY", "").strip() or cluster.vault.has("brave_api_key")
            ),
            tavily_api_key_set=bool(
                os.getenv("TAVILY_API_KEY", "").strip() or cluster.vault.has("tavily_api_key")
            ),
            auth_enabled=auth_on,
            agent_token_enabled=bool(cluster.agent_token or cluster.router_private_key),
            tls_enabled=tls_active,
            hsts_enabled=cluster.config.security.hsts_enabled,
            trusted_proxies=cluster.config.security.trusted_proxies,
            allowed_origins=list(cluster.config.security.allowed_origins),
            login_max_attempts=cluster.config.security.login_max_attempts,
            login_window_s=cluster.config.security.login_window_s,
            remote_provision_enabled=remote_prov,
        )

    def _write_settings(payload: dict) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                settings_file.chmod(0o600)

    def _load_mcps() -> list[McpServerConfig]:
        if not mcp_file.exists():
            return []
        try:
            raw = json.loads(mcp_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [McpServerConfig.model_validate(item) for item in raw if isinstance(item, dict)]
        except (OSError, ValueError):
            pass
        return []

    @router.get("/settings", response_model=RuntimeSettings)
    async def get_settings() -> RuntimeSettings:
        return _current_settings()

    @router.patch("/settings", response_model=RuntimeSettings)
    async def patch_settings(patch: RuntimeSettingsPatch) -> RuntimeSettings:
        g = cluster.config.guards
        r = cluster.config.router
        s = cluster.config.security

        if patch.non_llm_vram_threshold_mb is not None:
            g.non_llm_vram_threshold_mb = patch.non_llm_vram_threshold_mb
        if patch.min_vram_free_mb is not None:
            g.min_vram_free_mb = patch.min_vram_free_mb
        if patch.max_external_gpu_util_percent is not None:
            g.max_external_gpu_util_percent = patch.max_external_gpu_util_percent
        if patch.yield_cooldown_s is not None:
            g.yield_cooldown_s = patch.yield_cooldown_s
        if patch.auto_evict_on_yield is not None:
            g.auto_evict_on_yield = patch.auto_evict_on_yield
        if patch.poll_interval_s is not None:
            r.poll_interval_s = patch.poll_interval_s
        if patch.auto_heavy_char_threshold is not None:
            r.auto_heavy_char_threshold = patch.auto_heavy_char_threshold
        if patch.max_traces is not None:
            r.max_traces = patch.max_traces
            cluster.traces = deque(cluster.traces, maxlen=r.max_traces)
        if patch.public_url is not None:
            r.public_url = _validate_optional_http_url(patch.public_url, "public_url")
        if patch.searxng_url is not None:
            clean_sx = _validate_optional_http_url(patch.searxng_url, "searxng_url")
            if clean_sx:
                os.environ["SPLITCH_SEARXNG_URL"] = clean_sx
            else:
                os.environ.pop("SPLITCH_SEARXNG_URL", None)
        if patch.hsts_enabled is not None:
            s.hsts_enabled = patch.hsts_enabled
        if patch.trusted_proxies is not None:
            s.trusted_proxies = patch.trusted_proxies.strip()
        if patch.allowed_origins is not None:
            s.allowed_origins = [
                _validate_optional_http_url(o, "allowed_origins")
                for o in patch.allowed_origins
                if o.strip()
            ]
        if patch.login_max_attempts is not None:
            s.login_max_attempts = patch.login_max_attempts
        if patch.login_window_s is not None:
            s.login_window_s = patch.login_window_s

        current = _current_settings()
        persist_dict = {
            "non_llm_vram_threshold_mb": current.non_llm_vram_threshold_mb,
            "min_vram_free_mb": current.min_vram_free_mb,
            "max_external_gpu_util_percent": current.max_external_gpu_util_percent,
            "yield_cooldown_s": current.yield_cooldown_s,
            "auto_evict_on_yield": current.auto_evict_on_yield,
            "poll_interval_s": current.poll_interval_s,
            "auto_heavy_char_threshold": current.auto_heavy_char_threshold,
            "max_traces": current.max_traces,
            "public_url": current.public_url,
            "searxng_url": current.searxng_url,
            "hsts_enabled": current.hsts_enabled,
            "trusted_proxies": current.trusted_proxies,
            "allowed_origins": current.allowed_origins,
            "login_max_attempts": current.login_max_attempts,
            "login_window_s": current.login_window_s,
        }
        await asyncio.to_thread(_write_settings, persist_dict)
        await cluster.broadcast_snapshot()
        return current

    @router.post("/clients/probe", response_model=ClientProbeResponse)
    async def probe_client_protocol(
        req: ClientProbeRequest, request: Request
    ) -> ClientProbeResponse:
        """Live end-to-end verification of OpenAI Chat, Responses, Anthropic Messages, or /v1/models."""
        t0 = time.perf_counter()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if internal_auth_headers_getter is not None:
            headers.update(internal_auth_headers_getter())
        else:
            api_key = str(api_key_getter() or "")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        endpoint_map = {
            "models": "/v1/models",
            "openai_chat": "/v1/chat/completions",
            "openai_responses": "/v1/responses",
            "anthropic_messages": "/v1/messages",
            "anthropic_count_tokens": "/v1/messages/count_tokens",
        }
        endpoint = endpoint_map[req.protocol]

        try:
            transport = httpx.ASGITransport(app=request.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://splitch.internal", timeout=25.0
            ) as client:
                if req.protocol == "models":
                    resp = await client.get(endpoint, headers=headers)
                elif req.protocol == "openai_chat":
                    resp = await client.post(
                        endpoint,
                        headers=headers,
                        json={
                            "model": req.model,
                            "messages": [{"role": "user", "content": "Reply with OK."}],
                            "max_tokens": 12,
                            "temperature": 0,
                        },
                    )
                elif req.protocol == "openai_responses":
                    resp = await client.post(
                        endpoint,
                        headers=headers,
                        json={
                            "model": req.model,
                            "input": "Reply with OK.",
                            "max_output_tokens": 12,
                        },
                    )
                elif req.protocol == "anthropic_count_tokens":
                    resp = await client.post(
                        endpoint,
                        headers={**headers, "anthropic-version": "2023-06-01"},
                        json={
                            "model": req.model,
                            "system": "Be concise.",
                            "messages": [{"role": "user", "content": "Reply with OK."}],
                        },
                    )
                else:
                    resp = await client.post(
                        endpoint,
                        headers={**headers, "anthropic-version": "2023-06-01"},
                        json={
                            "model": req.model,
                            "messages": [{"role": "user", "content": "Reply with OK."}],
                            "max_tokens": 12,
                        },
                    )
        except httpx.HTTPError as exc:
            return ClientProbeResponse(
                ok=False,
                protocol=req.protocol,
                endpoint=endpoint,
                status_code=502,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 1),
                error=str(exc),
            )

        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if resp.status_code >= 400:
            err_detail = resp.text[:240]
            with contextlib.suppress(ValueError):
                j = resp.json()
                err_detail = str(j.get("detail") or j.get("error") or err_detail)
            return ClientProbeResponse(
                ok=False,
                protocol=req.protocol,
                endpoint=endpoint,
                status_code=resp.status_code,
                latency_ms=latency_ms,
                error=err_detail,
            )

        resolved_model = req.model
        snippet = ""
        try:
            body = resp.json()
            if req.protocol == "models":
                data = body.get("data") or []
                resolved_model = f"{len(data)} models"
                snippet = ", ".join(str(m.get("id", "")) for m in data[:6])
            elif req.protocol == "openai_chat":
                resolved_model = str(body.get("model") or req.model)
                choices = body.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    snippet = str((choices[0].get("message") or {}).get("content") or "")[:120]
            elif req.protocol == "openai_responses":
                resolved_model = str(body.get("model") or req.model)
                snippet = str(body.get("output_text") or "")[:120]
            elif req.protocol == "anthropic_count_tokens":
                resolved_model = req.model
                snippet = f"input_tokens={body.get('input_tokens', 0)}"
            else:
                resolved_model = str(body.get("model") or req.model)
                content = body.get("content") or []
                if content and isinstance(content[0], dict):
                    snippet = str(content[0].get("text") or "")[:120]
        except ValueError:
            snippet = resp.text[:120]

        return ClientProbeResponse(
            ok=True,
            protocol=req.protocol,
            endpoint=endpoint,
            status_code=resp.status_code,
            latency_ms=latency_ms,
            resolved_model=resolved_model,
            snippet=snippet.strip(),
        )

    @router.post("/clients/push")
    async def push_client_config(req: ClientPushRequest, request: Request) -> dict:
        """Push generated CLI/IDE client configuration directly to an online node via its agent."""
        node = cluster.nodes.get(req.node_id)
        if not node or not node.online:
            raise HTTPException(status_code=503, detail=f"Node '{req.node_id}' is offline")

        base_url = (
            req.router_url.strip().rstrip("/")
            or cluster.config.router.public_url.strip().rstrip("/")
            or str(request.base_url).rstrip("/")
        )
        mcps = await asyncio.to_thread(_load_mcps)
        cfg_dict, raw_text = _build_client_payload(req.client, base_url, req.model, cluster, mcps)

        try:
            async with httpx.AsyncClient(
                timeout=10.0, headers=cluster.agent_headers("POST", "/sync/client-config")
            ) as client:
                r = await client.post(
                    f"{node.agent_url}/sync/client-config",
                    json={"client": req.client, "config": cfg_dict, "raw_text": raw_text},
                )
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.text[:300])
                return r.json()
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Agent sync failed: {exc}") from exc

    return router
