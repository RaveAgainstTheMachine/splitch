"""
Native Anthropic Messages API (`/v1/messages` & `/v1/messages/count_tokens`) compatibility harness.

Enables external coding agent harnesses (Claude Code, OpenCode, Aider, Cline, Continue) to
connect directly to Splitch (`ANTHROPIC_BASE_URL=http://<router>:8080`) across any local
inference backend (`llama-server`, Ollama, LM Studio, vLLM) — supporting:
  - Automatic `claude-*` model name mapping (`*haiku*` -> `fast`, `*sonnet*`/`*opus*` -> `heavy`, or direct model/alias)
  - System prompts (string or block list), multi-turn `tool_use` / `tool_result` blocks, and base64/URL images
  - Tool definitions (`input_schema` -> OpenAI `parameters`) & `tool_choice` (`auto`, `any`, `tool`, `none`)
  - Both non-streaming JSON responses and full Anthropic SSE event streams (`message_start`,
    `content_block_start`, `content_block_delta` for `text_delta`, `thinking_delta`, and
    `input_json_delta`, `content_block_stop`, `message_delta`, `message_stop`)
  - Deterministic `/v1/messages/count_tokens` endpoint
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from backend.router.policy import RouteDecision, estimate_prompt_chars, resolve_route
from backend.router.telemetry import ClusterState
from backend.shared.models import AliasConfig, NodeStatus, TraceEntry


def map_anthropic_model_name(
    requested_model: str,
    aliases: dict[str, AliasConfig],
    nodes: dict[str, NodeStatus],
) -> str:
    """
    Map external `claude-*` model identifiers from Claude Code / Aider / Cline onto Splitch
    aliases when no literal `claude-*` alias or model is loaded in the cluster.
    """
    req = (requested_model or "auto").strip()
    if not req:
        return "auto"
    if req in aliases or "/" in req:
        return req
    for node in nodes.values():
        if node.online and node.telemetry and any(m.id == req for m in node.telemetry.models):
            return req
    lower = req.lower()
    if lower.startswith("claude-"):
        if "haiku" in lower and "fast" in aliases:
            return "fast"
        if "heavy" in aliases:
            return "heavy"
        return "auto"
    return req


def _extract_system_text(system_val: Any) -> str:
    if isinstance(system_val, str):
        return system_val.strip()
    if isinstance(system_val, list):
        parts: list[str] = []
        for item in system_val:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict) and item.get("type") == "text":
                txt = str(item.get("text") or "").strip()
                if txt:
                    parts.append(txt)
        return "\n\n".join(parts)
    return ""


def _stringify_tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(json.dumps(item))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content)


def anthropic_to_openai_request(body: dict[str, Any], target_model: str) -> dict[str, Any]:
    """Convert an Anthropic `/v1/messages` request payload into an OpenAI `/v1/chat/completions` payload."""
    openai_msgs: list[dict[str, Any]] = []

    sys_text = _extract_system_text(body.get("system"))
    if sys_text:
        openai_msgs.append({"role": "system", "content": sys_text})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")

        if isinstance(content, str):
            openai_msgs.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    txt = str(block.get("text") or "")
                    if txt:
                        text_parts.append(txt)
                elif btype == "tool_use":
                    tc_id = str(block.get("id") or f"toolu_{uuid.uuid4().hex[:12]}")
                    tc_name = str(block.get("name") or "tool")
                    tc_input = block.get("input")
                    args_str = (
                        tc_input
                        if isinstance(tc_input, str)
                        else json.dumps(tc_input if isinstance(tc_input, dict) else {})
                    )
                    tool_calls.append(
                        {
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": tc_name, "arguments": args_str},
                        }
                    )
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(text_parts) if text_parts else None,
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            openai_msgs.append(entry)
        else:
            # User turn: may contain text, image, and/or tool_result blocks
            user_parts: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    txt = str(block.get("text") or "")
                    if txt:
                        user_parts.append({"type": "text", "text": txt})
                elif btype == "image":
                    src = block.get("source") or {}
                    if isinstance(src, dict):
                        if src.get("type") == "base64" and src.get("data"):
                            media_type = src.get("media_type") or "image/png"
                            data_url = f"data:{media_type};base64,{src['data']}"
                            user_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                        elif src.get("type") == "url" and src.get("url"):
                            user_parts.append(
                                {"type": "image_url", "image_url": {"url": str(src["url"])}}
                            )
                elif btype == "tool_result":
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id") or "toolu_0"),
                            "content": _stringify_tool_result_content(block.get("content")),
                        }
                    )

            openai_msgs.extend(tool_results)
            if user_parts:
                if len(user_parts) == 1 and user_parts[0].get("type") == "text":
                    openai_msgs.append({"role": "user", "content": user_parts[0]["text"]})
                else:
                    openai_msgs.append({"role": "user", "content": user_parts})

    out: dict[str, Any] = {
        "model": target_model,
        "messages": openai_msgs,
        "stream": bool(body.get("stream", False)),
    }
    if out["stream"]:
        out["stream_options"] = {"include_usage": True}

    if isinstance(body.get("max_tokens"), int):
        out["max_tokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    if isinstance(body.get("stop_sequences"), list) and body["stop_sequences"]:
        out["stop"] = body["stop_sequences"]

    anth_tools = body.get("tools")
    if isinstance(anth_tools, list) and anth_tools:
        oai_tools: list[dict[str, Any]] = []
        for t in anth_tools:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            oai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": str(t["name"]),
                        "description": str(t.get("description") or ""),
                        "parameters": t.get("input_schema")
                        or t.get("parameters")
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        if oai_tools:
            out["tools"] = oai_tools

    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        tc_type = tc.get("type")
        if tc_type == "auto":
            out["tool_choice"] = "auto"
        elif tc_type == "any":
            out["tool_choice"] = "required"
        elif tc_type == "none":
            out["tool_choice"] = "none"
        elif tc_type == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": str(tc["name"])}}

    return out


def openai_to_anthropic_response(
    oai_data: dict[str, Any],
    *,
    requested_model: str,
) -> dict[str, Any]:
    """Convert a non-streaming OpenAI Chat Completion JSON object into an Anthropic Message object."""
    choices = oai_data.get("choices") or []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    msg = first.get("message") or {}
    finish_reason = first.get("finish_reason")

    content_blocks: list[dict[str, Any]] = []
    reasoning = msg.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        content_blocks.append({"type": "thinking", "thinking": reasoning})

    text = msg.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})

    tool_calls = msg.get("tool_calls") or []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed_input = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(parsed_input, dict):
                parsed_input = {"value": parsed_input}
        except ValueError:
            parsed_input = {"_raw": str(raw_args)}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": str(tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"),
                "name": str(fn.get("name") or "tool"),
                "input": parsed_input,
            }
        )

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    if tool_calls or finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"

    usage = oai_data.get("usage") or {}
    in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    return {
        "id": f"msg_{uuid.uuid4().hex[:20]}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _anth_sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


async def handle_anthropic_count_tokens(request: Request, state: ClusterState) -> JSONResponse:
    try:
        body: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc

    chars = estimate_prompt_chars(body)
    if isinstance(body.get("tools"), list):
        chars += len(json.dumps(body["tools"]))
    est_tokens = max(1, (chars + 3) // 4)
    return JSONResponse(content={"input_tokens": est_tokens})


async def handle_anthropic_messages(request: Request, state: ClusterState) -> Response:
    try:
        body: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc

    raw_requested_model = str(body.get("model") or "auto")
    mapped_model = map_anthropic_model_name(raw_requested_model, state.aliases, state.nodes)
    is_stream = bool(body.get("stream", False))

    decision: RouteDecision | None
    decision, skip_reasons = resolve_route(
        endpoint="/v1/messages",
        requested_model=mapped_model,
        body=body,
        nodes=state.nodes,
        aliases=state.aliases,
        control=state.control,
        guards=state.config.guards,
        heavy_char_threshold=state.config.router.auto_heavy_char_threshold,
    )
    if decision is None:
        raise HTTPException(
            status_code=503,
            detail={
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": f"No eligible Splitch node available for '{raw_requested_model}': {skip_reasons}",
                },
            },
        )

    oai_payload = anthropic_to_openai_request(body, decision.target_model)
    target_url, upstream_auth = state.resolve_upstream_target(
        decision, "/v1/chat/completions"
    )
    extra_headers = {
        "X-Splitch-Node": decision.node.node_id,
        "X-Splitch-Model": decision.target_model,
    }
    if decision.fallback_reason:
        extra_headers["X-Splitch-Fallback"] = decision.fallback_reason

    trace_id = uuid.uuid4().hex[:10]
    started = time.perf_counter()

    if not is_stream:
        decision.node.in_flight += 1
        try:
            upstream = await state.proxy_client.post(
                target_url, json=oai_payload, auth=upstream_auth
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            if upstream.status_code >= 400:
                asyncio.create_task(
                    state.record_trace(
                        TraceEntry(
                            id=trace_id,
                            timestamp=time.time(),
                            endpoint="/v1/messages",
                            requested_model=raw_requested_model,
                            resolved_node=decision.node.node_id,
                            resolved_model=decision.target_model,
                            fallback_reason=decision.fallback_reason,
                            status_code=upstream.status_code,
                            stream=False,
                            duration_ms=elapsed_ms,
                            error=upstream.text[:300],
                        )
                    )
                )
                return JSONResponse(
                    status_code=upstream.status_code,
                    content={
                        "type": "error",
                        "error": {"type": "api_error", "message": upstream.text[:500]},
                    },
                    headers=extra_headers,
                )

            oai_data = upstream.json()
            anth_resp = openai_to_anthropic_response(
                oai_data, requested_model=raw_requested_model
            )
            out_toks = int((anth_resp.get("usage") or {}).get("output_tokens") or 0)
            tps = round(out_toks / max(0.001, elapsed_ms / 1000.0), 1) if out_toks > 0 else None
            asyncio.create_task(
                state.record_trace(
                    TraceEntry(
                        id=trace_id,
                        timestamp=time.time(),
                        endpoint="/v1/messages",
                        requested_model=raw_requested_model,
                        resolved_node=decision.node.node_id,
                        resolved_model=decision.target_model,
                        fallback_reason=decision.fallback_reason,
                        status_code=200,
                        stream=False,
                        ttft_ms=elapsed_ms,
                        duration_ms=elapsed_ms,
                        completion_tokens=out_toks,
                        tokens_per_sec=tps,
                    )
                )
            )
            return JSONResponse(content=anth_resp, status_code=200, headers=extra_headers)
        except httpx.HTTPError as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            asyncio.create_task(
                state.record_trace(
                    TraceEntry(
                        id=trace_id,
                        timestamp=time.time(),
                        endpoint="/v1/messages",
                        requested_model=raw_requested_model,
                        resolved_node=decision.node.node_id,
                        resolved_model=decision.target_model,
                        fallback_reason=decision.fallback_reason,
                        status_code=502,
                        stream=False,
                        duration_ms=elapsed_ms,
                        error=str(exc),
                    )
                )
            )
            raise HTTPException(status_code=502, detail=f"Upstream node error: {exc}") from exc
        finally:
            decision.node.in_flight = max(0, decision.node.in_flight - 1)

    # Streaming: translate OpenAI SSE chunks into Anthropic Messages SSE protocol
    decision.node.in_flight += 1
    req_obj = state.proxy_client.build_request("POST", target_url, json=oai_payload)
    try:
        upstream = await state.proxy_client.send(req_obj, stream=True, auth=upstream_auth)
    except httpx.HTTPError as exc:
        decision.node.in_flight = max(0, decision.node.in_flight - 1)
        raise HTTPException(status_code=502, detail=f"Upstream node error: {exc}") from exc

    async def _stream_anthropic() -> AsyncGenerator[bytes, None]:
        msg_id = f"msg_{uuid.uuid4().hex[:20]}"
        est_in_tokens = max(1, (estimate_prompt_chars(body) + 3) // 4)
        ttft_ms: float | None = None
        out_tokens = 0
        in_tokens = est_in_tokens
        stop_reason = "end_turn"
        err_msg: str | None = None

        current_block_index = -1
        active_block_kind: str | None = None  # "thinking" | "text" | "tool_use"
        tool_block_indices: dict[int, int] = {}

        def _close_active_block() -> bytes:
            nonlocal active_block_kind
            if active_block_kind is not None and current_block_index >= 0:
                active_block_kind = None
                return _anth_sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": current_block_index},
                )
            return b""

        try:
            yield _anth_sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": raw_requested_model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": in_tokens,
                            "output_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                },
            )

            async for line in upstream.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload_str = line[6:].strip()
                if not payload_str or payload_str == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload_str)
                except ValueError:
                    continue

                if ttft_ms is None:
                    ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)

                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    if isinstance(usage.get("prompt_tokens"), int):
                        in_tokens = usage["prompt_tokens"]
                    if isinstance(usage.get("completion_tokens"), int):
                        out_tokens = usage["completion_tokens"]

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                first = choices[0] if isinstance(choices[0], dict) else {}
                delta = first.get("delta") or {}
                f_reason = first.get("finish_reason")
                if f_reason == "tool_calls":
                    stop_reason = "tool_use"
                elif f_reason == "length":
                    stop_reason = "max_tokens"

                rc = delta.get("reasoning_content")
                if isinstance(rc, str) and rc:
                    out_tokens += 1
                    if active_block_kind != "thinking":
                        stop_bytes = _close_active_block()
                        if stop_bytes:
                            yield stop_bytes
                        current_block_index += 1
                        active_block_kind = "thinking"
                        yield _anth_sse(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": current_block_index,
                                "content_block": {"type": "thinking", "thinking": ""},
                            },
                        )
                    yield _anth_sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": current_block_index,
                            "delta": {"type": "thinking_delta", "thinking": rc},
                        },
                    )

                txt = delta.get("content")
                if isinstance(txt, str) and txt:
                    out_tokens += 1
                    if active_block_kind != "text":
                        stop_bytes = _close_active_block()
                        if stop_bytes:
                            yield stop_bytes
                        current_block_index += 1
                        active_block_kind = "text"
                        yield _anth_sse(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": current_block_index,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                    yield _anth_sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": current_block_index,
                            "delta": {"type": "text_delta", "text": txt},
                        },
                    )

                for tc in delta.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    stop_reason = "tool_use"
                    tc_idx = int(tc.get("index", 0))
                    fn = tc.get("function") or {}
                    if tc_idx not in tool_block_indices:
                        stop_bytes = _close_active_block()
                        if stop_bytes:
                            yield stop_bytes
                        current_block_index += 1
                        tool_block_indices[tc_idx] = current_block_index
                        active_block_kind = "tool_use"
                        yield _anth_sse(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": current_block_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": str(tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"),
                                    "name": str(fn.get("name") or "tool"),
                                    "input": {},
                                },
                            },
                        )
                    b_idx = tool_block_indices[tc_idx]
                    arg_frag = fn.get("arguments")
                    if isinstance(arg_frag, str) and arg_frag:
                        out_tokens += 1
                        yield _anth_sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": b_idx,
                                "delta": {"type": "input_json_delta", "partial_json": arg_frag},
                            },
                        )

            if current_block_index < 0:
                current_block_index = 0
                yield _anth_sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                active_block_kind = "text"

            stop_bytes = _close_active_block()
            if stop_bytes:
                yield stop_bytes

            yield _anth_sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": out_tokens},
                },
            )
            yield _anth_sse("message_stop", {"type": "message_stop"})
        except (httpx.HTTPError, OSError) as exc:
            err_msg = str(exc)
        finally:
            await upstream.aclose()
            decision.node.in_flight = max(0, decision.node.in_flight - 1)
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            gen_s = max(0.001, (elapsed_ms - (ttft_ms or 0.0)) / 1000.0)
            tps = round(out_tokens / gen_s, 1) if out_tokens > 0 else None
            asyncio.create_task(
                state.record_trace(
                    TraceEntry(
                        id=trace_id,
                        timestamp=time.time(),
                        endpoint="/v1/messages",
                        requested_model=raw_requested_model,
                        resolved_node=decision.node.node_id,
                        resolved_model=decision.target_model,
                        fallback_reason=decision.fallback_reason,
                        status_code=upstream.status_code,
                        stream=True,
                        ttft_ms=ttft_ms,
                        duration_ms=elapsed_ms,
                        completion_tokens=out_tokens,
                        tokens_per_sec=tps,
                        error=err_msg,
                    )
                )
            )

    return StreamingResponse(
        _stream_anthropic(),
        status_code=200,
        media_type="text/event-stream",
        headers={
            **extra_headers,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
