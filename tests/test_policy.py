"""Unit tests for Splitch N-node routing policy, failover guards, and telemetry parsers."""

from __future__ import annotations

from backend.agent.main import _normalize_models, _normalize_ollama_models, _parse_pmon
from backend.router.config import GuardConfig
from backend.router.policy import resolve_route
from backend.shared.models import AliasConfig, ControlState, LoadedModel, NodeStatus, NodeTelemetry


def _make_nodes(
    *,
    gpu_online: bool = True,
    gpu_healthy: bool = True,
    gpu_vram_free: float = 20000.0,
    gpu_non_llm_vram: float = 1200.0,
    gpu_util: float = 15.0,
    gpu_loaded: list[str] | None = None,
    standby_online: bool = True,
    standby_healthy: bool = True,
    standby_mounted: bool = True,
    standby_loaded: list[str] | None = None,
) -> dict[str, NodeStatus]:
    gpu_models = [LoadedModel(id=m, status="loaded") for m in (gpu_loaded or ["coder-heavy"])]
    standby_models = [
        LoadedModel(id=m, status="loaded") for m in (standby_loaded or ["fast-small", "embed-bge"])
    ]
    return {
        "gpu-rig": NodeStatus(
            node_id="gpu-rig",
            display_name="GPU Workstation",
            role="gpu",
            yield_on_gaming=True,
            agent_url="http://192.168.1.50:8090",
            engine_url="http://192.168.1.50:8081",
            online=gpu_online,
            telemetry=(
                NodeTelemetry(
                    node_id="gpu-rig",
                    role="gpu",
                    platform="linux",
                    timestamp=1.0,
                    vram_total_mb=24564.0,
                    vram_used_mb=24564.0 - gpu_vram_free,
                    vram_free_mb=gpu_vram_free,
                    gpu_util_percent=gpu_util,
                    non_llm_vram_mb=gpu_non_llm_vram,
                    engine="llama-server",
                    engine_healthy=gpu_healthy,
                    models=gpu_models,
                )
                if gpu_online
                else None
            ),
        ),
        "always-on": NodeStatus(
            node_id="always-on",
            display_name="Always-On Node",
            role="unified",
            yield_on_gaming=False,
            agent_url="http://127.0.0.1:8090",
            engine_url="http://127.0.0.1:8081",
            online=standby_online,
            telemetry=(
                NodeTelemetry(
                    node_id="always-on",
                    role="unified",
                    platform="darwin",
                    timestamp=1.0,
                    vram_total_mb=12288.0,
                    vram_free_mb=8000.0,
                    storage_mounted=standby_mounted,
                    engine="llama-server",
                    engine_healthy=standby_healthy,
                    models=standby_models,
                )
                if standby_online
                else None
            ),
        ),
    }


def _make_aliases() -> dict[str, AliasConfig]:
    return {
        "heavy": AliasConfig(
            alias="heavy",
            task_kind="code",
            primary_node="gpu-rig",
            primary_model="coder-heavy",
            fallback_node="always-on",
            fallback_model="fast-small",
        ),
        "fast": AliasConfig(
            alias="fast",
            task_kind="chat",
            primary_node="always-on",
            primary_model="fast-small",
            fallback_node="gpu-rig",
            fallback_model="coder-heavy",
        ),
        "fim": AliasConfig(
            alias="fim",
            task_kind="fim",
            primary_node="always-on",
            primary_model="fim-coder",
            fallback_node="gpu-rig",
            fallback_model="coder-heavy",
        ),
        "embed": AliasConfig(
            alias="embed",
            task_kind="embed",
            primary_node="always-on",
            primary_model="embed-bge",
            fallback_node="gpu-rig",
            fallback_model="embed-bge",
        ),
    }


def test_heavy_routes_to_gpu_when_idle() -> None:
    dec, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="heavy",
        body={"messages": [{"role": "user", "content": "refactor"}]},
        nodes=_make_nodes(),
        aliases=_make_aliases(),
        control=ControlState(),
        guards=GuardConfig(),
    )
    assert dec is not None
    assert dec.node.node_id == "gpu-rig"
    assert dec.target_model == "coder-heavy"
    assert dec.fallback_reason is None


def test_gaming_mode_fails_over_heavy_to_standby() -> None:
    dec, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="heavy",
        body={"messages": [{"role": "user", "content": "build api"}]},
        nodes=_make_nodes(),
        aliases=_make_aliases(),
        control=ControlState(gaming_mode=True),
        guards=GuardConfig(),
    )
    assert dec is not None
    assert dec.node.node_id == "always-on"
    assert dec.target_model == "fast-small"
    assert dec.fallback_reason == "gaming_mode"


def test_auto_detects_game_via_non_llm_vram() -> None:
    dec, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="heavy",
        body={"messages": [{"role": "user", "content": "write rust"}]},
        nodes=_make_nodes(gpu_non_llm_vram=9500.0),
        aliases=_make_aliases(),
        control=ControlState(),
        guards=GuardConfig(non_llm_vram_threshold_mb=4096.0),
    )
    assert dec is not None
    assert dec.node.node_id == "always-on"
    assert dec.fallback_reason == "gpu-rig_gpu_busy_app"


def test_auto_yield_cooldown_holds_gpu_during_menu_dips() -> None:
    import time

    nodes = _make_nodes(gpu_non_llm_vram=400.0, gpu_util=5.0)
    nodes["gpu-rig"].yield_until_ts = time.time() + 45.0
    dec, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="heavy",
        body={"messages": [{"role": "user", "content": "write rust"}]},
        nodes=nodes,
        aliases=_make_aliases(),
        control=ControlState(),
        guards=GuardConfig(non_llm_vram_threshold_mb=4096.0),
    )
    assert dec is not None
    assert dec.node.node_id == "always-on"
    assert dec.fallback_reason == "gpu-rig_gpu_cooldown"


def test_gpu_offline_fails_over_to_standby() -> None:
    dec, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="heavy",
        body={"messages": [{"role": "user", "content": "hello"}]},
        nodes=_make_nodes(gpu_online=False),
        aliases=_make_aliases(),
        control=ControlState(),
        guards=GuardConfig(),
    )
    assert dec is not None
    assert dec.node.node_id == "always-on"
    assert dec.fallback_reason == "gpu-rig_offline"


def test_auto_routes_tools_to_heavy_and_short_chat_to_fast() -> None:
    nodes = _make_nodes()
    aliases = _make_aliases()

    tool_dec, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="auto",
        body={"messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function"}]},
        nodes=nodes,
        aliases=aliases,
        control=ControlState(),
        guards=GuardConfig(),
    )
    assert tool_dec is not None and tool_dec.node.node_id == "gpu-rig"

    short_dec, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="auto",
        body={"messages": [{"role": "user", "content": "summarize this line"}]},
        nodes=nodes,
        aliases=aliases,
        control=ControlState(),
        guards=GuardConfig(),
    )
    assert short_dec is not None and short_dec.node.node_id == "always-on"

    emb_dec, _ = resolve_route(
        endpoint="/v1/embeddings",
        requested_model="auto",
        body={"input": "hello"},
        nodes=nodes,
        aliases=aliases,
        control=ControlState(),
        guards=GuardConfig(),
    )
    assert emb_dec is not None and emb_dec.node.node_id == "always-on" and emb_dec.target_model == "embed-bge"


def test_zero_config_single_node_auto_routing() -> None:
    single = {"local": _make_nodes()["always-on"].model_copy(update={"node_id": "local"})}
    dec, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="auto",
        body={"messages": [{"role": "user", "content": "hello"}]},
        nodes=single,
        aliases={},
        control=ControlState(),
        guards=GuardConfig(),
    )
    assert dec is not None
    assert dec.node.node_id == "local"
    assert dec.target_model == "fast-small"


def test_unmounted_disk_blocks_node_and_falls_back() -> None:
    dec, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="fast",
        body={"messages": [{"role": "user", "content": "hi"}]},
        nodes=_make_nodes(standby_mounted=False),
        aliases=_make_aliases(),
        control=ControlState(),
        guards=GuardConfig(),
    )
    assert dec is not None
    assert dec.node.node_id == "gpu-rig"
    assert dec.fallback_reason == "always-on_storage_unmounted"


def test_force_node_and_node_prefix() -> None:
    dec, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="fast",
        body={"messages": [{"role": "user", "content": "hi"}]},
        nodes=_make_nodes(),
        aliases=_make_aliases(),
        control=ControlState(force_node="gpu-rig"),
        guards=GuardConfig(),
    )
    assert dec is not None and dec.node.node_id == "gpu-rig" and dec.fallback_reason == "forced_gpu-rig"

    prefixed, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="always-on/fast-small",
        body={"messages": [{"role": "user", "content": "hi"}]},
        nodes=_make_nodes(),
        aliases=_make_aliases(),
        control=ControlState(),
        guards=GuardConfig(),
    )
    assert prefixed is not None and prefixed.node.node_id == "always-on" and prefixed.target_model == "fast-small"


def test_pmon_and_multi_engine_models_parsing() -> None:
    sample = """
# gpu         pid   type     fb   ccpm    command
# Idx           #    C/G     MB     MB    name
    0        3483    C+G    169      0    kwin_wayland
    0        9001      C  18500      0    llama-server
    0        9002      G   6400      0    Cyberpunk2077.exe
    0        9003      C   4200      0    ollama
"""
    procs = _parse_pmon(sample)
    assert len(procs) == 4
    assert procs[1].is_llm is True
    assert procs[2].is_llm is False
    assert procs[3].is_llm is True
    non_llm = sum(p.vram_mb for p in procs if not p.is_llm)
    assert non_llm == 6569.0

    models = _normalize_models(
        {
            "data": [
                {"id": "fast-small", "status": {"value": "loaded"}},
                {"id": "embed-bge", "status": {"value": "unloaded"}},
            ]
        }
    )
    assert [(m.id, m.status) for m in models] == [("fast-small", "loaded"), ("embed-bge", "unloaded")]

    ollama_models = _normalize_ollama_models(
        {"models": [{"name": "qwen3:8b"}, {"name": "nomic-embed-text:latest"}]},
        {"models": [{"name": "qwen3:8b"}]},
    )
    assert [(m.id, m.status) for m in ollama_models] == [
        ("qwen3:8b", "loaded"),
        ("nomic-embed-text:latest", "unloaded"),
    ]


def test_responses_and_anthropic_prompt_estimation_and_sse_tokens() -> None:
    from backend.router.policy import estimate_prompt_chars
    from backend.router.proxy import _extract_tokens_from_sse, _extract_usage_tokens

    # OpenAI Responses API structured input + instructions
    resp_chars = estimate_prompt_chars(
        {
            "instructions": "Be concise.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "a" * 5000}]},
            ],
        }
    )
    assert resp_chars >= 5011

    # Anthropic Messages API system blocks + content blocks
    anth_chars = estimate_prompt_chars(
        {
            "system": [{"type": "text", "text": "You are an engineer."}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "b" * 3000}]},
            ],
        }
    )
    assert anth_chars >= 3020

    # SSE token extraction across Chat Completions, Responses API, and Anthropic Messages
    assert _extract_tokens_from_sse(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n') == 1
    assert _extract_tokens_from_sse(b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n') == 1
    assert _extract_tokens_from_sse(b'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n') == 1
    assert _extract_tokens_from_sse(b'data: {"type":"message_delta","usage":{"output_tokens":42}}\n\n') == 42
    assert _extract_usage_tokens({"response": {"usage": {"output_tokens": 77}}}) == 77
    assert _extract_usage_tokens({"usage": {"output_tokens": 19}}) == 19


def test_multi_protocol_routes_registered_on_router_app() -> None:
    from backend.router.app import app

    paths = set(app.openapi()["paths"].keys())
    assert "/v1/models" in paths
    assert "/v1/chat/completions" in paths
    assert "/v1/responses" in paths
    assert "/v1/messages" in paths
    assert "/v1/messages/count_tokens" in paths
    assert "/v1/completions" in paths
    assert "/v1/embeddings" in paths
    assert "/v1/images/generations" in paths
    assert "/v1/videos/generations" in paths
    assert "/infill" in paths


def test_post_splitch_work_gpu_util_grace_prevents_false_cooldown() -> None:
    """Cached 95% GPU util right after a Splitch media/LLM job must not trigger gpu_busy."""
    import time

    from backend.router.policy import check_node_eligibility

    nodes = _make_nodes(gpu_util=98.0, gpu_non_llm_vram=900.0)
    gpu = nodes["gpu-rig"]
    guards = GuardConfig()
    control = ControlState()

    # Without recent Splitch work, 98% GPU util flags gpu_busy
    assert check_node_eligibility(gpu, "coder-heavy", control, guards) == "gpu-rig_gpu_busy"

    # Immediately after a Splitch job finishes (in_flight == 0), grace window prevents false yield
    gpu.last_splitch_work_ts = time.time()
    assert check_node_eligibility(gpu, "coder-heavy", control, guards) is None

    # A real non-LLM game (high non_llm_vram_mb) STILL yields immediately despite grace window
    assert gpu.telemetry is not None
    gpu.telemetry.non_llm_vram_mb = 6144.0
    assert check_node_eligibility(gpu, "coder-heavy", control, guards) == "gpu-rig_gpu_busy_app"
