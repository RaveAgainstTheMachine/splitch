"""
End-to-end tests for Splitch's 3 Harnesses:
  1. Repo / CI / Pre-Flight Audit Harness (`AGENTS.md`, `scripts/preflight.sh`, `.github/workflows/ci.yml`)
  2. Cluster & Model Benchmark / Eval Harness (`backend/shared/bench.py`, `/api/bench/*`, `splitch bench`)
  3. Agentic Execution Harness:
     - Native Anthropic `/v1/messages` & `/v1/messages/count_tokens` adapter (non-stream, SSE stream,
       thinking blocks, tool_use/tool_result multi-turn translation, `claude-*` model mapping)
     - Surgical `edit_file` tool + automatic post-edit syntax/lint verification (`_run_post_edit_verification`)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.agent.main import _exec_fs_tool, _run_post_edit_verification
from backend.router.anthropic_api import (
    anthropic_to_openai_request,
    map_anthropic_model_name,
    openai_to_anthropic_response,
)
from backend.router.app import app as router_app
from backend.router.app import cluster
from backend.shared.bench import (
    BenchRunRequest,
    format_bench_table,
    run_benchmark_suite,
)
from backend.shared.models import (
    AliasConfig,
    LoadedModel,
    NodeStatus,
    NodeTelemetry,
    ToolExecRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_harness_1_preflight_and_ci_artifacts_exist() -> None:
    agents_md = REPO_ROOT / "AGENTS.md"
    preflight_sh = REPO_ROOT / "scripts" / "preflight.sh"
    ci_yml = REPO_ROOT / ".github" / "workflows" / "ci.yml"

    assert agents_md.is_file()
    assert "./scripts/preflight.sh" in agents_md.read_text(encoding="utf-8")
    assert preflight_sh.is_file()
    assert ci_yml.is_file()
    ci_text = ci_yml.read_text(encoding="utf-8")
    assert "ruff check" in ci_text
    assert "pytest" in ci_text
    assert "npm run build" in ci_text


def test_harness_3_edit_file_and_post_edit_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("backend.agent.main.WORKSPACE_ROOT", str(tmp_path))

    # 1. write_file on valid Python file triggers py_compile/ruff verification
    w_res = _exec_fs_tool(
        ToolExecRequest(
            tool="write_file",
            arguments={"path": "calc.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"},
        )
    )
    assert w_res.ok is True
    assert w_res.verification is not None
    assert w_res.verification["checked"] is True
    assert w_res.verification["ok"] is True
    assert "post-edit verification" in w_res.output

    # 2. edit_file surgically replaces target and runs verification
    e_res = _exec_fs_tool(
        ToolExecRequest(
            tool="edit_file",
            arguments={
                "path": "calc.py",
                "target": "return a + b",
                "replacement": "return (a + b) * 2",
            },
        )
    )
    assert e_res.ok is True
    assert e_res.diff is not None and "+    return (a + b) * 2" in e_res.diff
    assert e_res.verification is not None and e_res.verification["ok"] is True
    assert "return (a + b) * 2" in (tmp_path / "calc.py").read_text(encoding="utf-8")

    # 3. edit_file with syntax error reports verification failure back to the LLM
    bad_py = _exec_fs_tool(
        ToolExecRequest(
            tool="edit_file",
            arguments={
                "path": "calc.py",
                "target": "return (a + b) * 2",
                "replacement": "return (a + b",
            },
        )
    )
    assert bad_py.ok is True
    assert bad_py.verification is not None
    assert bad_py.verification["checked"] is True
    assert bad_py.verification["ok"] is False
    assert "✗" in bad_py.output

    # 4. JSON, TOML, and POSIX shell verification
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{invalid", encoding="utf-8")
    v_json = _run_post_edit_verification(bad_json, tmp_path)
    assert v_json["checked"] is True and v_json["ok"] is False

    good_toml = tmp_path / "ok.toml"
    good_toml.write_text('title = "splitch"\n', encoding="utf-8")
    v_toml = _run_post_edit_verification(good_toml, tmp_path)
    assert v_toml["checked"] is True and v_toml["ok"] is True

    bad_sh = tmp_path / "bad.sh"
    bad_sh.write_text("if [ 1 -eq 1 ]; then\n  echo hi\n", encoding="utf-8")
    v_sh = _run_post_edit_verification(bad_sh, tmp_path)
    assert v_sh["checked"] is True and v_sh["ok"] is False


def test_harness_3_anthropic_messages_translation_and_model_mapping() -> None:
    aliases = {
        "heavy": AliasConfig(
            alias="heavy",
            task_kind="code",
            primary_node="gpu-rig",
            primary_model="qwen3-coder-30b",
        ),
        "fast": AliasConfig(
            alias="fast",
            task_kind="chat",
            primary_node="mac-studio",
            primary_model="qwen3-8b",
        ),
    }
    nodes: dict[str, NodeStatus] = {}
    assert map_anthropic_model_name("claude-3-7-sonnet-20250219", aliases, nodes) == "heavy"
    assert map_anthropic_model_name("claude-sonnet-4-20250514", aliases, nodes) == "heavy"
    assert map_anthropic_model_name("claude-3-5-haiku-20241022", aliases, nodes) == "fast"
    assert map_anthropic_model_name("heavy", aliases, nodes) == "heavy"

    anth_req = {
        "model": "claude-3-7-sonnet-20250219",
        "max_tokens": 512,
        "temperature": 0.2,
        "system": [{"type": "text", "text": "You are a systems architect."}],
        "tools": [
            {
                "name": "read_file",
                "description": "Read file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
        "tool_choice": {"type": "any"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Inspect main.py"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Reading main.py now."},
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "read_file",
                        "input": {"path": "main.py"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_123",
                        "content": "print('hello')",
                    }
                ],
            },
        ],
    }

    oai_req = anthropic_to_openai_request(anth_req, "qwen3-coder-30b")
    assert oai_req["model"] == "qwen3-coder-30b"
    assert oai_req["tool_choice"] == "required"
    assert oai_req["tools"][0]["function"]["name"] == "read_file"
    assert oai_req["messages"][0] == {"role": "system", "content": "You are a systems architect."}
    assert oai_req["messages"][2]["role"] == "assistant"
    assert oai_req["messages"][2]["tool_calls"][0]["id"] == "toolu_123"
    assert oai_req["messages"][3] == {
        "role": "tool",
        "tool_call_id": "toolu_123",
        "content": "print('hello')",
    }

    oai_resp = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "reasoning_content": "Checking imports.",
                    "content": "Calling read_file.",
                    "tool_calls": [
                        {
                            "id": "toolu_456",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"app.py"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 84, "completion_tokens": 29},
    }
    anth_out = openai_to_anthropic_response(
        oai_resp, requested_model="claude-3-7-sonnet-20250219"
    )
    assert anth_out["type"] == "message"
    assert anth_out["stop_reason"] == "tool_use"
    assert anth_out["usage"]["input_tokens"] == 84
    assert anth_out["usage"]["output_tokens"] == 29
    types = [b["type"] for b in anth_out["content"]]
    assert types == ["thinking", "text", "tool_use"]
    assert anth_out["content"][2]["input"] == {"path": "app.py"}


@pytest.mark.asyncio
async def test_harness_2_and_3_live_router_anthropic_and_bench() -> None:
    # Build a mock OpenAI engine app that handles streaming, tool calls, and JSON eval
    mock_engine = FastAPI()

    @mock_engine.post("/v1/chat/completions")
    async def mock_chat_completions(request: Request):
        body: dict[str, Any] = await request.json()
        if body.get("stream"):
            async def _gen():
                chunks = [
                    {
                        "choices": [
                            {
                                "delta": {"reasoning_content": "Analyzing tensor parallelism."},
                                "finish_reason": None,
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "delta": {"content": "1. Splits weights across GPUs.\n2. Quantizes KV cache."},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 36, "completion_tokens": 24},
                    },
                ]
                for c in chunks:
                    yield f"data: {json.dumps(c)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(_gen(), media_type="text/event-stream")

        if body.get("tools"):
            return JSONResponse(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_vram_1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_node_vram",
                                            "arguments": '{"node_id":"gpu-rig","unit":"gb"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 42, "completion_tokens": 18},
                }
            )

        return JSONResponse(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"service": "splitch", "healthy": true}',
                        },
                    }
                ],
                "usage": {"prompt_tokens": 28, "completion_tokens": 12},
            }
        )

    old_nodes = dict(cluster.nodes)
    old_aliases = dict(cluster.aliases)
    old_client = cluster._proxy_client
    mock_proxy = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_engine),
        base_url="http://mock-engine",
    )
    try:
        cluster.nodes.clear()
        cluster.nodes["gpu-rig"] = NodeStatus(
            node_id="gpu-rig",
            display_name="GPU Workstation",
            role="gpu",
            agent_url="http://mock-engine:8090",
            engine_url="http://mock-engine",
            online=True,
            telemetry=NodeTelemetry(
                node_id="gpu-rig",
                role="gpu",
                platform="linux",
                timestamp=1.0,
                vram_total_mb=24576.0,
                vram_free_mb=20480.0,
                storage_mounted=True,
                engine="llama-server",
                engine_healthy=True,
                models=[LoadedModel(id="qwen3-coder-30b", status="loaded")],
            ),
        )
        cluster.aliases["heavy"] = AliasConfig(
            alias="heavy",
            task_kind="code",
            primary_node="gpu-rig",
            primary_model="qwen3-coder-30b",
        )
        cluster._proxy_client = mock_proxy

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router_app),
            base_url="http://127.0.0.1:8080",
        ) as b_client:
            # 1. Anthropic /v1/messages/count_tokens
            ct_res = await b_client.post(
                "/v1/messages/count_tokens",
                json={
                    "model": "claude-3-7-sonnet-20250219",
                    "system": "Be concise.",
                    "messages": [{"role": "user", "content": "Hello world"}],
                },
            )
            assert ct_res.status_code == 200
            assert ct_res.json()["input_tokens"] > 0

            # 2. Anthropic /v1/messages non-streaming with tool call
            m_res = await b_client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-7-sonnet-20250219",
                    "max_tokens": 128,
                    "tools": [
                        {
                            "name": "get_node_vram",
                            "description": "Get VRAM",
                            "input_schema": {"type": "object", "properties": {}},
                        }
                    ],
                    "messages": [{"role": "user", "content": "Check gpu-rig VRAM"}],
                },
            )
            assert m_res.status_code == 200
            assert m_res.headers.get("x-splitch-node") == "gpu-rig"
            m_body = m_res.json()
            assert m_body["type"] == "message"
            assert m_body["stop_reason"] == "tool_use"
            assert any(
                b["type"] == "tool_use" and b["name"] == "get_node_vram"
                for b in m_body["content"]
            )

            # 3. Anthropic /v1/messages SSE streaming
            s_res = await b_client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-7-sonnet-20250219",
                    "max_tokens": 128,
                    "stream": True,
                    "messages": [{"role": "user", "content": "Explain tensor parallelism"}],
                },
            )
            assert s_res.status_code == 200
            sse_text = s_res.text
            assert "event: message_start" in sse_text
            assert '"type": "thinking_delta"' in sse_text
            assert '"type": "text_delta"' in sse_text
            assert "event: message_delta" in sse_text
            assert "event: message_stop" in sse_text

            # 4. Benchmark harness suite (`run_benchmark_suite` + `format_bench_table`)
            report = await run_benchmark_suite(
                BenchRunRequest(models=["heavy"], max_tokens=64, concurrency=2),
                router_url="http://127.0.0.1:8080",
                client=b_client,
            )
            assert len(report.results) == 1
            r0 = report.results[0]
            assert r0.ok is True
            assert r0.resolved_node == "gpu-rig"
            assert r0.resolved_model == "qwen3-coder-30b"
            assert r0.tool_eval_passed is True
            assert r0.json_eval_passed is True
            table = format_bench_table(report)
            assert "gpu-rig" in table and "PASS" in table
    finally:
        await mock_proxy.aclose()
        cluster._proxy_client = old_client
        cluster.nodes.clear()
        cluster.nodes.update(old_nodes)
        cluster.aliases.clear()
        cluster.aliases.update(old_aliases)
