"""Real headless-Chromium UI end-to-end tests for ConfirmDialog, Trash, Bulk Delete & Pinning."""

from __future__ import annotations

import asyncio
import json
import os
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


def _build_test_spa_app(tmp_path: Path) -> tuple[FastAPI, dict]:
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

    media_state: dict = {
        "jev_running": True,
        "jev_healthy": False,
    }

    @app.get("/api/media/backends")
    async def media_backends():
        return [
            {
                "node_id": "dual-rig",
                "display_name": "Dual GPU Rig",
                "role": "gpu",
                "online": True,
                "media_engine": "none",
                "media_healthy": False,
                "media_url": "",
                "media_models": [],
                "sd_installed": False,
            }
        ]

    @app.get("/api/media/jobs")
    async def media_jobs():
        return []

    @app.get("/api/media/nodes/{node_id}/engine")
    async def media_node_engine(node_id: str):
        return {
            "node_id": node_id,
            "media_engine": "none",
            "media_healthy": False,
            "media_url": "",
            "sd_installed": False,
            "sd_binary_path": "",
            "sd_version": "",
            "sd_backend": "",
            "sd_managed_running": False,
            "sd_running_model": "",
            "sd_error": None,
            "install_in_progress": False,
            "install_step": "",
            "local_media_models": [],
            "active_downloads": [],
        }

    @app.get("/api/media/jeveration/status")
    async def media_jeveration_status():
        return [
            {
                "node_id": "dual-rig",
                "installed": True,
                "weights_ready": True,
                "cuda_available": True,
                "running": media_state["jev_running"],
                "healthy": media_state["jev_healthy"],
                "port": 8084,
                "vram_mb": 12000.0 if media_state["jev_healthy"] else 0.0,
                "install_in_progress": False,
                "install_step": "",
                "weights_downloading": False,
                "active_downloads": [],
                "error": None,
            }
        ]

    assets = DIST_DIR / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(str(DIST_DIR / "index.html"))

    return app, media_state


@pytest.mark.skipif(
    not CHROMIUM_BIN or not (DIST_DIR / "index.html").exists(),
    reason="Headless Chromium or built dashboard/dist not present",
)
async def test_ui_confirm_delete_trash_bulk_and_pin_e2e(tmp_path: Path) -> None:
    port = _free_port()
    cdp_port = _free_port()
    app, media_state = _build_test_spa_app(tmp_path)

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
        chrome_log = tmp_path / "chrome.err.log"
        chrome_log_fh = chrome_log.open("wb")
        chrome_env = dict(os.environ)
        chrome_env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/dev/null"
        chrome = await asyncio.create_subprocess_exec(
            str(CHROMIUM_BIN),
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-default-apps",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--mute-audio",
            "--remote-allow-origins=*",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={chrome_profile}",
            "--window-size=1280,900",
            "about:blank",
            env=chrome_env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=chrome_log_fh,
        )

        try:
            ws_url = ""
            cdp_deadline = time.monotonic() + 35.0
            while time.monotonic() < cdp_deadline:
                if chrome.returncode is not None:
                    break
                try:
                    r = await client.get(f"http://127.0.0.1:{cdp_port}/json")
                    if r.status_code == 200:
                        targets = r.json()
                        page = next((t for t in targets if t.get("type") == "page"), None)
                        if page and page.get("webSocketDebuggerUrl"):
                            ws_url = str(page["webSocketDebuggerUrl"])
                            break
                        # If CDP is up but no page tab exists yet, try opening about:blank
                        r_new = await client.put(f"http://127.0.0.1:{cdp_port}/json/new?about:blank")
                        if r_new.status_code == 200:
                            created = r_new.json()
                            if isinstance(created, dict) and created.get("webSocketDebuggerUrl"):
                                ws_url = str(created["webSocketDebuggerUrl"])
                                break
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.2)
            if not ws_url:
                err_tail = chrome_log.read_text(encoding="utf-8", errors="replace")[-600:]
                raise AssertionError(
                    f"Chromium CDP failed to start (rc={chrome.returncode}): {err_tail}"
                )

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

                # 5b. Verify unified single topbar (< 12% viewport height) & collapsible/resizable sidebar
                topbar_ok = await eval_js(
                    "(() => {"
                    "  const tb = document.querySelector('.topbar');"
                    "  const tl = document.querySelector('.topbar [role=\"tablist\"]');"
                    "  return Boolean(tb && tl && tb.contains(tl) && tb.getBoundingClientRect().height <= 56);"
                    "})()"
                )
                assert topbar_ok is True

                # Verify left sidebar splitter visibility, drag resize, localStorage persistence, keyboard, & reset
                splitter_rect = await eval_js(
                    "(() => {"
                    "  const sp = document.querySelector('.chat-splitter--left');"
                    "  if (!sp) return null;"
                    "  const r = sp.getBoundingClientRect();"
                    "  return {"
                    "    x: r.left + r.width / 2,"
                    "    y: r.top + r.height / 2,"
                    "    w: r.width,"
                    "    h: r.height,"
                    "    valuenow: sp.getAttribute('aria-valuenow'),"
                    "    varWidth: document.querySelector('.chat-layout').style.getPropertyValue('--sidebar-width')"
                    "  };"
                    "})()"
                )
                assert splitter_rect is not None
                assert splitter_rect["w"] > 0 and splitter_rect["h"] > 0
                assert splitter_rect["valuenow"] == "236"
                assert splitter_rect["varWidth"] == "236px"

                # Drag splitter right by +84px using real browser mouse events
                sx, sy = splitter_rect["x"], splitter_rect["y"]
                await cdp(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": sx, "y": sy},
                )
                await cdp(
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": sx, "y": sy, "button": "left", "clickCount": 1},
                )
                await cdp(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": sx + 84, "y": sy, "button": "left"},
                )
                await cdp(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "x": sx + 84,
                        "y": sy,
                        "button": "left",
                        "clickCount": 1,
                    },
                )
                await wait_until(
                    "Number(document.querySelector('.chat-splitter--left')?.getAttribute('aria-valuenow')) > 290"
                )
                dragged_state = await eval_js(
                    "(() => ({"
                    "  valuenow: document.querySelector('.chat-splitter--left').getAttribute('aria-valuenow'),"
                    "  varWidth: document.querySelector('.chat-layout').style.getPropertyValue('--sidebar-width'),"
                    "  storage: localStorage.getItem('splitch_chat_sidebar_width'),"
                    "  sidebarW: Math.round(document.querySelector('.chat-sidebar').getBoundingClientRect().width)"
                    "}))()"
                )
                assert dragged_state["varWidth"] == f"{dragged_state['valuenow']}px"
                assert dragged_state["storage"] == dragged_state["valuenow"]
                assert dragged_state["sidebarW"] >= 290

                # Keyboard ArrowRight / ArrowLeft on focused splitter
                await eval_js("document.querySelector('.chat-splitter--left').focus()")
                await cdp(
                    "Input.dispatchKeyEvent",
                    {"type": "keyDown", "key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39},
                )
                expected_after_key = int(dragged_state["valuenow"]) + 24
                await wait_until(
                    f"Number(document.querySelector('.chat-splitter--left')?.getAttribute('aria-valuenow')) === {expected_after_key}"
                )
                assert (
                    await eval_js("localStorage.getItem('splitch_chat_sidebar_width')")
                    == str(expected_after_key)
                )

                # Double-click splitter to reset width back to 236
                new_rect = await eval_js(
                    "(() => {"
                    "  const r = document.querySelector('.chat-splitter--left').getBoundingClientRect();"
                    "  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };"
                    "})()"
                )
                nx, ny = new_rect["x"], new_rect["y"]
                for count in (1, 2):
                    await cdp(
                        "Input.dispatchMouseEvent",
                        {"type": "mousePressed", "x": nx, "y": ny, "button": "left", "clickCount": count},
                    )
                    await cdp(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseReleased", "x": nx, "y": ny, "button": "left", "clickCount": count},
                    )
                await wait_until(
                    "document.querySelector('.chat-splitter--left')?.getAttribute('aria-valuenow') === '236'"
                )
                assert (
                    await eval_js(
                        "document.querySelector('.chat-layout').style.getPropertyValue('--sidebar-width')"
                    )
                    == "236px"
                )
                assert await eval_js("localStorage.getItem('splitch_chat_sidebar_width')") == "236"

                # Collapse sidebar -> splitter hides; expand -> splitter returns
                await eval_js("document.querySelector('.chat-sidebar-toggle').click()")
                await wait_until(
                    "Math.round(document.querySelector('.chat-sidebar').getBoundingClientRect().width) === 0 && "
                    "document.querySelector('.chat-splitter--left') === null"
                )
                await eval_js("document.querySelector('.chat-sidebar-toggle').click()")
                await wait_until(
                    "Math.round(document.querySelector('.chat-sidebar').getBoundingClientRect().width) > 200 && "
                    "document.querySelector('.chat-splitter--left') !== null"
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

                # Close Settings modal, switch to Media Studio, verify Jeveration loading -> ready transition & focused UI
                await eval_js(
                    "document.querySelector('dialog.settings-dialog .settings-modal__header button')?.click()"
                )
                await wait_until("!document.querySelector('dialog.settings-dialog[open]')")
                await eval_js(
                    "Array.from(document.querySelectorAll('[role=\"tab\"]'))"
                    ".find(t => t.textContent.includes('Media Studio')).click()"
                )
                await wait_until(
                    "Boolean(document.querySelector('.media-engine-manager'))"
                )
                # Starting state: running=True, healthy=False -> header shows loading, NOT 'NO ENGINE', Engines & Models collapsed
                try:
                    await wait_until(
                        "(document.querySelector('.tab-stack .panel-head')?.textContent || '').toLowerCase().includes('jeveration loading')"
                    )
                except AssertionError as exc:
                    actual = await eval_js("document.querySelector('.tab-stack .panel-head')?.textContent || ''")
                    raise AssertionError(f"{exc} (actual panel-head={actual!r})") from exc
                media_head = await eval_js("document.querySelector('.tab-stack .panel-head').textContent")
                assert "NO ENGINE" not in media_head
                assert not await eval_js(
                    "document.querySelector('.media-engine-manager').hasAttribute('open')"
                )

                # Flip Jeveration to healthy -> 1.5s poll must automatically update header, auto-enable 1-Pass, and hide diffusion clutter
                media_state["jev_healthy"] = True
                await wait_until(
                    "(document.querySelector('.tab-stack .panel-head')?.textContent || '').toLowerCase().includes('jeveration :8084')",
                    timeout_s=5.0,
                )
                await wait_until(
                    "(document.querySelector('.media-params-grid')?.textContent || '').includes('512×512 · Single Forward Pass')"
                )
                form_text = await eval_js("document.querySelector('.tab-stack .panel').textContent")
                assert "Negative Prompt" not in form_text
                assert "ComfyUI Workflow JSON" not in form_text
                assert "CFG Scale" not in form_text
        finally:
            chrome.terminate()
            try:
                await asyncio.wait_for(chrome.wait(), timeout=3.0)
            except TimeoutError:
                chrome.kill()
            chrome_log_fh.close()
            server.should_exit = True
            await asyncio.gather(srv_task, return_exceptions=True)
