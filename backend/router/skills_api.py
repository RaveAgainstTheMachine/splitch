"""Real filesystem Skills Library (<id>/SKILL.md) + GitHub import + multi-node sync."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException

from backend.router.telemetry import ClusterState
from backend.shared.models import (
    SkillImportRequest,
    SkillItem,
    SkillRegistryInstallRequest,
    SkillRegistryResult,
    SkillUpsertRequest,
    SyncResult,
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_skill_frontmatter(raw: str, fallback_id: str) -> tuple[str, str]:
    name = fallback_id
    desc = ""
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return name, desc
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip().lower()
        val = v.strip().strip('"').strip("'")
        if key == "name" and val:
            name = val
        elif key == "description" and val:
            desc = val
    return name, desc


def ensure_frontmatter(name: str, description: str, content: str) -> str:
    if FRONTMATTER_RE.match(content.lstrip()):
        return content
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{content.lstrip()}"


def _pick_best_skill_path(paths: list[str], skill_id: str) -> str | None:
    target = skill_id.lower().strip("/")
    exact: list[str] = []
    fuzzy: list[str] = []
    for p in paths:
        if not p.endswith("SKILL.md"):
            continue
        folder = Path(p).parent.name.lower()
        if folder == target:
            exact.append(p)
        elif target.endswith(folder) or folder in target or target in p.lower():
            fuzzy.append(p)
    if exact:
        return exact[0]
    if fuzzy:
        fuzzy.sort(key=lambda s: len(s))
        return fuzzy[0]
    return paths[0] if len(paths) == 1 else None


def scan_installed_skills(skills_dir: Path) -> list[SkillItem]:
    """Scan installed SKILL.md folders and registry metadata under skills_dir."""
    if not skills_dir.exists():
        return []
    meta_file = skills_dir / ".registry.json"
    meta: dict[str, dict] = {}
    if meta_file.exists():
        with contextlib.suppress(OSError, ValueError):
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
    items: list[SkillItem] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            mtime = skill_md.stat().st_mtime
        except OSError:
            continue
        name, desc = parse_skill_frontmatter(content, entry.name)
        m = meta.get(entry.name, {})
        items.append(
            SkillItem(
                id=entry.name,
                name=m.get("name") or name,
                description=m.get("description") or desc,
                enabled=bool(m.get("enabled", True)),
                content=content,
                source_url=m.get("source_url"),
                updated_at=mtime,
            )
        )
    return items


async def search_skills_registry_items(q: str, limit: int = 24) -> list[SkillRegistryResult]:
    """Query skills.sh public registry for matching skills."""
    query = q.strip()
    if len(query) < 2:
        return []
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://skills.sh/api/search",
            params={"q": query, "limit": min(limit, 36)},
        )
        r.raise_for_status()
        raw = r.json().get("skills") or []
    results: list[SkillRegistryResult] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("source") or not item.get("skillId"):
            continue
        results.append(
            SkillRegistryResult(
                id=str(item.get("id") or f"{item['source']}/{item['skillId']}"),
                source=str(item["source"]),
                skill_id=str(item["skillId"]),
                name=str(item.get("name") or item["skillId"]),
                installs=int(item.get("installs") or 0),
            )
        )
    return results


def build_skills_router(cluster: ClusterState, skills_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])
    meta_file = skills_dir / ".registry.json"

    def _load_meta() -> dict[str, dict]:
        if not meta_file.exists():
            return {}
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_meta(meta: dict[str, dict]) -> None:
        skills_dir.mkdir(parents=True, exist_ok=True)
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _scan_skills() -> list[SkillItem]:
        return scan_installed_skills(skills_dir)

    @router.get("/search", response_model=list[SkillRegistryResult])
    async def search_skills_registry(q: str, limit: int = 24) -> list[SkillRegistryResult]:
        try:
            return await search_skills_registry_items(q, limit=limit)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"skills.sh search failed: {exc}") from exc

    @router.post("/install-registry", response_model=SkillItem)
    async def install_from_registry(req: SkillRegistryInstallRequest) -> SkillItem:
        source = req.source.strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+", source):
            raise HTTPException(
                status_code=400,
                detail="Only GitHub owner/repo skill sources can be installed automatically",
            )
        skill_id = req.skill_id.strip().strip("/")
        if ".." in skill_id or not skill_id:
            raise HTTPException(status_code=400, detail="Invalid skill_id")

        content: str | None = None
        resolved_url: str | None = None
        candidates = [
            f"https://raw.githubusercontent.com/{source}/main/skills/{skill_id}/SKILL.md",
            f"https://raw.githubusercontent.com/{source}/main/{skill_id}/SKILL.md",
            f"https://raw.githubusercontent.com/{source}/main/SKILL.md",
        ]

        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            for url in candidates:
                try:
                    r = await client.get(url)
                    if r.status_code == 200 and r.text.strip():
                        content = r.text
                        resolved_url = url
                        break
                except httpx.HTTPError:
                    continue

            if content is None:
                # Fallback: inspect GitHub repo tree to locate exact SKILL.md path
                try:
                    tree_r = await client.get(
                        f"https://api.github.com/repos/{source}/git/trees/HEAD",
                        params={"recursive": "1"},
                        headers={"Accept": "application/vnd.github+json"},
                    )
                    if tree_r.status_code == 200:
                        skill_paths = [
                            str(t["path"])
                            for t in (tree_r.json().get("tree") or [])
                            if isinstance(t, dict) and str(t.get("path", "")).endswith("SKILL.md")
                        ]
                        best = _pick_best_skill_path(skill_paths, skill_id)
                        if best:
                            raw_url = f"https://raw.githubusercontent.com/{source}/HEAD/{best}"
                            raw_r = await client.get(raw_url)
                            if raw_r.status_code == 200 and raw_r.text.strip():
                                content = raw_r.text
                                resolved_url = raw_url
                except (httpx.HTTPError, ValueError):
                    pass

        if not content:
            raise HTTPException(
                status_code=404,
                detail=f"Could not locate SKILL.md for '{skill_id}' in github.com/{source}",
            )

        name, desc = parse_skill_frontmatter(content, req.name or skill_id)
        slug = re.sub(r"[^a-z0-9_\-]+", "-", skill_id.lower()).strip("-")[:64] or "imported-skill"
        return await upsert_skill(
            SkillUpsertRequest(
                id=slug,
                name=name,
                description=desc,
                content=content,
                enabled=True,
                source_url=resolved_url or f"https://github.com/{source}",
            )
        )

    @router.get("", response_model=list[SkillItem])
    async def list_skills() -> list[SkillItem]:
        return await asyncio.to_thread(_scan_skills)

    @router.post("", response_model=SkillItem)
    async def upsert_skill(req: SkillUpsertRequest) -> SkillItem:
        skill_id = req.id.strip().lower()
        if not SLUG_RE.fullmatch(skill_id):
            raise HTTPException(
                status_code=400,
                detail="Skill id must be lowercase alphanumeric with hyphens/underscores",
            )
        final_content = ensure_frontmatter(req.name.strip() or skill_id, req.description.strip(), req.content)

        def _write() -> float:
            skills_dir.mkdir(parents=True, exist_ok=True)
            target_dir = (skills_dir / skill_id).resolve()
            if not target_dir.is_relative_to(skills_dir.resolve()):
                raise HTTPException(status_code=403, detail="Invalid skill path")
            target_dir.mkdir(parents=True, exist_ok=True)
            md = target_dir / "SKILL.md"
            md.write_text(final_content, encoding="utf-8")
            meta = _load_meta()
            meta[skill_id] = {
                "name": req.name.strip() or skill_id,
                "description": req.description.strip(),
                "enabled": req.enabled,
                "source_url": req.source_url,
            }
            _save_meta(meta)
            return time.time()

        ts = await asyncio.to_thread(_write)
        return SkillItem(
            id=skill_id,
            name=req.name.strip() or skill_id,
            description=req.description.strip(),
            enabled=req.enabled,
            content=final_content,
            source_url=req.source_url,
            updated_at=ts,
        )

    @router.post("/{skill_id}/toggle", response_model=SkillItem)
    async def toggle_skill(skill_id: str, enabled: bool) -> SkillItem:
        if not SLUG_RE.fullmatch(skill_id):
            raise HTTPException(status_code=400, detail="Invalid skill id")

        def _toggle() -> SkillItem:
            items = {s.id: s for s in _scan_skills()}
            if skill_id not in items:
                raise HTTPException(status_code=404, detail="Skill not found")
            meta = _load_meta()
            entry = meta.get(skill_id, {})
            entry["enabled"] = enabled
            meta[skill_id] = entry
            _save_meta(meta)
            skill = items[skill_id]
            skill.enabled = enabled
            return skill

        return await asyncio.to_thread(_toggle)

    @router.delete("/{skill_id}")
    async def delete_skill(skill_id: str) -> dict:
        if not SLUG_RE.fullmatch(skill_id):
            raise HTTPException(status_code=400, detail="Invalid skill id")

        def _delete() -> None:
            target = (skills_dir / skill_id).resolve()
            if not target.is_relative_to(skills_dir.resolve()):
                raise HTTPException(status_code=403, detail="Path traversal blocked")
            if not target.exists():
                raise HTTPException(status_code=404, detail="Skill not found")
            shutil.rmtree(target)
            meta = _load_meta()
            meta.pop(skill_id, None)
            _save_meta(meta)

        await asyncio.to_thread(_delete)
        return {"ok": True, "deleted": skill_id}

    @router.post("/import", response_model=SkillItem)
    async def import_skill(req: SkillImportRequest) -> SkillItem:
        parsed = urlparse(req.url.strip())
        if parsed.scheme != "https" or parsed.hostname not in (
            "github.com",
            "raw.githubusercontent.com",
        ):
            raise HTTPException(
                status_code=400,
                detail="Only https://github.com or https://raw.githubusercontent.com URLs allowed",
            )

        raw_url = req.url.strip()
        if parsed.hostname == "github.com":
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            if len(parts) >= 4 and parts[2] == "blob":
                raw_url = f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/{'/'.join(parts[3:])}"
            elif len(parts) == 2:
                raw_url = f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/main/SKILL.md"

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
                r = await client.get(raw_url)
                r.raise_for_status()
                content = r.text
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Failed to fetch SKILL.md: {exc}") from exc

        inferred_slug = req.id
        name, desc = parse_skill_frontmatter(content, "imported-skill")
        if not inferred_slug:
            inferred_slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-") or "imported-skill"

        return await upsert_skill(
            SkillUpsertRequest(
                id=inferred_slug[:64],
                name=name,
                description=desc,
                content=content,
                enabled=True,
                source_url=req.url.strip(),
            )
        )

    @router.post("/sync", response_model=list[SyncResult])
    async def sync_skills_to_nodes() -> list[SyncResult]:
        all_skills = await asyncio.to_thread(_scan_skills)
        enabled_payload = [
            {"id": s.id, "content": s.content} for s in all_skills if s.enabled
        ]
        results: list[SyncResult] = []

        async def _push(node_id: str, agent_url: str) -> None:
            try:
                async with httpx.AsyncClient(
                    timeout=10.0, auth=cluster.agent_auth(), verify=cluster.httpx_verify
                ) as client:
                    r = await client.post(
                        f"{agent_url}/sync/skills",
                        json={"skills": enabled_payload},
                    )
                    if r.status_code == 200:
                        body = r.json()
                        results.append(
                            SyncResult(
                                node_id=node_id,
                                ok=True,
                                synced_count=int(body.get("synced_count", 0)),
                                paths=list(body.get("paths", [])),
                            )
                        )
                    else:
                        results.append(
                            SyncResult(node_id=node_id, ok=False, error=r.text[:200])
                        )
            except (httpx.HTTPError, ValueError) as exc:
                results.append(SyncResult(node_id=node_id, ok=False, error=str(exc)))

        await asyncio.gather(
            *(_push(n.node_id, n.agent_url) for n in cluster.nodes.values() if n.online)
        )
        results.sort(key=lambda r: r.node_id)
        return results

    return router
