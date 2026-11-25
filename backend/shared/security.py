"""
Cryptographic tool-approval assertions & trusted-proxy IP verification helpers.

Used by:
  - `backend/router/chat_api.py` (signs post-HITL-approval workspace tool calls)
  - `backend/agent/main.py` (verifies approval assertions at the execution boundary)
  - `backend/router/app.py` (gates X-Forwarded-For / X-Forwarded-Proto on trusted proxies)
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import threading
import time
from collections.abc import Iterable
from typing import Any


class NonceReplayCache:
    """Thread-safe bounded TTL cache rejecting replayed nonces/signatures within the validity window."""

    def __init__(self, *, max_entries: int = 2048) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._max_entries = max_entries

    def seen_or_record(self, key: str, *, ttl_s: float = 120.0, now_ts: float | None = None) -> bool:
        now = time.monotonic() if now_ts is None else now_ts
        with self._lock:
            if len(self._seen) >= self._max_entries:
                expired = [k for k, exp in self._seen.items() if now >= exp]
                for k in expired:
                    del self._seen[k]
                if len(self._seen) >= self._max_entries:
                    self._seen.clear()
            exp = self._seen.get(key)
            if exp is not None and now < exp:
                return True
            self._seen[key] = now + max(1.0, ttl_s)
            return False


DEFAULT_TOOL_NONCE_CACHE = NonceReplayCache()
DEFAULT_AGENT_NONCE_CACHE = NonceReplayCache()


def _canonical_tool_payload(tool: str, arguments: dict[str, Any], workspace_subpath: str) -> str:
    return json.dumps(
        {
            "tool": tool,
            "arguments": arguments,
            "workspace_subpath": workspace_subpath,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def sign_tool_approval(
    secret: str,
    tool: str,
    arguments: dict[str, Any],
    workspace_subpath: str = "",
    *,
    timestamp: int | None = None,
    nonce: str = "",
) -> tuple[str, str]:
    """Produce short-lived HMAC-SHA256 `(ts, sig)` headers proving router approval of a tool call."""
    ts_str = str(int(time.time()) if timestamp is None else int(timestamp))
    payload = _canonical_tool_payload(tool, arguments, workspace_subpath)
    body = f"{ts_str}:{nonce}:{payload}" if nonce else f"{ts_str}:{payload}"
    sig = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return ts_str, sig


def verify_tool_approval(
    secret: str,
    ts_header: str,
    sig_header: str,
    tool: str,
    arguments: dict[str, Any],
    workspace_subpath: str = "",
    *,
    nonce: str = "",
    max_age_s: int = 60,
    now_ts: float | None = None,
    replay_cache: NonceReplayCache | None = None,
) -> bool:
    """Verify a router-signed tool approval assertion (constant-time HMAC + bounded timestamp + nonce replay)."""
    if not secret or not ts_header or not sig_header:
        return False
    try:
        ts_val = int(ts_header.strip())
    except ValueError:
        return False
    now = time.time() if now_ts is None else now_ts
    if abs(now - ts_val) > max_age_s:
        return False
    _, expected = sign_tool_approval(
        secret, tool, arguments, workspace_subpath, timestamp=ts_val, nonce=nonce
    )
    if not hmac.compare_digest(expected, sig_header.strip().lower()):
        return False
    return not (
        replay_cache is not None
        and nonce
        and replay_cache.seen_or_record(f"hmac:{ts_val}:{nonce}", ttl_s=float(max_age_s * 2))
    )


def is_loopback_host(host: str | None) -> bool:
    """Return True if `host` is a loopback IP (127.0.0.0/8, ::1) or `localhost`."""
    if not host:
        return False
    cleaned = host.strip().lower()
    if cleaned == "localhost" or (cleaned == "testclient" and os.getenv("PYTEST_CURRENT_TEST")):
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def _extract_host_without_port(host_header: str | None) -> str:
    if not host_header:
        return ""
    raw = host_header.strip().lower()
    if not raw or any(ch.isspace() for ch in raw) or "/" in raw or "@" in raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        if end <= 1:
            return ""
        rest = raw[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return ""
        return raw[1:end]
    if raw.count(":") == 1:
        host_part, port_part = raw.split(":", 1)
        if not port_part.isdigit():
            return ""
        return host_part
    return raw


def is_allowed_host_header(
    host_header: str | None,
    *,
    extra_hosts: Iterable[str] = (),
) -> bool:
    """
    DNS-rebinding guard for HTTP `Host` headers.
    Allows loopback names/IPs, raw IPv4/IPv6 literals (immune to DNS rebinding),
    configured hostnames (`public_url`, `allowed_origins`, `SPLITCH_ALLOWED_HOSTS`),
    and test hosts (`testserver`/`testclient`) only under `PYTEST_CURRENT_TEST`.
    """
    hostname = _extract_host_without_port(host_header)
    if not hostname:
        return False
    if hostname in ("localhost", "splitch.internal"):
        return True
    if os.getenv("PYTEST_CURRENT_TEST") and hostname in ("testserver", "testclient"):
        return True
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    allowed = {h.strip().lower() for h in extra_hosts if h and h.strip()}
    for env_item in os.getenv("SPLITCH_ALLOWED_HOSTS", "").split(","):
        cleaned = env_item.strip().lower()
        if cleaned:
            allowed.add(cleaned)
    return hostname in allowed


def is_trusted_proxy(peer_host: str | None, trusted_spec: str) -> bool:
    """
    Return True only if socket peer `peer_host` matches an entry in comma-separated
    `trusted_spec` (exact IP, CIDR network, literal hostname like 'testclient', or '*').
    """
    if not peer_host or not trusted_spec:
        return False
    peer = peer_host.strip().lower()
    peer_ip = None
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        peer_ip = None

    for raw in trusted_spec.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if entry == peer:
            return True
        if peer_ip is not None:
            try:
                if "/" in entry:
                    if peer_ip in ipaddress.ip_network(entry, strict=False):
                        return True
                elif peer_ip == ipaddress.ip_address(entry):
                    return True
            except ValueError:
                continue
    return False
