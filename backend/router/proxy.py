"""Zero-copy SSE streaming & JSON proxy for OpenAI / infill endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
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
    max_attempts = min(4, max(2, len(state.nodes)))

    def _next_decision(attempted: set[str]) -> RouteDecision | None:
        fb_dec, _ = resolve_route(
            endpoint=endpoint,
            requested_model=requested_model,
            body=body,
            nodes=state.nodes,
            aliases=state.aliases,
            control=state.control,
            guards=state.config.guards,
            heavy_char_threshold=state.config.router.auto_heavy_char_threshold,
            exclude_nodes=attempted,
        )
        return fb_dec

    if is_stream:
        client = state.proxy_client
        attempted_nodes: set[str] = set()
        active_decision: RouteDecision = decision
        upstream: httpx.Response | None = None
        upstream_iter: AsyncIterator[bytes] | None = None
        first_chunk: bytes | None = None
        first_ttft_ms: float | None = None
        last_exc: Exception | None = None

        for attempt in range(max_attempts):
            if attempt > 0:
                fb_decision = _next_decision(attempted_nodes)
                if fb_decision is None:
                    break
                active_decision = fb_decision

            attempted_nodes.add(active_decision.node.node_id)
            active_decision.node.in_flight += 1
            outbound_body = {**body, "model": active_decision.target_model}
            target_url, upstream_auth = state.resolve_upstream_target(
                active_decision, endpoint
            )
            req_obj = client.build_request(
                "POST", target_url, json=outbound_body, headers=upstream_headers or None
            )
            cand_upstream: httpx.Response | None = None
            try:
                cand_upstream = await client.send(req_obj, stream=True, auth=upstream_auth)
                if cand_upstream.status_code in (502, 503, 504):
                    if attempt + 1 < max_attempts and _next_decision(attempted_nodes) is not None:
                        await cand_upstream.aclose()
                        active_decision.node.in_flight = max(
                            0, active_decision.node.in_flight - 1
                        )
                        continue
                    upstream = cand_upstream
                    upstream_iter = cand_upstream.aiter_bytes()
                    break

                cand_iter = cand_upstream.aiter_bytes()
                peeked: bytes | None = None
                async for chunk in cand_iter:
                    if chunk:
                        peeked = chunk
                        break

                if peeked is None:
                    if (
                        cand_upstream.status_code < 400
                        and attempt + 1 < max_attempts
                        and _next_decision(attempted_nodes) is not None
                    ):
                        await cand_upstream.aclose()
                        active_decision.node.in_flight = max(
                            0, active_decision.node.in_flight - 1
                        )
                        last_exc = httpx.RemoteProtocolError(
                            "Upstream stream closed before first chunk"
                        )
                        continue
                    upstream = cand_upstream
                    upstream_iter = cand_iter
                    break

                upstream = cand_upstream
                upstream_iter = cand_iter
                first_chunk = peeked
                first_ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                break
            except (httpx.HTTPError, OSError) as exc:
                last_exc = exc
                if cand_upstream is not None:
                    with contextlib.suppress(Exception):
                        await cand_upstream.aclose()
                active_decision.node.in_flight = max(0, active_decision.node.in_flight - 1)
                continue

        if upstream is None or upstream_iter is None:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            asyncio.create_task(
                state.record_trace(
                    TraceEntry(
                        id=trace_id,
                        timestamp=time.time(),
                        endpoint=endpoint,
                        requested_model=requested_model,
                        resolved_node=active_decision.node.node_id,
                        resolved_model=active_decision.target_model,
                        fallback_reason=active_decision.fallback_reason,
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

        extra_headers = _splitch_headers(active_decision)
        final_upstream = upstream
        final_iter = upstream_iter
        final_decision = active_decision

        async def _stream_body() -> AsyncGenerator[bytes, None]:
            ttft_ms: float | None = first_ttft_ms
            approx_tokens = 0
            err_msg: str | None = None
            try:
                if first_chunk is not None:
                    approx_tokens += _extract_tokens_from_sse(first_chunk)
                    yield first_chunk
                async for chunk in final_iter:
                    if not chunk:
                        continue
                    if ttft_ms is None:
                        ttft_ms = round((time.perf_counter() - started) * 1000.0, 1)
                    approx_tokens += _extract_tokens_from_sse(chunk)
                    yield chunk
            except (httpx.HTTPError, OSError) as exc:
                err_msg = str(exc)
            finally:
                await final_upstream.aclose()
                final_decision.node.last_splitch_work_ts = time.time()
                final_decision.node.in_flight = max(0, final_decision.node.in_flight - 1)
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
                            resolved_node=final_decision.node.node_id,
                            resolved_model=final_decision.target_model,
                            fallback_reason=final_decision.fallback_reason,
                            status_code=final_upstream.status_code,
                            stream=True,
                            ttft_ms=ttft_ms,
                            duration_ms=elapsed_ms,
                            completion_tokens=approx_tokens,
                            tokens_per_sec=tps,
                            error=err_msg,
                        )
                    )
                )

        media_type = final_upstream.headers.get("content-type", "text/event-stream")
        stream_headers = {
            **extra_headers,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(
            _stream_body(),
            status_code=final_upstream.status_code,
            media_type=media_type,
            headers=stream_headers,
        )

    # Non-streaming request (bounded multi-hop failover across unique nodes)
    attempted_nodes_ns: set[str] = set()
    active_decision = decision
    upstream_ns: httpx.Response | None = None
    last_ns_exc: httpx.HTTPError | None = None

    for attempt in range(max_attempts):
        if attempt > 0:
            fb_decision = _next_decision(attempted_nodes_ns)
            if fb_decision is None:
                break
            active_decision = fb_decision

        attempted_nodes_ns.add(active_decision.node.node_id)
        active_decision.node.in_flight += 1
        try:
            outbound_body = {**body, "model": active_decision.target_model}
            target_url, upstream_auth = state.resolve_upstream_target(
                active_decision, endpoint
            )
            resp_cand = await state.proxy_client.post(
                target_url,
                json=outbound_body,
                headers=upstream_headers or None,
                auth=upstream_auth,
            )
            if (
                resp_cand.status_code in (502, 503, 504)
                and attempt + 1 < max_attempts
                and _next_decision(attempted_nodes_ns) is not None
            ):
                continue
            upstream_ns = resp_cand
            break
        except httpx.HTTPError as exc:
            last_ns_exc = exc
            continue
        finally:
            active_decision.node.last_splitch_work_ts = time.time()
            active_decision.node.in_flight = max(0, active_decision.node.in_flight - 1)

    if upstream_ns is None:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        asyncio.create_task(
            state.record_trace(
                TraceEntry(
                    id=trace_id,
                    timestamp=time.time(),
                    endpoint=endpoint,
                    requested_model=requested_model,
                    resolved_node=active_decision.node.node_id,
                    resolved_model=active_decision.target_model,
                    fallback_reason=active_decision.fallback_reason,
                    status_code=502,
                    stream=False,
                    duration_ms=elapsed_ms,
                    error=str(last_ns_exc),
                )
            )
        )
        raise HTTPException(
            status_code=502, detail=f"Upstream node error: {last_ns_exc}"
        ) from last_ns_exc

    extra_headers = _splitch_headers(active_decision)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    comp_tokens = 0
    tps: float | None = None
    try:
        data = upstream_ns.json()
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
                resolved_node=active_decision.node.node_id,
                resolved_model=active_decision.target_model,
                fallback_reason=active_decision.fallback_reason,
                status_code=upstream_ns.status_code,
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
            content=data, status_code=upstream_ns.status_code, headers=extra_headers
        )
    return Response(
        content=upstream_ns.content,
        status_code=upstream_ns.status_code,
        media_type=upstream_ns.headers.get("content-type", "application/json"),
        headers=extra_headers,
    )


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
        for m in (*node.telemetry.models, *node.telemetry.strata_models):
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
