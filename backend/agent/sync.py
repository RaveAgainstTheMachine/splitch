"""Skills, client config, and custom MCP script synchronization helpers for `splitch-agent`."""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sys
from pathlib import Path

from fastapi import HTTPException

SKILL_ID_RE = re.compile(r"[a-z0-9][a-z0-9_\-]{0,63}")


def sync_skills_to_dir(target_root: Path, skills: list[dict[str, str]]) -> int:
    target_root.mkdir(parents=True, exist_ok=True)
    manifest_file = target_root / ".splitch-managed.json"
    prior_managed: list[str] = []
    if manifest_file.exists():
        try:
            prior_managed = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior_managed = []

    new_ids: list[str] = []
    for item in skills:
        skill_id = item.get("id", "")
        content = item.get("content", "")
        if not SKILL_ID_RE.fullmatch(skill_id):
            continue
        skill_dir = (target_root / skill_id).resolve()
        if not skill_dir.is_relative_to(target_root.resolve()):
            continue
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        new_ids.append(skill_id)

    for old_id in set(prior_managed):
        if old_id not in new_ids and SKILL_ID_RE.fullmatch(old_id):
            old_dir = (target_root / old_id).resolve()
            if old_dir.is_relative_to(target_root.resolve()) and old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)

    manifest_file.write_text(json.dumps(sorted(new_ids), indent=2), encoding="utf-8")
    return len(new_ids)


def merge_client_config(client: str, incoming: dict, raw_text: str | None = None) -> str:
    home = Path.home()
    if client in ("hermes", "codex", "aider"):
        targets = {
            "hermes": home / ".hermes" / "config.yaml",
            "codex": home / ".codex" / "config.toml",
            "aider": home / ".aider.conf.yml",
        }
        path = targets[client]
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (raw_text or "").strip()
        if not body:
            raise ValueError(f"Missing config text for client '{client}'")
        if client == "codex" and path.exists():
            prior = path.read_text(encoding="utf-8", errors="replace")
            marker_start = "# --- splitch-managed-start ---"
            marker_end = "# --- splitch-managed-end ---"
            managed = f"{marker_start}\n{body}\n{marker_end}\n"
            if marker_start in prior and marker_end in prior:
                pre = prior.split(marker_start, 1)[0].rstrip()
                post = prior.split(marker_end, 1)[1].lstrip()
                combined = (f"{pre}\n\n" if pre else "") + managed + (f"\n{post}" if post else "")
            else:
                combined = prior.rstrip() + ("\n\n" if prior.strip() else "") + managed
            path.write_text(combined, encoding="utf-8")
        else:
            path.write_text(body + "\n", encoding="utf-8")
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                path.chmod(0o600)
        return str(path)

    if client == "claude_code":
        path = home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
        env_map = existing.setdefault("env", {})
        if isinstance(env_map, dict) and isinstance(incoming.get("env"), dict):
            env_map.update(incoming["env"])
        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                path.chmod(0o600)
        return str(path)

    if client == "opencode":
        path = home / ".config" / "opencode" / "opencode.json"
    else:
        path = home / ".config" / "zed" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}

    if client == "opencode":
        existing.setdefault("$schema", "https://opencode.ai/config.json")
        providers = existing.setdefault("provider", {})
        if isinstance(providers, dict) and "provider" in incoming:
            providers.update(incoming["provider"])
        mcp = existing.setdefault("mcp", {})
        if isinstance(mcp, dict) and "mcp" in incoming:
            mcp.update(incoming["mcp"])
        if "model" in incoming and "model" not in existing:
            existing["model"] = incoming["model"]
        if "small_model" in incoming and "small_model" not in existing:
            existing["small_model"] = incoming["small_model"]
    else:
        lm = existing.setdefault("language_models", {})
        if isinstance(lm, dict) and "language_models" in incoming:
            oai = lm.setdefault("openai_compatible", {})
            oai.update(incoming["language_models"].get("openai_compatible", {}))
        cs = existing.setdefault("context_servers", {})
        if isinstance(cs, dict) and "context_servers" in incoming:
            cs.update(incoming["context_servers"])

    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return str(path)


def write_mcp_script(storage_path: str, script_id: str, code: str) -> str:
    scripts_root = Path(storage_path).parent / "mcp_servers"
    scripts_root.mkdir(parents=True, exist_ok=True)
    target = (scripts_root / f"{script_id}.py").resolve()
    if not target.is_relative_to(scripts_root.resolve()):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    target.write_text(code, encoding="utf-8")
    if sys.platform != "win32":
        target.chmod(0o700)
    return str(target)
