"""
Splitch Cluster & Model Benchmark / Eval Harness (`splitch bench` & `/api/bench/*`).

Measures across 1..N target models or aliases:
  1. Prompt Processing (`pp_tokens_per_sec`) & Time-To-First-Token (`ttft_ms`)
  2. Token Generation speed (`tg_tokens_per_sec`) & total latency (`duration_ms`)
  3. Concurrent aggregate cluster/node throughput (`aggregate_tokens_per_sec` at `--concurrency N`)
  4. Native Function/Tool-Calling accuracy (`tool_eval_passed`)
  5. Structured JSON adherence (`json_eval_passed`)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

DEFAULT_BENCH_PROMPT = (
    "Explain in three concise bullet points how multi-GPU tensor parallelism and KV-cache "
    "quantization reduce memory bandwidth bottlenecks during local LLM inference."
)

BENCH_TOOL_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_node_vram",
            "description": "Retrieve current GPU VRAM usage and temperature for a cluster node.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "Node identifier slug (e.g. 'gpu-rig')",
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["mb", "gb"],
                        "description": "Memory unit",
                    },
                },
                "required": ["node_id", "unit"],
            },
        },
    }
]


class BenchRunRequest(BaseModel):
    models: list[str] = Field(default_factory=lambda: ["auto"])
    max_tokens: int = Field(default=96, ge=8, le=1024)
    concurrency: int = Field(default=1, ge=1, le=16)
    include_tool_eval: bool = True
    include_json_eval: bool = True
    prompt: str = ""


class BenchTargetResult(BaseModel):
    requested_model: str
    resolved_node: str = ""
    resolved_model: str = ""
    ok: bool = True
    ttft_ms: float | None = None
    duration_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    pp_tokens_per_sec: float | None = None
    tg_tokens_per_sec: float | None = None
    aggregate_tokens_per_sec: float | None = None
    tool_eval_passed: bool | None = None
    tool_eval_detail: str | None = None
    json_eval_passed: bool | None = None
    error: str | None = None


class BenchReport(BaseModel):
    id: str = Field(default_factory=lambda: f"bench_{uuid.uuid4().hex[:10]}")
    timestamp: float = Field(default_factory=time.time)
    router_url: str = "http://127.0.0.1:8080"
    concurrency: int = 1
    max_tokens: int = 96
    results: list[BenchTargetResult] = Field(default_factory=list)


async def _single_stream_probe(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    headers: dict[str, str],
) -> dict[str, Any]:
    started = time.perf_counter()
    ttft_ms: float | None = None
    comp_tokens = 0
    prompt_tokens = max(1, (len(prompt) + 3) // 4)
    resolved_node = ""
    resolved_model = model

    req_body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    async with client.stream(
        "POST",
        f"{base_url.rstrip('/')}/v1/chat/completions",
        json=req_body,
        headers=headers,
    ) as resp:
        resolved_node = resp.headers.get("x-splitch-node", "")
        resolved_model = resp.headers.get("x-splitch-model", model)
        if resp.status_code >= 400:
            err_text = (await resp.aread()).decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"HTTP {resp.status_code}: {err_text}")

        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                chunk = json.loads(raw)
            except ValueError:
                continue

            usage = chunk.get("usage")
            if isinstance(usage, dict):
                if isinstance(usage.get("prompt_tokens"), int) and usage["prompt_tokens"] > 0:
                    prompt_tokens = usage["prompt_tokens"]
                if isinstance(usage.get("completion_tokens"), int) and usage["completion_tokens"] > 0:
                    comp_tokens = usage["completion_tokens"]

            choices = chunk.get("choices") or []
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content"):
                    if ttft_ms is None:
                        ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                    comp_tokens += 1

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    if ttft_ms is None:
        ttft_ms = elapsed_ms
    return {
        "resolved_node": resolved_node,
        "resolved_model": resolved_model,
        "ttft_ms": ttft_ms,
        "duration_ms": elapsed_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": max(1, comp_tokens),
    }


async def _run_tool_eval(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    headers: dict[str, str],
) -> tuple[bool, str]:
    req_body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Call the get_node_vram tool for node_id 'gpu-rig' with unit 'gb'.",
            }
        ],
        "tools": BENCH_TOOL_SCHEMA,
        "tool_choice": "auto",
        "temperature": 0.0,
        "max_tokens": 120,
        "stream": False,
    }
    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            json=req_body,
            headers=headers,
        )
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}"
        data = resp.json()
        choices = data.get("choices") or []
        msg = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        tcs = (msg or {}).get("tool_calls") or []
        if not tcs or not isinstance(tcs[0], dict):
            return False, "No tool_calls emitted"
        fn = tcs[0].get("function") or {}
        name = str(fn.get("name") or "")
        raw_args = fn.get("arguments") or "{}"
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        if name == "get_node_vram" and isinstance(args, dict) and args.get("node_id") == "gpu-rig":
            return True, f"get_node_vram({json.dumps(args)})"
        return False, f"Unexpected call: {name}({raw_args})"
    except (httpx.HTTPError, OSError, RuntimeError, ValueError, TypeError, KeyError, IndexError) as exc:
        return False, str(exc)[:160]


async def _run_json_eval(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    headers: dict[str, str],
) -> bool:
    req_body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    'Respond with ONLY a valid JSON object containing keys "service" '
                    '(set to "splitch") and "healthy" (set to true). No markdown fences.'
                ),
            }
        ],
        "temperature": 0.0,
        "max_tokens": 384,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": False,
    }
    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            json=req_body,
            headers=headers,
        )
        if resp.status_code >= 400:
            return False
        data = resp.json()
        choices = data.get("choices") or []
        content = str((choices[0].get("message") or {}).get("content") or "").strip()
        if "</think>" in content:
            content = content.split("</think>", 1)[1].strip()
        if content.startswith("```"):
            lines = [ln for ln in content.splitlines() if not ln.strip().startswith("```")]
            content = "\n".join(lines).strip()
        start_brace = content.find("{")
        end_brace = content.rfind("}")
        if start_brace != -1 and end_brace > start_brace:
            content = content[start_brace : end_brace + 1]
        parsed = json.loads(content)
        return (
            isinstance(parsed, dict)
            and str(parsed.get("service", "")).lower() == "splitch"
            and parsed.get("healthy") is True
        )
    except (httpx.HTTPError, OSError, RuntimeError, ValueError, TypeError, KeyError, IndexError):
        return False


async def run_benchmark_suite(
    req: BenchRunRequest,
    *,
    router_url: str = "http://127.0.0.1:8080",
    api_key: str = "",
    client: httpx.AsyncClient | None = None,
) -> BenchReport:
    prompt = req.prompt.strip() or DEFAULT_BENCH_PROMPT
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=5.0))
    results: list[BenchTargetResult] = []

    try:
        targets = [m.strip() for m in req.models if m.strip()] or ["auto"]
        for model in targets:
            try:
                batch_started = time.perf_counter()
                probes = await asyncio.gather(
                    *[
                        _single_stream_probe(
                            http, router_url, model, prompt, req.max_tokens, headers
                        )
                        for _ in range(req.concurrency)
                    ]
                )
                batch_s = max(0.001, time.perf_counter() - batch_started)

                first = probes[0]
                avg_ttft = round(sum(p["ttft_ms"] for p in probes) / len(probes), 1)
                avg_dur = round(sum(p["duration_ms"] for p in probes) / len(probes), 1)
                total_comp = sum(p["completion_tokens"] for p in probes)
                p_toks = int(first["prompt_tokens"])
                c_toks = round(total_comp / len(probes))

                pp_tps = round(p_toks / max(0.001, avg_ttft / 1000.0), 1)
                gen_s = max(0.001, (avg_dur - avg_ttft) / 1000.0)
                tg_tps = round(c_toks / gen_s, 1)
                agg_tps = round(total_comp / batch_s, 1)

                tool_ok: bool | None = None
                tool_detail: str | None = None
                if req.include_tool_eval:
                    tool_ok, tool_detail = await _run_tool_eval(http, router_url, model, headers)

                json_ok: bool | None = None
                if req.include_json_eval:
                    json_ok = await _run_json_eval(http, router_url, model, headers)

                results.append(
                    BenchTargetResult(
                        requested_model=model,
                        resolved_node=first["resolved_node"],
                        resolved_model=first["resolved_model"],
                        ok=True,
                        ttft_ms=avg_ttft,
                        duration_ms=avg_dur,
                        prompt_tokens=p_toks,
                        completion_tokens=c_toks,
                        pp_tokens_per_sec=pp_tps,
                        tg_tokens_per_sec=tg_tps,
                        aggregate_tokens_per_sec=agg_tps,
                        tool_eval_passed=tool_ok,
                        tool_eval_detail=tool_detail,
                        json_eval_passed=json_ok,
                    )
                )
            except (httpx.HTTPError, OSError, RuntimeError, ValueError, TypeError) as exc:
                results.append(
                    BenchTargetResult(
                        requested_model=model,
                        ok=False,
                        error=str(exc)[:300],
                    )
                )
    finally:
        if owns_client:
            await http.aclose()

    return BenchReport(
        router_url=router_url,
        concurrency=req.concurrency,
        max_tokens=req.max_tokens,
        results=results,
    )


def load_bench_history(data_dir: Path) -> list[BenchReport]:
    path = data_dir / "bench_history.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [BenchReport.model_validate(item) for item in raw if isinstance(item, dict)]
    except (OSError, ValueError):
        return []


def append_bench_history(data_dir: Path, report: BenchReport, *, max_entries: int = 25) -> None:
    history = load_bench_history(data_dir)
    history.insert(0, report)
    trimmed = history[:max_entries]
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "bench_history.json").write_text(
        json.dumps([r.model_dump(mode="json") for r in trimmed], indent=2),
        encoding="utf-8",
    )


def format_bench_table(report: BenchReport) -> str:
    header_title = (
        f"Splitch Benchmark Report ({report.id}) — target: {report.router_url} "
        f"(concurrency={report.concurrency}, max_tokens={report.max_tokens})"
    )
    header_cols = (
        f"{'TARGET':16s} {'NODE':12s} {'RESOLVED MODEL':24s} {'TTFT':>8s} {'PP t/s':>8s} "
        f"{'TG t/s':>8s} {'AGG t/s':>8s} {'TOOLS':>7s} {'JSON':>6s}"
    )
    lines = [
        header_title,
        "─" * 104,
        header_cols,
        "─" * 104,
    ]
    for r in report.results:
        if not r.ok:
            lines.append(f"{r.requested_model:16s} {'ERR':12s} {(r.error or 'failed')[:68]}")
            continue
        ttft_s = f"{r.ttft_ms:.0f}ms" if r.ttft_ms is not None else "—"
        pp_s = f"{r.pp_tokens_per_sec:.1f}" if r.pp_tokens_per_sec is not None else "—"
        tg_s = f"{r.tg_tokens_per_sec:.1f}" if r.tg_tokens_per_sec is not None else "—"
        agg_s = (
            f"{r.aggregate_tokens_per_sec:.1f}"
            if r.aggregate_tokens_per_sec is not None
            else "—"
        )
        tool_s = "—" if r.tool_eval_passed is None else ("PASS" if r.tool_eval_passed else "FAIL")
        json_s = "—" if r.json_eval_passed is None else ("PASS" if r.json_eval_passed else "FAIL")
        lines.append(
            f"{r.requested_model[:16]:16s} {(r.resolved_node or '—')[:12]:12s} "
            f"{(r.resolved_model or '—')[:24]:24s} {ttft_s:>8s} {pp_s:>8s} "
            f"{tg_s:>8s} {agg_s:>8s} {tool_s:>7s} {json_s:>6s}"
        )
    lines.append("─" * 104)
    return "\n".join(lines)
