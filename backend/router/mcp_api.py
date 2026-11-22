"""
Real MCP Registry, live MCP JSON-RPC protocol probe (stdio & HTTP/SSE),
custom Python MCP builder, and non-destructive OpenCode / Zed client config sync.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException

from backend.router.telemetry import VERSION, ClusterState
from backend.shared.models import (
    McpBuildRequest,
    McpEnvVarSpec,
    McpImportJsonRequest,
    McpRegistryResult,
    McpServerConfig,
    McpTestResult,
    McpToolSpec,
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")

_EXTERNAL_ENV_PREFIXES = (
    "GITHUB_",
    "BRAVE_",
    "TAVILY_",
    "OPENAI_",
    "ANTHROPIC_",
    "SLACK_",
    "NOTION_",
    "LINEAR_",
    "SENTRY_",
    "SUPABASE_",
    "CLOUDFLARE_",
    "UPSTASH_",
    "EXA_",
    "FIRECRAWL_",
)
_EXTERNAL_PKG_TOKENS = (
    "context7",
    "server-github",
    "server-slack",
    "server-brave",
    "tavily",
    "firecrawl",
    "exa-mcp",
)


def _is_local_or_lan_host(host: str) -> bool:
    h = host.strip().lower()
    if not h:
        return True
    if h in ("localhost", "host.docker.internal") or h.endswith((".local", ".lan", ".internal", ".home")):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return bool(ip.is_loopback or ip.is_private or ip.is_link_local)
    except ValueError:
        return False


def classify_mcp_egress(
    srv: McpServerConfig,
) -> tuple[Literal["local", "external_api", "remote_mcp"], str | None]:
    """Classify whether an MCP server stays local or can leak context/data externally."""
    if srv.transport in ("http", "sse") and srv.url:
        parsed = urlparse(srv.url.strip())
        host = (parsed.hostname or "").lower()
        if not _is_local_or_lan_host(host):
            return "remote_mcp", host or srv.url
        return "local", host or "localhost"

    cmd_line = f"{srv.command or ''} {' '.join(srv.args)}".lower()
    for tok in _EXTERNAL_PKG_TOKENS:
        if tok in cmd_line:
            return "external_api", tok
    for k in srv.env:
        ku = k.upper()
        if ku.startswith(_EXTERNAL_ENV_PREFIXES):
            return "external_api", ku
    return "local", None


def annotate_mcp_egress(srv: McpServerConfig) -> McpServerConfig:
    kind, target = classify_mcp_egress(srv)
    srv.egress = kind
    srv.egress_target = target
    return srv

MCP_INIT_MSG = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "splitch", "version": VERSION},
    },
}
MCP_INITIALIZED_NOTIF = {"jsonrpc": "2.0", "method": "notifications/initialized"}
MCP_LIST_TOOLS_MSG = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


async def probe_mcp_server(srv: McpServerConfig) -> McpTestResult:
    """Execute a real MCP JSON-RPC initialize + tools/list handshake."""
    t0 = time.perf_counter()

    if srv.transport in ("http", "sse"):
        if not srv.url:
            return McpTestResult(id=srv.id, ok=False, error="Missing URL")
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.post(
                    srv.url,
                    json=MCP_INIT_MSG,
                    headers={"Accept": "application/json, text/event-stream"},
                )
                latency = round((time.perf_counter() - t0) * 1000.0, 1)
                if r.status_code >= 400:
                    return McpTestResult(
                        id=srv.id,
                        ok=False,
                        latency_ms=latency,
                        error=f"HTTP {r.status_code}: {r.text[:200]}",
                    )
                server_name = None
                proto = None
                with contextlib.suppress(ValueError):
                    body = r.json()
                    res = body.get("result", {})
                    proto = res.get("protocolVersion")
                    server_name = (res.get("serverInfo") or {}).get("name")
                return McpTestResult(
                    id=srv.id,
                    ok=True,
                    protocol_version=proto,
                    server_name=server_name,
                    latency_ms=latency,
                )
        except httpx.HTTPError as exc:
            return McpTestResult(
                id=srv.id,
                ok=False,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 1),
                error=str(exc),
            )

    if not srv.command:
        return McpTestResult(id=srv.id, ok=False, error="Missing command")

    env = os.environ.copy()
    env.update(srv.env)
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

        async def _exchange() -> tuple[str | None, str | None, list[str]]:
            proc.stdin.write((json.dumps(MCP_INIT_MSG) + "\n").encode("utf-8"))
            await proc.stdin.drain()

            proto_ver = None
            srv_name = None
            tools: list[str] = []

            for _ in range(25):
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if msg.get("id") == 1 and "result" in msg:
                    res = msg["result"]
                    proto_ver = res.get("protocolVersion")
                    srv_name = (res.get("serverInfo") or {}).get("name")
                    break

            proc.stdin.write((json.dumps(MCP_INITIALIZED_NOTIF) + "\n").encode("utf-8"))
            proc.stdin.write((json.dumps(MCP_LIST_TOOLS_MSG) + "\n").encode("utf-8"))
            await proc.stdin.drain()

            for _ in range(25):
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if msg.get("id") == 2 and "result" in msg:
                    raw_tools = msg["result"].get("tools", [])
                    tools = [str(t["name"]) for t in raw_tools if isinstance(t, dict) and "name" in t]
                    break
            return proto_ver, srv_name, tools

        proto_ver, srv_name, tools = await asyncio.wait_for(_exchange(), timeout=8.0)
        return McpTestResult(
            id=srv.id,
            ok=proto_ver is not None,
            protocol_version=proto_ver,
            server_name=srv_name,
            tools=tools,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 1),
            error=None if proto_ver else "No valid MCP initialize response received",
        )
    except (FileNotFoundError, TimeoutError, OSError) as exc:
        return McpTestResult(
            id=srv.id,
            ok=False,
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 1),
            error=str(exc),
        )
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)


def generate_client_config(
    client: Literal["opencode", "zed"],
    router_base_url: str,
    cluster: ClusterState,
    servers: list[McpServerConfig],
) -> dict:
    enabled_mcps = [s for s in servers if s.enabled]
    base_v1 = f"{router_base_url.rstrip('/')}/v1"

    if client == "opencode":
        models_map: dict[str, dict[str, str]] = {"auto": {"name": "Splitch Auto Routing"}}
        for alias in cluster.aliases.values():
            models_map[alias.alias] = {
                "name": f"{alias.alias} ({alias.primary_node}/{alias.primary_model})"
            }
        mcp_map: dict[str, dict] = {}
        for s in enabled_mcps:
            if s.transport == "stdio" and s.command:
                entry: dict = {
                    "type": "local",
                    "command": [s.command, *s.args],
                    "enabled": True,
                }
                if s.env:
                    entry["environment"] = s.env
                mcp_map[s.id] = entry
            elif s.url:
                mcp_map[s.id] = {"type": "remote", "url": s.url, "enabled": True}

        default_primary = "splitch/heavy" if "heavy" in cluster.aliases else "splitch/auto"
        default_small = "splitch/fast" if "fast" in cluster.aliases else "splitch/auto"
        return {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "splitch": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Splitch Local Cluster",
                    "options": {"baseURL": base_v1, "apiKey": "splitch-local"},
                    "models": models_map,
                }
            },
            "model": default_primary,
            "small_model": default_small,
            "mcp": mcp_map,
        }

    zed_models = [
        {
            "name": "auto",
            "display_name": "Splitch Auto",
            "max_tokens": 65536,
            "capabilities": {
                "tools": True,
                "images": False,
                "parallel_tool_calls": True,
                "prompt_cache_key": False,
            },
        }
    ]
    for alias in cluster.aliases.values():
        if alias.task_kind in ("embed",):
            continue
        primary_node = cluster.nodes.get(alias.primary_node)
        is_gpu = primary_node.role == "gpu" if primary_node else True
        zed_models.append(
            {
                "name": alias.alias,
                "display_name": f"Splitch {alias.alias} ({alias.primary_node})",
                "max_tokens": 65536 if is_gpu else 16384,
                "capabilities": {
                    "tools": True,
                    "images": False,
                    "parallel_tool_calls": is_gpu,
                    "prompt_cache_key": False,
                },
            }
        )

    context_servers: dict[str, dict] = {}
    for s in enabled_mcps:
        if s.transport == "stdio" and s.command:
            context_servers[s.id] = {
                "source": "custom",
                "command": {"path": s.command, "args": s.args, "env": s.env},
            }
        elif s.url:
            context_servers[s.id] = {"source": "custom", "url": s.url}

    return {
        "language_models": {
            "openai_compatible": {
                "Splitch": {"api_url": base_v1, "available_models": zed_models}
            }
        },
        "context_servers": context_servers,
    }


def parse_mcp_json_snippet(raw_json: str, default_scope: str = "shared") -> list[McpServerConfig]:
    """Parse standard mcpServers, OpenCode mcp, or bare server JSON into McpServerConfig list."""
    parsed = json.loads(raw_json)
    if not isinstance(parsed, dict):
        raise TypeError("JSON root must be an object")

    entries: dict[str, dict] = {}
    if isinstance(parsed.get("mcpServers"), dict):
        entries = parsed["mcpServers"]
    elif isinstance(parsed.get("mcp"), dict):
        entries = parsed["mcp"]
    elif isinstance(parsed.get("context_servers"), dict):
        entries = parsed["context_servers"]
    elif "command" in parsed or "url" in parsed:
        entries = {str(parsed.get("id") or parsed.get("name") or "imported-mcp"): parsed}
    else:
        entries = {k: v for k, v in parsed.items() if isinstance(v, dict)}

    result: list[McpServerConfig] = []
    for raw_id, cfg in entries.items():
        if not isinstance(cfg, dict):
            continue
        slug = re.sub(r"[^a-z0-9_\-]+", "-", str(raw_id).lower()).strip("-")[:64]
        if not slug or not SLUG_RE.fullmatch(slug):
            slug = "imported-mcp"

        cmd = cfg.get("command")
        args: list[str] = []
        url = cfg.get("url")
        env = cfg.get("env") or cfg.get("environment") or {}
        if not isinstance(env, dict):
            env = {}
        str_env = {str(k): str(v) for k, v in env.items()}

        if isinstance(cmd, list) and cmd:
            args = [str(x) for x in cmd[1:]]
            cmd = str(cmd[0])
        elif isinstance(cmd, dict) and cmd.get("path"):
            args = [str(x) for x in (cmd.get("args") or [])]
            if isinstance(cmd.get("env"), dict):
                str_env.update({str(k): str(v) for k, v in cmd["env"].items()})
            cmd = str(cmd["path"])
        else:
            raw_args = cfg.get("args") or []
            if isinstance(raw_args, list):
                args = [str(x) for x in raw_args]

        if url:
            transport: Literal["stdio", "http", "sse"] = (
                "sse" if str(cfg.get("transport", "")).lower() == "sse" else "http"
            )
            result.append(
                McpServerConfig(
                    id=slug,
                    name=str(cfg.get("name") or slug),
                    description=str(cfg.get("description") or "Imported MCP server"),
                    transport=transport,
                    scope=default_scope,
                    enabled=bool(cfg.get("enabled", True)),
                    url=str(url),
                    env=str_env,
                )
            )
        elif cmd:
            result.append(
                McpServerConfig(
                    id=slug,
                    name=str(cfg.get("name") or slug),
                    description=str(cfg.get("description") or "Imported MCP server"),
                    transport="stdio",
                    scope=default_scope,
                    enabled=bool(cfg.get("enabled", True)),
                    command=str(cmd),
                    args=args,
                    env=str_env,
                )
            )
    if not result:
        raise ValueError("No valid MCP server definitions found in JSON")
    return result


def scaffold_python_mcp_server(server_name: str, tools: list[McpToolSpec]) -> str:
    """Generate a self-contained, zero-dependency JSON-RPC 2.0 stdio Python MCP server."""
    tool_defs: list[dict] = []
    for t in tools:
        props = {}
        req_fields = []
        for p in t.parameters:
            props[p.name] = {"type": p.type, "description": p.description or p.name}
            if p.required:
                req_fields.append(p.name)
        tool_defs.append(
            {
                "name": t.name,
                "description": t.description or f"Execute {t.name}",
                "inputSchema": {
                    "type": "object",
                    "properties": props,
                    "required": req_fields,
                },
            }
        )
    if not tool_defs:
        tool_defs = [
            {
                "name": "hello",
                "description": "Example tool returning a greeting",
                "inputSchema": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "Name to greet"}},
                    "required": ["name"],
                },
            }
        ]

    tools_json = json.dumps(tool_defs, indent=2)
    return f'''#!/usr/bin/env python3
"""
Custom Splitch MCP Server: {server_name}
Zero-dependency JSON-RPC 2.0 stdio MCP server. Edit handle_tool() below.
"""

from __future__ import annotations

import json
import sys
from typing import Any

SERVER_INFO = {{"name": {server_name!r}, "version": "0.1.0"}}

TOOLS: list[dict[str, Any]] = {tools_json}


def handle_tool(name: str, arguments: dict[str, Any]) -> str:
    """Implement your tool logic here and return a string result."""
    return f"Tool '{{name}}' executed with args: {{json.dumps(arguments)}}"


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")

        if method == "initialize":
            reply = {{
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {{
                    "protocolVersion": "2024-11-05",
                    "serverInfo": SERVER_INFO,
                    "capabilities": {{"tools": {{}}}},
                }},
            }}
        elif method == "tools/list":
            reply = {{"jsonrpc": "2.0", "id": msg_id, "result": {{"tools": TOOLS}}}}
        elif method == "tools/call":
            params = msg.get("params") or {{}}
            tool_name = str(params.get("name", ""))
            tool_args = params.get("arguments") or {{}}
            try:
                text = handle_tool(tool_name, tool_args)
                reply = {{
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {{"content": [{{"type": "text", "text": str(text)}}]}},
                }}
            except Exception as exc:
                reply = {{
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {{
                        "content": [{{"type": "text", "text": f"Error: {{exc}}"}}],
                        "isError": True,
                    }},
                }}
        elif msg_id is not None:
            reply = {{"jsonrpc": "2.0", "id": msg_id, "result": {{}}}}
        else:
            continue

        sys.stdout.write(json.dumps(reply) + "\\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
'''


def _slug_from_name(raw: str) -> str:
    tail = raw.split("/")[-1]
    slug = re.sub(r"[^a-z0-9_\-]+", "-", tail.lower()).strip("-")[:64]
    return slug if slug and SLUG_RE.fullmatch(slug) else "mcp-server"


def parse_official_registry_item(entry: dict) -> McpRegistryResult | None:
    srv = entry.get("server")
    if not isinstance(srv, dict) or not srv.get("name"):
        return None
    meta = (entry.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
    if meta.get("status") == "deleted":
        return None

    reg_name = str(srv["name"])
    repo_url = (srv.get("repository") or {}).get("url")
    website_url = srv.get("websiteUrl")
    updated_at = meta.get("updatedAt") or meta.get("publishedAt")

    packages = srv.get("packages") or []
    remotes = srv.get("remotes") or []

    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env_vars: list[McpEnvVarSpec] = []

    if isinstance(packages, list) and packages:
        pkg = next(
            (p for p in packages if isinstance(p, dict) and p.get("registryType") in ("npm", "pypi")),
            packages[0] if isinstance(packages[0], dict) else None,
        )
        if pkg:
            reg_type = str(pkg.get("registryType") or "").lower()
            ident = str(pkg.get("identifier") or "")
            hint = str(pkg.get("runtimeHint") or "")
            if reg_type == "npm" or hint == "npx":
                command = "npx"
                args = ["-y", ident] if ident else []
            elif reg_type == "pypi" or hint == "uvx":
                command = "uvx"
                args = [ident] if ident else []
            elif reg_type == "oci" or hint == "docker":
                command = "docker"
                args = ["run", "-i", "--rm", ident] if ident else []
            elif hint:
                command = hint
                args = [ident] if ident else []

            for ev in pkg.get("environmentVariables") or []:
                if isinstance(ev, dict) and ev.get("name"):
                    env_vars.append(
                        McpEnvVarSpec(
                            name=str(ev["name"]),
                            description=str(ev.get("description") or ""),
                            is_required=bool(ev.get("isRequired", False)),
                            is_secret=bool(ev.get("isSecret", False)),
                            default=str(ev["default"]) if ev.get("default") is not None else None,
                        )
                    )
    elif isinstance(remotes, list) and remotes and isinstance(remotes[0], dict):
        rem = remotes[0]
        url = str(rem.get("url") or "")
        rem_type = str(rem.get("type") or "").lower()
        transport = "sse" if "sse" in rem_type else "http"

    if not command and not url:
        return None

    return McpRegistryResult(
        id=_slug_from_name(reg_name),
        registry_name=reg_name,
        description=str(srv.get("description") or ""),
        source="official",
        version=str(srv.get("version") or ""),
        transport=transport,
        command=command,
        args=args,
        url=url,
        repo_url=str(repo_url) if repo_url else None,
        website_url=str(website_url) if website_url else None,
        updated_at=str(updated_at) if updated_at else None,
        env_vars=env_vars,
    )


def parse_npm_mcp_item(obj: dict) -> McpRegistryResult | None:
    pkg = obj.get("package")
    if not isinstance(pkg, dict) or not pkg.get("name"):
        return None
    name = str(pkg["name"])
    links = pkg.get("links") or {}
    dl = obj.get("downloads") or {}
    return McpRegistryResult(
        id=_slug_from_name(name),
        registry_name=name,
        description=str(pkg.get("description") or ""),
        source="npm",
        version=str(pkg.get("version") or ""),
        transport="stdio",
        command="npx",
        args=["-y", name],
        repo_url=links.get("repository"),
        website_url=links.get("homepage") or links.get("npm"),
        downloads_weekly=int(dl.get("weekly") or 0),
        updated_at=obj.get("updated") or pkg.get("date"),
    )


def build_mcp_router(
    cluster: ClusterState, data_dir: Path, scripts_dir: Path | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api/mcp", tags=["mcp"])
    registry_file = data_dir / "mcp_servers.json"
    mcp_scripts_dir = scripts_dir or (data_dir.parent / "mcp_servers")

    def _load() -> dict[str, McpServerConfig]:
        if not registry_file.exists():
            return {}
        try:
            raw = json.loads(registry_file.read_text(encoding="utf-8"))
            return {
                item["id"]: annotate_mcp_egress(McpServerConfig.model_validate(item))
                for item in raw
                if isinstance(item, dict) and "id" in item
            }
        except (OSError, ValueError):
            return {}

    def _save(items: dict[str, McpServerConfig]) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        for s in items.values():
            annotate_mcp_egress(s)
        payload = json.dumps([s.model_dump() for s in items.values()], indent=2)
        registry_file.write_text(payload, encoding="utf-8")
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                registry_file.chmod(0o600)

    @router.get("/search", response_model=list[McpRegistryResult])
    async def search_mcp_registry(
        q: str,
        source: Literal["official", "npm"] = "official",
        limit: int = 18,
    ) -> list[McpRegistryResult]:
        query = q.strip()
        if len(query) < 2:
            return []
        capped = min(limit, 30)
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                if source == "official":
                    resp = await client.get(
                        "https://registry.modelcontextprotocol.io/v0.1/servers",
                        params={"search": query, "limit": capped},
                    )
                    resp.raise_for_status()
                    raw_servers = resp.json().get("servers") or []
                    seen: dict[str, McpRegistryResult] = {}
                    for entry in raw_servers:
                        parsed = parse_official_registry_item(entry)
                        if parsed and parsed.registry_name not in seen:
                            seen[parsed.registry_name] = parsed
                    # Always prioritize local installable (stdio) MCP servers ahead of remote HTTP/SSE
                    return sorted(
                        seen.values(),
                        key=lambda r: (0 if r.transport == "stdio" else 1, r.registry_name),
                    )

                resp = await client.get(
                    "https://registry.npmjs.org/-/v1/search",
                    params={"text": f"mcp {query}", "size": capped},
                )
                resp.raise_for_status()
                objs = resp.json().get("objects") or []
                results: list[McpRegistryResult] = []
                for obj in objs:
                    parsed = parse_npm_mcp_item(obj)
                    if parsed:
                        results.append(parsed)
                return results
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"MCP search failed: {exc}") from exc

    @router.get("", response_model=list[McpServerConfig])
    async def list_mcp_servers() -> list[McpServerConfig]:
        items = await asyncio.to_thread(_load)
        return sorted(items.values(), key=lambda s: s.id)

    @router.post("", response_model=McpServerConfig)
    async def upsert_mcp_server(srv: McpServerConfig) -> McpServerConfig:
        srv_id = srv.id.strip().lower()
        if not SLUG_RE.fullmatch(srv_id):
            raise HTTPException(status_code=400, detail="Invalid MCP server id")
        srv.id = srv_id
        if srv.transport == "stdio" and not srv.command:
            raise HTTPException(status_code=400, detail="stdio transport requires command")
        if srv.transport in ("http", "sse") and not srv.url:
            raise HTTPException(status_code=400, detail="http/sse transport requires url")

        def _write() -> None:
            items = _load()
            items[srv_id] = srv
            _save(items)

        await asyncio.to_thread(_write)
        return annotate_mcp_egress(srv)

    @router.post("/{server_id}/toggle", response_model=McpServerConfig)
    async def toggle_mcp_server(server_id: str, enabled: bool) -> McpServerConfig:
        def _toggle() -> McpServerConfig:
            items = _load()
            if server_id not in items:
                raise HTTPException(status_code=404, detail="MCP server not found")
            items[server_id].enabled = enabled
            _save(items)
            return items[server_id]

        return await asyncio.to_thread(_toggle)

    @router.delete("/{server_id}")
    async def delete_mcp_server(server_id: str) -> dict:
        def _delete() -> None:
            items = _load()
            if server_id not in items:
                raise HTTPException(status_code=404, detail="MCP server not found")
            del items[server_id]
            _save(items)

        await asyncio.to_thread(_delete)
        return {"ok": True, "deleted": server_id}

    @router.post("/{server_id}/test", response_model=McpTestResult)
    async def test_mcp_server(server_id: str) -> McpTestResult:
        items = await asyncio.to_thread(_load)
        if server_id not in items:
            raise HTTPException(status_code=404, detail="MCP server not found")
        return await probe_mcp_server(items[server_id])

    @router.get("/export/{client}")
    async def export_config(
        client: Literal["opencode", "zed"],
        router_url: str = "http://localhost:8080",
    ) -> dict:
        items = await asyncio.to_thread(_load)
        return generate_client_config(client, router_url, cluster, list(items.values()))

    @router.post("/import-json", response_model=list[McpServerConfig])
    async def import_mcp_json(req: McpImportJsonRequest) -> list[McpServerConfig]:
        try:
            parsed = parse_mcp_json_snippet(req.raw_json, req.default_scope)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        def _merge() -> None:
            items = _load()
            for srv in parsed:
                items[srv.id] = srv
            _save(items)

        await asyncio.to_thread(_merge)
        return parsed

    @router.post("/build", response_model=McpServerConfig)
    async def build_custom_mcp(req: McpBuildRequest) -> McpServerConfig:
        srv_id = req.id.strip().lower()
        if not SLUG_RE.fullmatch(srv_id):
            raise HTTPException(status_code=400, detail="Invalid MCP server id")
        code = (
            req.code
            if req.code and req.code.strip()
            else scaffold_python_mcp_server(req.name.strip() or srv_id, req.tools)
        )

        def _write_local() -> str:
            mcp_scripts_dir.mkdir(parents=True, exist_ok=True)
            target = (mcp_scripts_dir / f"{srv_id}.py").resolve()
            if not target.is_relative_to(mcp_scripts_dir.resolve()):
                raise HTTPException(status_code=403, detail="Path traversal blocked")
            target.write_text(code, encoding="utf-8")
            target.chmod(0o755)
            return str(target)

        local_script_path = await asyncio.to_thread(_write_local)
        effective_path = local_script_path

        # If scoped to a specific node ID, mirror the custom Python MCP script onto that node
        target_node = cluster.nodes.get(req.scope)
        if target_node and target_node.online:
            try:
                async with httpx.AsyncClient(timeout=8.0, headers=cluster.agent_headers()) as http:
                    r = await http.post(
                        f"{target_node.agent_url}/sync/mcp-script",
                        json={"id": srv_id, "code": code},
                    )
                    if r.status_code == 200:
                        effective_path = str(r.json().get("path", effective_path))
            except httpx.HTTPError:
                pass

        srv = McpServerConfig(
            id=srv_id,
            name=req.name.strip() or srv_id,
            description=req.description.strip() or "Custom Python MCP server",
            transport="stdio",
            scope=req.scope,
            enabled=True,
            command="python3",
            args=[effective_path],
            env={},
        )

        def _save_entry() -> None:
            items = _load()
            items[srv_id] = srv
            _save(items)

        await asyncio.to_thread(_save_entry)
        return srv

    @router.get("/{server_id}/script")
    async def get_custom_script(server_id: str) -> dict:
        if not SLUG_RE.fullmatch(server_id):
            raise HTTPException(status_code=400, detail="Invalid server id")
        target = (mcp_scripts_dir / f"{server_id}.py").resolve()
        if not target.is_relative_to(mcp_scripts_dir.resolve()) or not target.exists():
            raise HTTPException(status_code=404, detail="No custom script found for this server")
        code = await asyncio.to_thread(target.read_text, "utf-8")
        return {"id": server_id, "path": str(target), "code": code}

    @router.post("/sync/{node_id}/{client}")
    async def sync_config_to_node(
        node_id: str,
        client: Literal["opencode", "zed"],
        router_url: str = "http://localhost:8080",
    ) -> dict:
        node = cluster.nodes.get(node_id)
        if not node or not node.online:
            raise HTTPException(status_code=503, detail=f"Node '{node_id}' is offline")
        items = await asyncio.to_thread(_load)
        cfg = generate_client_config(client, router_url, cluster, list(items.values()))
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=cluster.agent_headers()) as http:
                r = await http.post(
                    f"{node.agent_url}/sync/client-config",
                    json={"client": client, "config": cfg},
                )
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.text[:300])
                return r.json()
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return router
