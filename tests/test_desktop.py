"""Tests for Splitch desktop icon rasterizer, OS shortcuts, autostart, and universal tray."""

from __future__ import annotations

import sys
from pathlib import Path

from backend.desktop.icons import (
    ensure_tray_icon_theme,
    render_argb32_pixmap,
    render_ico_bytes,
    render_png_bytes,
)
from backend.desktop.shortcuts import (
    DesktopConfig,
    install_desktop_integration,
    load_desktop_config,
    set_tray_autostart,
    uninstall_desktop_integration,
)
from backend.desktop.tray import TrayController


def test_icon_rasterizer_png_argb_and_ico(tmp_path: Path) -> None:
    for status in ("plain", "ok", "warn", "offline"):
        png = render_png_bytes(32, status)  # type: ignore[arg-type]
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(png) > 150

        w, h, argb = render_argb32_pixmap(22, status)  # type: ignore[arg-type]
        assert (w, h) == (22, 22)
        assert len(argb) == 22 * 22 * 4

    ico = render_ico_bytes("plain")
    assert ico[:4] == b"\x00\x00\x01\x00"
    assert len(ico) > 400

    theme_dir = ensure_tray_icon_theme(tmp_path / "icons")
    for fname in ("splitch.png", "splitch-ok.png", "splitch-warn.png", "splitch-offline.png"):
        assert (theme_dir / fname).is_file()


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
    assert ctrl.icon_name() == "splitch-offline"

    ctrl.online = True
    ctrl.snapshot = {
        "version": "0.3.0",
        "control": {"gaming_mode": False, "force_node": "auto"},
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

    assert ctrl.status_badge() == "ok"
    assert ctrl.icon_name() == "splitch-ok"
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

    # Gaming mode flips badge to warn (amber)
    ctrl.snapshot["control"]["gaming_mode"] = True
    assert ctrl.status_badge() == "warn"
    assert ctrl.icon_name() == "splitch-warn"


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
        "version": "0.3.0",
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
