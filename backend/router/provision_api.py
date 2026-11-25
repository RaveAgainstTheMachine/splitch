"""
Click-to-Install Node Provisioner:
  A. Short-lived single-use Join Tokens + self-hosted `/install.sh` & `/install.ps1`
  B. Authenticated Direct Remote SSH Push (`POST /api/provision/ssh`, SSE live progress)

Security invariants:
  - Remote SSH push requires `SPLITCH_API_KEY` (authenticated session) OR explicit
    `SPLITCH_ENABLE_REMOTE_PROVISION=1` opt-in.
  - Strict regex validation on `ssh_target` and `node_id`; zero shell interpolation on the
    router host (uses `asyncio.create_subprocess_exec` arg arrays + `BatchMode=yes` + `shlex.quote`).
  - Join tokens are single-use, time-bounded (default 15m), compared via `hmac.compare_digest`,
    and never expose master `SPLITCH_API_KEY`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shlex
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from backend.router.telemetry import ClusterState
from backend.shared.models import (
    JoinTokenCreateRequest,
    JoinTokenInfo,
    JoinTokenResponse,
    NodeSshProvisionRequest,
    NodeSshTestRequest,
    NodeSshTestResponse,
)
from backend.shared.secrets import hash_secret_sha256, verify_secret_hash

SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._:\[\]-]+$")
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")
STEP_MARKER_RE = re.compile(r"\[(\d+)/(\d+)\]\s*(.*)")

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _StoredJoinToken:
    """In-memory join token entry storing ONLY a salted SHA-256 verifier (never plaintext)."""

    id: str
    secret_hash: str
    node_hint: str
    created_at: float
    expires_at: float
    used: bool = False


@dataclass(frozen=True)
class MintedJoinToken:
    """Ephemeral return value from `JoinTokenManager.create()` exposing plaintext once at mint time."""

    id: str
    secret: str
    node_hint: str
    created_at: float
    expires_at: float


class JoinTokenManager:
    """In-memory single-use short-lived join token registry (salted SHA-256 hashed at rest)."""

    def __init__(self) -> None:
        self._tokens: dict[str, _StoredJoinToken] = {}

    def _prune(self) -> None:
        now = time.time()
        expired = [k for k, t in self._tokens.items() if t.expires_at <= now or t.used]
        for k in expired:
            self._tokens.pop(k, None)

    def create(self, *, ttl_seconds: int = 900, node_hint: str = "") -> MintedJoinToken:
        self._prune()
        now = time.time()
        secret = f"spj_{secrets.token_urlsafe(24)}"
        tid = secrets.token_hex(6)
        tok = _StoredJoinToken(
            id=tid,
            secret_hash=hash_secret_sha256(secret),
            node_hint=node_hint,
            created_at=now,
            expires_at=now + ttl_seconds,
        )
        self._tokens[tid] = tok
        return MintedJoinToken(
            id=tid,
            secret=secret,
            node_hint=node_hint,
            created_at=now,
            expires_at=tok.expires_at,
        )

    def list_active(self) -> list[JoinTokenInfo]:
        self._prune()
        return [
            JoinTokenInfo(
                id=t.id,
                node_hint=t.node_hint,
                created_at=t.created_at,
                expires_at=t.expires_at,
                used=t.used,
            )
            for t in sorted(self._tokens.values(), key=lambda x: x.created_at, reverse=True)
        ]

    def revoke(self, token_id: str) -> bool:
        return self._tokens.pop(token_id, None) is not None

    def validate_and_consume(self, candidate: str) -> bool:
        """Atomically validate and consume a single-use join token (constant-time salted hash compare)."""
        cleaned = (candidate or "").strip()
        if not cleaned:
            return False
        self._prune()
        now = time.time()
        matched: _StoredJoinToken | None = None
        for tok in self._tokens.values():
            if not tok.used and tok.expires_at > now and verify_secret_hash(cleaned, tok.secret_hash):
                matched = tok
        if matched is None:
            return False
        matched.used = True
        self._tokens.pop(matched.id, None)
        return True

    def is_valid_unconsumed(self, candidate: str) -> bool:
        cleaned = (candidate or "").strip()
        if not cleaned:
            return False
        self._prune()
        now = time.time()
        return any(
            not tok.used and tok.expires_at > now and verify_secret_hash(cleaned, tok.secret_hash)
            for tok in self._tokens.values()
        )


def _guess_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def resolve_router_public_url(cluster: ClusterState, request: Request, override: str = "") -> str:
    candidate = (
        override.strip()
        or cluster.config.router.public_url.strip()
        or str(request.base_url).strip()
    ).rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="router_url must be a valid https:// URL")
    if parsed.hostname not in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and parsed.scheme != "https":
        raise HTTPException(
            status_code=400,
            detail="router_url must use https:// for non-loopback hosts",
        )
    if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
        lan = _guess_lan_ip()
        port_part = f":{parsed.port}" if parsed.port else ""
        scheme = "https" if (cluster.config.tls.enabled or cluster.config.tls.self_signed) else parsed.scheme
        return f"{scheme}://{lan}{port_part}"
    return candidate


def validate_ssh_target(raw: str) -> str:
    cleaned = raw.strip()
    if not cleaned or cleaned.startswith("-") or not SSH_TARGET_RE.fullmatch(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Invalid SSH target. Use format user@host (alphanumeric, dots, hyphens, underscores only).",
        )
    return cleaned


def _build_bootstrap_commands(
    router_url: str,
    token_secret: str,
    *,
    node_id: str = "",
    role: str = "gpu",
    yield_on_gaming: bool = True,
    engine: str = "auto",
    backend: str = "auto",
    with_media: bool = False,
    enable_tools: bool = False,
    install_service: bool = True,
) -> tuple[str, str]:
    sh_flags = [
        "--agent-only",
        "--engine",
        engine,
        "--backend",
        backend,
        "--join",
        router_url,
        "--join-token",
        token_secret,
        "--role",
        role,
    ]
    if node_id:
        sh_flags.extend(["--node-id", node_id])
    if yield_on_gaming:
        sh_flags.append("--yield-on-gaming")
    if with_media:
        sh_flags.append("--with-media")
    if enable_tools:
        sh_flags.append("--enable-tools")
    if install_service:
        sh_flags.append("--install-service")

    quoted_sh = " ".join(shlex.quote(f) for f in sh_flags)
    linux_macos_cmd = f"curl -fsSL {shlex.quote(router_url + '/install.sh')} | sh -s -- {quoted_sh}"

    ps_parts = [
        "-AgentOnly",
        f"-Engine '{engine}'",
        f"-Backend '{backend}'",
        f"-Join '{router_url}'",
        f"-JoinToken '{token_secret}'",
        f"-Role '{role}'",
    ]
    if node_id:
        ps_parts.append(f"-NodeId '{node_id}'")
    if yield_on_gaming:
        ps_parts.append("-YieldOnGaming")
    if with_media:
        ps_parts.append("-WithMedia")
    if enable_tools:
        ps_parts.append("-EnableTools")
    ps_args = " ".join(ps_parts)
    windows_ps_cmd = (
        f"$s = irm '{router_url}/install.ps1'; "
        f"& ([scriptblock]::Create($s)) {ps_args}"
    )
    return linux_macos_cmd, windows_ps_cmd


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def build_provision_router(
    cluster: ClusterState,
    join_tokens: JoinTokenManager,
    *,
    api_key_getter,
) -> APIRouter:
    router = APIRouter(tags=["provision"])

    def _ensure_remote_provision_allowed() -> None:
        api_key = str(api_key_getter() or "")
        explicit_opt_in = os.getenv("SPLITCH_ENABLE_REMOTE_PROVISION", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if not api_key and not explicit_opt_in:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Remote SSH node provisioning requires SPLITCH_API_KEY authentication "
                    "(or explicit SPLITCH_ENABLE_REMOTE_PROVISION=1 opt-in on trusted LANs)."
                ),
            )

    # ── Self-hosted installer scripts (always auth-exempt GET) ───────────────

    @router.get("/install.sh", include_in_schema=False)
    async def serve_install_sh():
        script = REPO_ROOT / "install.sh"
        if not script.is_file():
            raise HTTPException(status_code=404, detail="install.sh not found")
        return FileResponse(str(script), media_type="text/x-shellscript")

    @router.get("/install.ps1", include_in_schema=False)
    async def serve_install_ps1():
        script = REPO_ROOT / "install.ps1"
        if not script.is_file():
            raise HTTPException(status_code=404, detail="install.ps1 not found")
        return FileResponse(str(script), media_type="text/plain; charset=utf-8")

    # ── Join Token Bootstrap API ─────────────────────────────────────────────

    @router.post("/api/provision/join-token", response_model=JoinTokenResponse)
    async def create_join_token(
        req: JoinTokenCreateRequest, request: Request
    ) -> JoinTokenResponse:
        clean_id = req.node_id.strip().lower()
        if clean_id and not NODE_ID_RE.fullmatch(clean_id):
            raise HTTPException(
                status_code=400, detail="node_id must be lowercase alphanumeric/hyphen/underscore"
            )
        router_url = resolve_router_public_url(cluster, request, req.router_url)
        tok = join_tokens.create(ttl_seconds=req.ttl_seconds, node_hint=clean_id)
        sh_cmd, ps_cmd = _build_bootstrap_commands(
            router_url,
            tok.secret,
            node_id=clean_id,
            role=req.role,
            yield_on_gaming=req.yield_on_gaming,
            engine=req.engine,
            backend=req.backend,
            with_media=req.with_media,
            enable_tools=req.enable_tools,
            install_service=True,
        )
        return JoinTokenResponse(
            id=tok.id,
            token=tok.secret,
            expires_at=tok.expires_at,
            ttl_seconds=req.ttl_seconds,
            router_url=router_url,
            linux_macos_cmd=sh_cmd,
            windows_ps_cmd=ps_cmd,
        )

    @router.get("/api/provision/join-tokens", response_model=list[JoinTokenInfo])
    async def list_join_tokens() -> list[JoinTokenInfo]:
        return join_tokens.list_active()

    @router.delete("/api/provision/join-tokens/{token_id}")
    async def revoke_join_token(token_id: str) -> dict:
        if not join_tokens.revoke(token_id):
            raise HTTPException(status_code=404, detail="Join token not found")
        return {"ok": True, "revoked": token_id}

    # ── Direct SSH Push Preflight & Live SSE Installer ───────────────────────

    @router.post("/api/provision/ssh/test", response_model=NodeSshTestResponse)
    async def test_ssh_target(req: NodeSshTestRequest) -> NodeSshTestResponse:
        _ensure_remote_provision_allowed()
        target = validate_ssh_target(req.ssh_target)
        probe_script = (
            "printf 'OS=%s\\n' \"$(uname -sm 2>/dev/null || echo unknown)\"; "
            "if command -v nvidia-smi >/dev/null 2>&1; then "
            "  printf 'GPU=NVIDIA: %s\\n' \"$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | paste -sd ', ' -)\"; "
            "elif command -v rocm-smi >/dev/null 2>&1; then "
            "  printf 'GPU=AMD ROCm\\n'; "
            "elif [ \"$(uname -s)\" = 'Darwin' ] && [ \"$(uname -m)\" = 'arm64' ]; then "
            "  printf 'GPU=%s\\n' \"$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo Apple Silicon)\"; "
            "else printf 'GPU=CPU / Generic\\n'; fi; "
            "if command -v splitch >/dev/null 2>&1 || [ -x \"$HOME/.local/bin/splitch\" ]; then "
            "  printf 'SPLITCH=1\\n'; else printf 'SPLITCH=0\\n'; fi"
        )
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-p",
                str(req.ssh_port),
                "--",
                target,
                probe_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=12.0)
        except TimeoutError:
            if proc and proc.returncode is None:
                proc.kill()
            return NodeSshTestResponse(
                ok=False, ssh_target=target, error="SSH preflight timed out after 12s"
            )
        except OSError as exc:
            return NodeSshTestResponse(ok=False, ssh_target=target, error=str(exc))

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip() or f"SSH exit code {proc.returncode}"
            return NodeSshTestResponse(ok=False, ssh_target=target, error=err[:300])

        os_info = ""
        gpu_summary = ""
        splitch_installed = False
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if line.startswith("OS="):
                os_info = line[3:].strip()
            elif line.startswith("GPU="):
                gpu_summary = line[4:].strip()
            elif line.startswith("SPLITCH="):
                splitch_installed = line[8:].strip() == "1"

        return NodeSshTestResponse(
            ok=True,
            ssh_target=target,
            os_info=os_info,
            gpu_summary=gpu_summary,
            splitch_installed=splitch_installed,
        )

    @router.post("/api/provision/ssh")
    async def provision_node_ssh(req: NodeSshProvisionRequest, request: Request):
        _ensure_remote_provision_allowed()
        target = validate_ssh_target(req.ssh_target)
        clean_id = req.node_id.strip().lower()
        if clean_id and not NODE_ID_RE.fullmatch(clean_id):
            raise HTTPException(
                status_code=400, detail="node_id must be lowercase alphanumeric/hyphen/underscore"
            )
        router_url = resolve_router_public_url(cluster, request, req.router_url)
        tok = join_tokens.create(ttl_seconds=900, node_hint=clean_id)

        sh_flags = [
            "--agent-only",
            "--engine",
            req.engine,
            "--backend",
            req.backend,
            "--join",
            router_url,
            "--join-token",
            tok.secret,
            "--role",
            req.role,
            "--agent-port",
            str(req.agent_port),
            "--engine-port",
            str(req.engine_port),
        ]
        if clean_id:
            sh_flags.extend(["--node-id", clean_id])
        if req.display_name.strip():
            sh_flags.extend(["--node-name", req.display_name.strip()[:64]])
        if req.yield_on_gaming:
            sh_flags.append("--yield-on-gaming")
        if req.yield_gpu_indices:
            sh_flags.extend(["--yield-gpus", ",".join(str(i) for i in req.yield_gpu_indices)])
        if req.with_media:
            sh_flags.append("--with-media")
        if req.enable_tools:
            sh_flags.append("--enable-tools")
        if req.install_service:
            sh_flags.append("--install-service")

        quoted_flags = " ".join(shlex.quote(f) for f in sh_flags)
        env_parts: list[str] = []
        if cluster.router_public_key_compact:
            env_parts.append(
                f"SPLITCH_ROUTER_PUBLIC_KEY={shlex.quote(cluster.router_public_key_compact)}"
            )
        elif cluster.agent_token:
            env_parts.append(f"SPLITCH_AGENT_TOKEN={shlex.quote(cluster.agent_token)}")
        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        remote_cmd = (
            f"set -eu; {env_prefix}"
            f"curl -fsSL {shlex.quote(router_url + '/install.sh')} | sh -s -- {quoted_flags}"
        )

        async def _stream():
            yield _sse(
                "step",
                {"step": 0, "total": 7, "label": f"Connecting to {target} via SSH (BatchMode)..."},
            )
            proc: asyncio.subprocess.Process | None = None
            before_nodes = set(cluster.nodes.keys())
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    "-p",
                    str(req.ssh_port),
                    "--",
                    target,
                    remote_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                assert proc.stdout is not None
                deadline = time.monotonic() + 420.0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        proc.kill()
                        yield _sse("error", {"detail": "Remote installation timed out after 7 minutes"})
                        return
                    try:
                        raw_line = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=min(remaining, 30.0)
                        )
                    except TimeoutError:
                        yield _sse("log", {"line": "... still installing on remote host ..."})
                        continue
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                    # Scrub join token or agent token if echoed
                    line = line.replace(tok.secret, "<join-token>")
                    if cluster.agent_token:
                        line = line.replace(cluster.agent_token, "<agent-token>")
                    m = STEP_MARKER_RE.search(line)
                    if m:
                        yield _sse(
                            "step",
                            {
                                "step": int(m.group(1)),
                                "total": int(m.group(2)),
                                "label": m.group(3).strip(),
                            },
                        )
                    if line:
                        yield _sse("log", {"line": line})

                rc = await proc.wait()
                if rc != 0:
                    yield _sse(
                        "error",
                        {"detail": f"Remote installer exited with code {rc}"},
                    )
                    return

                # Refresh cluster telemetry so UI sees newly joined node immediately
                joined_id = clean_id
                for _ in range(10):
                    await cluster.poll_all_nodes()
                    new_nodes = set(cluster.nodes.keys()) - before_nodes
                    if clean_id and clean_id in cluster.nodes:
                        joined_id = clean_id
                        break
                    if new_nodes:
                        joined_id = next(iter(new_nodes))
                        break
                    await asyncio.sleep(1.0)

                yield _sse(
                    "done",
                    {
                        "ok": True,
                        "ssh_target": target,
                        "node_id": joined_id or target.split("@", 1)[1],
                    },
                )
            except (OSError, asyncio.CancelledError) as exc:
                yield _sse("error", {"detail": str(exc)})
            finally:
                if proc is not None and proc.returncode is None:
                    proc.terminate()
                    with contextlib.suppress(TimeoutError, ProcessLookupError):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
