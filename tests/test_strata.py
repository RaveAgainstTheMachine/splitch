"""Tests for Strata Phase 2: SHA-256 manifest install, 4-tier fit, CLI safety, sidecar proxy & Quick Tune."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.agent import main as agent_mod
from backend.router import app as router_app_mod
from backend.router.config import GuardConfig
from backend.router.policy import resolve_route
from backend.router.proxy import build_openai_models_list
from backend.shared.models import (
    ControlState,
    GpuDevice,
    LoadedModel,
    NodeStatus,
    NodeTelemetry,
    StrataCalibrateRequest,
    StrataLaunchRequest,
)
from backend.shared.strata_manager import (
    STRATA_CATALOG,
    _validate_strata_args,
    assess_strata_fit,
    build_strata_calibrate_cmd,
    build_strata_launch_cmd,
    install_or_update_strata,
    parse_install_manifest,
    strata_platform_key,
)


def _make_tar_gz(entry_name: str = "strata", body: bytes = b"#!/bin/sh\necho ok\n") -> tuple[bytes, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=entry_name)
        info.size = len(body)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(body))
    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def test_manifest_parsing_and_sha256_verification(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "data"))
    archive_bytes, good_sha = _make_tar_gz()
    plat = strata_platform_key() or "linux-x86_64"
    manifest_txt = f"{plat} 0.1.68 strata-control-0.1.68-{plat}.tar.gz {good_sha} {len(archive_bytes)}\n"

    parsed = parse_install_manifest(manifest_txt)
    assert parsed[plat]["version"] == "0.1.68"
    assert parsed[plat]["sha256"] == good_sha

    class _FakeResp:
        def __init__(self, *, text: str = "", data: bytes = b"") -> None:
            self.text = text
            self._data = data
            self.headers = {"content-length": str(len(data))}

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, chunk_size: int = 65536):
            yield self._data

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    bad_manifest = manifest_txt.replace(good_sha, "0" * 64)

    class _BadClient:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url: str):
            return _FakeResp(text=bad_manifest)

        def stream(self, _method: str, _url: str):
            return _FakeResp(data=archive_bytes)

    monkeypatch.setattr(httpx, "Client", _BadClient)
    fail_res = install_or_update_strata(bin_dir=tmp_path / "bin")
    assert fail_res.ok is False
    assert "SHA-256 mismatch" in (fail_res.error or "")

    class _GoodClient(_BadClient):
        def get(self, url: str):
            return _FakeResp(text=manifest_txt)

    monkeypatch.setattr(httpx, "Client", _GoodClient)
    ok_res = install_or_update_strata(bin_dir=tmp_path / "bin")
    assert ok_res.ok is True
    assert ok_res.current_tag == "0.1.68"
    assert Path(ok_res.shim_path).is_file()


def test_strata_cli_arg_validation_and_path_confinement(tmp_path: Path) -> None:
    fake_bin = tmp_path / "strata"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    storage = tmp_path / "models"
    storage.mkdir()

    # Reject shell metacharacters
    with pytest.raises(ValueError):
        build_strata_launch_cmd(
            fake_bin,
            StrataLaunchRequest(family="qwen;rm -rf /", model="q2_0"),
            storage_root=storage,
            port=8085,
        )

    # Reject model_path outside storage root
    with pytest.raises(ValueError):
        build_strata_launch_cmd(
            fake_bin,
            StrataLaunchRequest(model_path="/etc/passwd"),
            storage_root=storage,
            port=8085,
        )

    # Valid launch command + Quick Tune calibrate command
    cmd = build_strata_launch_cmd(
        fake_bin,
        StrataLaunchRequest(family="qwen", model="iq2_xs", context="128k", drafting="dflash2"),
        storage_root=storage,
        port=8085,
    )
    assert "--port" in cmd and "8085" in cmd
    assert "--no-tui" in cmd and "--no-api-key" in cmd
    assert "--drafting" in cmd and "dflash2" in cmd

    cal_cmd = build_strata_calibrate_cmd(
        fake_bin,
        StrataCalibrateRequest(family="qwen", model="iq2_xs", mode="quick"),
        storage_root=storage,
        port=8085,
    )
    assert "--tune" in cal_cmd and "quick" in cal_cmd


def test_four_tier_fit_estimator() -> None:
    iq2_xs = next(s for s in STRATA_CATALOG if s.family == "qwen" and s.model == "iq2_xs")

    pc_optimal = NodeStatus(
        node_id="pc",
        display_name="PC",
        role="gpu",
        agent_url="http://127.0.0.1:8090",
        engine_url="http://127.0.0.1:8081",
        online=True,
        telemetry=NodeTelemetry(
            node_id="pc",
            role="gpu",
            platform="linux",
            timestamp=1.0,
            vram_total_mb=24576.0,
            ram_total_mb=65536.0,
            storage_free_gb=500.0,
        ),
    )
    assert assess_strata_fit(iq2_xs, pc_optimal).tier == "optimal"

    pc_low_ram = pc_optimal.model_copy(
        update={
            "telemetry": pc_optimal.telemetry.model_copy(update={"ram_total_mb": 32768.0})  # type: ignore[union-attr]
        }
    )
    assert assess_strata_fit(iq2_xs, pc_low_ram).tier == "low_ram"

    mac_insufficient = NodeStatus(
        node_id="mac",
        display_name="Mac",
        role="unified",
        agent_url="http://127.0.0.1:8090",
        engine_url="http://127.0.0.1:8081",
        online=True,
        telemetry=NodeTelemetry(
            node_id="mac",
            role="unified",
            platform="darwin",
            timestamp=1.0,
            vram_total_mb=12288.0,
            ram_total_mb=16384.0,
            storage_free_gb=500.0,
            gpus=[GpuDevice(index=0, vendor="apple", name="M4", is_unified=True)],
        ),
    )
    assert assess_strata_fit(iq2_xs, mac_insufficient).tier == "insufficient"
    inkling = next(s for s in STRATA_CATALOG if s.family == "inkling")
    assert assess_strata_fit(inkling, mac_insufficient).tier == "tight"
    mac_32 = mac_insufficient.model_copy(
        update={
            "telemetry": mac_insufficient.telemetry.model_copy(  # type: ignore[union-attr]
                update={"vram_total_mb": 24576.0, "ram_total_mb": 32768.0}
            )
        }
    )
    assert assess_strata_fit(inkling, mac_32).tier == "optimal"


def test_strata_catalog_19_packs_and_arg_validation() -> None:
    assert len(STRATA_CATALOG) == 19
    expected_new_packs = {
        ("gyro", "gyro-s"),
        ("gyro", "gyro-m"),
        ("unsloth", "ud-iq4_xs"),
        ("unsloth", "ud-q4_k_xl"),
        ("nvfp4", "nvfp4"),
        ("hy4-flash", "sq-96"),
        ("unsloth", "ud-q5_k_xl"),
        ("unsloth", "ud-q6_k_xl"),
        ("hy4-flash", "sq-128"),
        ("unsloth", "q8_0"),
    }
    catalog_keys = {(spec.family, spec.model) for spec in STRATA_CATALOG}
    assert len(catalog_keys) == len(STRATA_CATALOG)
    assert expected_new_packs.issubset(catalog_keys)
    assert max(spec.download_gb for spec in STRATA_CATALOG) == 188.0

    for spec in STRATA_CATALOG:
        assert spec.family
        assert spec.model
        assert spec.download_gb > 0
        assert spec.experts_ram_gb > 0
        assert spec.min_vram_gb > 0
        assert spec.ssd_shard_gb >= 0
        _validate_strata_args(family=spec.family, model=spec.model)


def test_strata_fit_discrete_gpu_24g_62g_450g_and_edge_cases() -> None:
    by_key = {(spec.family, spec.model): spec for spec in STRATA_CATALOG}
    node = NodeStatus(
        node_id="gpu-node",
        display_name="Discrete GPU",
        role="gpu",
        agent_url="http://127.0.0.1:8090",
        engine_url="http://127.0.0.1:8081",
        online=True,
        telemetry=NodeTelemetry(
            node_id="gpu-node",
            role="gpu",
            platform="linux",
            timestamp=1.0,
            vram_total_mb=24 * 1024.0,
            ram_total_mb=62 * 1024.0,
            storage_free_gb=450.0,
        ),
    )

    assert assess_strata_fit(by_key[("gyro", "gyro-m")], node).tier == "optimal"
    assert assess_strata_fit(by_key[("unsloth", "ud-iq4_xs")], node).tier == "low_ram"
    assert assess_strata_fit(by_key[("unsloth", "ud-q5_k_xl")], node).tier == "tight"

    low_ssd_node = node.model_copy(
        update={
            "telemetry": node.telemetry.model_copy(update={"storage_free_gb": 126.0})  # type: ignore[union-attr]
        }
    )
    q8_fit = assess_strata_fit(by_key[("unsloth", "q8_0")], low_ssd_node)
    assert q8_fit.tier == "insufficient"
    assert "Needs 192 GB SSD free" in q8_fit.reason

    # Verify ssd_shard_gb is included in total_download SSD check (89.6 + 67.6 + 4 = 161.2)
    sq96_low_ssd = node.model_copy(
        update={
            "telemetry": node.telemetry.model_copy(update={"storage_free_gb": 150.0})  # type: ignore[union-attr]
        }
    )
    sq96_fit = assess_strata_fit(by_key[("hy4-flash", "sq-96")], sq96_low_ssd)
    assert sq96_fit.tier == "insufficient"
    assert "SSD free" in sq96_fit.reason

    # Verify min_vram_gb guard
    low_vram_node = node.model_copy(
        update={
            "telemetry": node.telemetry.model_copy(update={"vram_total_mb": 8 * 1024.0})  # type: ignore[union-attr]
        }
    )
    gyro_m_low_vram = assess_strata_fit(by_key[("gyro", "gyro-m")], low_vram_node)
    assert gyro_m_low_vram.tier == "insufficient"
    assert "VRAM" in gyro_m_low_vram.reason


def test_agent_strata_endpoints_and_proxy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_mod, "ROUTER_PUBLIC_KEY", None)
    monkeypatch.setattr(agent_mod, "STORAGE_PATH", str(tmp_path))

    (tmp_path / "bin").mkdir()
    fake_bin = tmp_path / "bin" / "strata"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("backend.agent.main.resolve_strata_bin", lambda: fake_bin)
    monkeypatch.setattr("backend.agent.main.list_installed_strata_models", lambda *_a: [])
    monkeypatch.setattr("backend.agent.main.list_strata_presets", lambda *_a: ["Office"])

    client = TestClient(agent_mod.app)
    st = client.get("/strata/status")
    assert st.status_code == 200
    assert st.json()["installed"] is True
    assert st.json()["presets"] == ["Office"]

    # Non-allowlisted proxy path is blocked
    bad_proxy = client.post("/strata/proxy/admin/shutdown", json={})
    assert bad_proxy.status_code == 403

    # Plan endpoint returns parsed JSON from strata --plan --output json
    class _FakeProc:
        returncode = 0
        stdout = json.dumps({"family": "qwen", "model": "iq2_xs", "download_gb": 39.2})
        stderr = ""

    monkeypatch.setattr("backend.agent.main.subprocess.run", lambda *_a, **_kw: _FakeProc())
    plan_res = client.post(
        "/strata/plan",
        json={"family": "qwen", "model": "iq2_xs", "context": "64k"},
    )
    assert plan_res.status_code == 200
    assert plan_res.json()["model"] == "iq2_xs"


def test_router_sidecar_policy_and_models_list() -> None:
    pc = NodeStatus(
        node_id="pc",
        display_name="PC",
        role="gpu",
        agent_url="http://10.0.0.2:8090",
        engine_url="http://10.0.0.2:8081",
        online=True,
        telemetry=NodeTelemetry(
            node_id="pc",
            role="gpu",
            platform="linux",
            timestamp=1.0,
            engine="llama-server",
            engine_healthy=True,
            models=[LoadedModel(id="dense-coder", status="loaded")],
            strata_installed=True,
            strata_healthy=True,
            strata_active_model="Qwen3.8-Flash-Next-IQ2_XS",
            strata_models=[LoadedModel(id="Qwen3.8-Flash-Next-IQ2_XS", status="loaded")],
        ),
    )

    dec_primary, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="dense-coder",
        body={},
        nodes={"pc": pc},
        aliases={},
        control=ControlState(),
        guards=GuardConfig(),
    )
    assert dec_primary is not None and dec_primary.use_strata_proxy is False

    dec_strata, _ = resolve_route(
        endpoint="/v1/chat/completions",
        requested_model="Qwen3.8-Flash-Next-IQ2_XS",
        body={},
        nodes={"pc": pc},
        aliases={},
        control=ControlState(),
        guards=GuardConfig(),
    )
    assert dec_strata is not None and dec_strata.use_strata_proxy is True

    old_nodes = dict(router_app_mod.cluster.nodes)
    try:
        router_app_mod.cluster.nodes = {"pc": pc}
        models_list = build_openai_models_list(router_app_mod.cluster)
        ids = {m["id"] for m in models_list["data"]}
        assert "pc/dense-coder" in ids
        assert "pc/Qwen3.8-Flash-Next-IQ2_XS" in ids
    finally:
        router_app_mod.cluster.nodes = old_nodes


def _make_node(
    node_id: str,
    *,
    role: str = "gpu",
    vram_gb: float = 24.0,
    ram_gb: float = 64.0,
    ssd_free_gb: float = 300.0,
    is_unified: bool = False,
    with_telemetry: bool = True,
) -> NodeStatus:
    telemetry = (
        NodeTelemetry(
            node_id=node_id,
            role=role,
            platform="darwin" if is_unified else "linux",
            timestamp=1.0,
            vram_total_mb=vram_gb * 1024.0,
            ram_total_mb=ram_gb * 1024.0,
            storage_free_gb=ssd_free_gb,
            gpus=[
                GpuDevice(
                    index=0,
                    vendor="apple" if is_unified else "nvidia",
                    name="Unified" if is_unified else "RTX",
                    vram_total_mb=vram_gb * 1024.0,
                    is_unified=is_unified,
                )
            ],
        )
        if with_telemetry
        else None
    )
    return NodeStatus(
        node_id=node_id,
        display_name=node_id,
        role=role,  # type: ignore[arg-type]
        agent_url="http://127.0.0.1:8090",
        engine_url="http://127.0.0.1:8081",
        online=True,
        telemetry=telemetry,
    )


def test_node_strata_budget_gb_all_tiers() -> None:
    from backend.router.models_api import _node_strata_budget_gb

    # Missing telemetry -> 0.0
    assert _node_strata_budget_gb(_make_node("none", with_telemetry=False)) == 0.0

    # Unified node: min(max(vram, ram) * 0.85, ssd_free - 4), or pool if ssd_free == 0
    assert _node_strata_budget_gb(
        _make_node("u1", role="unified", vram_gb=0.0, ram_gb=64.0, ssd_free_gb=500.0, is_unified=True)
    ) == 54.4
    assert _node_strata_budget_gb(
        _make_node("u2", role="unified", vram_gb=128.0, ram_gb=64.0, ssd_free_gb=50.0, is_unified=True)
    ) == 46.0
    assert _node_strata_budget_gb(
        _make_node("u3", role="unified", vram_gb=0.0, ram_gb=64.0, ssd_free_gb=0.0, is_unified=True)
    ) == 54.4
    assert _node_strata_budget_gb(
        _make_node("u4", role="unified", vram_gb=0.0, ram_gb=64.0, ssd_free_gb=2.0, is_unified=True)
    ) == 0.0

    # Discrete GPU with vram >= 16 and ram >= 28 and ssd_free > 0 -> ssd_free - 4.0
    assert _node_strata_budget_gb(
        _make_node("d1", vram_gb=16.0, ram_gb=28.0, ssd_free_gb=200.0)
    ) == 196.0
    assert _node_strata_budget_gb(
        _make_node("d2", vram_gb=24.0, ram_gb=64.0, ssd_free_gb=3.0)
    ) == 0.0

    # Smaller discrete GPU or ssd_free == 0 -> min(vram + ram, ssd_free - 4)
    assert _node_strata_budget_gb(
        _make_node("s1", vram_gb=12.0, ram_gb=32.0, ssd_free_gb=500.0)
    ) == 44.0
    assert _node_strata_budget_gb(
        _make_node("s2", vram_gb=12.0, ram_gb=32.0, ssd_free_gb=30.0)
    ) == 26.0
    assert _node_strata_budget_gb(
        _make_node("s3", vram_gb=24.0, ram_gb=16.0, ssd_free_gb=500.0)
    ) == 40.0
    assert _node_strata_budget_gb(
        _make_node("s4", vram_gb=24.0, ram_gb=64.0, ssd_free_gb=0.0)
    ) == 88.0


def test_router_models_search_strata_and_gguf(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.router import models_api as models_api_mod

    monkeypatch.setattr(router_app_mod, "API_KEY", "")
    monkeypatch.setattr(router_app_mod, "API_KEY_HASH", "")

    captured_requests: list[httpx.Request] = []

    def _hf_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.url.path == "/api/models":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "strata-org/Hy4-Flash-100B",
                        "downloads": 5000,
                        "likes": 120,
                        "siblings": [
                            {"rfilename": "Hy4-Flash-SQ64.strata"},
                            {"rfilename": "README.md"},
                        ],
                    }
                ],
            )
        return httpx.Response(404)

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = httpx.MockTransport(_hf_handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(models_api_mod.httpx, "AsyncClient", _MockAsyncClient)

    old_nodes = dict(router_app_mod.cluster.nodes)
    try:
        router_app_mod.cluster.nodes = {
            "pc": _make_node("pc", vram_gb=24.0, ram_gb=64.0, ssd_free_gb=300.0),
        }
        tc = TestClient(router_app_mod.app)

        # format=gguf with len(q) < 2 returns [] without hitting HF
        r_empty_gguf = tc.get("/api/models/search?format=gguf&q=")
        assert r_empty_gguf.status_code == 200
        assert r_empty_gguf.json() == []
        r_short_gguf = tc.get("/api/models/search?format=gguf&q=a")
        assert r_short_gguf.status_code == 200
        assert r_short_gguf.json() == []
        assert captured_requests == []

        # format=strata with empty q="" passes filter=strata without search param
        r_empty_strata = tc.get("/api/models/search?format=strata&q=")
        assert r_empty_strata.status_code == 200
        assert len(captured_requests) == 1
        params_empty = captured_requests[0].url.params
        assert params_empty.get("filter") == "strata"
        assert "search" not in params_empty
        body_empty = r_empty_strata.json()
        assert len(body_empty) == 1
        assert body_empty[0]["gguf_count"] == 1
        assert "pc" in body_empty[0]["fits_nodes"]

        # format=strata with non-empty q="qwen" passes both search=qwen and filter=strata
        r_q_strata = tc.get("/api/models/search?format=strata&q=qwen")
        assert r_q_strata.status_code == 200
        assert len(captured_requests) == 2
        params_q = captured_requests[1].url.params
        assert params_q.get("filter") == "strata"
        assert params_q.get("search") == "qwen"

        # Same 100B repo under format=gguf (est_min_gb=58) does NOT fit 24GB VRAM node
        r_q_gguf = tc.get("/api/models/search?format=gguf&q=qwen")
        assert r_q_gguf.status_code == 200
        assert r_q_gguf.json()[0]["fits_nodes"] == []
    finally:
        router_app_mod.cluster.nodes = old_nodes


def test_extract_repo_details_and_files_strata(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.router import models_api as models_api_mod
    from backend.router.models_api import _build_gguf_file_list, _extract_repo_details
    from backend.router.telemetry import ClusterState

    monkeypatch.setattr(router_app_mod, "API_KEY", "")
    monkeypatch.setattr(router_app_mod, "API_KEY_HASH", "")

    details = _extract_repo_details(
        {
            "id": "strata-org/Demo-30B",
            "siblings": [
                {"rfilename": "demo-sq64.strata"},
                {"rfilename": "demo-sq96.STRATA"},
                {"rfilename": "demo-Q4_K_M.gguf"},
                {"rfilename": "config.json"},
                {"rfilename": "README.md"},
            ],
        }
    )
    assert details.gguf_count == 3

    gib = 1024**3
    tree_entries = [
        {"type": "file", "path": "Hy4-Flash-SQ64.strata", "size": 100 * gib},
        {"type": "file", "path": "Hy4-Flash-Q4_K_M.gguf", "size": 10 * gib},
        {"type": "file", "path": "model.safetensors", "size": 50 * gib},
        {"type": "file", "path": "config.json", "size": 1024},
    ]

    cs = ClusterState(router_app_mod.cluster.config)
    cs.nodes = {"pc": _make_node("pc", vram_gb=24.0, ram_gb=64.0, ssd_free_gb=300.0)}

    strata_files = _build_gguf_file_list(tree_entries, cs, format="strata")
    assert [f.filename for f in strata_files] == [
        "Hy4-Flash-Q4_K_M.gguf",
        "Hy4-Flash-SQ64.strata",
    ]
    by_name_strata = {f.filename: f for f in strata_files}
    assert by_name_strata["Hy4-Flash-SQ64.strata"].fits_nodes == ["pc"]

    gguf_files = _build_gguf_file_list(tree_entries, cs, format="gguf")
    by_name_gguf = {f.filename: f for f in gguf_files}
    assert by_name_gguf["Hy4-Flash-SQ64.strata"].fits_nodes == []
    assert by_name_gguf["Hy4-Flash-Q4_K_M.gguf"].fits_nodes == ["pc"]

    # Endpoint test: GET /api/models/files?repo_id=...&format=strata
    def _tree_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/models/strata-org/Hy4-Flash/tree/main":
            return httpx.Response(200, json=tree_entries)
        return httpx.Response(404)

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = httpx.MockTransport(_tree_handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(models_api_mod.httpx, "AsyncClient", _MockAsyncClient)
    old_nodes = dict(router_app_mod.cluster.nodes)
    try:
        router_app_mod.cluster.nodes = cs.nodes
        tc = TestClient(router_app_mod.app)
        resp = tc.get("/api/models/files?repo_id=strata-org/Hy4-Flash&format=strata")
        assert resp.status_code == 200
        items = resp.json()
        assert [i["filename"] for i in items] == [
            "Hy4-Flash-Q4_K_M.gguf",
            "Hy4-Flash-SQ64.strata",
        ]
        assert items[1]["fits_nodes"] == ["pc"]
    finally:
        router_app_mod.cluster.nodes = old_nodes


def test_agent_models_pull_strata_accept_and_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_mod, "ROUTER_PUBLIC_KEY", None)
    monkeypatch.setattr(agent_mod, "STORAGE_PATH", str(tmp_path))

    def _dl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"strata-payload", headers={"content-length": "14"})

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            kwargs["transport"] = httpx.MockTransport(_dl_handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(agent_mod.httpx, "AsyncClient", _MockAsyncClient)
    tc = TestClient(agent_mod.app)

    ok_resp = tc.post(
        "/files/models/pull",
        json={"repo_id": "strata-org/Hy4-Flash", "filename": "Hy4-Flash-SQ64.strata"},
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["filename"] == "Hy4-Flash-SQ64.strata"
    assert ok_resp.json()["status"] == "downloading"

    ok_upper = tc.post(
        "/files/models/pull",
        json={"repo_id": "strata-org/Hy4-Flash", "filename": "Hy4-Flash-SQ64.STRATA"},
    )
    assert ok_upper.status_code == 200

    bad_ext = tc.post(
        "/files/models/pull",
        json={"repo_id": "strata-org/Hy4-Flash", "filename": "weights.bin"},
    )
    assert bad_ext.status_code == 400

    bad_traversal = tc.post(
        "/files/models/pull",
        json={"repo_id": "strata-org/Hy4-Flash", "filename": "../Hy4-Flash-SQ64.strata"},
    )
    assert bad_traversal.status_code == 400

    bad_mid_traversal = tc.post(
        "/files/models/pull",
        json={"repo_id": "strata-org/Hy4-Flash", "filename": "sub/../Hy4-Flash-SQ64.strata"},
    )
    assert bad_mid_traversal.status_code == 400
