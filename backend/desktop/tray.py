"""
Universal Cross-Platform System Tray for Splitch.

Linux Compositor / WM Support:
  - Tier 1 (Modern Wayland & X11): Native pure-Python `org.kde.StatusNotifierItem`
    + `com.canonical.dbusmenu` over `dbus-next`, registering with BOTH
    `org.kde.StatusNotifierWatcher` AND `org.freedesktop.StatusNotifierWatcher`,
    exporting BOTH `IconThemePath`/`IconName` AND `IconPixmap` (ARGB32), and
    auto-re-registering if the panel/bar restarts.
    Covers: KDE Plasma (Wayland/X11), Hyprland/Sway/River/Niri (Waybar, AGS, EWW,
    Quickshell, sfwbar), GNOME (AppIndicator/KStatusNotifierItem), COSMIC, XFCE,
    Cinnamon, MATE, LXQt, Budgie, Polybar, Deepin.
  - Tier 2 (Legacy Pure-X11 WMs without an SNI watcher: i3bar, dwm, openbox/tint2,
    stalonetray, trayer, fluxbox, icewm): Automatic fallback to `pystray` XEmbed.
  - macOS (`NSStatusBar`) & Windows (`Shell_NotifyIcon`): Native `pystray` + Pillow.

Live state updates arrive via `/ws/status` WebSocket (zero polling CPU/GPU wakeups).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.desktop._sni_dbus import run_linux_sni_tray
from backend.desktop.icons import (
    StatusBadge,
    render_argb32_pixmap,
    render_png_bytes,
)
from backend.desktop.shortcuts import (
    DesktopConfig,
    copy_to_clipboard,
    load_desktop_config,
    open_path_in_file_manager,
    read_live_pid,
    restart_splitch_service,
    send_desktop_notification,
    set_tray_autostart,
    stop_splitch_graceful,
    write_pid_file,
)
from backend.router.config import default_data_root
from backend.shared.engines import get_engine_status
from backend.shared.updater import check_update
from backend.version import __version__ as VERSION

logger = logging.getLogger("splitch.tray")


@dataclass
class TrayMenuItem:
    id: int
    label: str = ""
    enabled: bool = True
    is_separator: bool = False
    toggle_type: str = ""  # "" | "checkmark" | "radio"
    toggle_state: int = -1  # -1 unset | 0 off | 1 on
    children: list[TrayMenuItem] = field(default_factory=list)
    on_click: Callable[[], None] | None = None


class TrayController:
    """Shared live cluster state & menu model across all OS / compositor tray backends."""

    def __init__(self, router_url: str | None = None) -> None:
        self.cfg: DesktopConfig = load_desktop_config()
        if router_url:
            self.cfg.router_url = router_url.rstrip("/")
            if not any(h in self.cfg.router_url for h in ("127.0.0.1", "localhost")):
                self.cfg.mode = "remote"
        self.router_url: str = self.cfg.router_url.rstrip("/")
        self.api_key: str = os.getenv("SPLITCH_API_KEY", "").strip()
        self.online: bool = False
        self.snapshot: dict[str, Any] = {}
        self.update_note: str = ""
        self.revision: int = 1
        self._listeners: list[Callable[[], None]] = []
        self._action_map: dict[int, Callable[[], None]] = {}
        self._stop_event = threading.Event()
        self._quit_callbacks: list[Callable[[], None]] = []
        self._pixmap_cache: dict[StatusBadge, list[tuple[int, int, bytes]]] = {}

    def on_change(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def on_quit(self, cb: Callable[[], None]) -> None:
        self._quit_callbacks.append(cb)

    def _notify_changed(self) -> None:
        self.revision += 1
        for cb in list(self._listeners):
            with contextlib.suppress(Exception):
                cb()

    def request_quit(self) -> None:
        self._stop_event.set()
        for cb in list(self._quit_callbacks):
            with contextlib.suppress(Exception):
                cb()

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def status_badge(self) -> StatusBadge:
        if not self.online:
            return "offline"
        control = self.snapshot.get("control") or {}
        if control.get("gaming_mode"):
            return "warn"
        nodes = self.snapshot.get("nodes") or []
        if any(
            (n.get("busy_reason") or "").endswith(("_gpu_busy_app", "_gpu_busy", "_gpu_cooldown"))
            for n in nodes
        ):
            return "warn"
        if nodes and not all(n.get("online") for n in nodes):
            return "warn"
        return "ok"

    def icon_name(self) -> str:
        b = self.status_badge()
        return f"splitch-{b}" if b != "plain" else "splitch"

    def icon_pixmaps(self) -> list[tuple[int, int, bytes]]:
        badge = self.status_badge()
        if badge not in self._pixmap_cache:
            self._pixmap_cache[badge] = [
                render_argb32_pixmap(22, badge),
                render_argb32_pixmap(32, badge),
                render_argb32_pixmap(64, badge),
            ]
        return self._pixmap_cache[badge]

    def tooltip_summary(self) -> tuple[str, str]:
        nodes = self.snapshot.get("nodes") or []
        online_nodes = [n for n in nodes if n.get("online")]
        ver = self.snapshot.get("version") or VERSION
        if not self.online:
            return f"Splitch v{ver} — Offline", f"Router unreachable ({self.router_url})"

        control = self.snapshot.get("control") or {}
        state_lbl = "Gaming Yield" if control.get("gaming_mode") else "Online"
        title = f"Splitch v{ver} — {state_lbl} ({len(online_nodes)}/{len(nodes)} nodes)"
        lines = [f"API: {self.router_url}/v1"]
        for n in nodes:
            t = n.get("telemetry") or {}
            vram_tot = float(t.get("vram_total_mb") or 0.0)
            vram_used = float(t.get("vram_used_mb") or 0.0)
            mem_s = f"{vram_used / 1024:.1f}/{vram_tot / 1024:.1f} GB" if vram_tot > 0 else "CPU"
            st = "READY" if (n.get("online") and not n.get("busy_reason")) else (
                "OFFLINE" if not n.get("online") else str(n.get("busy_reason"))
            )
            lines.append(f"{n.get('display_name', n.get('node_id'))}: {mem_s} ({st})")
        return title, "\n".join(lines)

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_open_ui(self) -> None:
        from backend.desktop.shortcuts import launch_splitch_gui

        threading.Thread(
            target=lambda: launch_splitch_gui(
                router_url=self.router_url, start_tray=False, open_browser=True
            ),
            daemon=True,
        ).start()

    def action_copy_endpoint(self) -> None:
        endpoint = f"{self.router_url}/v1"
        ok = copy_to_clipboard(endpoint)
        send_desktop_notification(
            "Splitch",
            f"Copied {endpoint}" if ok else f"API endpoint: {endpoint}",
        )

    def _post_control(self, payload: dict[str, Any]) -> None:
        def _worker() -> None:
            try:
                r = httpx.post(
                    f"{self.router_url}/api/control",
                    json=payload,
                    headers=self.auth_headers(),
                    timeout=5.0,
                )
                if r.status_code == 200:
                    self.poll_once_sync()
            except httpx.HTTPError as exc:
                send_desktop_notification("Splitch", f"Control update failed: {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    def action_toggle_gaming(self) -> None:
        cur = bool((self.snapshot.get("control") or {}).get("gaming_mode"))
        self._post_control({"gaming_mode": not cur})

    def action_set_force_node(self, node_id: str) -> None:
        self._post_control({"force_node": node_id})

    def action_check_updates(self) -> None:
        def _worker() -> None:
            self.update_note = "Checking updates…"
            self._notify_changed()
            try:
                up = check_update(fetch_remote=True)
                eng = get_engine_status(check_remote=True)
                parts: list[str] = []
                if up.update_available:
                    parts.append(f"Splitch update available ({up.commits_behind} behind)")
                else:
                    parts.append(f"Splitch up to date ({up.current_sha or 'local'})")
                if eng.update_available and eng.latest_llama_tag:
                    parts.append(f"llama.cpp {eng.latest_llama_tag} available")
                elif eng.latest_llama_tag:
                    parts.append(f"llama.cpp {eng.latest_llama_tag} current")
                msg = " • ".join(parts)
                self.update_note = msg
                send_desktop_notification("Splitch Updates", msg)
            except (OSError, httpx.HTTPError, RuntimeError, ValueError) as exc:
                self.update_note = f"Update check failed: {exc}"
                send_desktop_notification("Splitch Updates", self.update_note)
            self._notify_changed()

        threading.Thread(target=_worker, daemon=True).start()

    def action_open_data_dir(self) -> None:
        open_path_in_file_manager(default_data_root())

    def action_toggle_autostart(self) -> None:
        new_val = not self.cfg.autostart
        set_tray_autostart(new_val)
        self.cfg.autostart = new_val
        send_desktop_notification(
            "Splitch", "Enabled tray start on login" if new_val else "Disabled tray start on login"
        )
        self._notify_changed()

    def action_restart(self) -> None:
        def _worker() -> None:
            ok, msg = restart_splitch_service()
            if not ok:
                send_desktop_notification("Splitch", msg)
            time.sleep(1.0)
            self.poll_once_sync()

        threading.Thread(target=_worker, daemon=True).start()

    def action_stop_and_quit(self) -> None:
        def _worker() -> None:
            stop_splitch_graceful(stop_tray=False)
            self.request_quit()

        threading.Thread(target=_worker, daemon=True).start()

    def action_quit_tray_only(self) -> None:
        self.request_quit()

    # ── Declarative Menu Builder ─────────────────────────────────────────────

    def build_menu(self) -> list[TrayMenuItem]:
        """Build fresh live menu items and index click callbacks by deterministic ID."""
        items: list[TrayMenuItem] = []
        actions: dict[int, Callable[[], None]] = {}
        next_id = 10

        def add(
            label: str = "",
            *,
            enabled: bool = True,
            is_separator: bool = False,
            toggle_type: str = "",
            toggle_state: int = -1,
            children: list[TrayMenuItem] | None = None,
            on_click: Callable[[], None] | None = None,
        ) -> TrayMenuItem:
            nonlocal next_id
            mid = next_id
            next_id += 1
            item = TrayMenuItem(
                id=mid,
                label=label,
                enabled=enabled,
                is_separator=is_separator,
                toggle_type=toggle_type,
                toggle_state=toggle_state,
                children=children or [],
                on_click=on_click,
            )
            if on_click is not None:
                actions[mid] = on_click
            return item

        title, _ = self.tooltip_summary()
        items.append(add(title, enabled=False))
        items.append(add(f"Endpoint: {self.router_url}/v1", enabled=False))

        nodes = self.snapshot.get("nodes") or []
        loaded_models: list[str] = []
        for n in nodes:
            t = n.get("telemetry") or {}
            vram_tot = float(t.get("vram_total_mb") or 0.0)
            vram_used = float(t.get("vram_used_mb") or 0.0)
            g_cnt = int(t.get("gpu_count") or len(t.get("gpus") or []))
            gpu_tag = f"{g_cnt} GPUs · " if g_cnt > 1 else ""
            mem_s = f"{vram_used / 1024:.1f}/{vram_tot / 1024:.1f} GB" if vram_tot > 0 else "CPU"
            dot = "●" if (n.get("online") and not n.get("busy_reason")) else ("◐" if n.get("online") else "○")
            name = n.get("display_name") or n.get("node_id") or "node"
            st = "READY" if (n.get("online") and not n.get("busy_reason")) else (
                "OFFLINE" if not n.get("online") else str(n.get("busy_reason")).upper()
            )
            items.append(add(f"  {dot} {name} — {gpu_tag}{mem_s} ({st})", enabled=False))
            for m in t.get("models") or []:
                if isinstance(m, dict) and m.get("status") == "loaded" and m.get("id"):
                    loaded_models.append(str(m["id"]))

        if loaded_models:
            preview = ", ".join(dict.fromkeys(loaded_models))
            if len(preview) > 52:
                preview = preview[:49] + "…"
            items.append(add(f"  Loaded: {preview}", enabled=False))

        if self.update_note:
            items.append(add(f"  {self.update_note}", enabled=False))

        items.append(add(is_separator=True))
        items.append(add("Open Web UI", on_click=self.action_open_ui))
        items.append(add("Copy API Endpoint (/v1)", on_click=self.action_copy_endpoint))

        control = self.snapshot.get("control") or {}
        gaming = bool(control.get("gaming_mode"))
        force_node = str(control.get("force_node") or "auto")

        items.append(
            add(
                "Gaming Mode (Yield GPUs)",
                enabled=self.online,
                toggle_type="checkmark",
                toggle_state=1 if gaming else 0,
                on_click=self.action_toggle_gaming,
            )
        )

        # Routing Target submenu
        route_children: list[TrayMenuItem] = [
            add(
                "Auto Split",
                enabled=self.online,
                toggle_type="radio",
                toggle_state=1 if force_node == "auto" else 0,
                on_click=lambda: self.action_set_force_node("auto"),
            )
        ]
        for n in nodes:
            nid = str(n.get("node_id") or "")
            dname = str(n.get("display_name") or nid)
            if nid:
                route_children.append(
                    add(
                        dname,
                        enabled=self.online,
                        toggle_type="radio",
                        toggle_state=1 if force_node == nid else 0,
                        on_click=lambda _nid=nid: self.action_set_force_node(_nid),
                    )
                )
        items.append(add("Routing Target", enabled=self.online, children=route_children))

        items.append(add(is_separator=True))
        items.append(add("Check for Updates…", on_click=self.action_check_updates))
        items.append(add("Open Data & Logs Folder", on_click=self.action_open_data_dir))
        items.append(
            add(
                "Start Tray on Login",
                toggle_type="checkmark",
                toggle_state=1 if self.cfg.autostart else 0,
                on_click=self.action_toggle_autostart,
            )
        )

        items.append(add(is_separator=True))
        restart_label = (
            f"Restart Local Agent ({self.cfg.local_service})"
            if (self.cfg.mode == "remote" and self.cfg.local_service)
            else "Start / Restart Splitch"
        )
        items.append(add(restart_label, on_click=self.action_restart))
        stop_label = (
            "Stop Local Agent & Quit"
            if self.cfg.mode == "remote"
            else "Graceful Shutdown & Quit"
        )
        items.append(add(stop_label, on_click=self.action_stop_and_quit))
        items.append(add("Quit Tray Only", on_click=self.action_quit_tray_only))

        self._action_map = actions
        return items

    def trigger_item(self, item_id: int) -> None:
        cb = self._action_map.get(item_id)
        if cb is not None:
            cb()

    # ── Live State Subscription (WebSocket with HTTP fallback) ───────────────

    def poll_once_sync(self) -> None:
        try:
            r = httpx.get(
                f"{self.router_url}/api/status",
                headers=self.auth_headers(),
                timeout=3.0,
            )
            if r.status_code == 200:
                self.snapshot = r.json()
                self.online = True
                self._notify_changed()
                return
        except (httpx.HTTPError, ValueError):
            pass
        if self.online:
            self.online = False
            self._notify_changed()

    async def watch_status_loop(self) -> None:
        """Async WebSocket subscriber to `/ws/status` (auto-reconnects cleanly)."""
        import websockets

        ws_base = self.router_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_base}/ws/status"
        if self.api_key:
            ws_url = f"{ws_url}?token={self.api_key}"

        backoff = 1.5
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(
                    ws_url,
                    open_timeout=4.0,
                    ping_interval=20.0,
                    ping_timeout=10.0,
                ) as ws:
                    backoff = 1.5
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        try:
                            data = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(data, dict):
                            self.snapshot = data
                            self.online = True
                            self._notify_changed()
            except (OSError, httpx.HTTPError, TimeoutError, ValueError, RuntimeError):
                # Fallback single HTTP check in case WS blocked by proxy
                await asyncio.to_thread(self.poll_once_sync)
                await asyncio.sleep(backoff)
                backoff = min(10.0, backoff * 1.4)


# ── Tier 2 / macOS / Windows: `pystray` + Pillow Backend ────────────────────


def _run_pystray_tray(ctrl: TrayController) -> None:
    import pystray
    from PIL import Image

    img_cache: dict[StatusBadge, Any] = {}

    def _get_image() -> Any:
        b = ctrl.status_badge()
        if b not in img_cache:
            img_cache[b] = Image.open(io.BytesIO(render_png_bytes(64, b)))
        return img_cache[b]

    def _to_pystray_items(items: list[TrayMenuItem]) -> list[Any]:
        out: list[Any] = []
        for it in items:
            if it.is_separator:
                out.append(pystray.Menu.SEPARATOR)
                continue
            if it.children:
                out.append(
                    pystray.MenuItem(
                        it.label,
                        pystray.Menu(*_to_pystray_items(it.children)),
                        enabled=it.enabled,
                    )
                )
                continue
            cb = it.on_click
            checked = (
                (lambda _item, _st=it.toggle_state: _st == 1)
                if it.toggle_type in ("checkmark", "radio")
                else None
            )
            out.append(
                pystray.MenuItem(
                    it.label,
                    (lambda _icon, _item, _cb=cb: _cb()) if cb else None,
                    enabled=it.enabled,
                    checked=checked,
                    radio=(it.toggle_type == "radio"),
                    default=(it.label == "Open Web UI"),
                )
            )
        return out

    def _build_menu() -> Any:
        return pystray.Menu(*_to_pystray_items(ctrl.build_menu()))

    icon = pystray.Icon("splitch", _get_image(), "Splitch", menu=_build_menu())

    def _sync_icon() -> None:
        title, _ = ctrl.tooltip_summary()
        icon.icon = _get_image()
        icon.title = title
        icon.menu = _build_menu()
        with contextlib.suppress(Exception):
            icon.update_menu()

    ctrl.on_change(_sync_icon)
    ctrl.on_quit(icon.stop)

    # Background asyncio WebSocket thread
    def _bg_ws() -> None:
        asyncio.run(ctrl.watch_status_loop())

    threading.Thread(target=_bg_ws, daemon=True).start()
    icon.run()


def run_tray(router_url: str | None = None) -> int:
    """Entry point for `splitch tray` with single-instance lock & automatic compositor selection."""
    pid_file = default_data_root() / "tray.pid"
    existing = read_live_pid(pid_file)
    if existing is not None and existing != os.getpid():
        print(f"[splitch] System tray is already running (PID {existing}).")
        return 0

    write_pid_file(pid_file, os.getpid())
    ctrl = TrayController(router_url=router_url)
    ctrl.poll_once_sync()

    try:
        if sys.platform.startswith("linux"):
            try:
                ran_sni = asyncio.run(run_linux_sni_tray(ctrl))
                if ran_sni:
                    return 0
            except ImportError:
                logger.warning("dbus-next not installed; falling back to pystray")
            except (OSError, RuntimeError) as exc:
                logger.warning("SNI tray backend unavailable (%s); trying pystray fallback", exc)

        try:
            _run_pystray_tray(ctrl)
            return 0
        except ImportError:
            print(
                "[splitch] System tray dependencies not installed. "
                "Install with: uv sync --extra desktop",
                file=sys.stderr,
            )
            return 1
        except (OSError, RuntimeError) as exc:
            print(f"[splitch] Tray backend error: {exc}", file=sys.stderr)
            return 1
    finally:
        if read_live_pid(pid_file) == os.getpid():
            with contextlib.suppress(OSError):
                pid_file.unlink()
