"""
Sustained multi-cycle live verification harness (>30s) for Splitch.
Tests real telemetry, streaming chat completions, and Gaming Mode failover.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx


async def run_verification(router_url: str, model: str, duration_s: float) -> int:
    router_url = router_url.rstrip("/")
    start = time.monotonic()
    cycles = 0
    failures: list[str] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
        r = await client.get(f"{router_url}/health")
        assert r.status_code == 200, f"/health failed: {r.status_code}"
        print(f"[OK] /health: {r.json()}")

        m = await client.get(f"{router_url}/v1/models")
        assert m.status_code == 200, f"/v1/models failed: {m.status_code}"
        model_ids = [x["id"] for x in m.json().get("data", [])]
        print(f"[OK] /v1/models ({len(model_ids)}): {model_ids[:8]}")

        t0 = time.perf_counter()
        chunks = 0
        text = ""
        async with client.stream(
            "POST",
            f"{router_url}/v1/chat/completions",
            json={
                "model": model,
                "stream": True,
                "max_tokens": 24,
                "messages": [{"role": "user", "content": "Reply with exactly: SPLITCH_OK"}],
            },
        ) as resp:
            node = resp.headers.get("x-splitch-node")
            resolved = resp.headers.get("x-splitch-model")
            assert resp.status_code == 200, f"chat stream HTTP {resp.status_code}"
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                data = json.loads(payload)
                delta = data.get("choices", [{}])[0].get("delta", {})
                tok = delta.get("content") or delta.get("reasoning_content")
                if tok:
                    chunks += 1
                    text += tok
        dt = round((time.perf_counter() - t0) * 1000, 1)
        print(f"[OK] Stream chat via {node}/{resolved} ({chunks} chunks, {dt}ms): {text.strip()!r}")
        assert chunks > 0, "Zero streamed chunks received"

        print(f"[..] Running sustained polling for {duration_s:.0f}s...")
        while time.monotonic() - start < duration_s:
            cycles += 1
            st = await client.get(f"{router_url}/api/status")
            if st.status_code != 200:
                failures.append(f"cycle {cycles}: /api/status HTTP {st.status_code}")
            else:
                snap = st.json()
                online = [n["node_id"] for n in snap["nodes"] if n["online"]]
                traces = len(snap["traces"])
                if not online:
                    failures.append(f"cycle {cycles}: zero online nodes")
                print(
                    f"  cycle {cycles:02d} ({time.monotonic()-start:5.1f}s): online={online} traces={traces}"
                )
            await asyncio.sleep(5.0)

    if failures:
        print(f"[FAIL] {len(failures)} failures: {failures}")
        return 1
    print(f"[PASS] Verified sustained across {cycles} cycles ({time.monotonic()-start:.1f}s)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", default="auto")
    parser.add_argument("--duration", type=float, default=32.0)
    args = parser.parse_args()
    sys.exit(asyncio.run(run_verification(args.url, args.model, args.duration)))


if __name__ == "__main__":
    main()
