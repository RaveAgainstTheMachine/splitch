"""
Splitch Agent Chat Backend:
- SQLite WAL persistence (off-thread via asyncio.to_thread, zero event-loop blocking)
- Native OpenAI streaming (tokens + reasoning_content + native tool_calls)
- Routes every turn through Splitch's real resolve_route() policy + Gaming Mode failover
- Workspace-sandboxed tool execution via target node's splitch-agent + HITL approval gates
- Injects selected Skills (SKILL.md) and live MCP server tools
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.router.mcp_api import annotate_mcp_egress
from backend.router.media_api import MediaService
from backend.router.policy import resolve_route
from backend.router.telemetry import ClusterState
from backend.router.web_search import WEB_TOOLS_SCHEMA, execute_web_fetch, execute_web_search
from backend.shared.models import (
    ApprovalDecision,
    ChatMessageRecord,
    ChatSendRequest,
    CompactResponse,
    ConversationBulkRequest,
    ConversationBulkResponse,
    ConversationCreate,
    ConversationSummary,
    ConversationUpdate,
    McpServerConfig,
    MediaGenerateRequest,
    ScopedExtensionsConfig,
    ScopeRule,
    TraceEntry,
    WorkspaceScopeUpdate,
)
from backend.shared.secrets import resolve_env_map, sign_tool_approval_ed25519
from backend.shared.security import sign_tool_approval
from backend.version import __version__ as VERSION

MEDIA_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate an image locally on a Splitch GPU/media node (sd-server / ComfyUI). "
                "Returns the saved /api/media/files/... URL and metadata."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed visual prompt"},
                    "negative_prompt": {"type": "string", "description": "Optional negative prompt"},
                    "width": {"type": "integer", "description": "Image width in pixels (default 1024)"},
                    "height": {"type": "integer", "description": "Image height in pixels (default 1024)"},
                    "steps": {"type": "integer", "description": "Sampling steps (default 20)"},
                    "model": {"type": "string", "description": "Optional checkpoint/model override"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": (
                "Generate a short video clip locally on a Splitch GPU/media node (ComfyUI / sd-server). "
                "Returns the saved /api/media/files/... URL and metadata."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed video motion prompt"},
                    "negative_prompt": {"type": "string", "description": "Optional negative prompt"},
                    "width": {"type": "integer", "description": "Video width (default 768)"},
                    "height": {"type": "integer", "description": "Video height (default 512)"},
                    "frames": {"type": "integer", "description": "Number of frames (default 16)"},
                    "fps": {"type": "integer", "description": "Frames per second (default 8)"},
                    "model": {"type": "string", "description": "Optional checkpoint/model override"},
                },
                "required": ["prompt"],
            },
        },
    },
]

WORKSPACE_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories inside the active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path (default '.')",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the workspace with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "start_line": {"type": "integer", "description": "1-based start line"},
                    "end_line": {"type": "integer", "description": "1-based inclusive end line"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file inside the workspace (automatically runs post-edit syntax/lint verification).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "content": {"type": "string", "description": "Complete file contents"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Surgically replace an exact target text block inside an existing workspace file (automatically runs post-edit syntax/lint verification).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "target": {"type": "string", "description": "Exact existing string to replace"},
                    "replacement": {"type": "string", "description": "New replacement string"},
                },
                "required": ["path", "target", "replacement"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Search for a text pattern across workspace files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search string"},
                    "path": {"type": "string", "description": "Optional relative subdir"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command inside the workspace (20s timeout).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "cwd": {"type": "string", "description": "Optional relative working dir"},
                },
                "required": ["command"],
            },
        },
    },
]

APPROVAL_REQUIRED_TOOLS = {"write_file", "edit_file", "run_command"}


class ChatStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT 'auto',
                    exec_node TEXT NOT NULL DEFAULT '',
                    workspace_subpath TEXT NOT NULL DEFAULT '',
                    tools_enabled INTEGER NOT NULL DEFAULT 1,
                    web_search_enabled INTEGER NOT NULL DEFAULT 0,
                    auto_approve INTEGER NOT NULL DEFAULT 0,
                    auto_compact INTEGER NOT NULL DEFAULT 1,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    compacted_summary TEXT NOT NULL DEFAULT '',
                    compacted_through_ts REAL NOT NULL DEFAULT 0.0,
                    enabled_skills_json TEXT NOT NULL DEFAULT '[]',
                    enabled_mcps_json TEXT NOT NULL DEFAULT '[]',
                    system_prompt TEXT NOT NULL DEFAULT '',
                    deleted_at REAL DEFAULT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    reasoning TEXT NOT NULL DEFAULT '',
                    images_json TEXT NOT NULL DEFAULT '[]',
                    tool_calls_json TEXT NOT NULL DEFAULT '[]',
                    tool_call_id TEXT,
                    tool_name TEXT,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
                """
            )
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(conversations)").fetchall()
            }
            if "deleted_at" not in cols:
                conn.execute("ALTER TABLE conversations ADD COLUMN deleted_at REAL DEFAULT NULL")
            if "web_search_enabled" not in cols:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN web_search_enabled INTEGER NOT NULL DEFAULT 0"
                )
            if "auto_compact" not in cols:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN auto_compact INTEGER NOT NULL DEFAULT 1"
                )
            if "compacted_summary" not in cols:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN compacted_summary TEXT NOT NULL DEFAULT ''"
                )
            if "compacted_through_ts" not in cols:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN compacted_through_ts REAL NOT NULL DEFAULT 0.0"
                )
            if "pinned" not in cols:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
                )
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                self.db_path.chmod(0o600)

    @staticmethod
    def _row_to_conv(row: sqlite3.Row) -> ConversationSummary:
        return ConversationSummary(
            id=row["id"],
            title=row["title"],
            model=row["model"],
            exec_node=row["exec_node"],
            workspace_subpath=row["workspace_subpath"],
            tools_enabled=bool(row["tools_enabled"]),
            web_search_enabled=bool(row["web_search_enabled"]),
            auto_approve=bool(row["auto_approve"]),
            auto_compact=bool(row["auto_compact"]),
            pinned=bool(row["pinned"]),
            compacted_summary=row["compacted_summary"] or "",
            compacted_through_ts=float(row["compacted_through_ts"] or 0.0),
            enabled_skills=json.loads(row["enabled_skills_json"] or "[]"),
            enabled_mcps=json.loads(row["enabled_mcps_json"] or "[]"),
            system_prompt=row["system_prompt"] or "",
            deleted_at=row["deleted_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_msg(row: sqlite3.Row) -> ChatMessageRecord:
        return ChatMessageRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            role=row["role"],
            content=row["content"] or "",
            reasoning=row["reasoning"] or "",
            images=json.loads(row["images_json"] or "[]"),
            tool_calls=json.loads(row["tool_calls_json"] or "[]"),
            tool_call_id=row["tool_call_id"],
            tool_name=row["tool_name"],
            meta=json.loads(row["meta_json"] or "{}"),
            created_at=row["created_at"],
        )

    def list_conversations(self) -> list[ConversationSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE deleted_at IS NULL "
                "ORDER BY pinned DESC, updated_at DESC LIMIT 100"
            ).fetchall()
            return [self._row_to_conv(r) for r in rows]

    def list_trash(self) -> list[ConversationSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT 100"
            ).fetchall()
            return [self._row_to_conv(r) for r in rows]

    def get_conversation(self, conv_id: str) -> ConversationSummary | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            return self._row_to_conv(row) if row else None

    def create_conversation(self, req: ConversationCreate) -> ConversationSummary:
        now = time.time()
        conv_id = uuid.uuid4().hex[:12]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations
                (id, title, model, exec_node, workspace_subpath, tools_enabled, web_search_enabled,
                 auto_approve, auto_compact, pinned, compacted_summary, compacted_through_ts,
                 enabled_skills_json, enabled_mcps_json, system_prompt, deleted_at,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0.0, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    conv_id,
                    req.title,
                    req.model,
                    req.exec_node,
                    req.workspace_subpath,
                    int(req.tools_enabled),
                    int(req.web_search_enabled),
                    int(req.auto_approve),
                    int(req.auto_compact),
                    int(req.pinned),
                    json.dumps(req.enabled_skills),
                    json.dumps(req.enabled_mcps),
                    req.system_prompt,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            return self._row_to_conv(row)

    def update_conversation(
        self, conv_id: str, patch: ConversationUpdate
    ) -> ConversationSummary | None:
        existing = self.get_conversation(conv_id)
        if not existing:
            return None
        patch_data = patch.model_dump(exclude_none=True)
        updated = existing.model_copy(update=patch_data)
        # Preserve updated_at when only toggling pin so unpinning returns to original chronological order
        new_updated_at = (
            existing.updated_at if set(patch_data.keys()) == {"pinned"} else time.time()
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE conversations SET
                  title = ?, model = ?, exec_node = ?, workspace_subpath = ?,
                  tools_enabled = ?, web_search_enabled = ?, auto_approve = ?, auto_compact = ?,
                  pinned = ?, enabled_skills_json = ?, enabled_mcps_json = ?,
                  system_prompt = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.title,
                    updated.model,
                    updated.exec_node,
                    updated.workspace_subpath,
                    int(updated.tools_enabled),
                    int(updated.web_search_enabled),
                    int(updated.auto_approve),
                    int(updated.auto_compact),
                    int(updated.pinned),
                    json.dumps(updated.enabled_skills),
                    json.dumps(updated.enabled_mcps),
                    updated.system_prompt,
                    new_updated_at,
                    conv_id,
                ),
            )
        updated.updated_at = new_updated_at
        return updated

    def save_compaction(
        self, conv_id: str, summary: str, through_ts: float
    ) -> ConversationSummary | None:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE conversations
                SET compacted_summary = ?, compacted_through_ts = ?, updated_at = ?
                WHERE id = ?
                """,
                (summary, through_ts, now, conv_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            return self._row_to_conv(row) if row else None

    def clear_compaction(self, conv_id: str) -> ConversationSummary | None:
        return self.save_compaction(conv_id, "", 0.0)

    def soft_delete_conversation(self, conv_id: str) -> bool:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE conversations SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now, conv_id),
            )
            return cur.rowcount > 0

    def restore_conversation(self, conv_id: str) -> ConversationSummary | None:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE conversations SET deleted_at = NULL, updated_at = ? WHERE id = ? AND deleted_at IS NOT NULL",
                (now, conv_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            return self._row_to_conv(row) if row else None

    def hard_delete_conversation(self, conv_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            return cur.rowcount > 0

    def empty_trash(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM conversations WHERE deleted_at IS NOT NULL")
            return int(cur.rowcount)

    @staticmethod
    def _clean_ids(conv_ids: list[str]) -> list[str]:
        return list(dict.fromkeys(cid.strip() for cid in conv_ids if cid and cid.strip()))

    def soft_delete_conversations(self, conv_ids: list[str]) -> list[str]:
        ids = self._clean_ids(conv_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM conversations WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                ids,
            ).fetchall()
            affected = [str(r["id"]) for r in rows]
            if affected:
                aff_ph = ",".join("?" for _ in affected)
                conn.execute(
                    f"UPDATE conversations SET deleted_at = ? WHERE id IN ({aff_ph})",
                    [now, *affected],
                )
            return affected

    def restore_conversations(self, conv_ids: list[str]) -> list[str]:
        ids = self._clean_ids(conv_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM conversations WHERE id IN ({placeholders}) AND deleted_at IS NOT NULL",
                ids,
            ).fetchall()
            affected = [str(r["id"]) for r in rows]
            if affected:
                aff_ph = ",".join("?" for _ in affected)
                conn.execute(
                    f"UPDATE conversations SET deleted_at = NULL, updated_at = ? WHERE id IN ({aff_ph})",
                    [now, *affected],
                )
            return affected

    def hard_delete_conversations(self, conv_ids: list[str]) -> list[str]:
        ids = self._clean_ids(conv_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM conversations WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            affected = [str(r["id"]) for r in rows]
            if affected:
                aff_ph = ",".join("?" for _ in affected)
                conn.execute(
                    f"DELETE FROM conversations WHERE id IN ({aff_ph})",
                    affected,
                )
            return affected

    def list_messages(self, conv_id: str) -> list[ChatMessageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conv_id,),
            ).fetchall()
            return [self._row_to_msg(r) for r in rows]

    def add_message(self, msg: ChatMessageRecord) -> ChatMessageRecord:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages
                (id, conversation_id, role, content, reasoning, images_json,
                 tool_calls_json, tool_call_id, tool_name, meta_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.id,
                    msg.conversation_id,
                    msg.role,
                    msg.content,
                    msg.reasoning,
                    json.dumps(msg.images),
                    json.dumps(msg.tool_calls),
                    msg.tool_call_id,
                    msg.tool_name,
                    json.dumps(msg.meta),
                    msg.created_at,
                ),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (msg.created_at, msg.conversation_id),
            )
        return msg

    def trim_from_message(self, message_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT conversation_id, created_at FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if not row:
                return None
            conv_id = str(row["conversation_id"])
            cut_ts = float(row["created_at"])
            conn.execute(
                "DELETE FROM messages WHERE conversation_id = ? AND created_at >= ?",
                (conv_id, cut_ts),
            )
            # Invalidate stale compacted summary if trimming crossed the compaction watermark
            conn.execute(
                """
                UPDATE conversations
                SET compacted_summary = '', compacted_through_ts = 0.0
                WHERE id = ? AND compacted_through_ts >= ?
                """,
                (conv_id, cut_ts),
            )
            return conv_id


async def _call_mcp_tool(
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


async def _fetch_mcp_tools(
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


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def build_chat_router(
    cluster: ClusterState,
    data_dir: Path,
    skills_dir: Path,
    media_service: MediaService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/chat", tags=["chat"])
    store = ChatStore(data_dir / "chats.db")
    scopes_file = data_dir / "workspace_scopes.json"
    pending_approvals: dict[str, asyncio.Future[bool]] = {}

    def _norm_ws_key(subpath: str) -> str:
        return subpath.strip().strip("/") or "."

    def _load_scopes() -> ScopedExtensionsConfig:
        if not scopes_file.exists():
            return ScopedExtensionsConfig()
        try:
            return ScopedExtensionsConfig.model_validate(
                json.loads(scopes_file.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            return ScopedExtensionsConfig()

    def _save_scopes(cfg: ScopedExtensionsConfig) -> ScopedExtensionsConfig:
        data_dir.mkdir(parents=True, exist_ok=True)
        scopes_file.write_text(
            json.dumps(cfg.model_dump(mode="json"), indent=2), encoding="utf-8"
        )
        return cfg

    def _resolve_effective_extensions(
        workspace_subpath: str,
        chat_skills: list[str],
        chat_mcps: list[str],
    ) -> tuple[list[str], list[str]]:
        scopes = _load_scopes()
        ws_rule = scopes.workspaces.get(_norm_ws_key(workspace_subpath), ScopeRule())
        eff_skills = list(
            dict.fromkeys([*scopes.global_scope.skills, *ws_rule.skills, *chat_skills])
        )
        eff_mcps = list(
            dict.fromkeys([*scopes.global_scope.mcps, *ws_rule.mcps, *chat_mcps])
        )
        return eff_skills, eff_mcps

    def _load_selected_skills(skill_ids: list[str]) -> str:
        if not skill_ids or not skills_dir.exists():
            return ""
        blocks: list[str] = []
        for sid in skill_ids:
            md = (skills_dir / sid / "SKILL.md").resolve()
            if md.is_relative_to(skills_dir.resolve()) and md.is_file():
                with contextlib.suppress(OSError):
                    blocks.append(f"<skill id=\"{sid}\">\n{md.read_text(encoding='utf-8')[:8000]}\n</skill>")
        return "\n\n".join(blocks)

    def _load_mcp_configs(mcp_ids: list[str]) -> dict[str, McpServerConfig]:
        reg = data_dir / "mcp_servers.json"
        if not mcp_ids or not reg.exists():
            return {}
        try:
            raw = json.loads(reg.read_text(encoding="utf-8"))
            wanted = set(mcp_ids)
            return {
                item["id"]: annotate_mcp_egress(McpServerConfig.model_validate(item))
                for item in raw
                if isinstance(item, dict) and item.get("id") in wanted and item.get("enabled", True)
            }
        except (OSError, ValueError):
            return {}

    @router.get("/scopes", response_model=ScopedExtensionsConfig)
    async def get_scopes() -> ScopedExtensionsConfig:
        return await asyncio.to_thread(_load_scopes)

    @router.put("/scopes/global", response_model=ScopedExtensionsConfig)
    async def update_global_scope(rule: ScopeRule) -> ScopedExtensionsConfig:
        def _upd() -> ScopedExtensionsConfig:
            cfg = _load_scopes()
            cfg.global_scope = ScopeRule(
                skills=list(dict.fromkeys(rule.skills)),
                mcps=list(dict.fromkeys(rule.mcps)),
            )
            return _save_scopes(cfg)

        return await asyncio.to_thread(_upd)

    @router.put("/scopes/workspace", response_model=ScopedExtensionsConfig)
    async def update_workspace_scope(req: WorkspaceScopeUpdate) -> ScopedExtensionsConfig:
        def _upd() -> ScopedExtensionsConfig:
            cfg = _load_scopes()
            key = _norm_ws_key(req.workspace)
            skills = list(dict.fromkeys(req.skills))
            mcps = list(dict.fromkeys(req.mcps))
            if not skills and not mcps:
                cfg.workspaces.pop(key, None)
            else:
                cfg.workspaces[key] = ScopeRule(skills=skills, mcps=mcps)
            return _save_scopes(cfg)

        return await asyncio.to_thread(_upd)

    @router.get("/conversations", response_model=list[ConversationSummary])
    async def list_conversations() -> list[ConversationSummary]:
        return await asyncio.to_thread(store.list_conversations)

    @router.post("/conversations", response_model=ConversationSummary)
    async def create_conversation(req: ConversationCreate) -> ConversationSummary:
        return await asyncio.to_thread(store.create_conversation, req)

    @router.post("/conversations/bulk-delete", response_model=ConversationBulkResponse)
    async def bulk_delete_conversations(
        req: ConversationBulkRequest,
    ) -> ConversationBulkResponse:
        affected = await asyncio.to_thread(store.soft_delete_conversations, req.ids)
        return ConversationBulkResponse(ok=True, affected_count=len(affected), ids=affected)

    @router.post("/conversations/bulk-restore", response_model=ConversationBulkResponse)
    async def bulk_restore_conversations(
        req: ConversationBulkRequest,
    ) -> ConversationBulkResponse:
        affected = await asyncio.to_thread(store.restore_conversations, req.ids)
        return ConversationBulkResponse(ok=True, affected_count=len(affected), ids=affected)

    @router.post(
        "/conversations/bulk-permanent-delete", response_model=ConversationBulkResponse
    )
    async def bulk_permanent_delete_conversations(
        req: ConversationBulkRequest,
    ) -> ConversationBulkResponse:
        affected = await asyncio.to_thread(store.hard_delete_conversations, req.ids)
        return ConversationBulkResponse(ok=True, affected_count=len(affected), ids=affected)

    @router.patch("/conversations/{conv_id}", response_model=ConversationSummary)
    async def update_conversation(
        conv_id: str, patch: ConversationUpdate
    ) -> ConversationSummary:
        res = await asyncio.to_thread(store.update_conversation, conv_id, patch)
        if not res:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return res

    @router.delete("/conversations/{conv_id}")
    async def delete_conversation(conv_id: str) -> dict:
        deleted = await asyncio.to_thread(store.soft_delete_conversation, conv_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"ok": True, "trashed": conv_id}

    @router.get("/trash", response_model=list[ConversationSummary])
    async def list_trash() -> list[ConversationSummary]:
        return await asyncio.to_thread(store.list_trash)

    @router.post("/conversations/{conv_id}/restore", response_model=ConversationSummary)
    async def restore_conversation(conv_id: str) -> ConversationSummary:
        restored = await asyncio.to_thread(store.restore_conversation, conv_id)
        if not restored:
            raise HTTPException(status_code=404, detail="Conversation not found in trash")
        return restored

    @router.delete("/conversations/{conv_id}/permanent")
    async def permanent_delete_conversation(conv_id: str) -> dict:
        deleted = await asyncio.to_thread(store.hard_delete_conversation, conv_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"ok": True, "deleted": conv_id}

    @router.delete("/trash")
    async def empty_trash() -> dict:
        count = await asyncio.to_thread(store.empty_trash)
        return {"ok": True, "deleted_count": count}

    @router.get("/conversations/{conv_id}/messages", response_model=list[ChatMessageRecord])
    async def get_messages(conv_id: str) -> list[ChatMessageRecord]:
        if not await asyncio.to_thread(store.get_conversation, conv_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return await asyncio.to_thread(store.list_messages, conv_id)

    @router.delete("/messages/{message_id}")
    async def trim_message(message_id: str) -> dict:
        conv_id = await asyncio.to_thread(store.trim_from_message, message_id)
        if not conv_id:
            raise HTTPException(status_code=404, detail="Message not found")
        return {"ok": True, "conversation_id": conv_id}

    @router.post("/approvals/{approval_id}")
    async def resolve_approval(approval_id: str, decision: ApprovalDecision) -> dict:
        fut = pending_approvals.get(approval_id)
        if fut is None or fut.done():
            raise HTTPException(status_code=404, detail="Approval request expired or not found")
        fut.set_result(decision.approved)
        return {"ok": True, "approved": decision.approved}

    def _estimate_msgs_tokens(
        summary: str, sys_text: str, msgs: list[ChatMessageRecord]
    ) -> int:
        chars = len(sys_text) + len(summary)
        for m in msgs:
            chars += len(m.content or "") + len(m.reasoning or "") + len(json.dumps(m.tool_calls))
        return max(1, chars // 4)

    def _split_for_compaction(
        uncompacted: list[ChatMessageRecord], keep_recent: int = 6
    ) -> tuple[list[ChatMessageRecord], list[ChatMessageRecord]]:
        if len(uncompacted) <= keep_recent:
            return [], uncompacted
        split_idx = len(uncompacted) - keep_recent
        # Prefer starting kept tail on a user message so tool_calls/results never split mid-turn
        while split_idx > 2 and uncompacted[split_idx].role != "user":
            split_idx -= 1
        if split_idx < 2:
            return [], uncompacted
        return uncompacted[:split_idx], uncompacted[split_idx:]

    async def _run_compaction(
        conv: ConversationSummary, keep_recent: int = 6
    ) -> CompactResponse:
        all_msgs = await asyncio.to_thread(store.list_messages, conv.id)
        uncompacted = [m for m in all_msgs if m.created_at > conv.compacted_through_ts]
        older, recent = _split_for_compaction(uncompacted, keep_recent=keep_recent)
        est_before = _estimate_msgs_tokens(conv.compacted_summary, conv.system_prompt, uncompacted)
        if not older:
            return CompactResponse(
                ok=True,
                compacted=False,
                compacted_messages=0,
                kept_messages=len(uncompacted),
                summary=conv.compacted_summary,
                compacted_through_ts=conv.compacted_through_ts,
                estimated_tokens_before=est_before,
                estimated_tokens_after=est_before,
                detail="Conversation is already concise enough — fewer than 2 older turns to compact.",
            )

        transcript_lines: list[str] = []
        if conv.compacted_summary.strip():
            transcript_lines.append(f"[Previous Summary]\n{conv.compacted_summary.strip()}\n")
        for m in older:
            role_tag = m.role.upper() if not m.tool_name else f"TOOL({m.tool_name})"
            body = (m.content or "").strip()
            if len(body) > 2200:
                body = body[:2200] + " ...[truncated]"
            transcript_lines.append(f"{role_tag}: {body}")

        compact_req = {
            "model": "fast" if "fast" in cluster.aliases else conv.model,
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 900,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Condense the conversation history into a crisp, high-signal engineering summary. "
                        "Preserve: user goals, technical decisions, file paths, symbol names, tool results, "
                        "errors fixed, and unresolved next steps. Omit pleasantries and verbatim large outputs. "
                        "Use concise markdown bullets."
                    ),
                },
                {
                    "role": "user",
                    "content": "\n\n".join(transcript_lines)[:32_000],
                },
            ],
        }

        decision, skip_reasons = resolve_route(
            endpoint="/v1/chat/completions",
            requested_model=str(compact_req["model"]),
            body=compact_req,
            nodes=cluster.nodes,
            aliases=cluster.aliases,
            control=cluster.control,
            guards=cluster.config.guards,
            heavy_char_threshold=cluster.config.router.auto_heavy_char_threshold,
        )
        if decision is None and compact_req["model"] != conv.model:
            compact_req["model"] = conv.model
            decision, skip_reasons = resolve_route(
                endpoint="/v1/chat/completions",
                requested_model=conv.model,
                body=compact_req,
                nodes=cluster.nodes,
                aliases=cluster.aliases,
                control=cluster.control,
                guards=cluster.config.guards,
                heavy_char_threshold=cluster.config.router.auto_heavy_char_threshold,
            )
        if decision is None:
            raise HTTPException(
                status_code=503, detail=f"No eligible node for compaction: {skip_reasons}"
            )

        compact_req["model"] = decision.target_model
        summary_text = ""
        try:
            resp = await cluster.proxy_client.post(
                f"{decision.node.engine_url}/v1/chat/completions",
                json=compact_req,
            )
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Compaction upstream HTTP {resp.status_code}"
                )
            data = resp.json()
            choices = data.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg_obj = choices[0].get("message") or {}
                summary_text = str(msg_obj.get("content") or "").strip()
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"Compaction failed: {exc}") from exc

        if not summary_text:
            # Deterministic fallback summary if model emitted empty content
            summary_text = "\n".join(
                f"- **{m.role}**: {(m.content or '')[:180].replace(chr(10), ' ')}" for m in older[:12]
            )

        watermark = older[-1].created_at
        await asyncio.to_thread(store.save_compaction, conv.id, summary_text, watermark)
        est_after = _estimate_msgs_tokens(summary_text, conv.system_prompt, recent)
        return CompactResponse(
            ok=True,
            compacted=True,
            compacted_messages=len(older),
            kept_messages=len(recent),
            summary=summary_text,
            compacted_through_ts=watermark,
            estimated_tokens_before=est_before,
            estimated_tokens_after=est_after,
        )

    @router.post("/conversations/{conv_id}/compact", response_model=CompactResponse)
    async def compact_conversation(conv_id: str, keep_recent: int = 4) -> CompactResponse:
        conv = await asyncio.to_thread(store.get_conversation, conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return await _run_compaction(conv, keep_recent=max(2, min(keep_recent, 12)))

    @router.delete("/conversations/{conv_id}/compact", response_model=ConversationSummary)
    async def clear_conversation_compaction(conv_id: str) -> ConversationSummary:
        cleared = await asyncio.to_thread(store.clear_compaction, conv_id)
        if not cleared:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return cleared

    @router.post("/conversations/{conv_id}/send")
    async def send_chat(conv_id: str, req: ChatSendRequest) -> StreamingResponse:
        conv = await asyncio.to_thread(store.get_conversation, conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

        user_msg = ChatMessageRecord(
            id=uuid.uuid4().hex[:12],
            conversation_id=conv_id,
            role="user",
            content=req.content,
            images=req.images,
            created_at=time.time(),
        )
        await asyncio.to_thread(store.add_message, user_msg)

        # Auto-title new conversations from first user message
        if conv.title == "New Chat" and req.content.strip():
            short_title = req.content.strip().splitlines()[0][:54]
            await asyncio.to_thread(
                store.update_conversation, conv_id, ConversationUpdate(title=short_title)
            )

        async def _event_stream() -> AsyncGenerator[bytes, None]:
            yield _sse("user_saved", user_msg.model_dump())

            # Assemble system prompt + injected skills + MCP tools
            sys_parts = [
                "You are Splitch, a precise local AI engineering assistant.",
                "Be concise, accurate, and use tools directly when inspecting or modifying workspace files.",
            ]
            if conv.system_prompt.strip():
                sys_parts.append(conv.system_prompt.strip())

            eff_skills, eff_mcps = await asyncio.to_thread(
                _resolve_effective_extensions,
                conv.workspace_subpath,
                conv.enabled_skills,
                conv.enabled_mcps,
            )

            skills_text = await asyncio.to_thread(_load_selected_skills, eff_skills)
            if skills_text:
                sys_parts.append(f"Active Skills:\n{skills_text}")

            if conv.web_search_enabled:
                sys_parts.append(
                    "Web search is enabled. Use `web_search` for current facts or documentation, "
                    "optionally call `web_fetch` on a result URL for full details, and cite source URLs."
                )

            # Auto-compact older turns non-destructively when active context crosses 80%
            active_conv = conv
            all_history = await asyncio.to_thread(store.list_messages, conv_id)
            uncompacted_msgs = [
                m for m in all_history if m.created_at > active_conv.compacted_through_ts
            ]
            ctx_budget = next(
                (
                    n.telemetry.context_length
                    for n in cluster.nodes.values()
                    if n.online and n.telemetry and n.telemetry.context_length
                ),
                32768,
            )
            est_active_tokens = _estimate_msgs_tokens(
                active_conv.compacted_summary, "\n\n".join(sys_parts), uncompacted_msgs
            )
            if (
                active_conv.auto_compact
                and ctx_budget > 0
                and (est_active_tokens / ctx_budget) >= 0.80
                and len(uncompacted_msgs) > 7
            ):
                yield _sse(
                    "compacting",
                    {"estimated_tokens": est_active_tokens, "context_window": ctx_budget},
                )
                try:
                    c_res = await _run_compaction(active_conv, keep_recent=6)
                    if c_res.compacted:
                        refreshed = await asyncio.to_thread(store.get_conversation, conv_id)
                        if refreshed:
                            active_conv = refreshed
                        yield _sse("compacted", c_res.model_dump())
                except HTTPException:
                    pass

            if active_conv.compacted_summary.strip():
                sys_parts.append(
                    f"<conversation_summary>\n{active_conv.compacted_summary.strip()}\n</conversation_summary>"
                )

            mcp_map = await asyncio.to_thread(_load_mcp_configs, eff_mcps)
            tools_payload: list[dict[str, Any]] = []
            if active_conv.tools_enabled:
                tools_payload.extend(WORKSPACE_TOOLS_SCHEMA)
                for srv in mcp_map.values():
                    tools_payload.extend(
                        await _fetch_mcp_tools(
                            srv, active_conv.workspace_subpath, vault=cluster.vault
                        )
                    )
            if active_conv.web_search_enabled:
                tools_payload.extend(WEB_TOOLS_SCHEMA)
            if media_service is not None and media_service.has_healthy_backend():
                tools_payload.extend(MEDIA_TOOLS_SCHEMA)

            history_records = [
                m for m in all_history if m.created_at > active_conv.compacted_through_ts
            ]
            openai_msgs: list[dict[str, Any]] = [
                {"role": "system", "content": "\n\n".join(sys_parts)}
            ]
            for m in history_records:
                if m.role == "user":
                    if m.images:
                        parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
                        for img in m.images:
                            parts.append({"type": "image_url", "image_url": {"url": img}})
                        openai_msgs.append({"role": "user", "content": parts})
                    else:
                        openai_msgs.append({"role": "user", "content": m.content})
                elif m.role == "assistant":
                    entry: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                    if m.tool_calls:
                        entry["tool_calls"] = m.tool_calls
                    openai_msgs.append(entry)
                elif m.role == "tool":
                    openai_msgs.append(
                        {
                            "role": "tool",
                            "tool_call_id": m.tool_call_id or "call_0",
                            "name": m.tool_name or "tool",
                            "content": m.content,
                        }
                    )

            max_rounds = 8
            for _round in range(max_rounds):
                req_body: dict[str, Any] = {
                    "model": conv.model,
                    "messages": openai_msgs,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if req.temperature is not None:
                    req_body["temperature"] = req.temperature
                if tools_payload:
                    req_body["tools"] = tools_payload

                decision, skip_reasons = resolve_route(
                    endpoint="/v1/chat/completions",
                    requested_model=conv.model,
                    body=req_body,
                    nodes=cluster.nodes,
                    aliases=cluster.aliases,
                    control=cluster.control,
                    guards=cluster.config.guards,
                    heavy_char_threshold=cluster.config.router.auto_heavy_char_threshold,
                )
                if decision is None:
                    yield _sse(
                        "error",
                        {"message": f"No eligible Splitch node available: {skip_reasons}"},
                    )
                    return

                req_body["model"] = decision.target_model
                yield _sse(
                    "meta",
                    {
                        "node": decision.node.node_id,
                        "model": decision.target_model,
                        "fallback_reason": decision.fallback_reason,
                    },
                )

                started = time.perf_counter()
                ttft_ms: float | None = None
                content_buf: list[str] = []
                reasoning_buf: list[str] = []
                tool_calls_acc: dict[int, dict[str, Any]] = {}
                approx_tokens = 0
                prompt_tokens_real: int | None = None
                completion_tokens_real: int | None = None
                total_tokens_real: int | None = None
                status_code = 200

                decision.node.in_flight += 1
                try:
                    async with cluster.proxy_client.stream(
                        "POST",
                        f"{decision.node.engine_url}/v1/chat/completions",
                        json=req_body,
                    ) as upstream:
                        status_code = upstream.status_code
                        if status_code >= 400:
                            err_body = (await upstream.aread()).decode("utf-8", errors="replace")[:400]
                            yield _sse("error", {"message": f"Upstream HTTP {status_code}: {err_body}"})
                            return

                        async for line in upstream.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            raw_data = line[6:].strip()
                            if not raw_data or raw_data == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(raw_data)
                            except ValueError:
                                continue

                            usage = chunk.get("usage")
                            if isinstance(usage, dict):
                                if isinstance(usage.get("prompt_tokens"), int):
                                    prompt_tokens_real = usage["prompt_tokens"]
                                if isinstance(usage.get("completion_tokens"), int):
                                    completion_tokens_real = usage["completion_tokens"]
                                if isinstance(usage.get("total_tokens"), int):
                                    total_tokens_real = usage["total_tokens"]

                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}

                            rc = delta.get("reasoning_content")
                            if rc:
                                if ttft_ms is None:
                                    ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                                approx_tokens += 1
                                reasoning_buf.append(rc)
                                yield _sse("reasoning", {"text": rc})

                            txt = delta.get("content")
                            if txt:
                                if ttft_ms is None:
                                    ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                                approx_tokens += 1
                                content_buf.append(txt)
                                yield _sse("token", {"text": txt})

                            for tc in delta.get("tool_calls") or []:
                                if not isinstance(tc, dict):
                                    continue
                                idx = int(tc.get("index", 0))
                                slot = tool_calls_acc.setdefault(
                                    idx,
                                    {
                                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    },
                                )
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["function"]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    slot["function"]["arguments"] += fn["arguments"]
                except httpx.HTTPError as exc:
                    yield _sse("error", {"message": f"Stream error: {exc}"})
                    return
                finally:
                    decision.node.in_flight = max(0, decision.node.in_flight - 1)

                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
                final_comp_tokens = (
                    completion_tokens_real if completion_tokens_real is not None else approx_tokens
                )
                est_prompt_tokens = max(1, len(json.dumps(openai_msgs)) // 4)
                final_prompt_tokens = (
                    prompt_tokens_real if prompt_tokens_real is not None else est_prompt_tokens
                )
                final_total_tokens = (
                    total_tokens_real
                    if total_tokens_real is not None
                    else (final_prompt_tokens + final_comp_tokens)
                )
                ctx_window = (
                    decision.node.telemetry.context_length
                    if decision.node.telemetry and decision.node.telemetry.context_length
                    else 32768
                )
                gen_s = max(0.001, (elapsed_ms - (ttft_ms or 0.0)) / 1000.0)
                tps = round(final_comp_tokens / gen_s, 1) if final_comp_tokens > 0 else None

                asyncio.create_task(
                    cluster.record_trace(
                        TraceEntry(
                            id=uuid.uuid4().hex[:10],
                            timestamp=time.time(),
                            endpoint="/api/chat/send",
                            requested_model=conv.model,
                            resolved_node=decision.node.node_id,
                            resolved_model=decision.target_model,
                            fallback_reason=decision.fallback_reason,
                            status_code=status_code,
                            stream=True,
                            ttft_ms=ttft_ms,
                            duration_ms=elapsed_ms,
                            completion_tokens=final_comp_tokens,
                            tokens_per_sec=tps,
                        )
                    )
                )

                assembled_tools = [tool_calls_acc[k] for k in sorted(tool_calls_acc.keys())]
                assistant_meta = {
                    "node": decision.node.node_id,
                    "model": decision.target_model,
                    "fallback_reason": decision.fallback_reason,
                    "ttft_ms": ttft_ms,
                    "duration_ms": elapsed_ms,
                    "tokens_per_sec": tps,
                    "prompt_tokens": final_prompt_tokens,
                    "completion_tokens": final_comp_tokens,
                    "total_tokens": final_total_tokens,
                    "context_window": ctx_window,
                }
                asst_msg = ChatMessageRecord(
                    id=uuid.uuid4().hex[:12],
                    conversation_id=conv_id,
                    role="assistant",
                    content="".join(content_buf),
                    reasoning="".join(reasoning_buf),
                    tool_calls=assembled_tools,
                    meta=assistant_meta,
                    created_at=time.time(),
                )
                await asyncio.to_thread(store.add_message, asst_msg)

                if not assembled_tools:
                    yield _sse("done", {"message": asst_msg.model_dump()})
                    return

                # Execute native tool calls and loop back to model
                openai_msgs.append(
                    {
                        "role": "assistant",
                        "content": asst_msg.content or None,
                        "tool_calls": assembled_tools,
                    }
                )

                for tc in assembled_tools:
                    t_id = tc["id"]
                    t_name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    try:
                        parsed_args = json.loads(raw_args) if raw_args else {}
                    except ValueError:
                        parsed_args = {}

                    yield _sse("tool_call", {"id": t_id, "name": t_name, "arguments": parsed_args})

                    is_external_mcp = False
                    external_mcp_target: str | None = None
                    if t_name.startswith("mcp__"):
                        _m_parts = t_name.split("__", 2)
                        _m_srv = mcp_map.get(_m_parts[1] if len(_m_parts) == 3 else "")
                        if _m_srv and _m_srv.egress != "local":
                            is_external_mcp = True
                            external_mcp_target = _m_srv.egress_target or _m_srv.url or _m_srv.id

                    needs_approval = (
                        t_name in APPROVAL_REQUIRED_TOOLS or is_external_mcp
                    ) and (not conv.auto_approve)
                    approved = True
                    if needs_approval:
                        approval_id = uuid.uuid4().hex[:10]
                        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                        pending_approvals[approval_id] = fut
                        yield _sse(
                            "approval_required",
                            {
                                "approval_id": approval_id,
                                "tool_call_id": t_id,
                                "name": t_name,
                                "arguments": parsed_args,
                                "exec_node": (
                                    f"EXTERNAL MCP ({external_mcp_target})"
                                    if is_external_mcp
                                    else conv.exec_node
                                ),
                            },
                        )
                        try:
                            approved = await asyncio.wait_for(fut, timeout=300.0)
                        except TimeoutError:
                            approved = False
                        finally:
                            pending_approvals.pop(approval_id, None)

                    tool_diff: str | None = None
                    tool_verification: dict | None = None
                    tool_media_url: str | None = None
                    tool_media_kind: str | None = None
                    if not approved:
                        tool_output = "Tool execution denied by user."
                        tool_ok = False
                    elif t_name == "web_search":
                        tool_ok, tool_output = await execute_web_search(
                            str(parsed_args.get("query", "")),
                            int(parsed_args.get("max_results") or 6),
                        )
                    elif t_name == "web_fetch":
                        tool_ok, tool_output = await execute_web_fetch(
                            str(parsed_args.get("url", ""))
                        )
                    elif t_name in ("generate_image", "generate_video"):
                        if media_service is None:
                            tool_ok = False
                            tool_output = "Media generation service is not configured."
                        else:
                            is_vid = t_name == "generate_video"
                            try:
                                m_job = await media_service.generate_sync(
                                    MediaGenerateRequest(
                                        kind="video" if is_vid else "image",
                                        prompt=str(parsed_args.get("prompt", "")),
                                        negative_prompt=str(parsed_args.get("negative_prompt", "")),
                                        model=str(parsed_args.get("model", "")),
                                        width=int(parsed_args.get("width") or (768 if is_vid else 1024)),
                                        height=int(parsed_args.get("height") or (512 if is_vid else 1024)),
                                        steps=int(parsed_args.get("steps") or 20),
                                        frames=int(parsed_args.get("frames") or 16),
                                        fps=int(parsed_args.get("fps") or 8),
                                    )
                                )
                                if m_job.status == "completed" and m_job.file_url:
                                    tool_ok = True
                                    tool_media_url = m_job.file_url
                                    tool_media_kind = m_job.kind
                                    tool_output = json.dumps(
                                        {
                                            "ok": True,
                                            "kind": m_job.kind,
                                            "url": m_job.file_url,
                                            "node": m_job.node_id,
                                            "engine": m_job.media_engine,
                                            "model": m_job.model,
                                            "seed": m_job.seed,
                                            "size": f"{m_job.width}x{m_job.height}",
                                            "duration_ms": m_job.duration_ms,
                                        }
                                    )
                                else:
                                    tool_ok = False
                                    tool_output = f"Media generation failed: {m_job.error or 'unknown error'}"
                            except HTTPException as exc:
                                tool_ok = False
                                tool_output = f"Media generation unavailable: {exc.detail}"
                            except (httpx.HTTPError, OSError, RuntimeError, ValueError, TypeError) as exc:
                                tool_ok = False
                                tool_output = f"Media generation error: {exc}"
                    elif t_name.startswith("mcp__"):
                        parts = t_name.split("__", 2)
                        srv_id = parts[1] if len(parts) == 3 else ""
                        real_tool = parts[2] if len(parts) == 3 else t_name
                        srv_cfg = mcp_map.get(srv_id)
                        if not srv_cfg:
                            tool_output = f"MCP server '{srv_id}' not enabled for this chat."
                            tool_ok = False
                        else:
                            tool_output = await _call_mcp_tool(
                                srv_cfg,
                                real_tool,
                                parsed_args,
                                active_conv.workspace_subpath,
                                vault=cluster.vault,
                            )
                            tool_ok = not tool_output.startswith("MCP error:")
                    else:
                        exec_node = cluster.nodes.get(conv.exec_node) if conv.exec_node else None
                        if exec_node is None or not exec_node.online:
                            exec_node = next(
                                (
                                    n
                                    for n in cluster.nodes.values()
                                    if n.online and n.telemetry and n.telemetry.tools_enabled
                                ),
                                next((n for n in cluster.nodes.values() if n.online), None),
                            )
                        if not exec_node or not exec_node.online:
                            tool_output = f"Execution node '{conv.exec_node or 'auto'}' is offline."
                            tool_ok = False
                        else:
                            try:
                                tool_headers: dict[str, str] = {
                                    **cluster.agent_headers("POST", "/tools/execute"),
                                    "X-Splitch-Router-Approved": "1",
                                }
                                if cluster.router_private_key is not None:
                                    ts_hdr, sig_hdr = sign_tool_approval_ed25519(
                                        cluster.router_private_key,
                                        t_name,
                                        parsed_args,
                                        conv.workspace_subpath,
                                    )
                                    tool_headers["X-Splitch-Approval-Ts"] = ts_hdr
                                    tool_headers["X-Splitch-Approval-Sig"] = sig_hdr
                                elif cluster.agent_token:
                                    ts_hdr, sig_hdr = sign_tool_approval(
                                        cluster.agent_token,
                                        t_name,
                                        parsed_args,
                                        conv.workspace_subpath,
                                    )
                                    tool_headers["X-Splitch-Approval-Ts"] = ts_hdr
                                    tool_headers["X-Splitch-Approval-Sig"] = sig_hdr
                                async with httpx.AsyncClient(
                                    timeout=25.0,
                                    headers=tool_headers,
                                ) as http:
                                    r = await http.post(
                                        f"{exec_node.agent_url}/tools/execute",
                                        json={
                                            "tool": t_name,
                                            "arguments": parsed_args,
                                            "workspace_subpath": conv.workspace_subpath,
                                        },
                                    )
                                    if r.status_code == 200:
                                        body = r.json()
                                        tool_ok = bool(body.get("ok", False))
                                        tool_output = str(body.get("output", ""))
                                        tool_diff = body.get("diff")
                                        tool_verification = body.get("verification")
                                    else:
                                        tool_ok = False
                                        tool_output = f"HTTP {r.status_code}: {r.text[:300]}"
                            except httpx.HTTPError as exc:
                                tool_ok = False
                                tool_output = f"Agent tool call failed: {exc}"

                    tool_msg = ChatMessageRecord(
                        id=uuid.uuid4().hex[:12],
                        conversation_id=conv_id,
                        role="tool",
                        content=tool_output,
                        tool_call_id=t_id,
                        tool_name=t_name,
                        meta={
                            "ok": tool_ok,
                            "diff": tool_diff,
                            "verification": tool_verification,
                            "media_url": tool_media_url,
                            "media_kind": tool_media_kind,
                        },
                        created_at=time.time(),
                    )
                    await asyncio.to_thread(store.add_message, tool_msg)
                    openai_msgs.append(
                        {
                            "role": "tool",
                            "tool_call_id": t_id,
                            "name": t_name,
                            "content": tool_output,
                        }
                    )
                    yield _sse(
                        "tool_result",
                        {
                            "id": t_id,
                            "name": t_name,
                            "ok": tool_ok,
                            "output": tool_output,
                            "diff": tool_diff,
                            "verification": tool_verification,
                            "media_url": tool_media_url,
                            "media_kind": tool_media_kind,
                        },
                    )

            yield _sse("done", {"message": asst_msg.model_dump(), "max_rounds_reached": True})

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
