"""Tests for Settings Modal API, Connect Clients, Multi-GPU/Heterogeneous GPU & Node Provisioning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent.main import _merge_client_config
from backend.router.config import GuardConfig, NodeConfig, SplitchConfig
from backend.router.models_api import (
    _node_budget_gb,
    _node_single_gpu_budget_gb,
    build_models_router,
)
from backend.router.policy import check_node_eligibility, watched_gpu_metrics
from backend.router.provision_api import (
    JoinTokenManager,
    build_provision_router,
    validate_ssh_target,
)
from backend.router.settings_api import apply_persisted_settings, build_settings_router
from backend.router.telemetry import ClusterState
from backend.shared.gpu_probe import (
    merge_gpu_lists,
    normalize_bus_id,
    parse_nvidia_csv,
    parse_rocm_json,
    parse_vulkaninfo_summary,
    parse_xpu_smi_json,
    probe_linux_drm_sysfs,
    recommend_backend_from_gpus,
    summarize_gpus,
)
from backend.shared.models import (
    ControlState,
    GpuDevice,
    LoadedModel,
    NodeStatus,
    NodeTelemetry,
)

# ── Phase 2: Multi-GPU & Heterogeneous GPU Tests ─────────────────────────────


def test_parse_nvidia_multi_gpu_with_per_card_pmon() -> None:
    csv_out = (
        "0, NVIDIA GeForce RTX 4090, 00000000:01:00.0, 24564, 14500, 10064, 68, 62\n"
        "1, NVIDIA GeForce RTX 3090, 00000000:05:00.0, 24576, 18000, 6576, 12, 41\n"
    )
    pmon_out = (
        "# gpu        pid  type    fb   ccpm  command\n"
        "    0      11111     G  8200      -  Cyberpunk2077\n"
        "    1      22222     C 17500      -  llama-server\n"
    )
    gpus, procs = parse_nvidia_csv(csv_out, pmon_out)
    assert len(gpus) == 2
    assert gpus[0].bus_id == "0000:01:00.0"
    assert gpus[0].non_llm_vram_mb == 8200.0
    assert gpus[1].bus_id == "0000:05:00.0"
    assert gpus[1].non_llm_vram_mb == 0.0
    assert len(procs) == 2
    assert procs[0].gpu_index == 0 and not procs[0].is_llm
    assert procs[1].gpu_index == 1 and procs[1].is_llm


def test_parse_rocm_xpu_drm_vulkan_and_heterogeneous_merge(tmp_path: Path) -> None:
    # 1. Synthetic NVIDIA + AMD + Intel + Vulkan overlap
    nv_gpus, _ = parse_nvidia_csv(
        "0, NVIDIA GeForce RTX 4090, 00000000:01:00.0, 24564, 4096, 20468, 10, 42\n"
    )
    rocm_gpus = parse_rocm_json(
        json.dumps(
            {
                "card0": {
                    "Card Series": "AMD Radeon RX 7900 XTX",
                    "PCI Bus": "0000:03:00.0",
                    "VRAM Total Memory (B)": str(24 * 1024**3),
                    "VRAM Total Used Memory (B)": str(2 * 1024**3),
                    "GPU use (%)": "8",
                    "Temperature (Sensor edge) (C)": "46",
                }
            }
        )
    )
    xpu_gpus = parse_xpu_smi_json(
        json.dumps(
            {
                "device_list": [
                    {
                        "device_name": "Intel Arc A770 Graphics",
                        "pci_bdf_address": "0000:06:00.0",
                        "memory_physical_size_byte": str(16 * 1024**3),
                    }
                ]
            }
        )
    )

    # 2. Synthetic Linux DRM sysfs with overlapping AMD bus ID + fresh generic card
    drm_root = tmp_path / "drm"
    amd_dev = drm_root / "card0" / "device"
    amd_dev.mkdir(parents=True)
    (amd_dev / "vendor").write_text("0x1002\n")
    (amd_dev / "device").write_text("0x744c\n")
    (amd_dev / "uevent").write_text("DRIVER=amdgpu\nPCI_SLOT_NAME=0000:03:00.0\n")
    (amd_dev / "mem_info_vram_total").write_text(str(24 * 1024**3))
    (amd_dev / "mem_info_vram_used").write_text(str(2 * 1024**3))
    (amd_dev / "gpu_busy_percent").write_text("9")
    hw0 = amd_dev / "hwmon" / "hwmon0"
    hw0.mkdir(parents=True)
    (hw0 / "temp1_input").write_text("47000")

    drm_gpus = probe_linux_drm_sysfs(drm_root)
    assert len(drm_gpus) == 1
    assert drm_gpus[0].vendor == "amd"
    assert drm_gpus[0].temp_c == 47.0

    vk_out = """
Devices:
========
GPU0:
\tvendorID = 0x10de
\tdeviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName = NVIDIA GeForce RTX 4090
GPU1:
\tvendorID = 0x10005
\tdeviceType = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName = llvmpipe (LLVM 18.1.8, 256 bits)
"""
    vk_gpus = parse_vulkaninfo_summary(vk_out)
    assert len(vk_gpus) == 1  # llvmpipe filtered out

    merged = merge_gpu_lists(nv_gpus, rocm_gpus, xpu_gpus, drm_gpus, vk_gpus)
    assert len(merged) == 3  # Deduplicated AMD bus_id + deduplicated NVIDIA vulkan
    assert [g.vendor for g in merged] == ["nvidia", "amd", "intel"]
    assert [g.index for g in merged] == [0, 1, 2]

    # Mixed-vendor auto-selects Vulkan so llama-server spans NVIDIA + AMD + Intel!
    assert recommend_backend_from_gpus(merged, os_name="linux", arch="x86_64") == "vulkan"
    # Homogeneous NVIDIA -> CUDA
    assert recommend_backend_from_gpus(nv_gpus + nv_gpus, os_name="linux", arch="x86_64") == "cuda"
    # Intel only -> Vulkan
    assert recommend_backend_from_gpus(xpu_gpus, os_name="linux", arch="x86_64") == "vulkan"
    # Apple Silicon -> Metal
    assert recommend_backend_from_gpus([], os_name="darwin", arch="arm64") == "metal"
    # CPU-only -> CPU
    assert recommend_backend_from_gpus([], os_name="linux", arch="x86_64") == "cpu"

    summary = summarize_gpus(merged)
    assert summary["gpu_count"] == 3
    assert summary["gpu_vendors"] == ["amd", "intel", "nvidia"]
    assert "RTX 4090" in summary["gpu_name"] and "RX 7900 XTX" in summary["gpu_name"]
    assert summary["vram_total_mb"] == pytest.approx(24564.0 + 24576.0 + 16384.0, abs=1.0)
    assert normalize_bus_id("00000000:0A:00.0") == "0000:0a:00.0"


def test_per_gpu_auto_yield_ignores_unwatched_gaming_card() -> None:
    guards = GuardConfig(non_llm_vram_threshold_mb=4096.0, min_vram_free_mb=4096.0)
    gpu0_gaming = GpuDevice(
        index=0,
        vendor="nvidia",
        name="RTX 4090 (Display)",
        vram_total_mb=24564.0,
        vram_used_mb=12000.0,
        vram_free_mb=12564.0,
        util_percent=96.0,
        non_llm_vram_mb=9500.0,
    )
    gpu1_compute = GpuDevice(
        index=1,
        vendor="amd",
        name="RX 7900 XTX (Compute)",
        vram_total_mb=24576.0,
        vram_used_mb=14000.0,
        vram_free_mb=10576.0,
        util_percent=15.0,
        non_llm_vram_mb=0.0,
    )
    telemetry = NodeTelemetry(
        node_id="dual-rig",
        role="gpu",
        platform="linux",
        timestamp=1.0,
        gpu_name="RTX 4090 + RX 7900 XTX",
        vram_total_mb=49140.0,
        vram_used_mb=26000.0,
        vram_free_mb=23140.0,
        gpu_util_percent=96.0,
        non_llm_vram_mb=9500.0,
        gpus=[gpu0_gaming, gpu1_compute],
        gpu_count=2,
        gpu_vendors=["amd", "nvidia"],
        engine="llama-server",
        engine_healthy=True,
        models=[LoadedModel(id="heavy", status="loaded")],
    )

    # Watching ONLY GPU 1 (dedicated compute) -> gaming on GPU 0 does NOT yield the node!
    node_watch_1 = NodeStatus(
        node_id="dual-rig",
        display_name="Dual Rig",
        role="gpu",
        yield_on_gaming=True,
        yield_gpu_indices=[1],
        agent_url="http://127.0.0.1:8090",
        engine_url="http://127.0.0.1:8081",
        online=True,
        telemetry=telemetry,
    )
    non_llm, util, _, _ = watched_gpu_metrics(node_watch_1)
    assert non_llm == 0.0 and util == 15.0
    assert check_node_eligibility(node_watch_1, "heavy", ControlState(), guards) is None

    # Watching ALL GPUs (empty list) -> gaming on GPU 0 DOES yield the node
    node_watch_all = node_watch_1.model_copy(update={"yield_gpu_indices": []})
    assert check_node_eligibility(node_watch_all, "heavy", ControlState(), guards) == "dual-rig_gpu_busy_app"


def test_multi_gpu_model_fit_budgets() -> None:
    node = NodeStatus(
        node_id="dual",
        display_name="Dual 24GB",
        role="gpu",
        agent_url="http://127.0.0.1:8090",
        engine_url="http://127.0.0.1:8081",
        online=True,
        telemetry=NodeTelemetry(
            node_id="dual",
            role="gpu",
            platform="linux",
            timestamp=1.0,
            vram_total_mb=49152.0,
            gpus=[
                GpuDevice(index=0, vendor="nvidia", name="RTX 4090", vram_total_mb=24576.0),
                GpuDevice(index=1, vendor="amd", name="RX 7900 XTX", vram_total_mb=24576.0),
            ],
            gpu_count=2,
        ),
    )
    single = _node_single_gpu_budget_gb(node)
    combined = _node_budget_gb(node)
    assert 20.0 < single < 22.0
    assert 41.0 < combined < 44.0


# ── Phase 1: Settings Persistence, Protocol Probe & Client Config Merge ──────


def test_settings_get_patch_persist_and_reload(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    cfg = SplitchConfig()
    cluster = ClusterState(cfg, data_dir=data_dir)
    app = FastAPI()
    app.include_router(build_settings_router(cluster, data_dir, api_key_getter=lambda: ""))

    client = TestClient(app)
    r0 = client.get("/api/settings")
    assert r0.status_code == 200
    assert r0.json()["yield_cooldown_s"] == 60.0

    r1 = client.patch(
        "/api/settings",
        json={
            "non_llm_vram_threshold_mb": 6144,
            "yield_cooldown_s": 90,
            "auto_evict_on_yield": False,
            "public_url": "https://ai.example.com/",
            "poll_interval_s": 5.0,
        },
    )
    assert r1.status_code == 200
    body = r1.json()
    assert body["non_llm_vram_threshold_mb"] == 6144.0
    assert body["yield_cooldown_s"] == 90.0
    assert body["auto_evict_on_yield"] is False
    assert body["public_url"] == "https://ai.example.com"
    assert (data_dir / "settings.json").exists()

    # Invalid URL rejected
    r_bad = client.patch("/api/settings", json={"public_url": "ftp://bad.host"})
    assert r_bad.status_code == 400

    # Reload into fresh ClusterState
    cluster2 = ClusterState(SplitchConfig(), data_dir=data_dir)
    apply_persisted_settings(cluster2, data_dir)
    assert cluster2.config.guards.non_llm_vram_threshold_mb == 6144.0
    assert cluster2.config.guards.yield_cooldown_s == 90.0
    assert cluster2.config.router.public_url == "https://ai.example.com"


def test_merge_client_config_for_all_cli_clients(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    p_hermes = _merge_client_config("hermes", {}, "model:\n  provider: custom\n")
    assert Path(p_hermes).read_text(encoding="utf-8").startswith("model:")

    # Codex preserves existing unrelated lines and updates managed block cleanly
    codex_path = tmp_path / ".codex" / "config.toml"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text('approval_policy = "on-request"\n', encoding="utf-8")
    _merge_client_config("codex", {}, 'model = "heavy"\n')
    _merge_client_config("codex", {}, 'model = "fast"\n')
    codex_txt = codex_path.read_text(encoding="utf-8")
    assert 'approval_policy = "on-request"' in codex_txt
    assert 'model = "fast"' in codex_txt
    assert 'model = "heavy"' not in codex_txt

    p_claude = _merge_client_config("claude_code", {"env": {"ANTHROPIC_BASE_URL": "http://localhost:8080"}})
    assert json.loads(Path(p_claude).read_text())["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:8080"

    p_aider = _merge_client_config("aider", {}, "openai-api-base: http://localhost:8080/v1\n")
    assert "openai-api-base" in Path(p_aider).read_text(encoding="utf-8")


# ── Phase 3: Join Tokens, Self-Hosted Installers & SSH Provisioning Guards ───


def test_join_token_lifecycle_and_self_hosted_installers(tmp_path: Path) -> None:
    cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
    jtm = JoinTokenManager()
    app = FastAPI()
    app.include_router(build_provision_router(cluster, jtm, api_key_getter=lambda: ""))

    client = TestClient(app)

    # Self-hosted scripts served directly
    r_sh = client.get("/install.sh")
    assert r_sh.status_code == 200
    assert "--join-token" in r_sh.text

    r_ps = client.get("/install.ps1")
    assert r_ps.status_code == 200
    assert "$JoinToken" in r_ps.text

    # Mint single-use join token
    r_tok = client.post(
        "/api/provision/join-token",
        json={"node_id": "win-rig", "role": "gpu", "backend": "vulkan", "router_url": "https://192.168.1.10:8080"},
    )
    assert r_tok.status_code == 200
    data = r_tok.json()
    assert data["token"].startswith("spj_")
    assert "/install.sh" in data["linux_macos_cmd"]
    assert "--join-token" in data["linux_macos_cmd"]
    assert "/install.ps1" in data["windows_ps_cmd"]
    assert "-JoinToken" in data["windows_ps_cmd"]

    # Listed without leaking secret
    listed = client.get("/api/provision/join-tokens").json()
    assert len(listed) == 1
    assert listed[0]["id"] == data["id"]
    assert "token" not in listed[0]

    # Single-use consumption
    assert jtm.validate_and_consume(data["token"]) is True
    assert jtm.validate_and_consume(data["token"]) is False  # replay rejected!


def test_ssh_provision_guardrails_and_target_validation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SPLITCH_ENABLE_REMOTE_PROVISION", raising=False)
    cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
    jtm = JoinTokenManager()

    # Without API key or opt-in -> 403
    app_locked = FastAPI()
    app_locked.include_router(build_provision_router(cluster, jtm, api_key_getter=lambda: ""))
    c_locked = TestClient(app_locked)
    r_blocked = c_locked.post("/api/provision/ssh/test", json={"ssh_target": "user@192.168.1.50"})
    assert r_blocked.status_code == 403

    # With API key enabled -> validates strict ssh_target regex
    app_open = FastAPI()
    app_open.include_router(build_provision_router(cluster, jtm, api_key_getter=lambda: "secret-key"))
    c_open = TestClient(app_open)
    for bad in ("user@host;id", "-oProxyCommand=sh", "user@host $(whoami)", "no-at-sign", "user@host | cat"):
        r_bad = c_open.post("/api/provision/ssh/test", json={"ssh_target": bad})
        assert r_bad.status_code == 400
    assert validate_ssh_target("dev@192.168.1.50") == "dev@192.168.1.50"


def test_router_app_join_token_allows_node_registration_under_api_key(monkeypatch) -> None:
    import backend.router.app as router_app

    monkeypatch.setattr(router_app, "API_KEY", "master-secret-key")
    client = TestClient(router_app.app)

    # Unauthenticated node registration -> 401
    payload = {
        "id": "tok-test-node",
        "display_name": "Token Node",
        "role": "gpu",
        "yield_on_gaming": True,
        "yield_gpu_indices": [1],
        "agent_url": "https://192.168.1.99:8090",
        "engine_url": "http://192.168.1.99:8081",
    }
    r_unauth = client.post("/api/nodes", json=payload)
    assert r_unauth.status_code == 401

    # Authenticated admin mints join token
    r_mint = client.post(
        "/api/provision/join-token",
        headers={"Authorization": "Bearer master-secret-key"},
        json={"node_id": "tok-test-node", "router_url": "https://192.168.1.10:8080"},
    )
    assert r_mint.status_code == 200
    jtok = r_mint.json()["token"]

    # Node registers with ONLY single-use X-Splitch-Join-Token (no master API key!)
    r_join = client.post("/api/nodes", headers={"X-Splitch-Join-Token": jtok}, json=payload)
    assert r_join.status_code == 200
    assert r_join.json()["yield_gpu_indices"] == [1]

    # Replay of consumed token -> 401
    r_replay = client.post("/api/nodes", headers={"X-Splitch-Join-Token": jtok}, json=payload)
    assert r_replay.status_code == 401

    # Cleanup registered test node
    client.delete("/api/nodes/tok-test-node", headers={"Authorization": "Bearer master-secret-key"})


def test_models_router_client_probe_and_fit_smoke(tmp_path: Path) -> None:
    cfg = SplitchConfig(
        nodes=[
            NodeConfig(
                id="local",
                display_name="Local",
                role="cpu",
                agent_url="http://127.0.0.1:8090",
                engine_url="http://127.0.0.1:8081",
            )
        ]
    )
    cluster = ClusterState(cfg, data_dir=tmp_path)
    app = FastAPI()
    app.include_router(build_models_router(cluster, tmp_path))
    app.include_router(build_settings_router(cluster, tmp_path, api_key_getter=lambda: ""))

    @app.get("/v1/models")
    async def _models():
        return {"object": "list", "data": [{"id": "auto", "object": "model"}]}

    client = TestClient(app)
    r = client.post("/api/clients/probe", json={"protocol": "models", "model": "auto"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["status_code"] == 200
    assert "1 models" in r.json()["resolved_model"]


@pytest.mark.asyncio
async def test_collect_all_gpus_skips_vulkaninfo_when_nvidia_already_named(monkeypatch) -> None:
    import backend.shared.gpu_probe as gp

    gp.reset_static_gpu_caches()
    vk_calls = 0

    async def _fake_nv():
        return [
            GpuDevice(
                index=0,
                vendor="nvidia",
                name="NVIDIA GeForce RTX 4090",
                bus_id="0000:01:00.0",
                vram_total_mb=24564.0,
            )
        ], []

    async def _fake_empty():
        return []

    async def _fake_vk():
        nonlocal vk_calls
        vk_calls += 1
        return []

    monkeypatch.setattr(gp.sys, "platform", "linux")
    monkeypatch.setattr(gp.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gp, "_collect_nvidia_async", _fake_nv)
    monkeypatch.setattr(gp, "_collect_rocm_async", _fake_empty)
    monkeypatch.setattr(gp, "_collect_intel_xpu_async", _fake_empty)
    monkeypatch.setattr(gp, "_collect_windows_cim_async", _fake_empty)
    monkeypatch.setattr(gp, "_collect_vulkan_async", _fake_vk)
    monkeypatch.setattr(gp, "probe_linux_drm_sysfs", list)

    gpus1, _, rec1 = await gp.collect_all_gpus()
    gpus2, _, rec2 = await gp.collect_all_gpus()
    assert len(gpus1) == 1 and rec1 == "cuda"
    assert len(gpus2) == 1 and rec2 == "cuda"
    assert vk_calls == 0
    gp.reset_static_gpu_caches()
