"""Real headless-Chromium UI end-to-end tests for ConfirmDialog, Trash, Bulk Delete & Pinning."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.router.chat_api import build_chat_router
from backend.router.config import SplitchConfig
from backend.router.mcp_api import build_mcp_router
from backend.router.models_api import build_models_router
from backend.router.provision_api import JoinTokenManager, build_provision_router
from backend.router.settings_api import build_settings_router
from backend.router.skills_api import build_skills_router
from backend.router.telemetry import ClusterState
from backend.shared.models import GpuDevice, NodeStatus, NodeTelemetry

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = REPO_ROOT / "dashboard" / "dist"
CHROMIUM_BIN = (
    shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _build_test_spa_app(tmp_path: Path) -> FastAPI:
    data_dir = tmp_path / "data"
    skills_dir = tmp_path / "skills"
    mcp_dir = tmp_path / "mcp"
    data_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)
    mcp_dir.mkdir(parents=True, exist_ok=True)

    cluster = ClusterState(SplitchConfig(), data_dir=data_dir)
    cluster.nodes["dual-rig"] = NodeStatus(
        node_id="dual-rig",
        display_name="Dual GPU Rig",
        role="gpu",
        yield_on_gaming=True,
        yield_gpu_indices=[1],
        agent_url="http://127.0.0.1:8090",
        engine_url="http://127.0.0.1:8081",
        online=True,
        telemetry=NodeTelemetry(
            node_id="dual-rig",
            role="gpu",
            platform="linux",
            timestamp=time.time(),
            gpu_name="RTX 4090 + RX 7900 XTX",
            vram_total_mb=49140.0,
            vram_used_mb=18000.0,
            vram_free_mb=31140.0,
            gpus=[
                GpuDevice(
                    index=0,
                    vendor="nvidia",
                    name="NVIDIA GeForce RTX 4090",
                    vram_total_mb=24564.0,
                    vram_used_mb=8000.0,
                    vram_free_mb=16564.0,
                    util_percent=22.0,
                    temp_c=48.0,
                ),
                GpuDevice(
                    index=1,
                    vendor="amd",
                    name="AMD Radeon RX 7900 XTX",
                    vram_total_mb=24576.0,
                    vram_used_mb=10000.0,
                    vram_free_mb=14576.0,
                    util_percent=12.0,
                    temp_c=44.0,
                ),
            ],
            gpu_count=2,
            gpu_vendors=["amd", "nvidia"],
            recommended_backend="vulkan",
            engine="llama-server",
            engine_healthy=True,
        ),
    )
    app = FastAPI()
    jtm = JoinTokenManager()
    app.include_router(build_chat_router(cluster, data_dir, skills_dir))
    app.include_router(build_models_router(cluster, data_dir))
    app.include_router(build_skills_router(cluster, skills_dir))
    app.include_router(build_mcp_router(cluster, data_dir, mcp_dir))
    app.include_router(build_settings_router(cluster, data_dir, api_key_getter=lambda: ""))
    app.include_router(build_provision_router(cluster, jtm, api_key_getter=lambda: ""))

    @app.get("/api/auth/status")
    async def auth_status():
        return {"auth_required": False, "authenticated": True}

    @app.get("/v1/models")
    async def list_v1_models():
        return {"object": "list", "data": [{"id": "auto", "object": "model"}]}

    @app.get("/api/status")
    async def status():
        return cluster.build_snapshot()

    assets = DIST_DIR / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(str(DIST_DIR / "index.html"))

    return app


@pytest.mark.skipif(
    not CHROMIUM_BIN or not (DIST_DIR / "index.html").exists(),
    reason="Headless Chromium or built dashboard/dist not present",
)
async def test_ui_confirm_delete_trash_bulk_and_pin_e2e(tmp_path: Path) -> None:
    port = _free_port()
    cdp_port = _free_port()
    app = _build_test_spa_app(tmp_path)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    srv_task = asyncio.create_task(server.serve())

    async with httpx.AsyncClient(timeout=3.0) as client:
        for _ in range(40):
            try:
                r = await client.get(f"http://127.0.0.1:{port}/api/status")
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                await asyncio.sleep(0.1)

        # Seed 3 conversations via real REST API
        for title in ("Alpha Chat", "Beta Chat", "Gamma Chat"):
            res = await client.post(
                f"http://127.0.0.1:{port}/api/chat/conversations",
                json={"title": title},
            )
            res.raise_for_status()
            await asyncio.sleep(0.02)

        chrome_profile = tmp_path / "chrome"
        chrome = await asyncio.create_subprocess_exec(
            str(CHROMIUM_BIN),
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={chrome_profile}",
            "--window-size=1280,900",
            "about:blank",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            ws_url = ""
            for _ in range(50):
                try:
                    r = await client.get(f"http://127.0.0.1:{cdp_port}/json")
                    targets = r.json()
                    page = next((t for t in targets if t.get("type") == "page"), None)
                    if page and page.get("webSocketDebuggerUrl"):
                        ws_url = str(page["webSocketDebuggerUrl"])
                        break
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(0.15)
            assert ws_url, "Chromium CDP failed to start"

            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                msg_id = 0

                async def cdp(method: str, params: dict | None = None) -> dict:
                    nonlocal msg_id
                    msg_id += 1
                    cid = msg_id
                    await ws.send(json.dumps({"id": cid, "method": method, "params": params or {}}))
                    while True:
                        data = json.loads(await ws.recv())
                        if data.get("id") == cid:
                            if "error" in data:
                                raise RuntimeError(f"CDP {method}: {data['error']}")
                            return data.get("result", {})

                async def eval_js(expr: str):
                    res = await cdp(
                        "Runtime.evaluate",
                        {"expression": expr, "returnByValue": True, "awaitPromise": True},
                    )
                    return res.get("result", {}).get("value")

                async def wait_until(expr: str, timeout_s: float = 5.0):
                    deadline = time.monotonic() + timeout_s
                    while time.monotonic() < deadline:
                        val = await eval_js(expr)
                        if val:
                            return val
                        await asyncio.sleep(0.1)
                    raise AssertionError(f"Timed out waiting for: {expr}")

                await cdp("Page.enable")
                await cdp("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
                await wait_until("document.querySelectorAll('.chat-conv-item').length === 3")

                # 0. Clicking Cancel in ConfirmDialog must NOT delete
                await eval_js(
                    "document.querySelector('button[aria-label=\"Move Gamma Chat to trash\"]').click()"
                )
                await wait_until("Boolean(document.querySelector('dialog.confirm-dialog[open]'))")
                await eval_js(
                    "document.querySelector('dialog.confirm-dialog button[value=\"cancel\"]').click()"
                )
                await wait_until("!document.querySelector('dialog.confirm-dialog[open]')")
                await asyncio.sleep(0.15)
                assert await eval_js("document.querySelectorAll('.chat-conv-item').length") == 3

                # 1. Single delete via ConfirmDialog -> moves Gamma Chat to Trash (1)
                await eval_js(
                    "document.querySelector('button[aria-label=\"Move Gamma Chat to trash\"]').click()"
                )
                await wait_until("Boolean(document.querySelector('dialog.confirm-dialog[open]'))")
                await eval_js(
                    "document.querySelector('dialog.confirm-dialog button[value=\"confirm\"]').click()"
                )
                await wait_until(
                    "document.querySelectorAll('.chat-conv-item').length === 2 && "
                    "Array.from(document.querySelectorAll('.chat-sidebar__tabs button'))"
                    ".some(b => b.textContent.includes('Trash (1)'))"
                )

                # 2. Switch to Trash tab and Restore Gamma Chat -> Chats (3), Trash (0)
                await eval_js(
                    "Array.from(document.querySelectorAll('.chat-sidebar__tabs button'))"
                    ".find(b => b.textContent.includes('Trash')).click()"
                )
                await wait_until(
                    "Boolean(document.querySelector('button[aria-label=\"Restore Gamma Chat\"]'))"
                )
                await eval_js(
                    "document.querySelector('button[aria-label=\"Restore Gamma Chat\"]').click()"
                )
                await wait_until(
                    "document.querySelectorAll('.chat-conv-item').length === 3 && "
                    "Array.from(document.querySelectorAll('.chat-sidebar__tabs button'))"
                    ".some(b => b.textContent.includes('Trash (0)'))"
                )

                # 3. Multi-select Bulk Delete 2 chats via ConfirmDialog -> Chats (1), Trash (2)
                await eval_js(
                    "Array.from(document.querySelectorAll('.chat-sidebar__head button'))"
                    ".find(b => b.textContent.includes('Select')).click()"
                )
                await wait_until(
                    "document.querySelectorAll('.chat-conv-fieldset input[type=\"checkbox\"]').length === 3"
                )
                await eval_js(
                    "const boxes = document.querySelectorAll('.chat-conv-fieldset input[type=\"checkbox\"]');"
                    "boxes[0].click(); boxes[1].click();"
                )
                await wait_until(
                    "Array.from(document.querySelectorAll('.chat-bulk-bar button'))"
                    ".some(b => b.textContent.includes('Delete (2)'))"
                )
                await eval_js(
                    "Array.from(document.querySelectorAll('.chat-bulk-bar button'))"
                    ".find(b => b.textContent.includes('Delete (2)')).click()"
                )
                await wait_until("Boolean(document.querySelector('dialog.confirm-dialog[open]'))")
                await eval_js(
                    "document.querySelector('dialog.confirm-dialog button[value=\"confirm\"]').click()"
                )
                await wait_until(
                    "document.querySelectorAll('.chat-conv-item').length === 1 && "
                    "Array.from(document.querySelectorAll('.chat-sidebar__tabs button'))"
                    ".some(b => b.textContent.includes('Trash (2)'))"
                )

                # 4. Switch to Trash -> Empty Trash via ConfirmDialog -> Trash (0)
                await eval_js(
                    "Array.from(document.querySelectorAll('.chat-sidebar__tabs button'))"
                    ".find(b => b.textContent.includes('Trash')).click()"
                )
                await wait_until(
                    "Array.from(document.querySelectorAll('.chat-sidebar button'))"
                    ".some(b => b.textContent.includes('Empty Trash (2)'))"
                )
                await eval_js(
                    "Array.from(document.querySelectorAll('.chat-sidebar button'))"
                    ".find(b => b.textContent.includes('Empty Trash (2)')).click()"
                )
                await wait_until("Boolean(document.querySelector('dialog.confirm-dialog[open]'))")
                await eval_js(
                    "document.querySelector('dialog.confirm-dialog button[value=\"confirm\"]').click()"
                )
                await wait_until(
                    "Array.from(document.querySelectorAll('.chat-sidebar__tabs button'))"
                    ".some(b => b.textContent.includes('Trash (0)'))"
                )

                # 5. Return to Chats & verify Pin button toggles aria-pressed="true"
                await eval_js(
                    "Array.from(document.querySelectorAll('.chat-sidebar__tabs button'))"
                    ".find(b => b.textContent.includes('Chats')).click()"
                )
                await wait_until("Boolean(document.querySelector('.chat-pin-btn'))")
                await eval_js("document.querySelector('.chat-pin-btn').click()")
                await wait_until(
                    "document.querySelector('.chat-pin-btn').getAttribute('aria-pressed') === 'true'"
                )

                # 5b. Verify unified single topbar (< 12% viewport height) & collapsible sidebar
                topbar_ok = await eval_js(
                    "(() => {"
                    "  const tb = document.querySelector('.topbar');"
                    "  const tl = document.querySelector('.topbar [role=\"tablist\"]');"
                    "  return Boolean(tb && tl && tb.contains(tl) && tb.getBoundingClientRect().height <= 56);"
                    "})()"
                )
                assert topbar_ok is True
                await eval_js("document.querySelector('.chat-sidebar-toggle').click()")
                await wait_until(
                    "Math.round(document.querySelector('.chat-sidebar').getBoundingClientRect().width) === 0"
                )
                await eval_js("document.querySelector('.chat-sidebar-toggle').click()")
                await wait_until(
                    "Math.round(document.querySelector('.chat-sidebar').getBoundingClientRect().width) > 200"
                )

                # 6. Overview tab -> verify Multi-GPU subgrid renders both NVIDIA + AMD devices
                await eval_js(
                    "Array.from(document.querySelectorAll('[role=\"tab\"]'))"
                    ".find(t => t.textContent.includes('Overview')).click()"
                )
                await wait_until("document.querySelectorAll('.gpu-subrow').length === 2")
                gpu_text = await eval_js("document.querySelector('.gpu-sublist').textContent")
                assert "NVIDIA" in gpu_text and "AMD" in gpu_text

                # 7. Clickable topbar endpoint pill opens Settings Modal on Connect Clients
                await eval_js("document.querySelector('.endpoint-pill--btn').click()")
                await wait_until("Boolean(document.querySelector('dialog.settings-dialog[open]'))")

                # Run live /v1/models protocol probe inside Settings -> Connect Clients
                await eval_js(
                    "Array.from(document.querySelectorAll('.settings-pane button'))"
                    ".find(b => b.textContent.includes('/v1/models')).click()"
                )
                await wait_until(
                    "(document.querySelector('.probe-list')?.textContent || '').includes('HTTP 200')"
                )

                # Switch to Install Nodes -> Join Token One-Liner -> mint token
                await eval_js(
                    "Array.from(document.querySelectorAll('.settings-nav__item'))"
                    ".find(b => b.textContent.includes('Install Nodes')).click()"
                )
                await wait_until(
                    "Array.from(document.querySelectorAll('.settings-pane button'))"
                    ".some(b => b.textContent.includes('Join Token'))"
                )
                await eval_js(
                    "Array.from(document.querySelectorAll('.settings-pane button'))"
                    ".find(b => b.textContent.includes('Join Token')).click()"
                )
                await eval_js(
                    "Array.from(document.querySelectorAll('.settings-pane button'))"
                    ".find(b => b.textContent.includes('Generate Join Command')).click()"
                )
                await wait_until(
                    "(document.querySelector('.settings-pane')?.textContent || '').includes('--join-token spj_')"
                )

                # 8. Switch to Benchmarks tab in Settings and verify interactive harness renders
                await eval_js(
                    "Array.from(document.querySelectorAll('.settings-nav__item'))"
                    ".find(b => b.textContent.includes('Benchmarks')).click()"
                )
                await wait_until(
                    "(document.querySelector('.settings-pane')?.textContent || '').includes('Cluster & Model Benchmark Harness')"
                )

                # 9. Switch to Appearance tab -> select Amber (#f59e0b) -> verify <link rel="icon"> updates dynamically
                await eval_js(
                    "Array.from(document.querySelectorAll('.settings-nav__item'))"
                    ".find(b => b.textContent.includes('Appearance')).click()"
                )
                await wait_until("document.querySelectorAll('.theme-swatch').length >= 6")
                await eval_js(
                    "Array.from(document.querySelectorAll('.theme-swatch'))"
                    ".find(b => b.textContent.includes('Amber')).click()"
                )
                await wait_until(
                    "(document.querySelector('link[rel=\"icon\"]')?.getAttribute('href') || '').toLowerCase().includes('%23f59e0b')"
                )
        finally:
            chrome.terminate()
            try:
                await asyncio.wait_for(chrome.wait(), timeout=3.0)
            except TimeoutError:
                chrome.kill()
            server.should_exit = True
            await asyncio.gather(srv_task, return_exceptions=True)
