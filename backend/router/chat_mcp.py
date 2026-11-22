"""MCP JSON-RPC stdio/HTTP tool discovery & invocation helpers for Agent Chat."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

import httpx

from backend.shared.models import McpServerConfig
from backend.shared.secrets import resolve_env_map
from backend.version import __version__ as VERSION


async def call_mcp_tool(
    srv: McpServerConfig,
    tool_name: str,
    arguments: dict[str, Any],
    workspace_subpath: str = "",
    *,
    vault=None,
) -> str:
    if srv.transport in ("http", "sse") and srv.url:
        call_msg = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    srv.url,
                    json=call_msg,
                    headers={"Accept": "application/json, text/event-stream"},
                )
                r.raise_for_status()
                body = r.json()
                res = body.get("result") or body.get("error") or {}
                parts = res.get("content") if isinstance(res, dict) else None
                if isinstance(parts, list):
                    return "\n".join(str(p.get("text", p)) for p in parts if isinstance(p, dict))
                return json.dumps(res)[:10_000]
        except (httpx.HTTPError, ValueError) as exc:
            return f"MCP error: {exc}"

    if not srv.command:
        return "MCP server missing command"
    env = os.environ.copy()
    if workspace_subpath:
        env["SPLITCH_WORKSPACE_SUBPATH"] = workspace_subpath
        base_ws = os.getenv("SPLITCH_WORKSPACE_ROOT", str(Path.home()))
        resolved_ws = (Path(base_ws).resolve() / workspace_subpath.lstrip("/")).resolve()
        if resolved_ws.is_relative_to(Path(base_ws).resolve()):
            env["GRAPHIFY_WORKSPACE"] = str(resolved_ws)
    env.update(resolve_env_map(srv.env, vault=vault))
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            srv.command,
            *srv.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=4 * 1024 * 1024,
        )
        assert proc.stdin is not None and proc.stdout is not None

        async def _run() -> str:
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "splitch-chat", "version": VERSION},
                },
            }
            proc.stdin.write((json.dumps(init) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            for _ in range(20):
                line = await proc.stdout.readline()
                if not line:
                    break
                with contextlib.suppress(ValueError):
                    if json.loads(line.decode("utf-8", errors="replace")).get("id") == 1:
                        break
            proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode(
                    "utf-8"
                )
            )
            call_msg = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
            proc.stdin.write((json.dumps(call_msg) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            for _ in range(30):
                line = await proc.stdout.readline()
                if not line:
                    break
                with contextlib.suppress(ValueError):
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                    if msg.get("id") == 2:
                        res = msg.get("result") or msg.get("error") or {}
                        parts = res.get("content") if isinstance(res, dict) else None
                        if isinstance(parts, list):
                            return "\n".join(
                                str(p.get("text", p)) for p in parts if isinstance(p, dict)
                            )
                        return json.dumps(res)[:10_000]
            return "MCP server returned no tools/call response"

        return await asyncio.wait_for(_run(), timeout=20.0)
    except (FileNotFoundError, TimeoutError, OSError, ValueError) as exc:
        return f"MCP error: {exc}"
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)


async def fetch_mcp_tools(
    srv: McpServerConfig, workspace_subpath: str = "", *, vault=None
) -> list[dict[str, Any]]:
    if srv.transport in ("http", "sse") and srv.url:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.post(
                    srv.url,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    headers={"Accept": "application/json, text/event-stream"},
                )
                if r.status_code == 200:
                    tools = (r.json().get("result") or {}).get("tools") or []
                    egress_tag = (
                        f" [EXTERNAL EGRESS -> {srv.egress_target}]"
                        if srv.egress != "local"
                        else ""
                    )
                    return [
                        {
                            "type": "function",
                            "function": {
                                "name": f"mcp__{srv.id}__{t['name']}",
                                "description": f"[MCP:{srv.id}]{egress_tag} {t.get('description', '')}",
                                "parameters": t.get(
                                    "inputSchema", {"type": "object", "properties": {}}
                                ),
                            },
                        }
                        for t in tools
                        if isinstance(t, dict) and t.get("name")
                    ]
        except (httpx.HTTPError, ValueError):
            return []
        return []

    if srv.transport != "stdio" or not srv.command:
        return []
    env = os.environ.copy()
    if workspace_subpath:
        env["SPLITCH_WORKSPACE_SUBPATH"] = workspace_subpath
        base_ws = os.getenv("SPLITCH_WORKSPACE_ROOT", str(Path.home()))
        resolved_ws = (Path(base_ws).resolve() / workspace_subpath.lstrip("/")).resolve()
        if resolved_ws.is_relative_to(Path(base_ws).resolve()):
            env["GRAPHIFY_WORKSPACE"] = str(resolved_ws)
    env.update(resolve_env_map(srv.env, vault=vault))
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            srv.command,
            *srv.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=4 * 1024 * 1024,
        )
        assert proc.stdin is not None and proc.stdout is not None

        async def _discover() -> list[dict[str, Any]]:
            proc.stdin.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "splitch-chat", "version": VERSION},
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            await proc.stdin.drain()
            for _ in range(20):
                line = await proc.stdout.readline()
                if not line:
                    break
                with contextlib.suppress(ValueError):
                    if json.loads(line.decode("utf-8", errors="replace")).get("id") == 1:
                        break
            proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode(
                    "utf-8"
                )
            )
            proc.stdin.write(
                (
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                    + "\n"
                ).encode("utf-8")
            )
            await proc.stdin.drain()
            for _ in range(25):
                line = await proc.stdout.readline()
                if not line:
                    break
                with contextlib.suppress(ValueError):
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                    if msg.get("id") == 2 and "result" in msg:
                        tools = msg["result"].get("tools") or []
                        return [
                            {
                                "type": "function",
                                "function": {
                                    "name": f"mcp__{srv.id}__{t['name']}",
                                    "description": f"[MCP:{srv.id}] {t.get('description', '')}",
                                    "parameters": t.get(
                                        "inputSchema", {"type": "object", "properties": {}}
                                    ),
                                },
                            }
                            for t in tools
                            if isinstance(t, dict) and t.get("name")
                        ]
            return []

        return await asyncio.wait_for(_discover(), timeout=6.0)
    except (FileNotFoundError, TimeoutError, OSError):
        return []
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
