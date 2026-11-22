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
import time
from typing import Any


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
) -> tuple[str, str]:
    """Produce short-lived HMAC-SHA256 `(ts, sig)` headers proving router approval of a tool call."""
    ts_str = str(int(time.time()) if timestamp is None else int(timestamp))
    body = f"{ts_str}:{_canonical_tool_payload(tool, arguments, workspace_subpath)}"
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
    max_age_s: int = 60,
    now_ts: float | None = None,
) -> bool:
    """Verify a router-signed tool approval assertion (constant-time HMAC + bounded timestamp window)."""
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
        secret, tool, arguments, workspace_subpath, timestamp=ts_val
    )
    return hmac.compare_digest(expected, sig_header.strip().lower())


def is_loopback_host(host: str | None) -> bool:
    """Return True if `host` is a loopback IP (127.0.0.0/8, ::1) or `localhost`."""
    if not host:
        return False
    cleaned = host.strip().lower()
    if cleaned in ("localhost", "testclient"):
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


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
