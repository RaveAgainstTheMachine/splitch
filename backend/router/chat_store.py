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
    SwarmAgentRecord,
    SwarmRunRecord,
    SwarmWorkerMessage,
)

MEDIA_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate an image locally on a Splitch GPU/media node (sd-server / ComfyUI / Jeveration). "
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
                    "model": {"type": "string", "description": "Optional checkpoint/model override (or 'jeveration')"},
                    "engine": {
                        "type": "string",
                        "enum": ["sd-server", "comfyui", "jeveration"],
                        "description": "Optional media engine override ('jeveration' for 1-pass 512x512)",
                    },
                    "jeveration_task": {
                        "type": "string",
                        "enum": ["t2i", "hed", "depth", "colorize", "anime", "ghibli", "watercolor", "dense"],
                        "description": "Jeveration task mode when engine='jeveration'",
                    },
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

MCP_DISCOVERY_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "suggest_mcp",
            "description": (
                "Search installed (including inactive) MCP servers and the live MCP Registry when a task "
                "would benefit from an external or specialized MCP tool. Renders an interactive card with "
                "Enable in Chat / Open in MCP Tab actions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Capability or server keyword (e.g. 'postgres', 'github', 'playwright')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Concise explanation of how this MCP server helps the current task",
                    },
                },
                "required": ["query"],
            },
        },
    }
]

SKILL_DISCOVERY_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "suggest_skill",
            "description": (
                "Search installed (including inactive) SKILL.md workflows and the skills.sh registry when "
                "a task would benefit from a specialized engineering skill. Renders an interactive card with "
                "Enable in Chat / Install in Skills Tab actions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Workflow or technology keyword (e.g. 'nextjs', 'playwright', 'postgres')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Concise explanation of how this skill helps the current task",
                    },
                },
                "required": ["query"],
            },
        },
    }
]

SWARM_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": (
                "Spawn an asynchronous worker agent in the active swarm run to execute a focused "
                "subtask in parallel (e.g. across cluster nodes). Returns the worker's agent_id immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "Short worker role label, e.g. 'Researcher', 'Coder', 'Verifier'",
                    },
                    "task": {
                        "type": "string",
                        "description": "Clear, self-contained task prompt and expected output for the worker",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model or alias override (e.g. 'auto', 'fast', 'heavy'). Defaults to 'auto'.",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Optional target node ID (e.g. 'pc', 'mac') for tool/inference affinity.",
                    },
                    "readonly": {
                        "type": "boolean",
                        "description": "If true (default), worker cannot call write_file/edit_file/run_command.",
                    },
                },
                "required": ["role", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_agents",
            "description": (
                "Wait for spawned worker agents in the current swarm run to finish and return their results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of agent_ids to wait on; omit to wait for all active workers in the run.",
                    },
                    "timeout_s": {
                        "type": "number",
                        "description": "Maximum seconds to wait (default 180).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": "List all worker agents in the current swarm run with their status, node, and step progress.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_agent",
            "description": "Cancel a queued or running worker agent in the current swarm run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "ID of the worker agent to cancel"}
                },
                "required": ["agent_id"],
            },
        },
    },
]


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
                    swarm_enabled INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS swarm_runs (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_swarm_runs_conv ON swarm_runs(conversation_id, created_at);
                CREATE TABLE IF NOT EXISTS swarm_agents (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    task TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT 'auto',
                    node_id TEXT NOT NULL DEFAULT '',
                    readonly INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'queued',
                    round INTEGER NOT NULL DEFAULT 0,
                    max_rounds INTEGER NOT NULL DEFAULT 8,
                    phase TEXT NOT NULL DEFAULT 'queued',
                    active_tool TEXT,
                    result TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    tokens_per_sec REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES swarm_runs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_swarm_agents_run ON swarm_agents(run_id, created_at);
                CREATE TABLE IF NOT EXISTS swarm_agent_messages (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    reasoning TEXT NOT NULL DEFAULT '',
                    tool_calls_json TEXT NOT NULL DEFAULT '[]',
                    tool_call_id TEXT,
                    tool_name TEXT,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES swarm_agents(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_swarm_msgs_agent ON swarm_agent_messages(agent_id, created_at);
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
            if "swarm_enabled" not in cols:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN swarm_enabled INTEGER NOT NULL DEFAULT 0"
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
            swarm_enabled=bool(row["swarm_enabled"]),
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
                 swarm_enabled, auto_approve, auto_compact, pinned, compacted_summary, compacted_through_ts,
                 enabled_skills_json, enabled_mcps_json, system_prompt, deleted_at,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0.0, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    conv_id,
                    req.title,
                    req.model,
                    req.exec_node,
                    req.workspace_subpath,
                    int(req.tools_enabled),
                    int(req.web_search_enabled),
                    int(bool(req.swarm_enabled)),
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
                  tools_enabled = ?, web_search_enabled = ?, swarm_enabled = ?,
                  auto_approve = ?, auto_compact = ?,
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
                    int(updated.swarm_enabled),
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

    # ── Swarm Persistence ────────────────────────────────────────────────────

    @staticmethod
    def _row_to_swarm_agent(row: sqlite3.Row) -> SwarmAgentRecord:
        return SwarmAgentRecord(
            id=row["id"],
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            role=row["role"],
            task=row["task"],
            model=row["model"],
            node_id=row["node_id"],
            readonly=bool(row["readonly"]),
            status=row["status"],
            round=int(row["round"] or 0),
            max_rounds=int(row["max_rounds"] or 8),
            phase=row["phase"] or "queued",
            active_tool=row["active_tool"],
            result=row["result"] or "",
            error=row["error"],
            tokens_per_sec=row["tokens_per_sec"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _row_to_swarm_msg(row: sqlite3.Row) -> SwarmWorkerMessage:
        return SwarmWorkerMessage(
            id=row["id"],
            agent_id=row["agent_id"],
            role=row["role"],
            content=row["content"] or "",
            reasoning=row["reasoning"] or "",
            tool_calls=json.loads(row["tool_calls_json"] or "[]"),
            tool_call_id=row["tool_call_id"],
            tool_name=row["tool_name"],
            meta=json.loads(row["meta_json"] or "{}"),
            created_at=float(row["created_at"]),
        )

    def create_swarm_run(self, conversation_id: str) -> SwarmRunRecord:
        now = time.time()
        run_id = uuid.uuid4().hex[:12]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO swarm_runs (id, conversation_id, status, created_at, updated_at) "
                "VALUES (?, ?, 'running', ?, ?)",
                (run_id, conversation_id, now, now),
            )
        return SwarmRunRecord(
            id=run_id, conversation_id=conversation_id, status="running", agents=[], created_at=now, updated_at=now
        )

    def update_swarm_run_status(self, run_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE swarm_runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), run_id),
            )

    def list_swarm_runs(self, conversation_id: str) -> list[SwarmRunRecord]:
        with self._connect() as conn:
            runs = conn.execute(
                "SELECT * FROM swarm_runs WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
            out: list[SwarmRunRecord] = []
            for r in runs:
                agents = conn.execute(
                    "SELECT * FROM swarm_agents WHERE run_id = ? ORDER BY created_at ASC",
                    (r["id"],),
                ).fetchall()
                out.append(
                    SwarmRunRecord(
                        id=r["id"],
                        conversation_id=r["conversation_id"],
                        status=r["status"],
                        agents=[self._row_to_swarm_agent(a) for a in agents],
                        created_at=float(r["created_at"]),
                        updated_at=float(r["updated_at"]),
                    )
                )
            return out

    def create_swarm_agent(
        self,
        *,
        run_id: str,
        conversation_id: str,
        role: str,
        task: str,
        model: str = "auto",
        node_id: str = "",
        readonly: bool = True,
        max_rounds: int = 8,
    ) -> SwarmAgentRecord:
        now = time.time()
        agent_id = uuid.uuid4().hex[:12]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO swarm_agents
                (id, run_id, conversation_id, role, task, model, node_id, readonly,
                 status, round, max_rounds, phase, active_tool, result, error, tokens_per_sec,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, 'queued', NULL, '', NULL, NULL, ?, ?)
                """,
                (
                    agent_id,
                    run_id,
                    conversation_id,
                    role,
                    task,
                    model,
                    node_id,
                    int(readonly),
                    max_rounds,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM swarm_agents WHERE id = ?", (agent_id,)).fetchone()
            return self._row_to_swarm_agent(row)

    def update_swarm_agent(self, agent_id: str, **fields: Any) -> SwarmAgentRecord | None:
        if not fields:
            return self.get_swarm_agent(agent_id)
        allowed = {
            "model",
            "node_id",
            "status",
            "round",
            "max_rounds",
            "phase",
            "active_tool",
            "result",
            "error",
            "tokens_per_sec",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        updates["updated_at"] = time.time()
        cols_sql = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [agent_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE swarm_agents SET {cols_sql} WHERE id = ?", vals)
            row = conn.execute("SELECT * FROM swarm_agents WHERE id = ?", (agent_id,)).fetchone()
            return self._row_to_swarm_agent(row) if row else None

    def get_swarm_agent(self, agent_id: str) -> SwarmAgentRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM swarm_agents WHERE id = ?", (agent_id,)).fetchone()
            return self._row_to_swarm_agent(row) if row else None

    def list_swarm_agents(self, run_id: str) -> list[SwarmAgentRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM swarm_agents WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
            return [self._row_to_swarm_agent(r) for r in rows]

    def add_swarm_agent_message(self, msg: SwarmWorkerMessage) -> SwarmWorkerMessage:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO swarm_agent_messages
                (id, agent_id, role, content, reasoning, tool_calls_json, tool_call_id, tool_name, meta_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.id,
                    msg.agent_id,
                    msg.role,
                    msg.content,
                    msg.reasoning,
                    json.dumps(msg.tool_calls),
                    msg.tool_call_id,
                    msg.tool_name,
                    json.dumps(msg.meta),
                    msg.created_at,
                ),
            )
        return msg

    def list_swarm_agent_messages(self, agent_id: str) -> list[SwarmWorkerMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM swarm_agent_messages WHERE agent_id = ? ORDER BY created_at ASC",
                (agent_id,),
            ).fetchall()
            return [self._row_to_swarm_msg(r) for r in rows]
