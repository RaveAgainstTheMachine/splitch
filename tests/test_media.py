"""End-to-end tests for Unified Media Plane (sd-server, ComfyUI, Auto-VRAM eviction, sd.cpp installer)."""

from __future__ import annotations

import base64
import json
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.router.config import SplitchConfig
from backend.router.media_api import (
    _apply_workflow_placeholders,
    _resolve_node_media_url,
    build_media_router,
)
from backend.router.telemetry import ClusterState
from backend.shared.engines import install_sd_from_archives, select_sd_assets
from backend.shared.models import LoadedModel, NodeStatus, NodeTelemetry

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_select_sd_assets_matrix_and_zip_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = [
        {"name": "cudart-sd-bin-win-cu12-x64.zip", "browser_download_url": "https://ex/rt.zip"},
        {"name": "sd-master-532d2c1-bin-Darwin-macOS-26.7.2-arm64.zip", "browser_download_url": "https://ex/mac.zip"},
        {"name": "sd-master-532d2c1-bin-Linux-Ubuntu-24.04-x86_64-rocm-10.1.0.zip", "browser_download_url": "https://ex/rocm.zip"},
        {"name": "sd-master-532d2c1-bin-Linux-Ubuntu-26.04-x86_64-vulkan.zip", "browser_download_url": "https://ex/vk.zip"},
        {"name": "sd-master-532d2c1-bin-Linux-Ubuntu-26.04-x86_64.zip", "browser_download_url": "https://ex/cpu.zip"},
        {"name": "sd-master-532d2c1-bin-win-cuda12-x64.zip", "browser_download_url": "https://ex/win_cu.zip"},
    ]
    # Linux CUDA falls back cleanly to Linux Vulkan when no Linux CUDA zip exists
    main, rt = select_sd_assets(assets, os_name="linux", arch="x64", backend="cuda")
    assert main is not None and "vulkan" in main["name"]
    assert rt is None

    # Windows CUDA pairs main zip + cudart zip
    w_main, w_rt = select_sd_assets(assets, os_name="win", arch="x64", backend="cuda")
    assert w_main is not None and "win-cuda12" in w_main["name"]
    assert w_rt is not None and "cudart-sd" in w_rt["name"]

    # macOS ARM64
    m_main, m_rt = select_sd_assets(assets, os_name="macos", arch="arm64", backend="metal")
    assert m_main is not None and "Darwin-macOS" in m_main["name"]
    assert m_rt is None

    # Atomic zip install + shim creation
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "data"))
    zip_path = tmp_path / "sd-master-test-bin-Linux-Ubuntu-26.04-x86_64-vulkan.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("sd-server", "#!/usr/bin/env sh\necho sd-server-ok\n")
        zf.writestr("sd-cli", "#!/usr/bin/env sh\necho sd-cli-ok\n")

    res = install_sd_from_archives(
        tag="master-1106-532d2c1",
        main_archive=zip_path,
        backend="vulkan",
        bin_dir=tmp_path / "bin",
    )
    assert res.ok is True
    assert res.current_tag == "master-1106-532d2c1"
    assert Path(res.binary_path).is_file()
    shim_ext = ".cmd" if sys.platform == "win32" else ""
    assert (tmp_path / "bin" / f"sd-server{shim_ext}").is_file()
    assert (tmp_path / "bin" / f"sd-cli{shim_ext}").is_file()


def test_workflow_placeholder_substitution_and_lan_url_resolution() -> None:
    tpl = json.dumps(
        {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "{{MODEL}}"}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{PROMPT}}"}},
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": "{{SEED}}",
                    "steps": "{{STEPS}}",
                    "cfg": "{{CFG}}",
                    "width": "{{WIDTH}}",
                    "height": "{{HEIGHT}}",
                    "frames": "{{FRAMES}}",
                    "fps": "{{FPS}}",
                },
            },
        }
    )
    out = _apply_workflow_placeholders(
        tpl,
        prompt="cyberpunk neon city",
        negative_prompt="blurry",
        model="flux1-dev.safetensors",
        width=1344,
        height=768,
        steps=24,
        cfg_scale=4.5,
        seed=42,
        frames=25,
        fps=12,
        uploaded_image=None,
    )
    assert out["1"]["inputs"]["ckpt_name"] == "flux1-dev.safetensors"
    assert out["2"]["inputs"]["text"] == "cyberpunk neon city"
    assert out["5"]["inputs"]["seed"] == 42
    assert out["5"]["inputs"]["cfg"] == 4.5
    assert out["5"]["inputs"]["frames"] == 25

    node = NodeStatus(
        node_id="pc",
        display_name="PC",
        role="gpu",
        agent_url="http://192.168.1.50:8090",
        engine_url="http://192.168.1.50:8081",
        online=True,
        telemetry=NodeTelemetry(
            node_id="pc",
            role="gpu",
            platform="linux",
            timestamp=1.0,
            media_engine="sd-server",
            media_healthy=True,
            media_url="http://127.0.0.1:8082",
        ),
    )
    assert _resolve_node_media_url(node) == "http://192.168.1.50:8090/media/proxy"


def test_media_api_sd_server_vram_eviction_and_gallery_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
    cluster.nodes["gpu1"] = NodeStatus(
        node_id="gpu1",
        display_name="GPU Rig",
        role="gpu",
        yield_on_gaming=True,
        agent_url="http://192.168.1.50:8090",
        engine_url="http://192.168.1.50:8081",
        online=True,
        telemetry=NodeTelemetry(
            node_id="gpu1",
            role="gpu",
            platform="linux",
            timestamp=1.0,
            vram_total_mb=24564.0,
            vram_free_mb=18000.0,
            engine="llama-server",
            engine_healthy=True,
            media_engine="sd-server",
            media_healthy=True,
            media_url="http://127.0.0.1:8082",
            media_models=["flux-schnell-q8.gguf"],
            models=[LoadedModel(id="coder-heavy", status="loaded")],
        ),
    )

    evicted_models: list[tuple[str, str, str]] = []

    async def fake_proxy_model_action(node_id: str, action: str, model: str):
        evicted_models.append((node_id, action, model))
        return 200, {"ok": True}

    monkeypatch.setattr(cluster, "proxy_model_action", fake_proxy_model_action)

    # Intercept outbound httpx calls to agent media proxy -> sd-server
    def fake_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/media/proxy/v1/images/generations":
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(TINY_PNG).decode("ascii")}]},
            )
        return httpx.Response(404)

    orig_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake_handler)
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr("backend.router.media_api.httpx.AsyncClient", patched_async_client)

    app = FastAPI()
    media_router, _ = build_media_router(cluster, tmp_path)
    app.include_router(media_router)
    client = TestClient(app)

    # 1. Backends list
    b_res = client.get("/api/media/backends")
    assert b_res.status_code == 200
    backends = b_res.json()
    assert len(backends) == 1 and backends[0]["media_engine"] == "sd-server"

    # 2. OpenAI-compatible /v1/images/generations triggers LLM VRAM eviction and returns PNG
    img_res = client.post(
        "/v1/images/generations",
        json={"prompt": "a clean geometric prism logo", "size": "512x512", "response_format": "url"},
    )
    assert img_res.status_code == 200
    data = img_res.json()["data"]
    assert len(data) == 1
    assert data[0]["url"].startswith("/api/media/files/med_")
    assert evicted_models == [("gpu1", "unload", "coder-heavy")]

    # 3. Saved file is retrievable over /api/media/files/{filename}
    file_res = client.get(data[0]["url"])
    assert file_res.status_code == 200
    assert file_res.content == TINY_PNG

    # 4. Path traversal blocked
    bad_res = client.get("/api/media/files/..%2Fjobs.json")
    assert bad_res.status_code in (400, 404)

    # 5. Gallery jobs list & delete
    jobs = client.get("/api/media/jobs").json()
    assert len(jobs) == 1 and jobs[0]["status"] == "completed"
    del_res = client.delete(f"/api/media/jobs/{jobs[0]['id']}")
    assert del_res.status_code == 200
    assert client.get(data[0]["url"]).status_code == 404


def test_agent_managed_media_engine_status_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.agent.main as agent_mod

    monkeypatch.setattr(agent_mod, "STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    (tmp_path / "sd_xl_turbo_1.0_fp16.safetensors").write_bytes(b"fake-ckpt")

    client = TestClient(agent_mod.app)
    st = client.get("/media/engine/status")
    assert st.status_code == 200
    body = st.json()
    assert any(m["filename"] == "sd_xl_turbo_1.0_fp16.safetensors" for m in body["local_media_models"])

    # Path traversal in model_file rejected
    bad_start = client.post("/media/engine/start", json={"model_file": "../etc/passwd"})
    assert bad_start.status_code == 400

    # Non-gguf/safetensors pull rejected
    bad_pull = client.post(
        "/files/models/pull",
        json={"repo_id": "stabilityai/sdxl-turbo", "filename": "malware.exe"},
    )
    assert bad_pull.status_code == 400


def test_sd_bundle_auto_wiring_and_media_hf_routes(tmp_path):
    """Multi-file Z-Image, FLUX, and Wan 2.1 bundles auto-wire companions; HF routes registered."""
    import backend.agent.main as agent_mod
    from backend.router.app import app as router_app

    z_img = tmp_path / "z-image-turbo-Q4_K_M.gguf"
    z_img.write_bytes(b"z")
    # Missing companions returns helpful error
    args, err = agent_mod._build_sd_model_args(tmp_path, z_img)
    assert args == [] and err and "Qwen3-4B" in err and "ae.safetensors" in err

    # With companions present, auto-wires --diffusion-model, --llm, --vae, --flash-attn
    (tmp_path / "Qwen3-4B-Q4_K_M.gguf").write_bytes(b"q")
    (tmp_path / "ae.safetensors").write_bytes(b"a")
    args, err = agent_mod._build_sd_model_args(tmp_path, z_img)
    assert err is None
    assert "--diffusion-model" in args and "--llm" in args and "--vae" in args and "--flash-attn" in args

    # Wan 2.1 video bundle auto-wiring
    wan = tmp_path / "wan2.1_t2v_1.3B_bf16.safetensors"
    wan.write_bytes(b"w")
    (tmp_path / "umt5_xxl_fp8_e4m3fn_scaled.safetensors").write_bytes(b"t")
    (tmp_path / "wan_2.1_vae.safetensors").write_bytes(b"v")
    w_args, w_err = agent_mod._build_sd_model_args(tmp_path, wan)
    assert w_err is None
    assert "--diffusion-model" in w_args and "--t5xxl" in w_args and "--flow-shift" in w_args

    # Router HF media search/files + agent video generation routes registered
    router_paths = set(router_app.openapi()["paths"].keys())
    assert "/api/media/hf/search" in router_paths
    assert "/api/media/hf/files" in router_paths
    agent_paths = set(agent_mod.app.openapi()["paths"].keys())
    assert "/media/video/generate" in agent_paths
