"""SQLite WAL persistence (`ChatStore`) and native OpenAI tool schemas for Agent Chat."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from backend.shared.models import (
    ChatMessageRecord,
    ConversationCreate,
    ConversationSummary,
    ConversationUpdate,
)

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
            "description": "Run a shell command inside the workspace (20s timeout; OS-sandboxed when available).",
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
            conn.execute(
                """
                UPDATE conversations
                SET compacted_summary = '', compacted_through_ts = 0.0
                WHERE id = ? AND compacted_through_ts >= ?
                """,
                (conv_id, cut_ts),
            )
            return conv_id
