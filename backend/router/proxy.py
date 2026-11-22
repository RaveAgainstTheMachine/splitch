"""Zero-copy SSE streaming & JSON proxy for OpenAI / infill endpoints."""

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

from backend.router.policy import RouteDecision, resolve_route
from backend.router.telemetry import ClusterState
from backend.shared.models import TraceEntry


def _splitch_headers(decision: RouteDecision) -> dict[str, str]:
    headers = {
        "X-Splitch-Node": decision.node.node_id,
        "X-Splitch-Model": decision.target_model,
    }
    if decision.fallback_reason:
        headers["X-Splitch-Fallback"] = decision.fallback_reason
    return headers


def _upstream_passthrough_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name in ("anthropic-version", "anthropic-beta"):
        val = request.headers.get(name)
        if val:
            headers[name] = val
    return headers


def _extract_usage_tokens(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if isinstance(usage, dict):
        for field in ("completion_tokens", "output_tokens"):
            val = usage.get(field)
            if isinstance(val, int):
                return val
    resp = data.get("response")
    if isinstance(resp, dict):
        r_usage = resp.get("usage")
        if isinstance(r_usage, dict) and isinstance(r_usage.get("output_tokens"), int):
            return r_usage["output_tokens"]
    return None


def _extract_tokens_from_sse(chunk: bytes) -> int:
    count = 0
    for line in chunk.splitlines():
        if not line.startswith(b"data: "):
            continue
        payload = line[6:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            parsed = json.loads(payload)
            usage_toks = _extract_usage_tokens(parsed)
            if usage_toks is not None:
                return usage_toks
            choices = parsed.get("choices")
            if isinstance(choices, list) and choices:
                delta = choices[0].get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
                    count += 1
                    continue
            ev_type = parsed.get("type")
            if ev_type in (
                "content_block_delta",
                "response.output_text.delta",
                "response.reasoning_summary_text.delta",
                "response.function_call_arguments.delta",
            ):
                count += 1
        except (ValueError, TypeError, AttributeError):
            pass
    return count


async def forward_inference(
    request: Request,
    state: ClusterState,
    endpoint: str,
) -> Response:
    try:
        body: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}") from exc

    requested_model = str(body.get("model", "auto"))
    is_stream = bool(body.get("stream", False))

    decision, skip_reasons = resolve_route(
        endpoint=endpoint,
        requested_model=requested_model,
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
                "error": "No eligible Splitch node available",
                "requested_model": requested_model,
                "reasons": skip_reasons,
            },
        )

    upstream_headers = _upstream_passthrough_headers(request)
    trace_id = uuid.uuid4().hex[:10]
    started = time.perf_counter()

    def _try_fallback_decision(failed_node_id: str) -> RouteDecision | None:
        fb_dec, _ = resolve_route(
            endpoint=endpoint,
            requested_model=requested_model,
            body=body,
            nodes=state.nodes,
            aliases=state.aliases,
            control=state.control,
            guards=state.config.guards,
            heavy_char_threshold=state.config.router.auto_heavy_char_threshold,
            exclude_nodes={failed_node_id},
        )
        return fb_dec

    if is_stream:
        client = state.proxy_client
        decision.node.in_flight += 1
        outbound_body = {**body, "model": decision.target_model}
        target_url = f"{decision.node.engine_url}{endpoint}"
        req_obj = client.build_request(
            "POST", target_url, json=outbound_body, headers=upstream_headers or None
        )
        upstream: httpx.Response | None = None
        last_exc: httpx.HTTPError | None = None
        try:
            upstream = await client.send(req_obj, stream=True)
            if upstream.status_code in (502, 503, 504):
                fb_decision = _try_fallback_decision(decision.node.node_id)
                if fb_decision is not None:
                    await upstream.aclose()
                    decision.node.in_flight = max(0, decision.node.in_flight - 1)
                    decision = fb_decision
                    decision.node.in_flight += 1
                    outbound_body = {**body, "model": decision.target_model}
                    target_url = f"{decision.node.engine_url}{endpoint}"
                    req_obj = client.build_request(
                        "POST", target_url, json=outbound_body, headers=upstream_headers or None
                    )
                    upstream = await client.send(req_obj, stream=True)
        except httpx.HTTPError as exc:
            last_exc = exc
            decision.node.in_flight = max(0, decision.node.in_flight - 1)
            fb_decision = _try_fallback_decision(decision.node.node_id)
            if fb_decision is not None:
                decision = fb_decision
                decision.node.in_flight += 1
                outbound_body = {**body, "model": decision.target_model}
                target_url = f"{decision.node.engine_url}{endpoint}"
                req_obj = client.build_request(
                    "POST", target_url, json=outbound_body, headers=upstream_headers or None
                )
                try:
                    upstream = await client.send(req_obj, stream=True)
                except httpx.HTTPError as fb_exc:
                    decision.node.in_flight = max(0, decision.node.in_flight - 1)
                    last_exc = fb_exc
        if upstream is None:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            asyncio.create_task(
                state.record_trace(
                    TraceEntry(
                        id=trace_id,
                        timestamp=time.time(),
                        endpoint=endpoint,
                        requested_model=requested_model,
                        resolved_node=decision.node.node_id,
                        resolved_model=decision.target_model,
                        fallback_reason=decision.fallback_reason,
                        status_code=502,
                        stream=True,
                        duration_ms=elapsed_ms,
                        error=str(last_exc),
                    )
                )
            )
            raise HTTPException(
                status_code=502, detail=f"Upstream node error: {last_exc}"
            ) from last_exc

        extra_headers = _splitch_headers(decision)

        async def _stream_body() -> AsyncGenerator[bytes, None]:
            ttft_ms: float | None = None
            approx_tokens = 0
            err_msg: str | None = None
            try:
                async for chunk in upstream.aiter_bytes():
                    if not chunk:
                        continue
                    if ttft_ms is None:
                        ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                    approx_tokens += _extract_tokens_from_sse(chunk)
                    yield chunk
            except (httpx.HTTPError, OSError) as exc:
                err_msg = str(exc)
            finally:
                await upstream.aclose()
                decision.node.in_flight = max(0, decision.node.in_flight - 1)
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
                gen_s = max(0.001, (elapsed_ms - (ttft_ms or 0.0)) / 1000.0)
                tps = round(approx_tokens / gen_s, 1) if approx_tokens > 0 else None
                asyncio.create_task(
                    state.record_trace(
                        TraceEntry(
                            id=trace_id,
                            timestamp=time.time(),
                            endpoint=endpoint,
                            requested_model=requested_model,
                            resolved_node=decision.node.node_id,
                            resolved_model=decision.target_model,
                            fallback_reason=decision.fallback_reason,
                            status_code=upstream.status_code,
                            stream=True,
                            ttft_ms=ttft_ms,
                            duration_ms=elapsed_ms,
                            completion_tokens=approx_tokens,
                            tokens_per_sec=tps,
                            error=err_msg,
                        )
                    )
                )

        media_type = upstream.headers.get("content-type", "text/event-stream")
        stream_headers = {
            **extra_headers,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(
            _stream_body(),
            status_code=upstream.status_code,
            media_type=media_type,
            headers=stream_headers,
        )

    # Non-streaming request (with automatic pre-response fallback retry on connection error / 502-504)
    decision.node.in_flight += 1
    try:
        outbound_body = {**body, "model": decision.target_model}
        target_url = f"{decision.node.engine_url}{endpoint}"
        try:
            upstream = await state.proxy_client.post(
                target_url, json=outbound_body, headers=upstream_headers or None
            )
            if upstream.status_code in (502, 503, 504):
                fb_decision = _try_fallback_decision(decision.node.node_id)
                if fb_decision is not None:
                    decision.node.in_flight = max(0, decision.node.in_flight - 1)
                    decision = fb_decision
                    decision.node.in_flight += 1
                    outbound_body = {**body, "model": decision.target_model}
                    target_url = f"{decision.node.engine_url}{endpoint}"
                    upstream = await state.proxy_client.post(
                        target_url, json=outbound_body, headers=upstream_headers or None
                    )
        except httpx.HTTPError:
            fb_decision = _try_fallback_decision(decision.node.node_id)
            if fb_decision is None:
                raise
            decision.node.in_flight = max(0, decision.node.in_flight - 1)
            decision = fb_decision
            decision.node.in_flight += 1
            outbound_body = {**body, "model": decision.target_model}
            target_url = f"{decision.node.engine_url}{endpoint}"
            upstream = await state.proxy_client.post(
                target_url, json=outbound_body, headers=upstream_headers or None
            )

        extra_headers = _splitch_headers(decision)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        comp_tokens = 0
        tps: float | None = None
        try:
            data = upstream.json()
            usage_toks = _extract_usage_tokens(data)
            if usage_toks is not None:
                comp_tokens = usage_toks
                tps = round(comp_tokens / max(0.001, elapsed_ms / 1000.0), 1)
        except (ValueError, TypeError):
            data = None

        asyncio.create_task(
            state.record_trace(
                TraceEntry(
                    id=trace_id,
                    timestamp=time.time(),
                    endpoint=endpoint,
                    requested_model=requested_model,
                    resolved_node=decision.node.node_id,
                    resolved_model=decision.target_model,
                    fallback_reason=decision.fallback_reason,
                    status_code=upstream.status_code,
                    stream=False,
                    ttft_ms=elapsed_ms,
                    duration_ms=elapsed_ms,
                    completion_tokens=comp_tokens,
                    tokens_per_sec=tps,
                )
            )
        )
        if data is not None:
            return JSONResponse(
                content=data, status_code=upstream.status_code, headers=extra_headers
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
            headers=extra_headers,
        )
    except httpx.HTTPError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        asyncio.create_task(
            state.record_trace(
                TraceEntry(
                    id=trace_id,
                    timestamp=time.time(),
                    endpoint=endpoint,
                    requested_model=requested_model,
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


def build_openai_models_list(state: ClusterState) -> dict[str, Any]:
    now = int(time.time())
    items: list[dict[str, Any]] = [
        {"id": "auto", "object": "model", "created": now, "owned_by": "splitch-alias"}
    ]
    seen = {"auto"}
    for alias in state.aliases.values():
        if alias.alias not in seen:
            items.append(
                {
                    "id": alias.alias,
                    "object": "model",
                    "created": now,
                    "owned_by": f"splitch-alias:{alias.primary_node}",
                }
            )
            seen.add(alias.alias)

    for node in state.nodes.values():
        if not node.online or node.telemetry is None:
            continue
        for m in node.telemetry.models:
            qualified = f"{node.node_id}/{m.id}"
            if qualified not in seen:
                items.append(
                    {
                        "id": qualified,
                        "object": "model",
                        "created": now,
                        "owned_by": f"splitch-node:{node.node_id}",
                    }
                )
                seen.add(qualified)

    return {"object": "list", "data": items}
