"""Tests for Splitch desktop icon rasterizer, dynamic theming, OS shortcuts, autostart, and universal tray."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from backend.desktop.icons import (
    ensure_tray_icon_theme,
    render_argb32_pixmap,
    render_icns_bytes,
    render_ico_bytes,
    render_png_bytes,
    render_svg_text,
    theme_tag,
)
from backend.desktop.shortcuts import (
    DesktopConfig,
    _sync_macos_app_bundle_icon,
    clear_desktop_api_key,
    desktop_icons_in_sync,
    install_desktop_integration,
    launch_splitch_gui,
    load_desktop_config,
    open_url_in_app_mode,
    resolve_desktop_api_key,
    save_desktop_api_key,
    save_desktop_config,
    set_tray_autostart,
    sync_desktop_icons,
    uninstall_desktop_integration,
)
from backend.desktop.tray import TrayController
from backend.router import app as router_app_mod
from backend.shared.updater import _uv_sync_cmd
from backend.version import __version__ as VERSION


def test_icon_rasterizer_png_argb_and_ico(tmp_path: Path) -> None:
    cyan_theme = {"scheme": "dark", "accent": "#22d3ee", "surface": "oled"}
    rose_light = {"scheme": "light", "accent": "#fb7185", "surface": "default"}

    for status in ("plain", "ok", "warn", "offline"):
        png = render_png_bytes(32, status)  # type: ignore[arg-type]
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(png) > 150

        w, h, argb = render_argb32_pixmap(22, status)  # type: ignore[arg-type]
        assert (w, h) == (22, 22)
        assert len(argb) == 22 * 22 * 4

    # Changing accent/surface/scheme changes rendered PNG, ARGB32, ICO, and SVG bytes
    png_cyan = render_png_bytes(32, "ok", theme=cyan_theme)
    png_rose = render_png_bytes(32, "ok", theme=rose_light)
    assert png_cyan != png_rose

    assert render_argb32_pixmap(22, "ok", theme=cyan_theme)[2] != render_argb32_pixmap(
        22, "ok", theme=rose_light
    )[2]

    svg_cyan = render_svg_text(theme=cyan_theme)
    svg_rose = render_svg_text(theme=rose_light)
    assert "#22d3ee" in svg_cyan and "#000000" in svg_cyan
    assert "#fb7185" in svg_rose and "#f6f7fb" in svg_rose

    ico_cyan = render_ico_bytes("plain", theme=cyan_theme)
    ico_rose = render_ico_bytes("plain", theme=rose_light)
    assert ico_cyan[:4] == b"\x00\x00\x01\x00"
    assert len(ico_cyan) > 400
    assert ico_cyan != ico_rose

    icons_dir = tmp_path / "icons"
    tag1 = theme_tag(cyan_theme)
    ensure_tray_icon_theme(icons_dir, theme=cyan_theme)
    for fname in (
        "splitch.png",
        "splitch-ok.png",
        "splitch-warn.png",
        "splitch-offline.png",
        f"splitch-ok-{tag1}.png",
        f"splitch-256-{tag1}.png",
    ):
        assert (icons_dir / fname).is_file()

    # Switching theme writes new hashed files and prunes stale hashed PNGs
    tag2 = theme_tag(rose_light)
    assert tag1 != tag2
    ensure_tray_icon_theme(icons_dir, theme=rose_light)
    assert (icons_dir / f"splitch-ok-{tag2}.png").is_file()
    assert not (icons_dir / f"splitch-ok-{tag1}.png").exists()


def test_linux_desktop_install_autostart_and_uninstall(tmp_path: Path, monkeypatch) -> None:
    if not sys.platform.startswith("linux"):
        return
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "splitch-data"))
    (tmp_path / "Desktop").mkdir()

    res = install_desktop_integration(
        desktop_icon=True,
        autostart=True,
        router_url="http://192.168.1.20:8080",
        mode="remote",
        local_service="splitch-agent.service",
        splitch_bin="/usr/local/bin/splitch",
    )
    assert res["ok"] is True

    app_file = tmp_path / ".local" / "share" / "applications" / "splitch.desktop"
    desk_file = tmp_path / "Desktop" / "splitch.desktop"
    auto_file = tmp_path / ".config" / "autostart" / "splitch-tray.desktop"
    svg_file = tmp_path / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "splitch.svg"

    assert app_file.is_file()
    assert desk_file.is_file()
    assert auto_file.is_file()
    assert svg_file.is_file()

    content = app_file.read_text(encoding="utf-8")
    assert 'Exec="/usr/local/bin/splitch" launch' in content
    assert "[Desktop Action StopSplitch]" in content
    assert "X-KDE-autostart-after=panel" in auto_file.read_text(encoding="utf-8")

    # Syncing a new accent + surface atomically updates Icon= in app menu, desktop, and autostart
    new_theme = {"scheme": "dark", "accent": "#22d3ee", "surface": "oled"}
    expected_tag = theme_tag(new_theme)
    sync_res = sync_desktop_icons(new_theme, refresh_caches=False)
    assert sync_res["ok"] is True
    assert sync_res["tag"] == expected_tag
    expected_icon = f"splitch-256-{expected_tag}.png"
    assert expected_icon in app_file.read_text(encoding="utf-8")
    assert expected_icon in desk_file.read_text(encoding="utf-8")
    assert expected_icon in auto_file.read_text(encoding="utf-8")
    assert "#22d3ee" in svg_file.read_text(encoding="utf-8")
    assert load_desktop_config().theme.accent == "#22d3ee"

    cfg = load_desktop_config()
    assert cfg.mode == "remote"
    assert cfg.router_url == "http://192.168.1.20:8080"
    assert cfg.autostart is True

    # Toggle autostart off
    set_tray_autostart(False, splitch_bin="/usr/local/bin/splitch")
    assert not auto_file.exists()
    assert load_desktop_config().autostart is False

    # Uninstall cleans everything
    un = uninstall_desktop_integration()
    assert un["ok"] is True
    assert not app_file.exists()
    assert not desk_file.exists()


def test_tray_controller_state_badges_and_menu_tree(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path))
    ctrl = TrayController(router_url="http://127.0.0.1:8080")
    ctrl.cfg = DesktopConfig(mode="local", router_url="http://127.0.0.1:8080")

    assert ctrl.status_badge() == "offline"
    assert ctrl.icon_name().startswith("splitch-offline-")

    snap = {
        "version": VERSION,
        "control": {"gaming_mode": False, "force_node": "auto"},
        "theme": {"scheme": "dark", "accent": "#a78bfa", "surface": "default"},
        "nodes": [
            {
                "node_id": "gpu-rig",
                "display_name": "Workstation (RTX 4090)",
                "online": True,
                "busy_reason": None,
                "telemetry": {
                    "vram_total_mb": 24564.0,
                    "vram_used_mb": 14336.0,
                    "gpu_count": 2,
                    "models": [{"id": "qwen3-coder-30b-a3b", "status": "loaded"}],
                },
            }
        ],
    }
    ctrl.apply_snapshot(snap, sync_desktop=False)

    assert ctrl.status_badge() == "ok"
    violet_icon = ctrl.icon_name()
    assert violet_icon.startswith("splitch-ok-")
    violet_pix = ctrl.icon_pixmaps()[0][2]

    title, desc = ctrl.tooltip_summary()
    assert "Online (1/1 nodes)" in title
    assert "Workstation (RTX 4090)" in desc

    menu = ctrl.build_menu()
    labels = [m.label for m in menu if not m.is_separator]
    assert any("2 GPUs" in lbl and "14.0/24.0 GB" in lbl for lbl in labels)
    assert any("Loaded: qwen3-coder-30b-a3b" in lbl for lbl in labels)
    assert "Open Web UI" in labels
    assert "Check for Updates…" in labels
    assert "Graceful Shutdown & Quit" in labels

    routing_item = next(m for m in menu if m.label == "Routing Target")
    assert len(routing_item.children) == 2
    assert routing_item.children[0].label == "Auto Split"
    assert routing_item.children[0].toggle_state == 1

    # Theme update over WebSocket snapshot immediately swaps icon_name & ARGB32 pixmap
    emerald_snap = {
        **snap,
        "theme": {"scheme": "dark", "accent": "#34d399", "surface": "slate"},
    }
    ctrl.apply_snapshot(emerald_snap, sync_desktop=False)
    emerald_icon = ctrl.icon_name()
    assert emerald_icon.startswith("splitch-ok-")
    assert emerald_icon != violet_icon
    assert ctrl.icon_pixmaps()[0][2] != violet_pix
    assert (tmp_path / "icons" / f"{emerald_icon}.png").is_file()

    # Gaming mode flips badge to warn (amber)
    ctrl.snapshot["control"]["gaming_mode"] = True
    assert ctrl.status_badge() == "warn"
    assert ctrl.icon_name().startswith("splitch-warn-")


def test_router_theme_api_and_dynamic_favicon(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(router_app_mod.cluster, "data_dir", tmp_path)
    old_theme = router_app_mod.cluster.theme.model_copy()
    try:
        client = TestClient(router_app_mod.app)
        bad = client.patch("/api/theme", json={"accent": "not-a-hex"})
        assert bad.status_code == 400

        res = client.patch(
            "/api/theme",
            json={"scheme": "dark", "accent": "#fb7185", "surface": "oled", "resolved_light": False},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["accent"] == "#fb7185"
        assert body["surface"] == "oled"
        assert body["updated_at"] > 0

        get_res = client.get("/api/theme")
        assert get_res.status_code == 200
        assert get_res.json()["accent"] == "#fb7185"

        status_res = client.get("/api/status")
        assert status_res.status_code == 200
        assert status_res.json()["theme"]["accent"] == "#fb7185"

        fav_res = client.get("/favicon.svg")
        assert fav_res.status_code == 200
        assert "#fb7185" in fav_res.text
        assert "#000000" in fav_res.text
    finally:
        router_app_mod.cluster.theme = old_theme


def test_dbus_next_struct_signatures_valid(tmp_path: Path, monkeypatch) -> None:
    """Verify every SNI/DBusMenu struct payload conforms to dbus-next list-for-struct rules."""
    if not sys.platform.startswith("linux"):
        return
    import pytest

    dbus_next = pytest.importorskip("dbus_next")
    Variant = dbus_next.Variant

    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path))
    ctrl = TrayController(router_url="http://127.0.0.1:8080")
    ctrl.online = True
    ctrl.snapshot = {
        "version": VERSION,
        "control": {"gaming_mode": False, "force_node": "auto"},
        "nodes": [{"node_id": "n1", "display_name": "Node 1", "online": True, "telemetry": {}}],
    }

    pixmaps = [[w, h, data] for w, h, data in ctrl.icon_pixmaps()]
    assert Variant("a(iiay)", pixmaps).value == pixmaps

    title, desc = ctrl.tooltip_summary()
    tt = [ctrl.icon_name(), [], title, desc]
    assert Variant("(sa(iiay)ss)", tt).value[2] == title

    def pack(item) -> list[object]:
        props = (
            {"type": Variant("s", "separator")}
            if item.is_separator
            else {"label": Variant("s", item.label), "enabled": Variant("b", item.enabled)}
        )
        return [item.id, props, [Variant("(ia{sv}av)", pack(c)) for c in item.children]]

    root = [
        0,
        {"children-display": Variant("s", "submenu")},
        [Variant("(ia{sv}av)", pack(it)) for it in ctrl.build_menu()],
    ]
    assert Variant("(ia{sv}av)", root).value[0] == 0


def test_tray_watch_status_loop_reconnects_after_websocket_close(monkeypatch, tmp_path):
    """WebSocket ConnectionClosedError (router restart) must reconnect and apply new themes."""
    import asyncio
    import json

    import websockets
    import websockets.exceptions

    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path))
    ctrl = TrayController(router_url="http://127.0.0.1:8080")
    attempts = 0

    class _FakeWs:
        def __init__(self, idx: int) -> None:
            self._idx = idx
            self._sent = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._idx == 1:
                raise websockets.exceptions.ConnectionClosedError(None, None)
            if not self._sent:
                self._sent = True
                return json.dumps(
                    {
                        "version": VERSION,
                        "control": {"gaming_mode": False, "force_node": "auto"},
                        "nodes": [],
                        "theme": {
                            "scheme": "dark",
                            "accent": "#22d3ee",
                            "surface": "oled",
                            "resolved_light": False,
                        },
                    }
                )
            ctrl._stop_event.set()
            raise StopAsyncIteration

    def _fake_connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return _FakeWs(attempts)

    async def _fast_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    monkeypatch.setattr(ctrl, "poll_once_sync", lambda: None)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    asyncio.run(ctrl.watch_status_loop())
    assert attempts == 2
    assert ctrl.theme["accent"] == "#22d3ee"
    assert ctrl.theme["surface"] == "oled"


def test_render_icns_bytes_and_macos_app_bundle_sync(tmp_path: Path) -> None:
    import struct

    cyan = {"scheme": "dark", "accent": "#22d3ee", "surface": "oled"}
    rose = {"scheme": "light", "accent": "#fb7185", "surface": "default"}

    icns_cyan = render_icns_bytes("plain", theme=cyan)
    icns_rose = render_icns_bytes("plain", theme=rose)
    assert icns_cyan[:4] == b"icns"
    total_len = struct.unpack(">I", icns_cyan[4:8])[0]
    assert total_len == len(icns_cyan)
    assert icns_cyan != icns_rose

    # Walk ICNS chunk headers and ensure all embedded PNGs are valid
    pos = 8
    tags: list[bytes] = []
    while pos < len(icns_cyan):
        ctag = icns_cyan[pos : pos + 4]
        clen = struct.unpack(">I", icns_cyan[pos + 4 : pos + 8])[0]
        payload = icns_cyan[pos + 8 : pos + clen]
        assert payload.startswith(b"\x89PNG\r\n\x1a\n")
        tags.append(ctag)
        pos += clen
    assert tags == [b"icp4", b"icp5", b"icp6", b"ic07", b"ic08"]

    # Syncing a macOS .app bundle writes cache-busted .icns, updates Info.plist, and prunes old .icns
    app_dir = tmp_path / "Splitch.app"
    (app_dir / "Contents" / "Resources").mkdir(parents=True)
    tag1 = theme_tag(cyan)
    tag2 = theme_tag(rose)
    _sync_macos_app_bundle_icon(app_dir, cyan, refresh_caches=False)
    assert (app_dir / "Contents" / "Resources" / f"splitch-{tag1}.icns").is_file()
    plist_txt = (app_dir / "Contents" / "Info.plist").read_text(encoding="utf-8")
    assert f"<string>splitch-{tag1}.icns</string>" in plist_txt

    _sync_macos_app_bundle_icon(app_dir, rose, refresh_caches=False)
    assert (app_dir / "Contents" / "Resources" / f"splitch-{tag2}.icns").is_file()
    assert not (app_dir / "Contents" / "Resources" / f"splitch-{tag1}.icns").exists()
    plist_txt2 = (app_dir / "Contents" / "Info.plist").read_text(encoding="utf-8")
    assert f"<string>splitch-{tag2}.icns</string>" in plist_txt2


def test_desktop_api_key_resolution_precedence_and_vault(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "splitch-data"))
    monkeypatch.delenv("SPLITCH_API_KEY", raising=False)
    # Force file-backed master key in tmp_path instead of host keyring
    monkeypatch.setattr("backend.shared.secrets._try_keyring_master_key", lambda: None)

    assert resolve_desktop_api_key() == ""

    # 1. Env file fallback preserves symbols like @, &, %, $
    tray_env = tmp_path / ".config" / "splitch" / "tray.env"
    tray_env.parent.mkdir(parents=True, exist_ok=True)
    special_key = "@abc&123%$xyz!"
    tray_env.write_text(f"export SPLITCH_API_KEY='{special_key}'\n", encoding="utf-8")
    assert resolve_desktop_api_key() == special_key

    # 2. Encrypted vault overrides tray.env
    vault_key = "vault&secret%$99"
    ref = save_desktop_api_key(vault_key)
    assert ref == "vault:desktop_api_key"
    assert resolve_desktop_api_key() == vault_key

    # 3. Explicit env var overrides vault
    monkeypatch.setenv("SPLITCH_API_KEY", "env-override-key")
    assert resolve_desktop_api_key() == "env-override-key"
    monkeypatch.delenv("SPLITCH_API_KEY", raising=False)

    # 4. Clearing vault falls back cleanly to tray.env
    assert clear_desktop_api_key() is True
    assert resolve_desktop_api_key() == special_key


def test_tray_auth_error_state_and_auto_recovery(tmp_path: Path, monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "splitch-data"))
    monkeypatch.delenv("SPLITCH_API_KEY", raising=False)
    monkeypatch.setattr("backend.shared.secrets._try_keyring_master_key", lambda: None)

    expected_key = "s3cr3t&key%with$symbols"

    def _fake_get(url: str, headers: dict | None = None, timeout: float = 3.0):
        auth = (headers or {}).get("Authorization", "")
        if auth != f"Bearer {expected_key}":
            return httpx.Response(401, json={"detail": "Unauthorized"})
        return httpx.Response(
            200,
            json={
                "version": VERSION,
                "control": {"gaming_mode": False, "force_node": "auto"},
                "nodes": [],
                "theme": {"scheme": "dark", "accent": "#22d3ee", "surface": "oled"},
            },
        )

    monkeypatch.setattr(httpx, "get", _fake_get)
    ctrl = TrayController(router_url="http://127.0.0.1:8080")
    ctrl.poll_once_sync()
    assert ctrl.online is False
    assert ctrl.auth_error is True
    title, desc = ctrl.tooltip_summary()
    assert "Auth Required" in title
    assert "splitch desktop set-api-key" in desc
    assert any("set-api-key" in m.label for m in ctrl.build_menu())

    # Saving key into encrypted vault lets running tray auto-recover on next poll without restart
    save_desktop_api_key(expected_key)
    ctrl.poll_once_sync()
    assert ctrl.online is True
    assert ctrl.auth_error is False
    assert ctrl.theme["accent"] == "#22d3ee"


def test_agent_desktop_sync_theme_endpoint(tmp_path: Path, monkeypatch) -> None:
    from backend.agent import main as agent_mod

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "splitch-data"))
    monkeypatch.delenv("SPLITCH_NO_DESKTOP", raising=False)
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN", "")
    monkeypatch.setattr(agent_mod, "ROUTER_PUBLIC_KEY", None)
    (tmp_path / "Desktop").mkdir()

    client = TestClient(agent_mod.app)
    # Without installed shortcuts, endpoint returns synced=False safely
    r0 = client.post(
        "/desktop/sync-theme",
        json={"scheme": "dark", "accent": "#22d3ee", "surface": "oled", "resolved_light": False},
    )
    assert r0.status_code == 200
    assert r0.json()["synced"] is False

    # After desktop shortcuts are installed, signed router push syncs them immediately
    if sys.platform.startswith("linux"):
        install_desktop_integration(
            desktop_icon=True,
            autostart=False,
            splitch_bin="/usr/local/bin/splitch",
        )
        r1 = client.post(
            "/desktop/sync-theme",
            json={"scheme": "dark", "accent": "#34d399", "surface": "slate", "resolved_light": False},
        )
        assert r1.status_code == 200
        body = r1.json()
        assert body["synced"] is True
        expected_tag = theme_tag({"scheme": "dark", "accent": "#34d399", "surface": "slate"})
        assert body["tag"] == expected_tag
        assert desktop_icons_in_sync({"scheme": "dark", "accent": "#34d399", "surface": "slate"})

        # Mutating .desktop to stale icon makes desktop_icons_in_sync return False (triggers self-heal)
        desk = tmp_path / "Desktop" / "splitch.desktop"
        desk.write_text(desk.read_text(encoding="utf-8").replace(expected_tag, "deadbeef"), encoding="utf-8")
        assert not desktop_icons_in_sync({"scheme": "dark", "accent": "#34d399", "surface": "slate"})


def test_updater_preserves_desktop_extra(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "splitch-data"))
    monkeypatch.delenv("SPLITCH_NO_DESKTOP", raising=False)

    assert _uv_sync_cmd("uv", tmp_path, {}) == ["uv", "sync"]

    (tmp_path / "splitch-data").mkdir(parents=True)
    (tmp_path / "splitch-data" / "desktop.json").write_text("{}", encoding="utf-8")
    assert _uv_sync_cmd("uv", tmp_path, {}) == ["uv", "sync", "--extra", "desktop"]

    monkeypatch.setenv("SPLITCH_NO_DESKTOP", "1")
    assert _uv_sync_cmd("uv", tmp_path, {"desktop_extra": True}) == ["uv", "sync"]


def test_pwa_static_assets_and_router_serving() -> None:
    """Router serves manifest.webmanifest, sw.js, and PWA icons with expected headers and content."""
    client = TestClient(router_app_mod.app)

    manifest_res = client.get("/manifest.webmanifest")
    assert manifest_res.status_code == 200
    assert "manifest+json" in manifest_res.headers.get("content-type", "")
    manifest = manifest_res.json()
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    icon_srcs = {item["src"] for item in manifest.get("icons", [])}
    assert {"/icons/icon-192.png", "/icons/icon-512.png", "/favicon.svg"}.issubset(icon_srcs)

    sw_res = client.get("/sw.js")
    assert sw_res.status_code == 200
    assert "javascript" in sw_res.headers.get("content-type", "")
    sw_body = sw_res.text
    for bypass_prefix in ("/v1/", "/api/", "/health", "/metrics"):
        assert bypass_prefix in sw_body

    for icon_path in ("/icons/icon-192.png", "/icons/icon-512.png", "/icons/apple-touch-icon.png"):
        icon_res = client.get(icon_path)
        assert icon_res.status_code == 200
        assert icon_res.content.startswith(b"\x89PNG\r\n\x1a\n")

    fav_res = client.get("/favicon.svg")
    assert fav_res.status_code == 200
    assert "<svg" in fav_res.text


def test_desktop_config_launch_mode_validation() -> None:
    import pytest
    from pydantic import ValidationError

    cfg = DesktopConfig()
    assert cfg.launch_mode == "app"

    for valid in ("app", "browser", "firefoxpwa"):
        assert DesktopConfig(launch_mode=valid).launch_mode == valid  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        DesktopConfig(launch_mode="kiosk")  # type: ignore[arg-type]


def test_open_url_in_app_mode_cross_platform_and_fallbacks(tmp_path: Path, monkeypatch) -> None:
    import shutil
    import subprocess
    import webbrowser

    url = "http://127.0.0.1:8080"
    spawned: list[list[str]] = []
    opened_web: list[str] = []

    def _fake_popen(cmd, **_kwargs):
        spawned.append(list(cmd))

        class _Proc:
            pid = 4242

        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(webbrowser, "open", lambda target: opened_web.append(target) or True)

    # 1. mode="browser" never uses --app= or firefoxpwa
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("firefoxpwa", "google-chrome", "xdg-open") else None,
    )
    assert open_url_in_app_mode(url, mode="browser") is True
    assert spawned[-1] == ["xdg-open", url]

    # 2. mode="auto" / "app" prefers firefoxpwa when matching origin + 26-char ULID exists
    ulid = "01JABCDEFGHJKMNPQRSTVWXYZ0"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"Splitch ({ulid}) - http://127.0.0.1:8080/\n",
            stderr="",
        ),
    )
    assert open_url_in_app_mode(url, mode="app") is True
    assert spawned[-1] == ["/usr/bin/firefoxpwa", "site", "launch", ulid]

    # 3. Non-matching firefoxpwa site falls back to Linux Chromium --app= with --class=splitch
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"OtherApp ({ulid}) - http://example.com/\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("firefoxpwa", "chromium", "xdg-open") else None,
    )
    assert open_url_in_app_mode(url, mode="auto") is True
    assert spawned[-1] == ["/usr/bin/chromium", f"--app={url}", "--class=splitch"]

    # 4. macOS launches `open -na <App> --args --app=<url>`
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    chrome_app = tmp_path / "Applications" / "Google Chrome.app"
    chrome_app.mkdir(parents=True)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/open" if name == "open" else None)
    assert open_url_in_app_mode(url, mode="app") is True
    assert spawned[-1] == ["open", "-na", str(chrome_app), "--args", f"--app={url}"]

    # 5. Windows launches `[exe, f"--app={url}"]`
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: r"C:\Program Files\Microsoft\Edge\msedge.exe" if name == "msedge" else None,
    )
    assert open_url_in_app_mode(url, mode="app") is True
    assert spawned[-1] == [r"C:\Program Files\Microsoft\Edge\msedge.exe", f"--app={url}"]

    # 6. Neither firefoxpwa nor Chromium found -> falls back to open_url_in_browser
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert open_url_in_app_mode(url, mode="app") is True
    assert opened_web[-1] == url


def test_launch_splitch_gui_and_cli_launch_flags(tmp_path: Path, monkeypatch) -> None:
    import shutil
    import subprocess

    import httpx
    import pytest

    from backend import cli as cli_mod

    monkeypatch.setenv("SPLITCH_DATA_ROOT", str(tmp_path / "splitch-data"))
    monkeypatch.setenv("SPLITCH_NO_DESKTOP", "1")
    monkeypatch.setattr(sys, "platform", "linux")

    spawned: list[list[str]] = []

    def _fake_popen(cmd, **_kwargs):
        spawned.append(list(cmd))

        class _Proc:
            pid = 5151

        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *_args, **_kwargs: httpx.Response(200, json={"status": "ok"}),
    )
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("google-chrome", "xdg-open") else None,
    )

    save_desktop_config(DesktopConfig(router_url="http://127.0.0.1:8080", launch_mode="browser"))

    # Uses cfg.launch_mode when launch_mode=None
    ok, _ = launch_splitch_gui(start_tray=False, open_browser=True, launch_mode=None)
    assert ok is True
    assert spawned[-1] == ["xdg-open", "http://127.0.0.1:8080"]

    # Explicit launch_mode="app" overrides cfg.launch_mode
    ok, _ = launch_splitch_gui(start_tray=False, open_browser=True, launch_mode="app")
    assert ok is True
    assert spawned[-1] == ["/usr/bin/google-chrome", "--app=http://127.0.0.1:8080", "--class=splitch"]

    # CLI: --app and --browser are mutually exclusive
    monkeypatch.setattr(sys, "argv", ["splitch", "launch", "--app", "--browser"])
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()
    assert exc_info.value.code != 0

    # CLI: --browser passes launch_mode="browser" through
    monkeypatch.setattr(
        sys, "argv", ["splitch", "launch", "--browser", "--no-tray", "--url", "http://127.0.0.1:8080"]
    )
    with pytest.raises(SystemExit) as exc_browser:
        cli_mod.main()
    assert exc_browser.value.code == 0
    assert spawned[-1] == ["xdg-open", "http://127.0.0.1:8080"]

    # CLI: --app passes launch_mode="app" through
    monkeypatch.setattr(
        sys, "argv", ["splitch", "launch", "--app", "--no-tray", "--url", "http://127.0.0.1:8080"]
    )
    with pytest.raises(SystemExit) as exc_app:
        cli_mod.main()
    assert exc_app.value.code == 0
    assert spawned[-1] == ["/usr/bin/google-chrome", "--app=http://127.0.0.1:8080", "--class=splitch"]
