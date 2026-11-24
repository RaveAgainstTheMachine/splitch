"""
splitch-router (:8080)
Hardware-aware multi-node OpenAI-compatible gateway, telemetry hub,
model/MCP/skills control plane, Agent Chat backend, and static dashboard host.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.desktop.icons import render_svg_text
from backend.router.anthropic_api import (
    handle_anthropic_count_tokens,
    handle_anthropic_messages,
)
from backend.router.chat_api import build_chat_router
from backend.router.config import default_data_root, load_config
from backend.router.mcp_api import build_mcp_router
from backend.router.media_api import build_media_router
from backend.router.models_api import build_models_router
from backend.router.provision_api import JoinTokenManager, build_provision_router
from backend.router.proxy import build_openai_models_list, forward_inference
from backend.router.settings_api import build_settings_router
from backend.router.skills_api import build_skills_router
from backend.router.telemetry import VERSION, ClusterState
from backend.router.workspace_api import build_workspace_router
from backend.shared.bench import (
    BenchReport,
    BenchRunRequest,
    append_bench_history,
    load_bench_history,
    run_benchmark_suite,
)
from backend.shared.engines import EngineStatus, get_engine_status
from backend.shared.models import (
    AuthLoginRequest,
    AuthStatusResponse,
    ClusterRollbackRequest,
    ClusterUpdateJob,
    ClusterUpdateRequest,
    ControlState,
    ControlUpdate,
    DashboardSnapshot,
    ModelActionRequest,
    NodeRegisterRequest,
    NodeStatus,
    NodeUpdateRunResult,
    UiThemeConfig,
    UiThemeUpdate,
    UpdateSummary,
    WorkspaceDirListing,
)
from backend.shared.secrets import (
    load_or_create_session_secret,
    resolve_secret_value,
    verify_secret_hash,
)
from backend.shared.security import is_loopback_host, is_trusted_proxy
from backend.shared.updater import (
    DoctorReport,
    UpdateStatus,
    _restart_active_services,
    check_update,
    perform_rollback,
    perform_update,
    run_doctor,
)

_ROOT = default_data_root()
DATA_DIR = Path(os.getenv("SPLITCH_DATA_DIR", str(_ROOT / "data")))
SKILLS_DIR = Path(os.getenv("SPLITCH_SKILLS_DIR", str(_ROOT / "skills")))
MCP_SCRIPTS_DIR = Path(os.getenv("SPLITCH_MCP_SCRIPTS_DIR", str(_ROOT / "mcp_servers")))


def _safe_resolve_env_secret(name: str) -> str:
    raw = os.getenv(name, "").strip()
    if not raw:
        return ""
    try:
        return resolve_secret_value(raw, check_permissions=True)
    except (OSError, PermissionError, ValueError):
        return ""


API_KEY = _safe_resolve_env_secret("SPLITCH_API_KEY")
API_KEY_HASH = _safe_resolve_env_secret("SPLITCH_API_KEY_HASH")
SESSION_SECRET = load_or_create_session_secret(DATA_DIR)

config = load_config()
cluster = ClusterState(config, data_dir=DATA_DIR)
join_tokens = JoinTokenManager()

NODE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await cluster.start()
    yield
    await cluster.stop()


app = FastAPI(title="Splitch Local AI Router", version=VERSION, lifespan=lifespan)

_EXTRA_ORIGINS = [
    o.strip().rstrip("/")
    for o in [
        *config.security.allowed_origins,
        *os.getenv("SPLITCH_ALLOWED_ORIGINS", "").split(","),
    ]
    if o.strip()
]
HSTS_ENABLED = (
    os.getenv("SPLITCH_HSTS", "1" if config.security.hsts_enabled else "0").strip().lower()
    not in ("0", "false", "no", "off")
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_EXTRA_ORIGINS,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


SESSION_COOKIE_NAME = "splitch_session"
_AUTH_EXEMPT_PATHS = {"/api/auth/status", "/api/auth/login", "/api/auth/logout"}
_LOGIN_FAILURES: dict[str, deque[float]] = {}
_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-src 'self' data: blob:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def _peer_host(request: Request) -> str:
    return request.client.host if request.client else ""


def _is_https_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    if is_trusted_proxy(_peer_host(request), config.security.trusted_proxies):
        fwd = request.headers.get("x-forwarded-proto", "")
        return fwd.split(",")[0].strip().lower() == "https"
    return False


def _client_ip(request: Request) -> str:
    peer = _peer_host(request)
    if is_trusted_proxy(peer, config.security.trusted_proxies):
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            return xff.split(",")[0].strip()
    return peer or "unknown"


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_link_local or ip.is_multicast or ip.is_unspecified


def _validate_http_url(raw: str, field_name: str, *, allow_empty: bool = False) -> str:
    cleaned = raw.strip()
    if not cleaned:
        if allow_empty:
            return ""
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must be a valid http:// or https:// URL"
        )
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must not contain embedded credentials"
        )
    host = parsed.hostname.lower()
    if host in ("metadata.google.internal", "instance-data", "169.254.169.254"):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} points to a forbidden cloud metadata or link-local address",
        )
    try:
        ip = ipaddress.ip_address(host)
        if _is_forbidden_ip(ip):
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} points to a forbidden link-local, multicast, or unspecified IP",
            )
    except ValueError:
        pass
    return cleaned


async def _validate_node_url(raw: str, field_name: str, *, allow_empty: bool = False) -> str:
    cleaned = _validate_http_url(raw, field_name, allow_empty=allow_empty)
    if not cleaned:
        return ""
    host = (urlparse(cleaned).hostname or "").lower()
    try:
        ipaddress.ip_address(host)
        return cleaned
    except ValueError:
        pass
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except OSError:
        return cleaned
    for info in infos:
        addr = info[4][0]
        try:
            resolved_ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            continue
        if _is_forbidden_ip(resolved_ip):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{field_name} hostname '{host}' resolves to a forbidden link-local, "
                    "multicast, or unspecified IP"
                ),
            )
    return cleaned


def _auth_enabled() -> bool:
    return bool(API_KEY or API_KEY_HASH)


def _verify_api_key(candidate: str) -> bool:
    cleaned = candidate.strip()
    if not cleaned:
        return False
    if API_KEY_HASH and verify_secret_hash(cleaned, API_KEY_HASH):
        return True
    return bool(API_KEY and hmac.compare_digest(cleaned, API_KEY))


def _session_cookie_token(secret: str | None = None) -> str:
    signing_key = (secret if secret is not None else SESSION_SECRET) or API_KEY
    return hmac.new(
        signing_key.encode("utf-8"), b"splitch-ui-session-v1", hashlib.sha256
    ).hexdigest()


def _internal_loopback_auth_headers() -> dict[str, str]:
    if not _auth_enabled():
        return {}
    if API_KEY:
        return {"Authorization": f"Bearer {API_KEY}"}
    return {"Cookie": f"{SESSION_COOKIE_NAME}={_session_cookie_token()}"}


def _auth_method(request: Request) -> Literal["open", "header", "cookie", "none"]:
    if not _auth_enabled():
        return "open"
    auth = request.headers.get("authorization", "")
    header_tok = (
        auth[7:].strip()
        if auth.lower().startswith("bearer ")
        else request.headers.get("x-api-key", "").strip()
    )
    if header_tok and _verify_api_key(header_tok):
        return "header"
    cookie_tok = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    if cookie_tok and hmac.compare_digest(cookie_tok, _session_cookie_token()):
        return "cookie"
    return "none"


def _is_request_authenticated(request: Request) -> bool:
    return _auth_method(request) != "none"


def _is_origin_allowed(request: Request, origin: str) -> bool:
    parsed = urlparse(origin.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    norm_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
    req_host = (request.headers.get("host") or request.url.netloc or "").strip().lower()
    if req_host and parsed.netloc.lower() == req_host:
        return True
    if is_loopback_host(parsed.hostname):
        return True
    allowed_set = {
        o.strip().rstrip("/").lower()
        for o in [*config.security.allowed_origins, *_EXTRA_ORIGINS]
        if o.strip()
    }
    return norm_origin in allowed_set


@app.middleware("http")
async def _security_and_auth_middleware(request: Request, call_next):
    path = request.url.path
    auth_kind = _auth_method(request)
    has_join_token = (
        path == "/api/nodes"
        and request.method == "POST"
        and join_tokens.is_valid_unconsumed(request.headers.get("x-splitch-join-token", ""))
    )
    if (
        _auth_enabled()
        and path not in _AUTH_EXEMPT_PATHS
        and (path.startswith(("/v1/", "/api/")) or path == "/infill")
        and auth_kind == "none"
        and not has_join_token
    ):
        response: Response = JSONResponse(
            status_code=401, content={"detail": "Invalid or missing API key"}
        )
    elif (
        auth_kind == "cookie"
        and request.method in ("POST", "PUT", "PATCH", "DELETE")
        and (path.startswith(("/v1/", "/api/")) or path == "/infill")
    ):
        origin = request.headers.get("origin", "").strip()
        sec_fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
        if (origin and not _is_origin_allowed(request, origin)) or (
            not origin and sec_fetch_site == "cross-site"
        ):
            response = JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin cookie-authenticated mutation blocked"},
            )
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    if not path.startswith(("/docs", "/redoc")):
        response.headers.setdefault("Content-Security-Policy", _CSP_HEADER)
    if (HSTS_ENABLED and config.security.hsts_enabled) and _is_https_request(request):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


_media_router, _media_service = build_media_router(cluster, DATA_DIR)
app.include_router(_media_router)
app.include_router(build_chat_router(cluster, DATA_DIR, SKILLS_DIR, media_service=_media_service))
app.include_router(build_models_router(cluster, DATA_DIR))
app.include_router(build_skills_router(cluster, SKILLS_DIR))
app.include_router(build_mcp_router(cluster, DATA_DIR, MCP_SCRIPTS_DIR))
app.include_router(
    build_settings_router(
        cluster,
        DATA_DIR,
        api_key_getter=lambda: API_KEY,
        auth_enabled_getter=_auth_enabled,
        internal_auth_headers_getter=_internal_loopback_auth_headers,
    )
)
app.include_router(
    build_provision_router(
        cluster,
        join_tokens,
        api_key_getter=lambda: ("1" if _auth_enabled() else ""),
    )
)
app.include_router(
    build_workspace_router(
        cluster,
        internal_auth_headers_getter=_internal_loopback_auth_headers,
        app_ref=app,
    )
)


# ── OpenAI-compatible + FIM Inference API ────────────────────────────────────


@app.get("/v1/models")
async def list_models() -> dict:
    return build_openai_models_list(cluster)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await forward_inference(request, cluster, "/v1/chat/completions")


@app.post("/v1/responses")
async def responses_api(request: Request):
    return await forward_inference(request, cluster, "/v1/responses")


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    return await handle_anthropic_messages(request, cluster)


@app.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    return await handle_anthropic_count_tokens(request, cluster)


@app.post("/v1/completions")
async def completions(request: Request):
    return await forward_inference(request, cluster, "/v1/completions")


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    return await forward_inference(request, cluster, "/v1/embeddings")


@app.post("/infill")
async def infill(request: Request):
    return await forward_inference(request, cluster, "/infill")


# ── Splitch Control Plane & Dynamic Cluster Registry ─────────────────────────


@app.get("/health")
async def health() -> dict:
    online = [n.node_id for n in cluster.nodes.values() if n.online]
    return {
        "status": "ok",
        "version": VERSION,
        "online_nodes": online,
        "gaming_mode": cluster.control.gaming_mode,
        "force_node": cluster.control.force_node,
    }


@app.get("/api/auth/status", response_model=AuthStatusResponse)
async def get_auth_status(request: Request) -> AuthStatusResponse:
    return AuthStatusResponse(
        auth_required=_auth_enabled(),
        authenticated=_is_request_authenticated(request),
    )


@app.post("/api/auth/login", response_model=AuthStatusResponse)
async def login_session(
    req: AuthLoginRequest, request: Request, response: Response
) -> AuthStatusResponse:
    if not _auth_enabled():
        return AuthStatusResponse(auth_required=False, authenticated=True)
    ip = _client_ip(request)
    now = time.monotonic()
    window = max(5.0, config.security.login_window_s)
    max_attempts = max(1, config.security.login_max_attempts)
    bucket = _LOGIN_FAILURES.setdefault(ip, deque())
    while bucket and (now - bucket[0]) > window:
        bucket.popleft()
    if len(bucket) >= max_attempts:
        retry_after = max(1, int(window - (now - bucket[0])))
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    candidate = req.api_key.strip()
    if not _verify_api_key(candidate):
        bucket.append(now)
        raise HTTPException(status_code=401, detail="Invalid SPLITCH_API_KEY")

    _LOGIN_FAILURES.pop(ip, None)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=_session_cookie_token(),
        httponly=True,
        secure=_is_https_request(request),
        samesite="strict",
        max_age=86400 * 30,
        path="/",
    )
    return AuthStatusResponse(auth_required=True, authenticated=True)


@app.post("/api/auth/logout", response_model=AuthStatusResponse)
async def logout_session(response: Response) -> AuthStatusResponse:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return AuthStatusResponse(auth_required=_auth_enabled(), authenticated=not _auth_enabled())


@app.get("/api/status", response_model=DashboardSnapshot)
async def get_status() -> DashboardSnapshot:
    return cluster.build_snapshot()


@app.get("/api/system/doctor", response_model=DoctorReport)
async def get_system_doctor(check_updates: bool = False) -> DoctorReport:
    return await asyncio.to_thread(run_doctor, check_engine_updates=check_updates)


@app.get("/api/system/engine", response_model=EngineStatus)
async def get_system_engine(check_remote: bool = False) -> EngineStatus:
    return await asyncio.to_thread(get_engine_status, check_remote=check_remote)


@app.get("/api/system/update-status", response_model=UpdateStatus)
async def get_system_update_status(fetch: bool = False) -> UpdateStatus:
    res = await asyncio.to_thread(check_update, fetch_remote=fetch)
    if fetch and not res.error:
        prev_eng_avail = bool(
            cluster.update_summary and cluster.update_summary.engine_update_available
        )
        prev_llama_tag = (
            cluster.update_summary.latest_llama_tag if cluster.update_summary else ""
        )
        cluster.update_summary = UpdateSummary(
            update_available=res.update_available,
            behind_count=res.behind_count,
            local_sha=res.local_sha,
            remote_sha=res.remote_sha,
            remote_version=res.remote_version,
            checked_at=time.time(),
            engine_update_available=prev_eng_avail,
            latest_llama_tag=prev_llama_tag,
        )
        await cluster.broadcast_snapshot()
    return res


_cluster_update_job: ClusterUpdateJob | None = None
_cluster_update_task: asyncio.Task | None = None


def _assert_ui_update_allowed(request: Request) -> None:
    if _auth_enabled():
        return
    if os.getenv("SPLITCH_ALLOW_UI_UPDATE", "").strip().lower() in ("1", "true", "yes"):
        return
    if is_loopback_host(_client_ip(request)):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "UI-triggered cluster updates are disabled on unauthenticated LAN routers. "
            "Enable SPLITCH_API_KEY, access from localhost, or set SPLITCH_ALLOW_UI_UPDATE=1."
        ),
    )


def _is_colocated_agent(node: NodeStatus) -> bool:
    host = (urlparse(node.agent_url).hostname or "").strip().lower()
    return is_loopback_host(host)


def _schedule_router_restart(delay_s: float = 1.5) -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return

    async def _do_restart() -> None:
        await asyncio.sleep(delay_s)
        with contextlib.suppress(OSError, RuntimeError):
            await asyncio.to_thread(_restart_active_services)

    asyncio.create_task(_do_restart())


async def _call_worker_agent_system(
    client: httpx.AsyncClient,
    node: NodeStatus,
    endpoint: Literal["/system/update", "/system/rollback"],
    payload: dict,
) -> dict:
    resp = await client.post(
        f"{node.agent_url}{endpoint}",
        json=payload,
        headers=cluster.agent_headers("POST", endpoint),
        timeout=httpx.Timeout(300.0, connect=5.0),
    )
    resp.raise_for_status()
    return resp.json()


async def _run_cluster_update_job(job: ClusterUpdateJob, req: ClusterUpdateRequest) -> None:
    online_nodes = [n for n in cluster.nodes.values() if n.online]
    remote_workers = [n for n in online_nodes if not _is_colocated_agent(n)]
    colocated_names = [n.display_name for n in online_nodes if _is_colocated_agent(n)]
    router_label = (
        f"Router (+ {', '.join(colocated_names)})" if colocated_names else "Router"
    )

    worker_entries: dict[str, NodeUpdateRunResult] = {
        n.node_id: NodeUpdateRunResult(node_id=n.node_id, display_name=n.display_name)
        for n in remote_workers
    }
    router_entry = NodeUpdateRunResult(node_id="router", display_name=router_label)
    job.nodes = [*worker_entries.values(), router_entry]

    ca_bundle = os.getenv("SPLITCH_CA_BUNDLE", "").strip() or config.tls.ca_bundle.strip()
    verify: bool | str = ca_bundle if ca_bundle else True

    async with httpx.AsyncClient(verify=verify) as client:

        async def _update_worker(node: NodeStatus) -> None:
            entry = worker_entries[node.node_id]
            entry.status = "updating"
            try:
                data = await _call_worker_agent_system(
                    client,
                    node,
                    "/system/update",
                    {"with_engine": req.with_engine},
                )
                entry.ok = bool(data.get("ok"))
                entry.updated = bool(data.get("updated"))
                entry.rolled_back = bool(data.get("rolled_back"))
                entry.previous_sha = str(data.get("previous_sha") or "")
                entry.current_sha = str(data.get("current_sha") or "")
                entry.steps = [str(s) for s in (data.get("steps") or [])]
                entry.error = data.get("error")
                if entry.ok:
                    entry.status = "restarting" if entry.updated else "done"
                else:
                    entry.status = "rolled_back" if entry.rolled_back else "failed"
            except (httpx.HTTPError, OSError, ValueError) as exc:
                entry.ok = False
                entry.status = "failed"
                entry.error = str(exc)

        async def _rollback_succeeded_workers() -> None:
            to_revert = [
                n
                for n in remote_workers
                if worker_entries[n.node_id].ok and worker_entries[n.node_id].updated
            ]
            if not to_revert:
                return

            async def _revert_one(node: NodeStatus) -> None:
                entry = worker_entries[node.node_id]
                try:
                    data = await _call_worker_agent_system(
                        client,
                        node,
                        "/system/rollback",
                        {"to_sha": entry.previous_sha or None},
                    )
                    if data.get("ok"):
                        entry.rolled_back = True
                        entry.status = "rolled_back"
                        entry.current_sha = str(data.get("current_sha") or entry.previous_sha)
                        entry.steps.extend([str(s) for s in (data.get("steps") or [])])
                        job.rolled_back = True
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    entry.steps.append(f"Sibling rollback failed: {exc}")

            await asyncio.gather(*(_revert_one(n) for n in to_revert))

        if remote_workers:
            await asyncio.gather(*(_update_worker(n) for n in remote_workers))

        failed_workers = [e for e in worker_entries.values() if not e.ok]
        if failed_workers:
            router_entry.status = "failed"
            router_entry.error = "Aborted before router update because a worker node failed"
            if req.rollback_on_failure:
                await _rollback_succeeded_workers()
            job.status = "aborted"
            job.error = "; ".join(
                f"{e.display_name}: {e.error or 'update failed'}" for e in failed_workers
            )
            job.finished_at = time.time()
            return

        router_entry.status = "updating"
        try:
            res = await asyncio.to_thread(
                perform_update,
                restart_services=False,
                with_engine=req.with_engine,
            )
            router_entry.ok = res.ok
            router_entry.updated = res.updated or res.ui_rebuilt
            router_entry.rolled_back = res.rolled_back
            router_entry.previous_sha = res.previous_sha
            router_entry.current_sha = res.current_sha
            router_entry.steps = list(res.steps)
            router_entry.error = res.error
        except (OSError, RuntimeError, ValueError) as exc:
            router_entry.ok = False
            router_entry.error = str(exc)

        if not router_entry.ok:
            router_entry.status = "rolled_back" if router_entry.rolled_back else "failed"
            if req.rollback_on_failure:
                await _rollback_succeeded_workers()
            job.status = "failed"
            job.error = router_entry.error or "Router update failed"
            job.finished_at = time.time()
            return

        if router_entry.updated:
            router_entry.status = "restarting"
            job.status = "restarting"
            job.router_restarting = True
            job.finished_at = time.time()
            _schedule_router_restart(1.5)
        else:
            router_entry.status = "done"
            job.status = "done"
            job.finished_at = time.time()


async def _run_cluster_rollback_job(
    job: ClusterUpdateJob, req: ClusterRollbackRequest
) -> None:
    online_nodes = [n for n in cluster.nodes.values() if n.online]
    remote_workers = [n for n in online_nodes if not _is_colocated_agent(n)]
    colocated_names = [n.display_name for n in online_nodes if _is_colocated_agent(n)]
    router_label = (
        f"Router (+ {', '.join(colocated_names)})" if colocated_names else "Router"
    )

    worker_entries: dict[str, NodeUpdateRunResult] = {
        n.node_id: NodeUpdateRunResult(node_id=n.node_id, display_name=n.display_name)
        for n in remote_workers
    }
    router_entry = NodeUpdateRunResult(node_id="router", display_name=router_label)
    job.nodes = [*worker_entries.values(), router_entry]

    ca_bundle = os.getenv("SPLITCH_CA_BUNDLE", "").strip() or config.tls.ca_bundle.strip()
    verify: bool | str = ca_bundle if ca_bundle else True

    async with httpx.AsyncClient(verify=verify) as client:

        async def _rb_worker(node: NodeStatus) -> None:
            entry = worker_entries[node.node_id]
            entry.status = "updating"
            try:
                data = await _call_worker_agent_system(
                    client,
                    node,
                    "/system/rollback",
                    {"to_sha": req.to_sha},
                )
                entry.ok = bool(data.get("ok"))
                entry.updated = bool(data.get("updated"))
                entry.rolled_back = bool(data.get("rolled_back"))
                entry.previous_sha = str(data.get("previous_sha") or "")
                entry.current_sha = str(data.get("current_sha") or "")
                entry.steps = [str(s) for s in (data.get("steps") or [])]
                entry.error = data.get("error")
                entry.status = "rolled_back" if entry.ok else "failed"
            except (httpx.HTTPError, OSError, ValueError) as exc:
                entry.ok = False
                entry.status = "failed"
                entry.error = str(exc)

        if remote_workers:
            await asyncio.gather(*(_rb_worker(n) for n in remote_workers))

    router_entry.status = "updating"
    try:
        res = await asyncio.to_thread(
            perform_rollback,
            to_sha=req.to_sha,
            restart_services=False,
        )
        router_entry.ok = res.ok
        router_entry.updated = res.updated
        router_entry.rolled_back = res.rolled_back
        router_entry.previous_sha = res.previous_sha
        router_entry.current_sha = res.current_sha
        router_entry.steps = list(res.steps)
        router_entry.error = res.error
        router_entry.status = "restarting" if (res.ok and res.updated) else ("rolled_back" if res.ok else "failed")
    except (OSError, RuntimeError, ValueError) as exc:
        router_entry.ok = False
        router_entry.status = "failed"
        router_entry.error = str(exc)

    any_rolled_back = any(e.rolled_back for e in job.nodes)
    any_failed = any(not e.ok for e in job.nodes)
    job.rolled_back = any_rolled_back
    if any_failed:
        job.error = "; ".join(
            f"{e.display_name}: {e.error or 'rollback failed'}"
            for e in job.nodes
            if not e.ok
        )
    if router_entry.ok and router_entry.updated:
        job.status = "restarting"
        job.router_restarting = True
        job.finished_at = time.time()
        _schedule_router_restart(1.5)
    else:
        job.status = "failed" if any_failed else "done"
        job.finished_at = time.time()


@app.post("/api/system/update-cluster", response_model=ClusterUpdateJob)
async def start_cluster_update(
    request: Request, req: ClusterUpdateRequest | None = None
) -> ClusterUpdateJob:
    global _cluster_update_job, _cluster_update_task
    _assert_ui_update_allowed(request)
    if _cluster_update_job and _cluster_update_job.status == "running":
        raise HTTPException(status_code=409, detail="A cluster update or rollback is already in progress")
    body = req or ClusterUpdateRequest()
    job = ClusterUpdateJob(
        id=uuid.uuid4().hex[:12],
        action="update",
        status="running",
        started_at=time.time(),
        with_engine=body.with_engine,
    )
    _cluster_update_job = job
    _cluster_update_task = asyncio.create_task(_run_cluster_update_job(job, body))
    return job


@app.post("/api/system/rollback-cluster", response_model=ClusterUpdateJob)
async def start_cluster_rollback(
    request: Request, req: ClusterRollbackRequest | None = None
) -> ClusterUpdateJob:
    global _cluster_update_job, _cluster_update_task
    _assert_ui_update_allowed(request)
    if _cluster_update_job and _cluster_update_job.status == "running":
        raise HTTPException(status_code=409, detail="A cluster update or rollback is already in progress")
    body = req or ClusterRollbackRequest()
    job = ClusterUpdateJob(
        id=uuid.uuid4().hex[:12],
        action="rollback",
        status="running",
        started_at=time.time(),
    )
    _cluster_update_job = job
    _cluster_update_task = asyncio.create_task(_run_cluster_rollback_job(job, body))
    return job


@app.get("/api/system/update-cluster/status", response_model=ClusterUpdateJob)
async def get_cluster_update_status() -> ClusterUpdateJob:
    if _cluster_update_job is None:
        return ClusterUpdateJob(id="", status="idle")
    return _cluster_update_job


@app.get("/api/bench/history", response_model=list[BenchReport])
async def get_bench_history() -> list[BenchReport]:
    return await asyncio.to_thread(load_bench_history, DATA_DIR)


@app.post("/api/bench/run", response_model=BenchReport)
async def run_cluster_benchmark(req: BenchRunRequest) -> BenchReport:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://splitch.internal",
        headers=_internal_loopback_auth_headers(),
        timeout=httpx.Timeout(90.0, connect=5.0),
    ) as client:
        report = await run_benchmark_suite(
            req,
            router_url="http://splitch.internal",
            api_key=API_KEY,
            client=client,
        )
    await asyncio.to_thread(append_bench_history, DATA_DIR, report)
    return report


@app.post("/api/control", response_model=ControlState)
async def update_control(update: ControlUpdate) -> ControlState:
    return await cluster.update_control(
        gaming_mode=update.gaming_mode,
        force_node=update.force_node,
    )


@app.get("/api/theme", response_model=UiThemeConfig)
async def get_ui_theme() -> UiThemeConfig:
    return cluster.theme


@app.patch("/api/theme", response_model=UiThemeConfig)
async def patch_ui_theme(update: UiThemeUpdate) -> UiThemeConfig:
    try:
        return await cluster.update_theme(update)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/nodes", response_model=NodeStatus)
async def register_node(req: NodeRegisterRequest, request: Request) -> NodeStatus:
    if _auth_enabled() and not _is_request_authenticated(request):
        jtok = request.headers.get("x-splitch-join-token", "")
        if not join_tokens.validate_and_consume(jtok):
            raise HTTPException(status_code=401, detail="Invalid or expired join token")
    else:
        # Consume optional join token even on open routers so single-use semantics stay consistent
        jtok = request.headers.get("x-splitch-join-token", "")
        if jtok:
            join_tokens.validate_and_consume(jtok)
    slug = req.id.strip().lower()
    if not NODE_SLUG_RE.fullmatch(slug):
        raise HTTPException(
            status_code=400, detail="Node id must be lowercase alphanumeric/hyphen/underscore"
        )
    req.id = slug
    req.agent_url = await _validate_node_url(req.agent_url, "agent_url")
    req.engine_url = await _validate_node_url(req.engine_url, "engine_url")
    req.media_url = await _validate_node_url(req.media_url, "media_url", allow_empty=True)
    return await cluster.register_node(req)


@app.delete("/api/nodes/{node_id}")
async def remove_node(node_id: str) -> dict:
    removed = await cluster.remove_node(node_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Node not found")
    return {"ok": True, "removed": node_id}


@app.post("/api/nodes/{node_id}/models/{action}")
async def control_node_model(
    node_id: str,
    action: Literal["load", "unload"],
    req: ModelActionRequest,
) -> dict:
    code, payload = await cluster.proxy_model_action(node_id, action, req.model)
    if code >= 400:
        raise HTTPException(status_code=code, detail=payload.get("detail", "Action failed"))
    return payload


@app.get("/api/nodes/{node_id}/dirs", response_model=WorkspaceDirListing)
async def browse_node_workspace(node_id: str, subpath: str = "") -> WorkspaceDirListing:
    node = cluster.nodes.get(node_id)
    if not node or not node.online:
        raise HTTPException(status_code=503, detail=f"Node '{node_id}' is offline")
    try:
        async with httpx.AsyncClient(
            timeout=8.0, headers=cluster.agent_headers("GET", "/workspace/dirs")
        ) as client:
            r = await client.get(f"{node.agent_url}/workspace/dirs", params={"subpath": subpath})
            if r.status_code >= 400:
                detail = r.text[:300]
                try:
                    detail = str(r.json().get("detail", detail))
                except ValueError:
                    pass
                raise HTTPException(status_code=r.status_code, detail=detail)
            return WorkspaceDirListing.model_validate(r.json())
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    if _auth_enabled():
        tok = websocket.query_params.get("token", "").strip()
        cookie_tok = websocket.cookies.get(SESSION_COOKIE_NAME, "").strip()
        ok_query = bool(tok and _verify_api_key(tok))
        ok_cookie = bool(
            cookie_tok and hmac.compare_digest(cookie_tok, _session_cookie_token())
        )
        if not (ok_query or ok_cookie):
            await websocket.close(code=1008)
            return
    await websocket.accept()
    cluster.register_ws(websocket)
    try:
        await websocket.send_json(cluster.build_snapshot().model_dump(mode="json"))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        cluster.unregister_ws(websocket)


@app.api_route("/favicon.svg", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_favicon():
    return Response(
        content=render_svg_text(theme=cluster.theme),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"},
    )


# ── Static Dashboard Serving (built Vite bundle) ─────────────────────────────

_DIST_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "dist"
if _DIST_DIR.exists():
    _assets = _DIST_DIR / "assets"
    if _assets.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_dashboard():
        return FileResponse(str(_DIST_DIR / "index.html"))

    _theme_init = _DIST_DIR / "theme-init.js"
    if _theme_init.exists():

        @app.api_route("/theme-init.js", methods=["GET", "HEAD"], include_in_schema=False)
        async def serve_theme_init():
            return FileResponse(str(_theme_init), media_type="application/javascript")
