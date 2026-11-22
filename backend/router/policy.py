"""Pure deterministic N-node routing policy for Splitch."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from backend.router.config import GuardConfig
from backend.shared.models import AliasConfig, ControlState, NodeStatus


@dataclass(frozen=True)
class RouteDecision:
    node: NodeStatus
    target_model: str
    requested_model: str
    fallback_reason: str | None = None


def _extract_text_len(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        total = 0
        for item in value:
            if isinstance(item, str):
                total += len(item)
            elif isinstance(item, dict):
                for field in ("text", "content", "input_text"):
                    if field in item:
                        total += _extract_text_len(item[field])
        return total
    return 0


def estimate_prompt_chars(body: dict[str, Any]) -> int:
    total = 0
    if "messages" in body and isinstance(body["messages"], list):
        for msg in body["messages"]:
            if isinstance(msg, dict):
                total += _extract_text_len(msg.get("content"))
    for key in ("system", "instructions", "prompt", "input_prefix", "input_suffix", "input"):
        if key in body:
            total += _extract_text_len(body.get(key))
    return total


def watched_gpu_metrics(node: NodeStatus) -> tuple[float, float, float, float]:
    """
    Compute `(non_llm_vram_mb, gpu_util_percent, vram_total_mb, vram_free_mb)` across the GPUs
    monitored for yield on `node`.
    If `node.yield_gpu_indices` is set (e.g. `[1]` for a dedicated compute GPU while GPU 0 is
    the gaming display card), gaming on GPU 0 does NOT force-yield the node.
    """
    t = node.telemetry
    if t is None:
        return 0.0, 0.0, 0.0, 0.0
    if t.gpus and node.yield_gpu_indices:
        idx_set = set(node.yield_gpu_indices)
        watched = [g for g in t.gpus if g.index in idx_set]
        if watched:
            return (
                round(sum(g.non_llm_vram_mb for g in watched), 1),
                round(max((g.util_percent for g in watched), default=0.0), 1),
                round(sum(g.vram_total_mb for g in watched), 1),
                round(sum(g.vram_free_mb for g in watched), 1),
            )
    return t.non_llm_vram_mb, t.gpu_util_percent, t.vram_total_mb, t.vram_free_mb


def check_node_eligibility(
    node: NodeStatus | None,
    target_model: str,
    control: ControlState,
    guards: GuardConfig,
) -> str | None:
    """Returns None if eligible, or a concise machine-readable block reason."""
    if node is None:
        return "node_unknown"
    if not node.online or node.telemetry is None:
        return f"{node.node_id}_offline"
    if not node.telemetry.engine_healthy:
        return f"{node.node_id}_engine_down"
    if control.force_node != "auto" and node.node_id != control.force_node:
        return f"forced_{control.force_node}"
    if control.gaming_mode and node.yield_on_gaming:
        return "gaming_mode"

    t = node.telemetry
    model_loaded = any(m.id == target_model and m.status == "loaded" for m in t.models)

    if node.role == "gpu":
        non_llm_mb, gpu_util, vram_total, vram_free = watched_gpu_metrics(node)
        if non_llm_mb >= guards.non_llm_vram_threshold_mb:
            return f"{node.node_id}_gpu_busy_app"
        if gpu_util >= guards.max_external_gpu_util_percent and node.in_flight == 0:
            return f"{node.node_id}_gpu_busy"
        if node.yield_on_gaming and node.yield_until_ts > time.time():
            return f"{node.node_id}_gpu_cooldown"
        if not model_loaded and vram_total > 0 and vram_free < guards.min_vram_free_mb:
            return f"{node.node_id}_vram_low"

    if not t.storage_mounted:
        return f"{node.node_id}_storage_unmounted"

    return None


def _select_auto_alias(
    endpoint: str,
    body: dict[str, Any],
    aliases: dict[str, AliasConfig],
    heavy_char_threshold: int,
) -> str:
    if endpoint == "/v1/embeddings":
        return "embed" if "embed" in aliases else next(iter(aliases), "fast")
    if endpoint == "/infill":
        return "fim" if "fim" in aliases else next(iter(aliases), "fast")

    has_tools = bool(body.get("tools") or body.get("functions"))
    prompt_chars = estimate_prompt_chars(body)
    if (has_tools or prompt_chars >= heavy_char_threshold) and "heavy" in aliases:
        return "heavy"
    return "fast" if "fast" in aliases else next(iter(aliases), "heavy")


def resolve_route(
    *,
    endpoint: str,
    requested_model: str,
    body: dict[str, Any],
    nodes: dict[str, NodeStatus],
    aliases: dict[str, AliasConfig],
    control: ControlState,
    guards: GuardConfig,
    heavy_char_threshold: int = 12000,
) -> tuple[RouteDecision | None, list[str]]:
    """
    Resolve target (node, model) for an incoming OpenAI/infill request.
    Works across 1..N heterogeneous nodes.
    """
    reasons: list[str] = []
    req = (requested_model or "auto").strip()

    # 1. Explicit node-prefixed model: "<node_id>/<model>"
    if "/" in req:
        prefix, real_model = req.split("/", 1)
        if prefix in nodes:
            node = nodes[prefix]
            block = check_node_eligibility(node, real_model, control, guards)
            if block is None:
                return RouteDecision(node=node, target_model=real_model, requested_model=req), reasons
            reasons.append(f"{prefix}:{block}")
            return None, reasons

    # 2. Automatic or alias resolution
    alias_key = (
        _select_auto_alias(endpoint, body, aliases, heavy_char_threshold)
        if req in ("auto", "splitch/auto", "")
        else req
    )
    alias_cfg = aliases.get(alias_key)

    if alias_cfg is not None:
        primary_node = nodes.get(alias_cfg.primary_node)
        primary_block = check_node_eligibility(
            primary_node, alias_cfg.primary_model, control, guards
        )
        if primary_block is None and primary_node is not None:
            return (
                RouteDecision(
                    node=primary_node,
                    target_model=alias_cfg.primary_model,
                    requested_model=req,
                ),
                reasons,
            )
        reasons.append(f"{alias_cfg.primary_node}:{primary_block}")

        if alias_cfg.fallback_node and alias_cfg.fallback_model:
            fb_node = nodes.get(alias_cfg.fallback_node)
            fb_block = check_node_eligibility(fb_node, alias_cfg.fallback_model, control, guards)
            if fb_block is None and fb_node is not None:
                return (
                    RouteDecision(
                        node=fb_node,
                        target_model=alias_cfg.fallback_model,
                        requested_model=req,
                        fallback_reason=primary_block,
                    ),
                    reasons,
                )
            reasons.append(f"{alias_cfg.fallback_node}:{fb_block}")
        return None, reasons

    # 3. Direct model ID lookup across online nodes (prefer loaded, lowest in_flight)
    direct_candidates: list[tuple[int, int, NodeStatus]] = []
    for node in nodes.values():
        block = check_node_eligibility(node, req, control, guards)
        if block is not None:
            reasons.append(f"{node.node_id}:{block}")
            continue
        assert node.telemetry is not None
        known = [m for m in node.telemetry.models if m.id == req]
        if known:
            loaded_rank = 0 if known[0].status == "loaded" else 1
            direct_candidates.append((loaded_rank, node.in_flight, node))

    if direct_candidates:
        direct_candidates.sort(key=lambda item: (item[0], item[1]))
        chosen = direct_candidates[0][2]
        return RouteDecision(node=chosen, target_model=req, requested_model=req), reasons

    # 4. Zero-config / auto fallback when no aliases are configured: pick first loaded model on eligible node
    eligible_any = [
        n for n in nodes.values() if check_node_eligibility(n, req, control, guards) is None
    ]
    if eligible_any:
        eligible_any.sort(key=lambda n: n.in_flight)
        chosen = eligible_any[0]
        if req in ("auto", "splitch/auto", "") and chosen.telemetry and chosen.telemetry.models:
            loaded = next((m.id for m in chosen.telemetry.models if m.status == "loaded"), None)
            chosen_model = loaded or chosen.telemetry.models[0].id
            return RouteDecision(node=chosen, target_model=chosen_model, requested_model=req), reasons
        return RouteDecision(node=chosen, target_model=req, requested_model=req), reasons

    return None, reasons
