"""Unit tests for Models/Aliases, dynamic VRAM fit, Skills sync, and MCP JSON-RPC probing/export."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent.main import _sync_skills_to_dir
from backend.router.config import SplitchConfig
from backend.router.mcp_api import (
    build_mcp_router,
    classify_mcp_egress,
    generate_client_config,
    parse_mcp_json_snippet,
    parse_npm_mcp_item,
    parse_official_registry_item,
    probe_mcp_server,
)
from backend.router.models_api import (
    _extract_quant,
    _extract_repo_details,
    _node_budget_gb,
    build_models_router,
    extract_hf_readme_description,
)
from backend.router.skills_api import (
    _pick_best_skill_path,
    build_skills_router,
    parse_skill_frontmatter,
)
from backend.router.telemetry import ClusterState
from backend.shared.models import (
    AliasConfig,
    McpServerConfig,
    NodeRegisterRequest,
    NodeStatus,
    NodeTelemetry,
)


def test_quant_extraction_and_frontmatter() -> None:
    assert _extract_quant("Qwen3-Coder-30B-A3B-Q4_K_M.gguf") == "Q4_K_M"
    assert _extract_quant("Qwen3.5-9B-UD-Q4_K_XL.gguf") == "UD-Q4_K_XL"
    assert _extract_quant("bge-small-f16.gguf") == "F16"

    name, desc = parse_skill_frontmatter(
        "---\nname: Rust Audit\ndescription: Checks unsafe blocks\n---\n# Body",
        "fallback",
    )
    assert name == "Rust Audit"
    assert desc == "Checks unsafe blocks"


def test_dynamic_node_budget_and_registration(tmp_path: Path) -> None:
    gpu_node = NodeStatus(
        node_id="gpu-rig",
        display_name="GPU Rig",
        role="gpu",
        agent_url="http://127.0.0.1:8090",
        engine_url="http://127.0.0.1:8081",
        telemetry=NodeTelemetry(
            node_id="gpu-rig", role="gpu", platform="linux", timestamp=1.0, vram_total_mb=24576.0
        ),
    )
    assert 20.0 <= _node_budget_gb(gpu_node) <= 22.0

    cpu_node = NodeStatus(
        node_id="cpu-box",
        display_name="CPU Box",
        role="cpu",
        agent_url="http://127.0.0.1:8091",
        engine_url="http://127.0.0.1:8082",
        telemetry=NodeTelemetry(
            node_id="cpu-box", role="cpu", platform="linux", timestamp=1.0, ram_total_mb=32768.0
        ),
    )
    assert 18.0 <= _node_budget_gb(cpu_node) <= 20.0


@pytest.mark.asyncio
async def test_dynamic_node_persistence(tmp_path: Path) -> None:
    cluster = ClusterState(SplitchConfig(), data_dir=tmp_path)
    await cluster.register_node(
        NodeRegisterRequest(
            id="laptop",
            display_name="Dev Laptop",
            role="unified",
            agent_url="http://192.168.1.99:8090",
            engine_url="http://192.168.1.99:11434",
        )
    )
    assert "laptop" in cluster.nodes

    cluster2 = ClusterState(SplitchConfig(), data_dir=tmp_path)
    assert "laptop" in cluster2.nodes
    assert await cluster2.remove_node("laptop") is True
    assert "laptop" not in cluster2.nodes


def test_skills_crud_and_manifest_sync(tmp_path: Path) -> None:
    cluster = ClusterState(SplitchConfig())
    app = FastAPI()
    app.include_router(build_skills_router(cluster, tmp_path / "skills"))
    client = TestClient(app)

    r = client.post(
        "/api/skills",
        json={
            "id": "strict-review",
            "name": "Strict Review",
            "description": "Audit diffs thoroughly",
            "content": "# Review Steps\n1. Check types",
            "enabled": True,
        },
    )
    assert r.status_code == 200
    assert "name: Strict Review" in r.json()["content"]

    items = client.get("/api/skills").json()
    assert len(items) == 1 and items[0]["id"] == "strict-review"

    target = tmp_path / "opencode_skills"
    (target / "hand-made").mkdir(parents=True)
    (target / "hand-made" / "SKILL.md").write_text("manual", encoding="utf-8")

    count = _sync_skills_to_dir(
        target, [{"id": "strict-review", "content": items[0]["content"]}]
    )
    assert count == 1
    assert (target / "strict-review" / "SKILL.md").exists()
    assert (target / "hand-made" / "SKILL.md").exists()

    count2 = _sync_skills_to_dir(target, [])
    assert count2 == 0
    assert not (target / "strict-review").exists()
    assert (target / "hand-made" / "SKILL.md").exists()

    assert client.delete("/api/skills/strict-review").status_code == 200
    assert client.get("/api/skills").json() == []


def test_aliases_crud_persists(tmp_path: Path) -> None:
    cluster = ClusterState(SplitchConfig())
    app = FastAPI()
    app.include_router(build_models_router(cluster, tmp_path))
    client = TestClient(app)

    payload = AliasConfig(
        alias="reasoning",
        task_kind="code",
        primary_node="gpu-rig",
        primary_model="coder-heavy",
        fallback_node="always-on",
        fallback_model="fast-small",
    ).model_dump()
    assert client.put("/api/aliases/reasoning", json=payload).status_code == 200
    assert any(a["alias"] == "reasoning" for a in client.get("/api/aliases").json())

    cluster2 = ClusterState(SplitchConfig())
    build_models_router(cluster2, tmp_path)
    assert "reasoning" in cluster2.aliases


@pytest.mark.asyncio
async def test_mcp_real_stdio_jsonrpc_probe_and_export(tmp_path: Path) -> None:
    srv_script = tmp_path / "echo_mcp.py"
    srv_script.write_text(
        """import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"protocolVersion":"2024-11-05","serverInfo":{"name":"test-mcp","version":"1.0"},"capabilities":{"tools":{}}}}), flush=True)
    elif msg.get("method") == "tools/list":
        print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"tools":[{"name":"ping_tool"},{"name":"echo_tool"}]}}), flush=True)
""",
        encoding="utf-8",
    )

    cfg = McpServerConfig(
        id="echo-mcp",
        name="Echo MCP",
        transport="stdio",
        scope="shared",
        command=sys.executable,
        args=[str(srv_script)],
    )
    res = await probe_mcp_server(cfg)
    assert res.ok is True
    assert res.server_name == "test-mcp"
    assert res.tools == ["ping_tool", "echo_tool"]

    cluster = ClusterState(SplitchConfig())
    cluster.aliases["heavy"] = AliasConfig(
        alias="heavy", primary_node="gpu-rig", primary_model="coder-heavy"
    )
    oc = generate_client_config("opencode", "http://localhost:8080", cluster, [cfg])
    assert "echo-mcp" in oc["mcp"]
    assert oc["provider"]["splitch"]["options"]["baseURL"] == "http://localhost:8080/v1"

    zed = generate_client_config("zed", "http://localhost:8080", cluster, [cfg])
    assert "echo-mcp" in zed["context_servers"]

    app = FastAPI()
    app.include_router(build_mcp_router(cluster, tmp_path))
    tc = TestClient(app)
    assert tc.post("/api/mcp", json=cfg.model_dump()).status_code == 200
    listed = tc.get("/api/mcp").json()
    assert len(listed) == 1 and listed[0]["egress"] == "local"
    assert tc.post("/api/mcp/echo-mcp/toggle?enabled=false").json()["enabled"] is False
    assert tc.delete("/api/mcp/echo-mcp").status_code == 200

    # Verify external data-egress classification (local stdio vs external API vs remote HTTP)
    ext_api_srv = McpServerConfig(
        id="github",
        name="GitHub",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_test"},
    )
    assert classify_mcp_egress(ext_api_srv)[0] == "external_api"

    rem_srv = McpServerConfig(
        id="cloud-mcp",
        name="Cloud MCP",
        transport="http",
        url="https://mcp.example.com/v1",
    )
    assert classify_mcp_egress(rem_srv) == ("remote_mcp", "mcp.example.com")

    lan_srv = McpServerConfig(
        id="lan-mcp",
        name="LAN MCP",
        transport="http",
        url="http://192.168.1.50:8900/mcp",
    )
    assert classify_mcp_egress(lan_srv)[0] == "local"


def test_hf_repo_details_extraction() -> None:
    sample = {
        "id": "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF",
        "downloads": 42000,
        "likes": 512,
        "trendingScore": 88.4,
        "lastModified": "2026-11-10T12:00:00.000Z",
        "createdAt": "2026-10-01T00:00:00.000Z",
        "pipeline_tag": "text-generation",
        "gated": False,
        "tags": ["gguf", "unsloth", "coder", "imatrix", "license:apache-2.0"],
        "gguf": {
            "total": 30_532_122_624,
            "architecture": "qwen3moe",
            "context_length": 262144,
            "totalFileSize": 17_310_784_672,
        },
        "siblings": [
            {"rfilename": "README.md"},
            {"rfilename": "Qwen3-Coder-30B-A3B-Q4_K_M.gguf"},
            {"rfilename": "Qwen3-Coder-30B-A3B-Q8_0.gguf"},
        ],
    }
    res = _extract_repo_details(sample, node_budgets={"pc": 21.1, "mac": 10.6})
    assert res.param_size == "30B-A3B"
    assert res.parameters_b == 30.53
    assert res.architecture == "qwen3moe"
    assert res.context_length == 262144
    assert res.est_q4_gb is not None and 15.5 <= res.est_q4_gb <= 17.0
    assert res.est_min_gb is not None and res.est_min_gb <= 17.0
    assert res.fits_nodes == ["pc"]
    assert res.gguf_count == 2
    assert res.license == "apache-2.0"
    assert res.trending_score == 88.4
    assert "coder" in res.capabilities and "imatrix" in res.capabilities


def test_hf_readme_description_cleaner_and_details_validation(tmp_path: Path) -> None:
    raw = """---
license: apache-2.0
base_model:
  - Qwen/Qwen3-Coder-30B-A3B-Instruct
tags:
  - gguf
---
<div align="center">
<a href="https://example.com"><img src="https://img.shields.io/badge/test.svg" /></a>
</div>
[![Banner](https://example.com/banner.png)](https://example.com)

# Qwen3 Coder 30B GGUF

High-throughput coding model quantized with **Unsloth Dynamic 2.0**.

```python
print("<keep-html-inside-code>")
```
"""
    desc, truncated, base_model = extract_hf_readme_description(raw, max_chars=2400)
    assert truncated is False
    assert base_model == "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    assert "license: apache-2.0" not in desc
    assert "<div" not in desc and "shields.io" not in desc
    assert "Unsloth Dynamic 2.0" in desc
    assert "<keep-html-inside-code>" in desc

    long_desc, was_trunc, _ = extract_hf_readme_description("Paragraph A.\n\n" * 400, max_chars=200)
    assert was_trunc is True and len(long_desc) <= 200

    cluster = ClusterState(SplitchConfig())
    app = FastAPI()
    app.include_router(build_models_router(cluster, tmp_path))
    tc = TestClient(app)
    assert tc.get("/api/models/details?repo_id=bad/repo/id").status_code == 400


@pytest.mark.asyncio
async def test_mcp_json_import_and_custom_builder_probe(tmp_path: Path) -> None:
    claude_json = """{
      "mcpServers": {
        "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] },
        "remote-docs": { "url": "http://localhost:8900/mcp" }
      }
    }"""
    imported = parse_mcp_json_snippet(claude_json, "shared")
    assert len(imported) == 2
    assert imported[0].id == "fetch" and imported[0].command == "uvx"
    assert imported[1].id == "remote-docs" and imported[1].transport == "http"

    cluster = ClusterState(SplitchConfig())
    app = FastAPI()
    app.include_router(build_mcp_router(cluster, tmp_path / "data", tmp_path / "mcp_servers"))
    tc = TestClient(app)

    r = tc.post(
        "/api/mcp/build",
        json={
            "id": "lan-ping",
            "name": "LAN Ping",
            "description": "Custom built ping tool",
            "scope": "shared",
            "tools": [
                {
                    "name": "ping_host",
                    "description": "Ping a LAN host",
                    "parameters": [
                        {"name": "host", "type": "string", "description": "IP", "required": True}
                    ],
                }
            ],
        },
    )
    assert r.status_code == 200
    built = McpServerConfig.model_validate(r.json())
    assert built.command == "python3"
    assert Path(built.args[0]).exists()

    built.command = sys.executable
    probe = await probe_mcp_server(built)
    assert probe.ok is True
    assert probe.server_name == "LAN Ping"
    assert probe.tools == ["ping_host"]


def test_mcp_registry_and_skills_path_parsers() -> None:
    official_pkg = {
        "server": {
            "name": "com.example/cool-db",
            "description": "DB MCP",
            "version": "1.2.0",
            "packages": [
                {
                    "registryType": "npm",
                    "identifier": "@example/cool-db-mcp",
                    "runtimeHint": "npx",
                    "environmentVariables": [
                        {"name": "DB_URL", "isRequired": True, "isSecret": True}
                    ],
                }
            ],
        },
        "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}},
    }
    res = parse_official_registry_item(official_pkg)
    assert res is not None
    assert res.id == "cool-db"
    assert res.command == "npx"

    from backend.router.mcp_api import _official_registry_query_variants

    assert _official_registry_query_variants("rea mcp") == ["rea", "rea-mcp"]
    assert "reverse-engineer-anything" in _official_registry_query_variants(
        "reverse engineer anything"
    )
    assert "reverse-engineer-anything" in _official_registry_query_variants(
        "reverse engineer anything mcp"
    )
    assert res.args == ["-y", "@example/cool-db-mcp"]
    assert res.env_vars[0].name == "DB_URL" and res.env_vars[0].is_required is True

    npm_res = parse_npm_mcp_item(
        {
            "downloads": {"weekly": 240},
            "package": {"name": "@demo/mcp-weather", "version": "0.5.0", "description": "Weather"},
        }
    )
    assert npm_res is not None and npm_res.command == "npx" and npm_res.downloads_weekly == 240

    best = _pick_best_skill_path(
        ["skills/react-best-practices/SKILL.md", "skills/deploy/SKILL.md"],
        "vercel-react-best-practices",
    )
    assert best == "skills/react-best-practices/SKILL.md"
