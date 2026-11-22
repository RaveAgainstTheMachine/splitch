"""Models & Aliases management routes (HF search, dynamic VRAM fit, GGUF pulls, live alias routing)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

from backend.router.telemetry import ClusterState
from backend.shared.models import (
    AliasConfig,
    DownloadJob,
    HfGgufFile,
    HfRepoDetails,
    HfRepoResult,
    NodeModelInventory,
    NodeStatus,
    PullModelRequest,
)

QUANT_RE = re.compile(
    r"(IQ\d_[A-Z0-9]+|Q\d_[A-Z0-9_]+|F16|BF16|F32|MXFP4|UD-Q\d_[A-Z0-9_]+)", re.IGNORECASE
)
PARAM_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?[BM](?:-A\d+(?:\.\d+)?[BM])?)(?![A-Za-z0-9])", re.IGNORECASE
)
VALID_HF_SORTS = {"downloads", "likes", "lastModified", "trendingScore", "createdAt"}
HF_REPO_ID_RE = re.compile(r"[A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+")
HF_SEARCH_EXPAND = (
    "gguf",
    "safetensors",
    "siblings",
    "downloads",
    "likes",
    "trendingScore",
    "lastModified",
    "createdAt",
    "tags",
    "pipeline_tag",
    "gated",
    "author",
)
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HTML_NOISE_BLOCK_RE = re.compile(r"<(?:style|script)\b[^>]*>.*?</(?:style|script)>", re.IGNORECASE | re.DOTALL)
HTML_BADGE_LINK_RE = re.compile(r"<a\b[^>]*>\s*<img\b[^>]*>\s*</a>", re.IGNORECASE | re.DOTALL)
HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
MD_BADGE_LINK_RE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s+[^<>]*)?/?>")


def _extract_quant(filename: str) -> str:
    m = QUANT_RE.search(filename)
    return m.group(1).upper() if m else "GGUF"


def _quant_gb_per_billion(quant: str) -> float | None:
    """Approximate GGUF file size in GB per 1B parameters for a quantization tag."""
    q = quant.upper().removeprefix("UD-")
    if q.startswith("IQ1"):
        return 0.24
    if q.startswith("IQ2"):
        return 0.34
    if q.startswith("Q2"):
        return 0.38
    if q.startswith("IQ3"):
        return 0.45
    if q.startswith("Q3"):
        return 0.49
    if q.startswith(("IQ4", "MXFP4")):
        return 0.56
    if q.startswith("Q4"):
        return 0.60
    if q.startswith("Q5"):
        return 0.72
    if q.startswith("Q6"):
        return 0.83
    if q.startswith("Q8"):
        return 1.07
    if q in {"F16", "BF16"}:
        return 2.02
    if q == "F32":
        return 4.02
    return None


def _parse_params_b(param_size: str | None) -> float | None:
    if not param_size:
        return None
    m = re.match(r"(\d+(?:\.\d+)?)([BM])", param_size.strip(), re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    return round(val / 1000.0, 3) if m.group(2).upper() == "M" else round(val, 2)


def _format_params_b(params_b: float) -> str:
    if params_b < 1.0:
        return f"{round(params_b * 1000)}M"
    if params_b >= 10.0 and abs(params_b - round(params_b)) < 0.25:
        return f"{round(params_b)}B"
    return f"{params_b:.1f}".rstrip("0").rstrip(".") + "B"


def _extract_frontmatter_base_model(frontmatter: str) -> str | None:
    lines = frontmatter.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("base_model:"):
            inline = stripped.split(":", 1)[1].strip().strip("\"'")
            if inline and inline not in {"[]", "{}"}:
                return inline
            for sub in lines[idx + 1 :]:
                s = sub.strip()
                if s.startswith("- "):
                    val = s[2:].strip().strip("\"'")
                    if val:
                        return val
                elif s and not s.startswith("#"):
                    break
    return None


def extract_hf_readme_description(
    raw_md: str, *, max_chars: int = 2400
) -> tuple[str, bool, str | None]:
    """
    Clean a Hugging Face model card README.md into safe, concise Markdown prose.
    Returns (cleaned_markdown, was_truncated, frontmatter_base_model).
    """
    if not raw_md:
        return "", False, None

    text = raw_md.replace("\r\n", "\n")
    fm_base: str | None = None
    fm_match = FRONTMATTER_RE.match(text)
    if fm_match:
        fm_base = _extract_frontmatter_base_model(fm_match.group(1))
        text = text[fm_match.end() :]

    text = HTML_NOISE_BLOCK_RE.sub("", text)
    text = HTML_COMMENT_RE.sub("", text)

    # Preserve fenced code blocks while stripping noisy HTML badges/layout tags outside them
    segments = re.split(r"(```[\s\S]*?```)", text)
    cleaned_parts: list[str] = []
    for idx, seg in enumerate(segments):
        if idx % 2 == 1:
            cleaned_parts.append(seg)
            continue
        s = HTML_BADGE_LINK_RE.sub("", seg)
        s = HTML_IMG_RE.sub("", s)
        s = MD_BADGE_LINK_RE.sub("", s)
        s = MD_IMAGE_RE.sub("", s)
        s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
        s = re.sub(r"</(?:p|div|section|article|header|footer|h[1-6]|li|tr)>", "\n", s, flags=re.IGNORECASE)
        s = HTML_TAG_RE.sub("", s)
        cleaned_parts.append(s)

    cleaned = "".join(cleaned_parts)
    # Remove empty bullet/separator debris and unintended HTML indentation (4+ spaces -> code block)
    compact_lines: list[str] = []
    in_fence = False
    for raw_ln in cleaned.splitlines():
        if raw_ln.lstrip().startswith("```"):
            in_fence = not in_fence
            compact_lines.append(raw_ln.strip())
            continue
        if in_fence:
            compact_lines.append(raw_ln.rstrip())
            continue
        s = raw_ln.strip()
        if s in {"|", "||", "·", "•"}:
            continue
        if not s:
            compact_lines.append("")
        elif re.match(r"^(?:[-*+]|\d+\.)\s+", s):
            indent = min(len(raw_ln) - len(raw_ln.lstrip(" ")), 2)
            compact_lines.append((" " * indent) + s)
        else:
            compact_lines.append(s)

    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(compact_lines)).strip()
    if len(cleaned) <= max_chars:
        return cleaned, False, fm_base

    # Truncate cleanly on a paragraph or line boundary (avoid breaking mid-fence)
    cut = cleaned[:max_chars]
    if cut.count("```") % 2 == 1:
        last_fence = cut.rfind("```")
        if last_fence > max_chars // 3:
            cut = cut[:last_fence]
    para_break = cut.rfind("\n\n")
    if para_break > max_chars // 2:
        cut = cut[:para_break]
    else:
        line_break = cut.rfind("\n")
        if line_break > max_chars // 2:
            cut = cut[:line_break]
    return cut.rstrip(), True, fm_base


def _node_budget_gb(node: NodeStatus) -> float:
    """Compute realistic combined GGUF memory fit budget in GB from live node telemetry."""
    t = node.telemetry
    if t is None:
        return 0.0
    if t.vram_total_mb > 0:
        return round((t.vram_total_mb / 1024.0) * 0.88, 2)
    if t.ram_total_mb > 0:
        return round((t.ram_total_mb / 1024.0) * 0.60, 2)
    return 0.0


def _node_single_gpu_budget_gb(node: NodeStatus) -> float:
    """Compute largest single-GPU VRAM budget in GB (for distinguishing 1-GPU vs multi-GPU split)."""
    t = node.telemetry
    if t is None:
        return 0.0
    if t.gpus:
        max_vram = max((g.vram_total_mb for g in t.gpus), default=0.0)
        if max_vram > 0:
            return round((max_vram / 1024.0) * 0.88, 2)
    return _node_budget_gb(node)


def _extract_repo_details(
    item: dict, node_budgets: dict[str, float] | None = None
) -> HfRepoResult:
    repo_id = str(item["id"])
    raw_tags = item.get("tags") or []
    tags_lower = {str(t).lower() for t in raw_tags if isinstance(t, str)}

    license_val = None
    for t in raw_tags:
        if isinstance(t, str) and t.lower().startswith("license:"):
            license_val = t.split(":", 1)[1]
            break

    gguf_meta = item.get("gguf") if isinstance(item.get("gguf"), dict) else {}
    safe_meta = item.get("safetensors") if isinstance(item.get("safetensors"), dict) else {}

    architecture = str(gguf_meta["architecture"]) if gguf_meta.get("architecture") else None
    raw_ctx = gguf_meta.get("context_length")
    context_length = int(raw_ctx) if isinstance(raw_ctx, (int, float)) and raw_ctx > 0 else None

    param_match = PARAM_RE.search(repo_id)
    param_size = param_match.group(1).upper() if param_match else None

    raw_total = gguf_meta.get("total") or safe_meta.get("total")
    parameters_b: float | None = None
    if isinstance(raw_total, (int, float)) and raw_total > 0:
        parameters_b = round(float(raw_total) / 1e9, 2)
    else:
        parameters_b = _parse_params_b(param_size)

    if not param_size and parameters_b is not None and parameters_b > 0:
        param_size = _format_params_b(parameters_b)

    siblings = item.get("siblings") or []
    gguf_names = [
        str(s.get("rfilename", ""))
        for s in siblings
        if isinstance(s, dict) and str(s.get("rfilename", "")).lower().endswith(".gguf")
    ]
    gguf_count = len(gguf_names)

    # Estimate minimum runnable GGUF size & representative Q4 size for hardware fit filtering
    rep_bytes = gguf_meta.get("totalFileSize")
    rep_gb = (
        round(float(rep_bytes) / (1024**3), 2)
        if isinstance(rep_bytes, (int, float)) and rep_bytes > 0
        else None
    )

    weight_bpps: list[float] = []
    for fname in gguf_names:
        low = fname.lower()
        if "mmproj" in low or "imatrix" in low:
            continue
        bpp = _quant_gb_per_billion(_extract_quant(fname))
        if bpp is not None:
            weight_bpps.append(bpp)

    est_q4_gb: float | None = rep_gb
    if est_q4_gb is None and parameters_b is not None and parameters_b > 0:
        est_q4_gb = round(parameters_b * 0.60, 2)

    est_min_gb: float | None = None
    if parameters_b is not None and parameters_b > 0 and weight_bpps:
        est_min_gb = round(parameters_b * min(weight_bpps), 2)
        if rep_gb is not None:
            est_min_gb = min(est_min_gb, rep_gb)
    elif rep_gb is not None:
        est_min_gb = rep_gb
    elif parameters_b is not None and parameters_b > 0:
        est_min_gb = round(parameters_b * 0.58, 2)

    fits_nodes: list[str] = []
    if node_budgets and est_min_gb is not None:
        fits_nodes = [
            nid for nid, budget in node_budgets.items() if budget > 0 and est_min_gb <= budget
        ]

    pipeline_tag = item.get("pipeline_tag")
    capabilities: list[str] = []
    if pipeline_tag == "image-text-to-text" or "vision" in tags_lower or "multimodal" in tags_lower:
        capabilities.append("vision")
    if "reasoning" in tags_lower or "thinking" in tags_lower or "r1" in repo_id.lower():
        capabilities.append("reasoning")
    if "coder" in tags_lower or "code" in tags_lower or "coder" in repo_id.lower():
        capabilities.append("coder")
    if "imatrix" in tags_lower:
        capabilities.append("imatrix")
    if "embedding" in repo_id.lower() or pipeline_tag == "feature-extraction":
        capabilities.append("embed")
    if "unsloth" in tags_lower or repo_id.lower().startswith("unsloth/"):
        capabilities.append("unsloth")

    return HfRepoResult(
        repo_id=repo_id,
        downloads=int(item.get("downloads") or 0),
        likes=int(item.get("likes") or 0),
        trending_score=round(float(item.get("trendingScore") or 0.0), 1),
        last_modified=item.get("lastModified"),
        created_at=item.get("createdAt"),
        pipeline_tag=str(pipeline_tag) if pipeline_tag else None,
        license=license_val,
        param_size=param_size,
        parameters_b=parameters_b,
        architecture=architecture,
        context_length=context_length,
        est_min_gb=est_min_gb,
        est_q4_gb=est_q4_gb,
        fits_nodes=fits_nodes,
        gguf_count=gguf_count,
        gated=bool(item.get("gated", False)),
        capabilities=capabilities,
    )


def _build_gguf_file_list(entries: list, cluster: ClusterState) -> list[HfGgufFile]:
    node_budgets = {nid: _node_budget_gb(n) for nid, n in cluster.nodes.items()}
    single_gpu_budgets = {nid: _node_single_gpu_budget_gb(n) for nid, n in cluster.nodes.items()}
    multi_gpu_ids = {
        nid for nid, n in cluster.nodes.items() if n.telemetry and len(n.telemetry.gpus) > 1
    }

    files: list[HfGgufFile] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            continue
        path = str(entry.get("path", ""))
        if not path.lower().endswith(".gguf"):
            continue
        size_bytes = int(entry.get("size") or (entry.get("lfs") or {}).get("size") or 0)
        size_gb = round(size_bytes / (1024**3), 2)
        fits = [nid for nid, budget in node_budgets.items() if budget > 0 and size_gb <= budget]
        fits_single = [nid for nid in fits if single_gpu_budgets.get(nid, 0.0) >= size_gb]
        fits_multi = [nid for nid in fits if nid in multi_gpu_ids and nid not in fits_single]
        files.append(
            HfGgufFile(
                filename=path,
                size_bytes=size_bytes,
                size_gb=size_gb,
                quant=_extract_quant(path),
                fits_nodes=fits,
                fits_single_gpu_nodes=fits_single,
                fits_multi_gpu_nodes=fits_multi,
            )
        )
    files.sort(key=lambda f: f.size_bytes)
    return files


def build_models_router(cluster: ClusterState, data_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["models"])
    aliases_file = data_dir / "aliases.json"

    if aliases_file.exists():
        try:
            raw = json.loads(aliases_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    cfg = AliasConfig.model_validate(item)
                    cluster.aliases[cfg.alias] = cfg
        except (OSError, ValueError):
            pass

    async def _persist_aliases() -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([a.model_dump() for a in cluster.aliases.values()], indent=2)
        await asyncio.to_thread(aliases_file.write_text, payload, "utf-8")

    @router.get("/models/search", response_model=list[HfRepoResult])
    async def search_hf_models(
        q: str,
        sort: str = "downloads",
        direction: int = -1,
        limit: int = 18,
    ) -> list[HfRepoResult]:
        query = q.strip()
        if len(query) < 2:
            return []
        hf_sort = sort if sort in VALID_HF_SORTS else "downloads"
        hf_dir = 1 if direction == 1 else -1
        params: list[tuple[str, str | int]] = [
            ("search", query),
            ("filter", "gguf"),
            ("sort", hf_sort),
            ("direction", hf_dir),
            ("limit", min(limit, 30)),
            *[("expand[]", field) for field in HF_SEARCH_EXPAND],
        ]
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.get("https://huggingface.co/api/models", params=params)
                resp.raise_for_status()
                items = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"HF search failed: {exc}") from exc

        node_budgets = {nid: _node_budget_gb(n) for nid, n in cluster.nodes.items()}
        results: list[HfRepoResult] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            results.append(_extract_repo_details(item, node_budgets=node_budgets))
        return results

    @router.get("/models/files", response_model=list[HfGgufFile])
    async def list_hf_ggufs(repo_id: str) -> list[HfGgufFile]:
        if not HF_REPO_ID_RE.fullmatch(repo_id):
            raise HTTPException(status_code=400, detail="Invalid repo_id")
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.get(
                    f"https://huggingface.co/api/models/{repo_id}/tree/main",
                    params={"recursive": "true"},
                )
                resp.raise_for_status()
                entries = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"HF tree fetch failed: {exc}") from exc
        return _build_gguf_file_list(entries if isinstance(entries, list) else [], cluster)

    @router.get("/models/details", response_model=HfRepoDetails)
    async def get_hf_repo_details(repo_id: str) -> HfRepoDetails:
        if not HF_REPO_ID_RE.fullmatch(repo_id):
            raise HTTPException(status_code=400, detail="Invalid repo_id")

        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            async def _fetch_tree() -> list:
                resp = await client.get(
                    f"https://huggingface.co/api/models/{repo_id}/tree/main",
                    params={"recursive": "true"},
                )
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else []

            async def _fetch_meta() -> dict:
                try:
                    resp = await client.get(f"https://huggingface.co/api/models/{repo_id}")
                    if resp.status_code == 200:
                        data = resp.json()
                        return data if isinstance(data, dict) else {}
                except (httpx.HTTPError, ValueError):
                    pass
                return {}

            async def _fetch_readme() -> str:
                try:
                    async with client.stream(
                        "GET", f"https://huggingface.co/{repo_id}/raw/main/README.md"
                    ) as resp:
                        if resp.status_code != 200:
                            return ""
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in resp.aiter_bytes(8192):
                            chunks.append(chunk)
                            total += len(chunk)
                            if total >= 65536:
                                break
                        return b"".join(chunks).decode("utf-8", errors="replace")
                except httpx.HTTPError:
                    return ""

            try:
                entries, meta, raw_readme = await asyncio.gather(
                    _fetch_tree(), _fetch_meta(), _fetch_readme()
                )
            except (httpx.HTTPError, ValueError) as exc:
                raise HTTPException(status_code=502, detail=f"HF details fetch failed: {exc}") from exc

        files = _build_gguf_file_list(entries, cluster)
        description, truncated, fm_base = extract_hf_readme_description(raw_readme)

        card_data = meta.get("cardData") if isinstance(meta.get("cardData"), dict) else {}
        gguf_meta = meta.get("gguf") if isinstance(meta.get("gguf"), dict) else {}
        safe_meta = meta.get("safetensors") if isinstance(meta.get("safetensors"), dict) else {}

        base_raw = card_data.get("base_model") or fm_base
        base_model: str | None = None
        if isinstance(base_raw, list) and base_raw:
            base_model = str(base_raw[0])
        elif isinstance(base_raw, str) and base_raw.strip():
            base_model = base_raw.strip()

        raw_tags = meta.get("tags") or []
        license_val: str | None = (
            str(card_data["license"]) if isinstance(card_data.get("license"), str) else None
        )
        curated_tags: list[str] = []
        for t in raw_tags:
            if not isinstance(t, str):
                continue
            low = t.lower()
            if low.startswith("license:") and not license_val:
                license_val = t.split(":", 1)[1]
            elif ":" not in t and low not in {"gguf", "endpoints_compatible", "region:us"}:
                curated_tags.append(t)

        raw_total = gguf_meta.get("total") or safe_meta.get("total")
        parameters_b = (
            round(float(raw_total) / 1e9, 2)
            if isinstance(raw_total, (int, float)) and raw_total > 0
            else None
        )
        raw_ctx = gguf_meta.get("context_length")
        context_length = int(raw_ctx) if isinstance(raw_ctx, (int, float)) and raw_ctx > 0 else None
        architecture = str(gguf_meta["architecture"]) if gguf_meta.get("architecture") else None

        return HfRepoDetails(
            repo_id=repo_id,
            description=description,
            description_truncated=truncated,
            base_model=base_model,
            architecture=architecture,
            context_length=context_length,
            parameters_b=parameters_b,
            license=license_val,
            tags=curated_tags[:8],
            files=files,
        )

    @router.get("/models/inventory", response_model=list[NodeModelInventory])
    async def get_inventory() -> list[NodeModelInventory]:
        inventories: list[NodeModelInventory] = []
        headers = cluster.agent_headers("GET", "/files/models")

        async def _fetch(agent_url: str) -> None:
            try:
                async with httpx.AsyncClient(timeout=4.0, headers=headers) as client:
                    r = await client.get(f"{agent_url}/files/models")
                    if r.status_code == 200:
                        inventories.append(NodeModelInventory.model_validate(r.json()))
            except (httpx.HTTPError, ValueError):
                pass

        await asyncio.gather(*(_fetch(n.agent_url) for n in cluster.nodes.values() if n.online))
        inventories.sort(key=lambda inv: inv.node_id)
        return inventories

    @router.post("/models/pull", response_model=DownloadJob)
    async def pull_model(req: PullModelRequest) -> DownloadJob:
        node = cluster.nodes.get(req.node_id)
        if not node or not node.online:
            raise HTTPException(status_code=503, detail=f"Node '{req.node_id}' is offline")
        try:
            async with httpx.AsyncClient(
                timeout=15.0, headers=cluster.agent_headers("POST", "/files/models/pull")
            ) as client:
                r = await client.post(
                    f"{node.agent_url}/files/models/pull",
                    json=req.model_dump(),
                )
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.text[:300])
                return DownloadJob.model_validate(r.json())
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.delete("/models/{node_id}/{filename}")
    async def delete_model(node_id: str, filename: str) -> dict:
        node = cluster.nodes.get(node_id)
        if not node or not node.online:
            raise HTTPException(status_code=503, detail=f"Node '{node_id}' is offline")
        try:
            async with httpx.AsyncClient(
                timeout=10.0, headers=cluster.agent_headers("DELETE", f"/files/models/{filename}")
            ) as client:
                r = await client.delete(f"{node.agent_url}/files/models/{filename}")
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.text[:300])
                return r.json()
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/aliases", response_model=list[AliasConfig])
    async def list_aliases() -> list[AliasConfig]:
        return list(cluster.aliases.values())

    @router.put("/aliases/{alias_name}", response_model=AliasConfig)
    async def upsert_alias(alias_name: str, cfg: AliasConfig) -> AliasConfig:
        clean = alias_name.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,31}", clean):
            raise HTTPException(status_code=400, detail="Invalid alias name")
        cfg.alias = clean
        cluster.aliases[clean] = cfg
        await _persist_aliases()
        await cluster.broadcast_snapshot()
        return cfg

    @router.delete("/aliases/{alias_name}")
    async def delete_alias(alias_name: str) -> dict:
        if alias_name not in cluster.aliases:
            raise HTTPException(status_code=404, detail="Alias not found")
        del cluster.aliases[alias_name]
        await _persist_aliases()
        await cluster.broadcast_snapshot()
        return {"ok": True, "deleted": alias_name}

    return router
