"""
splitch-router (:8080)
Hardware-aware multi-node OpenAI-compatible gateway, telemetry hub,
model/MCP/skills control plane, Agent Chat backend, and static dashboard host.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import time
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
    ControlState,
    ControlUpdate,
    DashboardSnapshot,
    ModelActionRequest,
    NodeRegisterRequest,
    NodeStatus,
    WorkspaceDirListing,
)
from backend.shared.updater import DoctorReport, UpdateStatus, check_update, run_doctor

_ROOT = default_data_root()
DATA_DIR = Path(os.getenv("SPLITCH_DATA_DIR", str(_ROOT / "data")))
SKILLS_DIR = Path(os.getenv("SPLITCH_SKILLS_DIR", str(_ROOT / "skills")))
MCP_SCRIPTS_DIR = Path(os.getenv("SPLITCH_MCP_SCRIPTS_DIR", str(_ROOT / "mcp_servers")))
API_KEY = os.getenv("SPLITCH_API_KEY", "").strip()

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
    allow_origin_regex=(
        r"https?://(localhost|127\.0\.0\.1|10\..*|192\.168\..*|172\.(1[6-9]|2\d|3[0-1])\..*"
        r"|100\..*|[^/]+\.local)(:\d+)?"
    ),
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


def _is_https_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    fwd = request.headers.get("x-forwarded-proto", "")
    return fwd.split(",")[0].strip().lower() == "https"


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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
    return cleaned


def _session_cookie_token(api_key: str) -> str:
    return hmac.new(api_key.encode("utf-8"), b"splitch-ui-session-v1", hashlib.sha256).hexdigest()


def _is_request_authenticated(request: Request) -> bool:
    if not API_KEY:
        return True
    auth = request.headers.get("authorization", "")
    header_tok = (
        auth[7:].strip()
        if auth.lower().startswith("bearer ")
        else request.headers.get("x-api-key", "").strip()
    )
    if header_tok and hmac.compare_digest(header_tok, API_KEY):
        return True
    cookie_tok = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    return bool(
        cookie_tok and hmac.compare_digest(cookie_tok, _session_cookie_token(API_KEY))
    )


@app.middleware("http")
async def _security_and_auth_middleware(request: Request, call_next):
    path = request.url.path
    has_join_token = (
        path == "/api/nodes"
        and request.method == "POST"
        and join_tokens.is_valid_unconsumed(request.headers.get("x-splitch-join-token", ""))
    )
    if (
        API_KEY
        and path not in _AUTH_EXEMPT_PATHS
        and (path.startswith(("/v1/", "/api/")) or path == "/infill")
        and not _is_request_authenticated(request)
        and not has_join_token
    ):
        response: Response = JSONResponse(
            status_code=401, content={"detail": "Invalid or missing API key"}
        )
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
app.include_router(build_settings_router(cluster, DATA_DIR, api_key_getter=lambda: API_KEY))
app.include_router(build_provision_router(cluster, join_tokens, api_key_getter=lambda: API_KEY))


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
        auth_required=bool(API_KEY),
        authenticated=_is_request_authenticated(request),
    )


@app.post("/api/auth/login", response_model=AuthStatusResponse)
async def login_session(
    req: AuthLoginRequest, request: Request, response: Response
) -> AuthStatusResponse:
    if not API_KEY:
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
    if not candidate or not hmac.compare_digest(candidate, API_KEY):
        bucket.append(now)
        raise HTTPException(status_code=401, detail="Invalid SPLITCH_API_KEY")

    _LOGIN_FAILURES.pop(ip, None)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=_session_cookie_token(API_KEY),
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
    return AuthStatusResponse(auth_required=bool(API_KEY), authenticated=not bool(API_KEY))


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
    return await asyncio.to_thread(check_update, fetch_remote=fetch)


@app.get("/api/bench/history", response_model=list[BenchReport])
async def get_bench_history() -> list[BenchReport]:
    return await asyncio.to_thread(load_bench_history, DATA_DIR)


@app.post("/api/bench/run", response_model=BenchReport)
async def run_cluster_benchmark(req: BenchRunRequest) -> BenchReport:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://splitch.internal",
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


@app.post("/api/nodes", response_model=NodeStatus)
async def register_node(req: NodeRegisterRequest, request: Request) -> NodeStatus:
    if API_KEY and not _is_request_authenticated(request):
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
    req.agent_url = _validate_http_url(req.agent_url, "agent_url")
    req.engine_url = _validate_http_url(req.engine_url, "engine_url")
    req.media_url = _validate_http_url(req.media_url, "media_url", allow_empty=True)
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
        async with httpx.AsyncClient(timeout=8.0, headers=cluster.agent_headers()) as client:
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
    if API_KEY:
        tok = websocket.query_params.get("token", "").strip()
        cookie_tok = websocket.cookies.get(SESSION_COOKIE_NAME, "").strip()
        ok_query = bool(tok and hmac.compare_digest(tok, API_KEY))
        ok_cookie = bool(
            cookie_tok and hmac.compare_digest(cookie_tok, _session_cookie_token(API_KEY))
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


# ── Static Dashboard Serving (built Vite bundle) ─────────────────────────────

_DIST_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "dist"
if _DIST_DIR.exists():
    _assets = _DIST_DIR / "assets"
    if _assets.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_dashboard():
        return FileResponse(str(_DIST_DIR / "index.html"))

    _favicon = _DIST_DIR / "favicon.svg"
    if _favicon.exists():

        @app.api_route("/favicon.svg", methods=["GET", "HEAD"], include_in_schema=False)
        async def serve_favicon():
            return FileResponse(str(_favicon), media_type="image/svg+xml")

    _theme_init = _DIST_DIR / "theme-init.js"
    if _theme_init.exists():

        @app.api_route("/theme-init.js", methods=["GET", "HEAD"], include_in_schema=False)
        async def serve_theme_init():
            return FileResponse(str(_theme_init), media_type="application/javascript")
