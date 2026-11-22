# ruff: noqa: F821
"""
Native Linux StatusNotifierItem + com.canonical.dbusmenu backend (`dbus-next`).

NOTE: Intentionally omits `from __future__ import annotations` because `dbus-next`
inspects raw string literals in `__annotations__` (`'s'`, `'u(ia{sv}av)'`, etc.).
"""

import asyncio
import contextlib
import os
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.desktop.tray import TrayController, TrayMenuItem

SNI_WATCHERS = (
    ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher"),
    (
        "org.freedesktop.StatusNotifierWatcher",
        "/StatusNotifierWatcher",
        "org.freedesktop.StatusNotifierWatcher",
    ),
)


async def run_linux_sni_tray(ctrl: "TrayController") -> bool:
    """
    Export StatusNotifierItem + com.canonical.dbusmenu over session D-Bus.
    Returns False only if no SNI watcher is present on pure X11 so caller can try XEmbed.
    """
    from dbus_next import BusType, DBusError, Message, Variant
    from dbus_next.aio import MessageBus
    from dbus_next.constants import PropertyAccess
    from dbus_next.service import ServiceInterface, dbus_property, method
    from dbus_next.service import signal as dbus_signal

    from backend.desktop.icons import ensure_tray_icon_theme
    from backend.router.config import default_data_root

    icons_dir = ensure_tray_icon_theme(default_data_root() / "icons", theme=ctrl.theme)
    bus = await MessageBus(bus_type=BusType.SESSION).connect()

    async def _list_bus_names() -> set[str]:
        reply = await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="ListNames",
            )
        )
        if reply and reply.body and isinstance(reply.body[0], list):
            return set(reply.body[0])
        return set()

    names = await _list_bus_names()
    has_watcher = any(w[0] in names for w in SNI_WATCHERS)
    if not has_watcher and os.getenv("DISPLAY") and not os.getenv("WAYLAND_DISPLAY"):
        # Pure legacy X11 WM (i3bar, dwm, openbox/tint2, stalonetray) -> hand off to pystray XEmbed
        bus.disconnect()
        return False

    bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

    def _item_props(item: "TrayMenuItem") -> dict[str, Variant]:
        if item.is_separator:
            return {"type": Variant("s", "separator")}
        props: dict[str, Variant] = {
            "label": Variant("s", item.label),
            "enabled": Variant("b", item.enabled),
            "visible": Variant("b", True),
        }
        if item.toggle_type:
            props["toggle-type"] = Variant("s", item.toggle_type)
            props["toggle-state"] = Variant("i", item.toggle_state)
        if item.children:
            props["children-display"] = Variant("s", "submenu")
        return props

    def _item_to_layout(
        item: "TrayMenuItem", depth: int
    ) -> list[object]:
        props = _item_props(item)
        child_variants: list[Variant] = []
        if item.children and depth != 0:
            next_depth = depth - 1 if depth > 0 else -1
            for ch in item.children:
                child_variants.append(Variant("(ia{sv}av)", _item_to_layout(ch, next_depth)))
        return [item.id, props, child_variants]

    def _find_item(items: "list[TrayMenuItem]", target_id: int) -> "TrayMenuItem | None":
        for it in items:
            if it.id == target_id:
                return it
            if it.children:
                hit = _find_item(it.children, target_id)
                if hit is not None:
                    return hit
        return None

    class SplitchDBusMenu(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("com.canonical.dbusmenu")
            self._items = ctrl.build_menu()

        def refresh(self) -> None:
            self._items = ctrl.build_menu()
            self.LayoutUpdated(ctrl.revision, 0)

        @dbus_property(access=PropertyAccess.READ)
        def Version(self) -> "u":  # type: ignore[valid-type]
            return 3

        @dbus_property(access=PropertyAccess.READ)
        def TextDirection(self) -> "s":  # type: ignore[valid-type]
            return "ltr"

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":  # type: ignore[valid-type]
            return "normal"

        @dbus_property(access=PropertyAccess.READ)
        def IconThemePath(self) -> "as":  # type: ignore[valid-type]
            return [str(icons_dir)]

        @method()
        def GetLayout(
            self, parent_id: "i", recursion_depth: "i", property_names: "as"  # type: ignore[valid-type]
        ) -> "u(ia{sv}av)":  # type: ignore[valid-type]
            self._items = ctrl.build_menu()
            if parent_id == 0:
                children = [
                    Variant("(ia{sv}av)", _item_to_layout(it, recursion_depth))
                    for it in self._items
                ]
                return [ctrl.revision, [0, {"children-display": Variant("s", "submenu")}, children]]
            target = _find_item(self._items, parent_id)
            if target is None:
                return [ctrl.revision, [parent_id, {}, []]]
            return [ctrl.revision, _item_to_layout(target, recursion_depth)]

        @method()
        def GetGroupProperties(
            self, ids: "ai", property_names: "as"  # type: ignore[valid-type]
        ) -> "a(ia{sv})":  # type: ignore[valid-type]
            out = []
            for mid in ids:
                if mid == 0:
                    out.append([0, {"children-display": Variant("s", "submenu")}])
                else:
                    it = _find_item(self._items, mid)
                    if it is not None:
                        out.append([mid, _item_props(it)])
            return out

        @method()
        def GetProperty(self, id: "i", name: "s") -> "v":  # type: ignore[valid-type]
            it = _find_item(self._items, id)
            if it is None:
                return Variant("s", "")
            return _item_props(it).get(name, Variant("s", ""))

        @method()
        def Event(
            self, id: "i", event_id: "s", data: "v", timestamp: "u"  # type: ignore[valid-type]
        ) -> None:
            if event_id == "clicked":
                ctrl.trigger_item(id)

        @method()
        def EventGroup(self, events: "a(isvu)") -> "ai":  # type: ignore[valid-type]
            for mid, ev_id, _data, _ts in events:
                if ev_id == "clicked":
                    ctrl.trigger_item(mid)
            return []

        @method()
        def AboutToShow(self, id: "i") -> "b":  # type: ignore[valid-type]
            self._items = ctrl.build_menu()
            return True

        @method()
        def AboutToShowGroup(self, ids: "ai") -> "aiai":  # type: ignore[valid-type]
            self._items = ctrl.build_menu()
            return [list(ids), []]

        @dbus_signal()
        def ItemsPropertiesUpdated(
            self, updated: "a(ia{sv})", removed: "a(ias)"  # type: ignore[valid-type]
        ) -> "a(ia{sv})a(ias)":  # type: ignore[valid-type]
            return [updated, removed]

        @dbus_signal()
        def LayoutUpdated(self, revision: "u", parent: "i") -> "ui":  # type: ignore[valid-type]
            return [revision, parent]

        @dbus_signal()
        def ItemActivationRequested(self, id: "i", timestamp: "u") -> "iu":  # type: ignore[valid-type]
            return [id, timestamp]

    class SplitchStatusNotifierItem(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.kde.StatusNotifierItem")

        @dbus_property(access=PropertyAccess.READ)
        def Category(self) -> "s":  # type: ignore[valid-type]
            return "ApplicationStatus"

        @dbus_property(access=PropertyAccess.READ)
        def Id(self) -> "s":  # type: ignore[valid-type]
            return "splitch"

        @dbus_property(access=PropertyAccess.READ)
        def Title(self) -> "s":  # type: ignore[valid-type]
            return "Splitch"

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":  # type: ignore[valid-type]
            return "Active"

        @dbus_property(access=PropertyAccess.READ)
        def WindowId(self) -> "u":  # type: ignore[valid-type]
            return 0

        @dbus_property(access=PropertyAccess.READ)
        def IconThemePath(self) -> "s":  # type: ignore[valid-type]
            return str(icons_dir)

        @dbus_property(access=PropertyAccess.READ)
        def IconName(self) -> "s":  # type: ignore[valid-type]
            return ctrl.icon_name()

        @dbus_property(access=PropertyAccess.READ)
        def IconPixmap(self) -> "a(iiay)":  # type: ignore[valid-type]
            return [[w, h, data] for w, h, data in ctrl.icon_pixmaps()]

        @dbus_property(access=PropertyAccess.READ)
        def OverlayIconName(self) -> "s":  # type: ignore[valid-type]
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def OverlayIconPixmap(self) -> "a(iiay)":  # type: ignore[valid-type]
            return []

        @dbus_property(access=PropertyAccess.READ)
        def AttentionIconName(self) -> "s":  # type: ignore[valid-type]
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def AttentionIconPixmap(self) -> "a(iiay)":  # type: ignore[valid-type]
            return []

        @dbus_property(access=PropertyAccess.READ)
        def AttentionMovieName(self) -> "s":  # type: ignore[valid-type]
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def ToolTip(self) -> "(sa(iiay)ss)":  # type: ignore[valid-type]
            title, desc = ctrl.tooltip_summary()
            return [ctrl.icon_name(), [], title, desc]

        @dbus_property(access=PropertyAccess.READ)
        def ItemIsMenu(self) -> "b":  # type: ignore[valid-type]
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Menu(self) -> "o":  # type: ignore[valid-type]
            return "/MenuBar"

        @method()
        def Activate(self, x: "i", y: "i") -> None:  # type: ignore[valid-type]
            ctrl.action_open_ui()

        @method()
        def SecondaryActivate(self, x: "i", y: "i") -> None:  # type: ignore[valid-type]
            ctrl.action_toggle_gaming()

        @method()
        def ContextMenu(self, x: "i", y: "i") -> None:  # type: ignore[valid-type]
            pass

        @method()
        def Scroll(self, delta: "i", orientation: "s") -> None:  # type: ignore[valid-type]
            pass

        @dbus_signal()
        def NewTitle(self) -> None:
            return None

        @dbus_signal()
        def NewIcon(self) -> None:
            return None

        @dbus_signal()
        def NewAttentionIcon(self) -> None:
            return None

        @dbus_signal()
        def NewOverlayIcon(self) -> None:
            return None

        @dbus_signal()
        def NewToolTip(self) -> None:
            return None

        @dbus_signal()
        def NewStatus(self, status: "s") -> "s":  # type: ignore[valid-type]
            return status

    menu_iface = SplitchDBusMenu()
    sni_iface = SplitchStatusNotifierItem()
    bus.export("/MenuBar", menu_iface)
    bus.export("/StatusNotifierItem", sni_iface)
    await bus.request_name(bus_name)

    async def _register_with_watchers() -> None:
        current = await _list_bus_names()
        for dest, path, iface in SNI_WATCHERS:
            if dest in current:
                with contextlib.suppress(DBusError, OSError, RuntimeError):
                    await bus.call(
                        Message(
                            destination=dest,
                            path=path,
                            interface=iface,
                            member="RegisterStatusNotifierItem",
                            signature="s",
                            body=[bus_name],
                        )
                    )

    await _register_with_watchers()

    dbus_intro = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
    dbus_obj = bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", dbus_intro)
    dbus_iface = dbus_obj.get_interface("org.freedesktop.DBus")
    loop = asyncio.get_running_loop()

    def _on_name_owner_changed(name: str, _old: str, new_owner: str) -> None:
        if new_owner and name in {w[0] for w in SNI_WATCHERS}:
            loop.create_task(_register_with_watchers())

    dbus_iface.on_name_owner_changed(_on_name_owner_changed)

    def _push_updates() -> None:
        def _emit() -> None:
            sni_iface.NewIcon()
            sni_iface.NewToolTip()
            menu_iface.refresh()

        loop.call_soon_threadsafe(_emit)

    ctrl.on_change(_push_updates)

    stop_fut: asyncio.Future[None] = loop.create_future()

    def _signal_stop() -> None:
        if not stop_fut.done():
            stop_fut.set_result(None)

    ctrl.on_quit(lambda: loop.call_soon_threadsafe(_signal_stop))

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, ctrl.request_quit)

    ws_task = asyncio.create_task(ctrl.watch_status_loop())
    try:
        await stop_fut
    finally:
        ws_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ws_task
        bus.disconnect()
    return True
