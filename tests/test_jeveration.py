"""Independent adversarial & edge-case tests for Splitch v0.26.0 Jeveration integration."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent import main as agent_main
from backend.router.chat_store import MEDIA_TOOLS_SCHEMA
from backend.router.config import SplitchConfig
from backend.router.media_api import build_media_router
from backend.router.telemetry import AgentEd25519Auth, ClusterState
from backend.shared.gpu_probe import parse_nvidia_csv, parse_nvidia_pmon
from backend.shared.models import (
    LoadedModel,
    MediaGenerateRequest,
    NodeJeverationStatus,
    NodeStatus,
    NodeTelemetry,
)
from backend.shared.secrets import generate_ed25519_keypair, load_ed25519_public_key

# Distinct valid 1x1 PNG payloads for verifying primary + dense artifact bytes
PNG_RED = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)
PNG_SEG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\x88\xf8\xcf"
    b"\x00\x00\x03\x01\x01\x00\x8d\xfa\x1e\xa9\x00\x00\x00\x00IEND\xaeB`\x82"
)
PNG_DEPTH = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
PNG_NORMAL = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\x80\x88\xf8"
    b"\x0f\x00\x01\x09\x01\x00\x9e\xb0\x27\x31\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _start_loopback_server(handler_cls: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://{srv.server_address[0]}:{srv.server_address[1]}"


def test_agent_jeveration_status_proxy_allowlist_and_ed25519_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_calls: list[tuple[str, str, bytes]] = []

    class _JeverationWorkerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            worker_calls.append(("GET", self.path, b""))
            if self.path == "/health":
                out = json.dumps({"status": "ok", "ready": True, "vram_mb": 4096.5}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            out = json.dumps({"secret": "should-never-be-proxied"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            worker_calls.append(("POST", self.path, raw))
            if self.path == "/v1/text-to-image":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("X-Jeveration-Latency-Ms", "42.5")
                self.send_header("Content-Length", str(len(PNG_RED)))
                self.end_headers()
                self.wfile.write(PNG_RED)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    srv, _ = _start_loopback_server(_JeverationWorkerHandler)
    worker_port = srv.server_address[1]

    _, compact_pub = generate_ed25519_keypair(tmp_path / "router_ed25519.pem")
    pub_key = load_ed25519_public_key(compact_pub)
    cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
    cluster.agent_token = ""
    agent_auth = cluster.agent_auth()
    assert isinstance(agent_auth, AgentEd25519Auth)

    monkeypatch.setattr(agent_main, "STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(agent_main, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_main, "ROUTER_PUBLIC_KEY", pub_key)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    orig_port = agent_main.state.jev_port
    orig_client = agent_main.state.client
    orig_healthy = agent_main.state.jev_healthy
    orig_vram = agent_main.state.jev_vram_mb

    async def _run_checks() -> None:
        agent_main.state.jev_port = worker_port
        agent_main.state.jev_healthy = True
        agent_main.state.jev_vram_mb = 4096.5
        agent_main.state.client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
        try:
            transport = httpx.ASGITransport(app=agent_main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                # 1. Unauthenticated access rejected with 401
                unauth = await client.get("/jeveration/status")
                assert unauth.status_code == 401

                # 2. Signed /jeveration/status returns accurate snapshot
                st_res = await client.get("/jeveration/status", auth=agent_auth)
                assert st_res.status_code == 200, st_res.text
                st_body = st_res.json()
                assert st_body["node_id"] == agent_main.NODE_ID
                assert st_body["port"] == worker_port
                assert st_body["healthy"] is True
                assert st_body["weights_ready"] is False
                assert st_body["installed"] is False

                # Weights check flips to True once both required checkpoints exist
                wdir = tmp_path / "media" / "jeveration"
                wdir.mkdir(parents=True, exist_ok=True)
                (wdir / "jeveration.pt").write_bytes(b"pt")
                (wdir / "decoder512.pt").write_bytes(b"dec")
                st_res2 = await client.get("/jeveration/status", auth=agent_auth)
                assert st_res2.status_code == 200
                assert st_res2.json()["weights_ready"] is True

                # 3. Allowlisted GET /jeveration/proxy/health proxies cleanly
                h_res = await client.get("/jeveration/proxy/health", auth=agent_auth)
                assert h_res.status_code == 200, h_res.text
                assert h_res.json()["ready"] is True
                assert h_res.json()["vram_mb"] == 4096.5

                # 4. Allowlisted POST /jeveration/proxy/v1/text-to-image proxies with latency header
                t2i_res = await client.post(
                    "/jeveration/proxy/v1/text-to-image",
                    json={"prompt": "crystal fox", "seed": 7},
                    auth=agent_auth,
                )
                assert t2i_res.status_code == 200, t2i_res.text
                assert t2i_res.content == PNG_RED
                assert t2i_res.headers.get("x-jeveration-latency-ms") == "42.5"
                assert json.loads(worker_calls[-1][2]) == {"prompt": "crystal fox", "seed": 7}

                # 5. Non-allowlisted paths return 403 and NEVER reach the worker
                bad_admin = await client.post(
                    "/jeveration/proxy/v1/admin",
                    json={"cmd": "shutdown"},
                    auth=agent_auth,
                )
                assert bad_admin.status_code == 403

                bad_trav = await client.get("/jeveration/proxy/..%2Fetc", auth=agent_auth)
                assert bad_trav.status_code == 403

                bad_extra = await client.post(
                    "/jeveration/proxy/v1/text-to-image/extra",
                    json={"prompt": "x"},
                    auth=agent_auth,
                )
                assert bad_extra.status_code == 403

                assert all(path not in ("/v1/admin", "/../etc", "/v1/text-to-image/extra") for _, path, _ in worker_calls)

                # 6. Tampered body with Ed25519 signature computed for a different payload -> 401
                signed_req = client.build_request(
                    "POST",
                    "/jeveration/proxy/v1/text-to-image",
                    json={"prompt": "legitimate prompt", "seed": 1},
                )
                list(agent_auth.auth_flow(signed_req))
                sig_headers = {
                    k: v
                    for k, v in signed_req.headers.items()
                    if k.lower().startswith("x-splitch-auth-")
                }
                tampered_res = await client.post(
                    "/jeveration/proxy/v1/text-to-image",
                    json={"prompt": "tampered prompt", "seed": 1},
                    headers=sig_headers,
                )
                assert tampered_res.status_code == 401
        finally:
            await agent_main.state.client.aclose()
            agent_main.state.client = orig_client
            agent_main.state.jev_port = orig_port
            agent_main.state.jev_healthy = orig_healthy
            agent_main.state.jev_vram_mb = orig_vram

    try:
        asyncio.run(_run_checks())
    finally:
        srv.shutdown()
        srv.server_close()


def test_media_service_jeveration_discovery_generation_and_dense(tmp_path: Path) -> None:
    recorded_calls: list[tuple[str, str, dict[str, Any] | None]] = []

    class _AgentJevServer(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            recorded_calls.append(("GET", self.path, None))
            if self.path == "/jeveration/status":
                out = NodeJeverationStatus(
                    node_id="gpu-jev",
                    cuda_available=True,
                    installed=True,
                    weights_ready=True,
                    running=True,
                    healthy=True,
                    port=8084,
                    vram_used_mb=5120.0,
                ).model_dump_json().encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            if self.path == "/metrics":
                out = NodeTelemetry(
                    node_id="gpu-jev",
                    role="gpu",
                    platform="linux",
                    timestamp=1.0,
                    vram_total_mb=24576.0,
                    vram_free_mb=18000.0,
                    media_engine="none",
                    media_healthy=False,
                    jeveration_healthy=False,
                ).model_dump_json().encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            payload = json.loads(raw.decode("utf-8")) if raw else None
            recorded_calls.append(("POST", self.path, payload))

            if self.path == "/jeveration/proxy/v1/text-to-image":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("X-Jeveration-Latency-Ms", "31.2")
                self.send_header("Content-Length", str(len(PNG_RED)))
                self.end_headers()
                self.wfile.write(PNG_RED)
                return

            if self.path == "/jeveration/proxy/v1/image-to-image":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("X-Jeveration-Latency-Ms", "28.4")
                self.send_header("Content-Length", str(len(PNG_DEPTH)))
                self.end_headers()
                self.wfile.write(PNG_DEPTH)
                return

            if self.path == "/jeveration/proxy/v1/dense-predict":
                out = json.dumps(
                    {
                        "seg_png_b64": base64.b64encode(PNG_SEG).decode("ascii"),
                        "depth_png_b64": base64.b64encode(PNG_DEPTH).decode("ascii"),
                        "normal_png_b64": base64.b64encode(PNG_NORMAL).decode("ascii"),
                        "depth_min_m": 0.45,
                        "depth_max_m": 12.8,
                        "latency_ms": 34.1,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

            if self.path == "/jeveration/stop":
                out = NodeJeverationStatus(node_id="gpu-jev", healthy=False).model_dump_json().encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    srv, agent_url = _start_loopback_server(_AgentJevServer)
    try:
        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        cluster._poll_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
        cluster.nodes["gpu-jev"] = NodeStatus(
            node_id="gpu-jev",
            display_name="Jeveration GPU",
            role="gpu",
            agent_url=agent_url,
            engine_url="http://127.0.0.1:9",
            online=True,
            telemetry=NodeTelemetry(
                node_id="gpu-jev",
                role="gpu",
                platform="linux",
                timestamp=1.0,
                vram_total_mb=24576.0,
                vram_free_mb=18000.0,
                media_engine="none",
                media_healthy=False,
                jeveration_healthy=True,
            ),
        )

        app = FastAPI()
        media_router, service = build_media_router(cluster, tmp_path)
        app.include_router(media_router)
        client = TestClient(app)

        # 1. list_backends() includes Jeveration even when media_engine="none"
        backends = service.list_backends()
        jev_backends = [b for b in backends if b.media_engine == "jeveration"]
        assert len(jev_backends) == 1
        assert jev_backends[0].node_id == "gpu-jev"
        assert jev_backends[0].media_healthy is True
        assert jev_backends[0].media_url == f"{agent_url}/jeveration/proxy"
        assert jev_backends[0].media_models == ["jeveration-512"]

        # 2. resolve_media_node(preferred_engine="jeveration") selects node even with media_engine="none"
        node, eng, base_url = service.resolve_media_node(preferred_engine="jeveration")
        assert node.node_id == "gpu-jev"
        assert eng == "jeveration"
        assert base_url == f"{agent_url}/jeveration/proxy"

        # Auto-selection fallback also picks Jeveration when sd-server/ComfyUI is absent
        node2, eng2, _ = service.resolve_media_node(preferred_engine=None)
        assert node2.node_id == "gpu-jev" and eng2 == "jeveration"

        # Model-hint routing picks Jeveration
        node3, eng3, _ = service.resolve_media_node(preferred_model="jeveration-512")
        assert node3.node_id == "gpu-jev" and eng3 == "jeveration"

        async def _run_jobs() -> None:
            # 3. t2i generation
            t2i_job = await service.generate_sync(
                MediaGenerateRequest(
                    prompt="isometric glass prism",
                    engine="jeveration",
                    jeveration_task="t2i",
                    seed=123,
                    evict_llm_vram=False,
                )
            )
            assert t2i_job.status == "completed", t2i_job.error
            assert t2i_job.media_engine == "jeveration"
            assert t2i_job.width == 512 and t2i_job.height == 512
            assert t2i_job.file_url is not None
            assert client.get(t2i_job.file_url).content == PNG_RED
            assert recorded_calls[-1][1] == "/jeveration/proxy/v1/text-to-image"
            assert recorded_calls[-1][2] == {"prompt": "isometric glass prism", "seed": 123}

            # 4. i2i colorize and hed
            init_data_url = "data:image/png;base64," + base64.b64encode(PNG_RED).decode("ascii")
            col_job = await service.generate_sync(
                MediaGenerateRequest(
                    prompt="warm sunset tones",
                    engine="jeveration",
                    jeveration_task="colorize",
                    init_image=init_data_url,
                    seed=5,
                    evict_llm_vram=False,
                )
            )
            assert col_job.status == "completed", col_job.error
            assert recorded_calls[-1][1] == "/jeveration/proxy/v1/image-to-image"
            assert recorded_calls[-1][2] is not None
            assert recorded_calls[-1][2]["task"] == "colorize"
            assert recorded_calls[-1][2]["caption"] == "warm sunset tones"
            assert recorded_calls[-1][2]["seed"] == 5

            hed_job = await service.generate_sync(
                MediaGenerateRequest(
                    prompt="brutalist concrete tower",
                    engine="jeveration",
                    jeveration_task="hed",
                    init_image=init_data_url,
                    from_photo=True,
                    seed=9,
                    evict_llm_vram=False,
                )
            )
            assert hed_job.status == "completed", hed_job.error
            assert recorded_calls[-1][2] is not None
            assert recorded_calls[-1][2]["task"] == "hed"
            assert recorded_calls[-1][2]["from_photo"] is True

            # 5. dense prediction saves all 3 PNG files and populates extra_outputs
            dense_job = await service.generate_sync(
                MediaGenerateRequest(
                    prompt="analyze scene geometry",
                    engine="jeveration",
                    jeveration_task="dense",
                    init_image=init_data_url,
                    evict_llm_vram=False,
                )
            )
            assert dense_job.status == "completed", dense_job.error
            assert len(dense_job.extra_outputs) == 3
            assert [o.label for o in dense_job.extra_outputs] == ["Segmentation", "Depth", "Normals"]
            expected_bytes = [PNG_SEG, PNG_DEPTH, PNG_NORMAL]
            for artifact, exp in zip(dense_job.extra_outputs, expected_bytes, strict=True):
                assert artifact.file_url.startswith("/api/media/files/")
                assert artifact.mime_type == "image/png"
                disk_path = service.media_dir / artifact.filename
                assert disk_path.is_file()
                assert disk_path.read_bytes() == exp
                dl = client.get(artifact.file_url)
                assert dl.status_code == 200
                assert dl.content == exp

            # 6. i2i without init_image fails cleanly with status="error"
            missing_img_job = await service.generate_sync(
                MediaGenerateRequest(
                    prompt="colorize nothing",
                    engine="jeveration",
                    jeveration_task="colorize",
                    init_image=None,
                    evict_llm_vram=False,
                )
            )
            assert missing_img_job.status == "error"
            assert missing_img_job.error and "input image" in missing_img_job.error.lower()

            # dense without init_image also fails cleanly
            missing_dense_job = await service.generate_sync(
                MediaGenerateRequest(
                    prompt="dense nothing",
                    engine="jeveration",
                    jeveration_task="dense",
                    init_image=None,
                    evict_llm_vram=False,
                )
            )
            assert missing_dense_job.status == "error"
            assert missing_dense_job.error and "input image" in missing_dense_job.error.lower()

        asyncio.run(_run_jobs())

        # 7. Router Jeveration status & stop routes
        st_res = client.get("/api/media/jeveration/status")
        assert st_res.status_code == 200
        assert len(st_res.json()) == 1
        assert st_res.json()[0]["node_id"] == "gpu-jev"
        assert st_res.json()[0]["healthy"] is True

        stop_res = client.post("/api/media/nodes/gpu-jev/jeveration/stop")
        assert stop_res.status_code == 200
        assert stop_res.json()["healthy"] is False

        offline_res = client.post("/api/media/nodes/missing-node/jeveration/stop")
        assert offline_res.status_code == 503

        # When jeveration_healthy is False, resolve_media_node(preferred_engine="jeveration") raises 503
        assert cluster.nodes["gpu-jev"].telemetry is not None
        cluster.nodes["gpu-jev"].telemetry.jeveration_healthy = False
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            service.resolve_media_node(preferred_engine="jeveration")
        assert exc_info.value.status_code == 503
    finally:
        if cluster._poll_client is not None:
            asyncio.run(cluster._poll_client.aclose())
        srv.shutdown()
        srv.server_close()


def test_gaming_yield_auto_evict_stops_jeveration(tmp_path: Path) -> None:
    stop_calls: list[tuple[str, bool]] = []
    return_error = {"flag": False}

    class _EvictAgentHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            has_sig = bool(self.headers.get("x-splitch-auth-sig"))
            if self.path == "/jeveration/stop":
                stop_calls.append((self.path, has_sig))
                if return_error["flag"]:
                    self.send_response(500)
                    self.end_headers()
                    return
                out = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            if self.path == "/models/unload":
                out = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    srv, agent_url = _start_loopback_server(_EvictAgentHandler)
    try:
        generate_ed25519_keypair(tmp_path / "router_ed25519.pem")
        cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
        telemetry = NodeTelemetry(
            node_id="gaming-pc",
            role="gpu",
            platform="linux",
            timestamp=1.0,
            jeveration_healthy=True,
            models=[LoadedModel(id="qwen-32b", status="loaded")],
        )
        node = NodeStatus(
            node_id="gaming-pc",
            display_name="Gaming PC",
            role="gpu",
            yield_on_gaming=True,
            agent_url=agent_url,
            engine_url="http://127.0.0.1:9",
            online=True,
            in_flight=1,
            telemetry=telemetry,
        )

        async def _run_evict() -> None:
            cluster._poll_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
            try:
                # 1. Active in-flight request protects Jeveration from eviction
                await cluster._auto_evict_loaded_models(node, telemetry)
                assert stop_calls == []
                assert telemetry.jeveration_healthy is True

                # 2. Failed /jeveration/stop keeps telemetry.jeveration_healthy=True
                node.in_flight = 0
                return_error["flag"] = True
                await cluster._auto_evict_loaded_models(node, telemetry)
                assert len(stop_calls) == 1
                assert telemetry.jeveration_healthy is True

                # 3. Successful /jeveration/stop clears jeveration_healthy and signs request
                return_error["flag"] = False
                await cluster._auto_evict_loaded_models(node, telemetry)
                assert len(stop_calls) == 2
                assert stop_calls[-1] == ("/jeveration/stop", True)
                assert telemetry.jeveration_healthy is False
            finally:
                await cluster._poll_client.aclose()

        asyncio.run(_run_evict())
    finally:
        srv.shutdown()
        srv.server_close()


def test_chat_generate_image_schema_exposes_jeveration() -> None:
    gen_img = next(t for t in MEDIA_TOOLS_SCHEMA if t["function"]["name"] == "generate_image")
    props = gen_img["function"]["parameters"]["properties"]
    assert "jeveration" in props["engine"]["enum"]
    assert set(props["jeveration_task"]["enum"]) == {
        "t2i",
        "hed",
        "depth",
        "colorize",
        "anime",
        "ghibli",
        "watercolor",
        "dense",
    }


def test_agent_jeveration_pull_weights_creates_download_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeHfHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = b"JEVERATION_WEIGHT_BYTES"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    hf_srv, hf_url = _start_loopback_server(_FakeHfHandler)
    try:
        monkeypatch.setattr(agent_main, "STORAGE_PATH", str(tmp_path))
        monkeypatch.setattr(agent_main, "AGENT_TOKEN", "tok-pull")
        monkeypatch.setattr(agent_main, "ROUTER_PUBLIC_KEY", None)
        monkeypatch.setenv("SPLITCH_HF_PROTOCOL", "http")
        monkeypatch.setenv("HF_ENDPOINT", hf_url)
        agent_main.state.downloads.clear()

        with TestClient(agent_main.app) as client:
            r = client.post(
                "/jeveration/pull-weights",
                headers={"Authorization": "Bearer tok-pull"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body["active_downloads"]) == 3
            names = {d["filename"] for d in body["active_downloads"]}
            assert names == {"jeveration.pt", "decoder512.pt", "source_decoder512.pt"}

            # Poll until background _pull_all() task reaches terminal state (zero leaked tasks)
            deadline = time.monotonic() + 10.0
            final_status = body
            while time.monotonic() < deadline:
                st_r = client.get(
                    "/jeveration/status",
                    headers={"Authorization": "Bearer tok-pull"},
                )
                assert st_r.status_code == 200
                final_status = st_r.json()
                if final_status["weights_ready"] and not final_status["weights_downloading"]:
                    break
                time.sleep(0.05)
            assert final_status["weights_ready"] is True, final_status
            assert final_status["weights_downloading"] is False, final_status
        agent_main.state.downloads.clear()
    finally:
        hf_srv.shutdown()
        hf_srv.server_close()


def _setup_fake_jeveration_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_code: str, token: str
) -> Path:
    eng = tmp_path / ".local" / "share" / "splitch" / "engines" / "jeveration"
    (eng / "repo" / "jeveration").mkdir(parents=True, exist_ok=True)
    bin_dir = eng / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    py_bin = bin_dir / "python"
    py_bin.write_text(worker_code, encoding="utf-8")
    py_bin.chmod(0o755)

    wdir = tmp_path / "media" / "jeveration"
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "jeveration.pt").write_bytes(b"pt")
    (wdir / "decoder512.pt").write_bytes(b"dec")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agent_main, "STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(agent_main, "AGENT_TOKEN", token)
    monkeypatch.setattr(agent_main, "ROUTER_PUBLIC_KEY", None)
    return wdir


def _find_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Uses POSIX shebang executable shim for fake venv python",
)
def test_agent_jeveration_start_nonblocking_and_log_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_code = (
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdout.write('WORKER_STDOUT_START\\n' + ('A' * 140000) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('WORKER_STDERR_LOADING\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n"
    )
    wdir = _setup_fake_jeveration_env(tmp_path, monkeypatch, worker_code, "tok-nonblock")
    port = _find_unused_port()

    orig_proc = agent_main.state.jev_proc
    orig_port = agent_main.state.jev_port
    orig_healthy = agent_main.state.jev_healthy
    orig_vram = agent_main.state.jev_vram_mb
    orig_err = agent_main.state.jev_last_error
    try:
        with TestClient(agent_main.app) as client:
            t0 = time.monotonic()
            r = client.post(
                "/jeveration/start",
                json={"port": port, "load_source_decoder": False, "evict_primary_models": False},
                headers={"Authorization": "Bearer tok-nonblock"},
            )
            elapsed = time.monotonic() - t0
            assert r.status_code == 200, r.text
            assert elapsed < 10.0, f"start_jeveration blocked for {elapsed:.2f}s"
            body = r.json()
            assert body["running"] is True
            assert body["healthy"] is False
            assert body["port"] == port
            assert body["error"] is None

            log_path = wdir / "jeveration-worker.log"
            assert log_path.is_file()
            log_text = log_path.read_text(encoding="utf-8")
            assert "WORKER_STDOUT_START" in log_text
            assert "WORKER_STDERR_LOADING" in log_text

            stop_r = client.post(
                "/jeveration/stop",
                headers={"Authorization": "Bearer tok-nonblock"},
            )
            assert stop_r.status_code == 200, stop_r.text
            assert stop_r.json()["running"] is False
            assert stop_r.json()["healthy"] is False
    finally:
        agent_main.state.jev_proc = orig_proc
        agent_main.state.jev_port = orig_port
        agent_main.state.jev_healthy = orig_healthy
        agent_main.state.jev_vram_mb = orig_vram
        agent_main.state.jev_last_error = orig_err


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Uses POSIX shebang executable shim for fake venv python",
)
def test_agent_jeveration_start_crash_captures_log_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker_code = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('PREFIX_NOISE_' + ('X' * 700) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('RuntimeError: cuda_oom_marker_xyz_987\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(2)\n"
    )
    wdir = _setup_fake_jeveration_env(tmp_path, monkeypatch, worker_code, "tok-crash")
    port = _find_unused_port()

    orig_proc = agent_main.state.jev_proc
    orig_port = agent_main.state.jev_port
    orig_healthy = agent_main.state.jev_healthy
    orig_vram = agent_main.state.jev_vram_mb
    orig_err = agent_main.state.jev_last_error
    try:
        with TestClient(agent_main.app) as client:
            r = client.post(
                "/jeveration/start",
                json={"port": port, "load_source_decoder": False, "evict_primary_models": False},
                headers={"Authorization": "Bearer tok-crash"},
            )
            assert r.status_code == 500
            detail = r.json()["detail"]
            assert "cuda_oom_marker_xyz_987" in detail
            assert len(detail) <= 500
            assert "PREFIX_NOISE_" not in detail

            st_r = client.get(
                "/jeveration/status",
                headers={"Authorization": "Bearer tok-crash"},
            )
            assert st_r.status_code == 200
            st_body = st_r.json()
            assert st_body["running"] is False
            assert st_body["healthy"] is False
            assert st_body["error"] is not None
            assert "cuda_oom_marker_xyz_987" in st_body["error"]

            log_path = wdir / "jeveration-worker.log"
            assert log_path.is_file()
            assert "PREFIX_NOISE_" in log_path.read_text(encoding="utf-8")
    finally:
        agent_main.state.jev_proc = orig_proc
        agent_main.state.jev_port = orig_port
        agent_main.state.jev_healthy = orig_healthy
        agent_main.state.jev_vram_mb = orig_vram
        agent_main.state.jev_last_error = orig_err


def test_nvidia_gpu_probe_managed_pids_and_jeveration_worker_cmdline(tmp_path: Path) -> None:
    """Managed sidecars and jeveration_worker Python processes never trigger non-LLM VRAM eviction."""
    worker_script = tmp_path / "jeveration_worker.py"
    worker_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    sidecar_script = tmp_path / "custom_sidecar.py"
    sidecar_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    unrelated_script = tmp_path / "unrelated_app.py"
    unrelated_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    jev_proc = subprocess.Popen([sys.executable, str(worker_script)])
    managed_proc = subprocess.Popen([sys.executable, str(sidecar_script)])
    unrelated_proc = subprocess.Popen([sys.executable, str(unrelated_script)])
    try:
        csv_out = "0, NVIDIA GeForce RTX 4090, 00000000:01:00.0, 24564, 18500, 6064, 45, 58\n"
        game_pid = 9999991
        pmon_out = (
            "# gpu        pid  type    fb   ccpm  command\n"
            f"    0  {jev_proc.pid:>7}     C  6000      -  python\n"
            f"    0  {managed_proc.pid:>7}     C  3500      -  python\n"
            f"    0  {unrelated_proc.pid:>7}     C  1200      -  python\n"
            f"    0  {game_pid:>7}     G  4000      -  Cyberpunk2077\n"
        )

        # Direct parse_nvidia_pmon verification
        pmon_procs = parse_nvidia_pmon(pmon_out, managed_pids={managed_proc.pid})
        by_pid = {p.pid: p for p in pmon_procs}
        assert by_pid[jev_proc.pid].is_llm is True
        assert by_pid[managed_proc.pid].is_llm is True
        assert by_pid[unrelated_proc.pid].is_llm is False
        assert by_pid[game_pid].is_llm is False

        # parse_nvidia_csv verification: only unrelated python + game count toward non_llm_vram_mb
        gpus, csv_procs = parse_nvidia_csv(
            csv_out, pmon_out, managed_pids={managed_proc.pid}
        )
        assert len(gpus) == 1
        assert gpus[0].non_llm_vram_mb == 5200.0
        csv_by_pid = {p.pid: p for p in csv_procs}
        assert csv_by_pid[jev_proc.pid].is_llm is True
        assert csv_by_pid[managed_proc.pid].is_llm is True
        assert csv_by_pid[unrelated_proc.pid].is_llm is False
        assert csv_by_pid[game_pid].is_llm is False

        # Without managed_pids, managed_proc (custom_sidecar.py) falls back to non-LLM
        gpus_unmanaged, unmanaged_procs = parse_nvidia_csv(csv_out, pmon_out)
        unmanaged_by_pid = {p.pid: p for p in unmanaged_procs}
        assert unmanaged_by_pid[jev_proc.pid].is_llm is True
        assert unmanaged_by_pid[managed_proc.pid].is_llm is False
        assert gpus_unmanaged[0].non_llm_vram_mb == 8700.0
    finally:
        for proc in (jev_proc, managed_proc, unrelated_proc):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
