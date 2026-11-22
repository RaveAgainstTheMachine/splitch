"""
Cross-platform Desktop Shortcuts, App Menu / Start Menu / macOS .app integration,
Smart GUI Launcher (`splitch launch`), Autostart, Notifications & Graceful Shutdown.

Supports Linux (KDE Plasma, GNOME, Hyprland, Sway, XFCE, Cinnamon, MATE, LXQt,
COSMIC, i3, bspwm, AwesomeWM, etc.), macOS, and Windows with zero extra dependencies.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Any, Literal

import httpx
import psutil
from pydantic import BaseModel, Field

from backend.desktop.icons import (
    ensure_tray_icon_theme,
    render_ico_bytes,
    render_png_bytes,
    render_svg_text,
    theme_tag,
)
from backend.router.config import default_data_root
from backend.shared.models import UiThemeConfig
from backend.version import __version__ as VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
SVG_LOGO_SRC = REPO_ROOT / "docs" / "assets" / "splitch-logo.svg"
_ICON_LINE_RE = re.compile(r"^Icon=.*$", re.MULTILINE)


class DesktopConfig(BaseModel):
    mode: Literal["local", "remote"] = "local"
    router_url: str = "http://127.0.0.1:8080"
    auto_tray: bool = True
    autostart: bool = False
    desktop_icon: bool = True
    local_service: str = ""
    theme: UiThemeConfig = Field(default_factory=UiThemeConfig)


def desktop_config_path() -> Path:
    return default_data_root() / "desktop.json"


def load_desktop_config() -> DesktopConfig:
    p = desktop_config_path()
    if not p.exists():
        return DesktopConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return DesktopConfig.model_validate(raw)
    except (OSError, ValueError):
        return DesktopConfig()


def save_desktop_config(cfg: DesktopConfig) -> Path:
    p = desktop_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cfg.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            p.chmod(0o600)
    return p


def is_desktop_applicable() -> bool:
    """Return True if this host appears to have an interactive graphical desktop session."""
    if os.getenv("SPLITCH_NO_DESKTOP", "").strip().lower() in ("1", "true", "yes"):
        return False
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        return not (os.getenv("SSH_CONNECTION") and not os.getenv("TERM_PROGRAM"))
    # Linux / BSD: check Wayland, X11, or desktop session env vars
    if any(
        os.getenv(k)
        for k in (
            "WAYLAND_DISPLAY",
            "DISPLAY",
            "XDG_CURRENT_DESKTOP",
            "DESKTOP_SESSION",
            "HYPRLAND_INSTANCE_SIGNATURE",
            "SWAYSOCK",
        )
    ):
        return True
    return os.getenv("XDG_SESSION_TYPE", "").lower() in ("wayland", "x11")


def resolve_splitch_bin() -> str:
    """Resolve the most stable `splitch` executable path for desktop launcher files."""
    if sys.platform == "win32":
        managed = default_data_root() / "bin" / "splitch.cmd"
        if managed.is_file():
            return str(managed)
        venv_exe = Path(sys.executable).parent / "splitch.exe"
        if venv_exe.is_file():
            return str(venv_exe)
        return shutil.which("splitch") or str(venv_exe)

    user_shim = Path.home() / ".local" / "bin" / "splitch"
    if user_shim.is_file() and os.access(user_shim, os.X_OK):
        return str(user_shim)
    found = shutil.which("splitch")
    if found:
        return found
    venv_bin = Path(sys.executable).parent / "splitch"
    if venv_bin.is_file():
        return str(venv_bin)
    return "splitch"


def _xdg_desktop_dir() -> Path:
    if sys.platform.startswith("linux") and shutil.which("xdg-user-dir"):
        try:
            res = subprocess.run(
                ["xdg-user-dir", "DESKTOP"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            val = res.stdout.strip()
            if (
                res.returncode == 0
                and val
                and Path(val).is_dir()
                and Path(val).resolve().is_relative_to(Path.home().resolve())
            ):
                return Path(val)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return Path.home() / "Desktop"


def _detect_local_user_service() -> str:
    """Detect existing local systemd user unit for Splitch if present."""
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        return ""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    for name in ("splitch-router.service", "splitch-agent.service", "splitch-llama.service"):
        if (unit_dir / name).is_file():
            return name
    return ""


def _linux_desktop_entry_content(splitch_bin: str, icon_value: str = "splitch") -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.5\n"
        "Name=Splitch\n"
        "GenericName=Local AI Router & Control Plane\n"
        "Comment=Launch Splitch Web UI (auto-starts local server and system tray if needed)\n"
        f'Exec="{splitch_bin}" launch\n'
        f"Icon={icon_value}\n"
        "Terminal=false\n"
        "StartupNotify=true\n"
        "StartupWMClass=splitch\n"
        "Categories=Development;\n"
        "Keywords=AI;LLM;GPU;Router;Ollama;Llama;MCP;\n"
        "Actions=OpenUI;StartTray;CheckUpdates;StopSplitch;\n"
        "\n"
        "[Desktop Action OpenUI]\n"
        "Name=Open Web UI\n"
        f"Icon={icon_value}\n"
        f'Exec="{splitch_bin}" launch --no-tray\n'
        "\n"
        "[Desktop Action StartTray]\n"
        "Name=Start System Tray\n"
        f"Icon={icon_value}\n"
        f'Exec="{splitch_bin}" tray\n'
        "\n"
        "[Desktop Action CheckUpdates]\n"
        "Name=Check for Updates\n"
        f"Icon={icon_value}\n"
        f'Exec="{splitch_bin}" desktop check-updates\n'
        "\n"
        "[Desktop Action StopSplitch]\n"
        "Name=Stop Splitch\n"
        f"Icon={icon_value}\n"
        f'Exec="{splitch_bin}" stop\n'
    )


def _linux_autostart_content(splitch_bin: str, icon_value: str = "splitch") -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Splitch Tray\n"
        "Comment=Splitch cluster status monitor and system tray\n"
        f'Exec="{splitch_bin}" tray\n'
        f"Icon={icon_value}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-KDE-autostart-after=panel\n"
        "StartupNotify=false\n"
    )


def _atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Atomically replace a file in place so DE monitors (KDirWatch) reload icons without losing grid placement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None and sys.platform != "win32":
            tmp_path.chmod(mode)
        os.replace(tmp_path, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _rewrite_desktop_file_icon(desktop_file: Path, icon_value: str) -> bool:
    """Update all `Icon=` lines inside an existing `.desktop` file atomically."""
    if not desktop_file.is_file():
        return False
    try:
        old = desktop_file.read_text(encoding="utf-8")
    except OSError:
        return False
    updated = _ICON_LINE_RE.sub(f"Icon={icon_value}", old)
    if updated == old:
        return False
    _atomic_write_text(desktop_file, updated, mode=0o755)
    return True


def _refresh_linux_icon_caches(app_dir: Path | None = None) -> None:
    """Refresh Freedesktop, GTK, and KDE Plasma in-memory icon & menu caches (non-fatal)."""
    cmds: list[list[str]] = []
    if app_dir is not None and app_dir.is_dir():
        cmds.append(["update-desktop-database", str(app_dir)])
    hicolor = Path.home() / ".local" / "share" / "icons" / "hicolor"
    if (hicolor / "index.theme").is_file():
        cmds.append(["gtk-update-icon-cache", "-f", "-t", str(hicolor)])
    cmds.extend(
        [
            ["kbuildsycoca6", "--noincremental"],
            ["kbuildsycoca5", "--noincremental"],
        ]
    )
    for cmd in cmds:
        if shutil.which(cmd[0]):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5.0,
                    check=False,
                )

    # Broadcast KDE icon-change signals on session D-Bus so plasmashell flushes
    # in-memory icon pixmap caches immediately without restarting the shell
    if shutil.which("dbus-send"):
        for sig_cmd in (
            [
                "dbus-send",
                "--session",
                "--type=signal",
                "/KIconLoader",
                "org.kde.KIconLoader.iconChanged",
                "int32:0",
            ],
            [
                "dbus-send",
                "--session",
                "--type=signal",
                "/KGlobalSettings",
                "org.kde.KGlobalSettings.notifyChange",
                "int32:4",
                "int32:0",
            ],
        ):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    sig_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                    check=False,
                )


def _notify_windows_shell_icons() -> None:
    """Ask Windows Explorer to refresh desktop & Start Menu shortcut icon caches."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(AttributeError, OSError):
        # SHCNE_ASSOCCHANGED = 0x08000000, SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)  # type: ignore[attr-defined]


def _write_windows_themed_ico(icons_root: Path, theme: Any = None) -> Path:
    """Write both canonical `splitch.ico` and cache-busted `splitch-<tag>.ico`, pruning stale ones."""
    icons_root.mkdir(parents=True, exist_ok=True)
    tag = theme_tag(theme)
    ico_bytes = render_ico_bytes("plain", theme=theme)
    canonical = icons_root / "splitch.ico"
    hashed = icons_root / f"splitch-{tag}.ico"
    canonical.write_bytes(ico_bytes)
    hashed.write_bytes(ico_bytes)
    for entry in icons_root.iterdir():
        if (
            entry.is_file()
            and entry.name.startswith("splitch-")
            and entry.name.endswith(".ico")
            and entry.name != hashed.name
        ):
            with contextlib.suppress(OSError):
                entry.unlink()
    return hashed


def _update_windows_shortcut_icon(lnk_path: Path, icon_path: Path) -> bool:
    if sys.platform != "win32" or not lnk_path.is_file():
        return False
    ps_bin = shutil.which("powershell") or shutil.which("pwsh")
    if not ps_bin:
        return False

    def esc(s: str) -> str:
        return s.replace("'", "''")

    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{esc(str(lnk_path))}'); "
        f"$sc.IconLocation = '{esc(str(icon_path))},0'; "
        "$sc.Save()"
    )
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        proc = subprocess.run([ps_bin, "-NoProfile", "-Command", script], timeout=8.0, check=False)
        return proc.returncode == 0
    return False


def desktop_shortcuts_installed() -> bool:
    """Return True if any Splitch OS shortcut / app-menu entry currently exists on this host."""
    if sys.platform.startswith("linux"):
        return any(
            p.is_file()
            for p in (
                Path.home() / ".local" / "share" / "applications" / "splitch.desktop",
                _xdg_desktop_dir() / "splitch.desktop",
                Path.home() / ".config" / "autostart" / "splitch-tray.desktop",
            )
        )
    if sys.platform == "darwin":
        return (Path.home() / "Applications" / "Splitch.app").is_dir()
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA", "")
        cands = [Path.home() / "Desktop" / "Splitch.lnk"]
        if appdata:
            cands.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Splitch.lnk")
        return any(p.is_file() for p in cands)
    return False


def sync_desktop_icons(
    theme: UiThemeConfig | dict[str, Any] | None = None,
    *,
    refresh_caches: bool = True,
) -> dict:
    """
    Regenerate Splitch icon assets (tray, hicolor, desktop, app menu / Start Menu / macOS .app)
    for `theme` and update existing shortcuts atomically with cache-busted icon paths.
    """
    cfg = load_desktop_config()
    if theme is None:
        eff_theme = cfg.theme
    elif isinstance(theme, UiThemeConfig):
        eff_theme = theme
    else:
        eff_theme = UiThemeConfig.model_validate(theme)

    if cfg.theme.model_dump() != eff_theme.model_dump():
        cfg.theme = eff_theme
        save_desktop_config(cfg)

    tag = theme_tag(eff_theme)
    icons_root = ensure_tray_icon_theme(default_data_root() / "icons", theme=eff_theme)
    updated_paths: list[str] = [str(icons_root / f"splitch-256-{tag}.png")]

    if sys.platform.startswith("linux"):
        hicolor = Path.home() / ".local" / "share" / "icons" / "hicolor"
        if (hicolor / "scalable" / "apps").is_dir() or desktop_shortcuts_installed():
            svg_dst = hicolor / "scalable" / "apps" / "splitch.svg"
            svg_dst.parent.mkdir(parents=True, exist_ok=True)
            svg_dst.write_text(render_svg_text(theme=eff_theme), encoding="utf-8")
            updated_paths.append(str(svg_dst))

            for px in (48, 64, 128, 256):
                png_dst = hicolor / f"{px}x{px}" / "apps" / "splitch.png"
                png_dst.parent.mkdir(parents=True, exist_ok=True)
                png_dst.write_bytes(render_png_bytes(px, "plain", theme=eff_theme))
                updated_paths.append(str(png_dst))

            top_icon = Path.home() / ".local" / "share" / "icons" / "splitch.png"
            top_icon.parent.mkdir(parents=True, exist_ok=True)
            top_icon.write_bytes(render_png_bytes(256, "plain", theme=eff_theme))
            updated_paths.append(str(top_icon))

        abs_icon_path = str(icons_root / f"splitch-256-{tag}.png")
        app_desktop = Path.home() / ".local" / "share" / "applications" / "splitch.desktop"
        desk_file = _xdg_desktop_dir() / "splitch.desktop"
        auto_file = Path.home() / ".config" / "autostart" / "splitch-tray.desktop"
        for df in (app_desktop, desk_file, auto_file):
            if _rewrite_desktop_file_icon(df, abs_icon_path):
                updated_paths.append(str(df))

        if refresh_caches:
            _refresh_linux_icon_caches(app_desktop.parent)

    elif sys.platform == "darwin":
        app_dir = Path.home() / "Applications" / "Splitch.app"
        res_dir = app_dir / "Contents" / "Resources"
        if res_dir.is_dir():
            png_dst = res_dir / "splitch.png"
            png_dst.write_bytes(render_png_bytes(256, "plain", theme=eff_theme))
            updated_paths.append(str(png_dst))
            with contextlib.suppress(OSError):
                os.utime(app_dir, None)
            lsreg = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
            if refresh_caches and Path(lsreg).is_file():
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    subprocess.run(
                        [lsreg, "-f", str(app_dir)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=4.0,
                        check=False,
                    )

    elif sys.platform == "win32":
        hashed_ico = _write_windows_themed_ico(icons_root, theme=eff_theme)
        updated_paths.append(str(hashed_ico))
        appdata = os.getenv("APPDATA", "")
        lnks: list[Path] = [Path.home() / "Desktop" / "Splitch.lnk"]
        if appdata:
            lnks.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Splitch.lnk")
            lnks.append(
                Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Splitch Tray.lnk"
            )
        for lnk in lnks:
            if _update_windows_shortcut_icon(lnk, hashed_ico):
                updated_paths.append(str(lnk))
        if refresh_caches:
            _notify_windows_shell_icons()

    return {
        "ok": True,
        "platform": sys.platform,
        "tag": tag,
        "theme": eff_theme.model_dump(),
        "updated_paths": updated_paths,
    }


def set_tray_autostart(enabled: bool, *, splitch_bin: str | None = None) -> Path | None:
    """Enable or disable system tray login autostart across Linux, macOS, and Windows."""
    bin_path = splitch_bin or resolve_splitch_bin()
    cfg = load_desktop_config()
    cfg.autostart = enabled
    save_desktop_config(cfg)
    tag = theme_tag(cfg.theme)

    if sys.platform.startswith("linux"):
        auto_file = Path.home() / ".config" / "autostart" / "splitch-tray.desktop"
        if enabled:
            hashed_png = default_data_root() / "icons" / f"splitch-256-{tag}.png"
            icon_png = default_data_root() / "icons" / "splitch-256.png"
            icon_val = str(hashed_png) if hashed_png.is_file() else (str(icon_png) if icon_png.is_file() else "splitch")
            _atomic_write_text(auto_file, _linux_autostart_content(bin_path, icon_val), mode=0o755)
            return auto_file
        if auto_file.exists():
            auto_file.unlink()
        return None

    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.splitch.tray.plist"
        if enabled:
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                "  <key>Label</key><string>com.splitch.tray</string>\n"
                "  <key>ProgramArguments</key><array>\n"
                f"    <string>{bin_path}</string><string>tray</string>\n"
                "  </array>\n"
                "  <key>RunAtLoad</key><true/>\n"
                "  <key>KeepAlive</key><false/>\n"
                "</dict></plist>\n",
                encoding="utf-8",
            )
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(["launchctl", "load", "-w", str(plist)], timeout=4.0, check=False)
            return plist
        if plist.exists():
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(["launchctl", "unload", str(plist)], timeout=4.0, check=False)
            plist.unlink()
        return None

    if sys.platform == "win32":
        appdata = os.getenv("APPDATA", "")
        if not appdata:
            return None
        lnk = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Splitch Tray.lnk"
        if enabled:
            hashed_ico = default_data_root() / "icons" / f"splitch-{tag}.ico"
            ico = hashed_ico if hashed_ico.is_file() else (default_data_root() / "icons" / "splitch.ico")
            _create_windows_shortcut(lnk, bin_path, "tray", ico, "Splitch System Tray")
            return lnk
        if lnk.exists():
            lnk.unlink()
        return None

    return None


def _create_windows_shortcut(
    lnk_path: Path, target: str, arguments: str, icon_path: Path, description: str
) -> None:
    ps_bin = shutil.which("powershell") or shutil.which("pwsh")
    if not ps_bin:
        return
    lnk_path.parent.mkdir(parents=True, exist_ok=True)

    def esc(s: str) -> str:
        return s.replace("'", "''")
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{esc(str(lnk_path))}'); "
        f"$sc.TargetPath = '{esc(target)}'; "
        f"$sc.Arguments = '{esc(arguments)}'; "
        f"$sc.IconLocation = '{esc(str(icon_path))},0'; "
        f"$sc.Description = '{esc(description)}'; "
        "$sc.WindowStyle = 7; "
        "$sc.Save()"
    )
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run([ps_bin, "-NoProfile", "-Command", script], timeout=8.0, check=False)


def install_desktop_integration(
    *,
    desktop_icon: bool = True,
    autostart: bool = False,
    router_url: str | None = None,
    mode: Literal["local", "remote"] | None = None,
    local_service: str | None = None,
    splitch_bin: str | None = None,
    theme: UiThemeConfig | dict[str, Any] | None = None,
) -> dict:
    """Install OS Application Menu / Start Menu entry, optional Desktop Icon, icons & config."""
    bin_path = splitch_bin or resolve_splitch_bin()
    existing = load_desktop_config()
    eff_url = (router_url or existing.router_url or "http://127.0.0.1:8080").rstrip("/")
    eff_mode: Literal["local", "remote"] = (
        mode
        if mode is not None
        else ("remote" if router_url and not any(h in eff_url for h in ("127.0.0.1", "localhost")) else existing.mode)
    )
    eff_service = (
        local_service if local_service is not None else (existing.local_service or _detect_local_user_service())
    )
    eff_theme = (
        theme
        if isinstance(theme, UiThemeConfig)
        else (UiThemeConfig.model_validate(theme) if isinstance(theme, dict) else existing.theme)
    )

    cfg = DesktopConfig(
        mode=eff_mode,
        router_url=eff_url,
        auto_tray=True,
        autostart=autostart,
        desktop_icon=desktop_icon,
        local_service=eff_service,
        theme=eff_theme,
    )
    save_desktop_config(cfg)

    tag = theme_tag(eff_theme)
    icons_root = ensure_tray_icon_theme(default_data_root() / "icons", theme=eff_theme)
    installed_paths: list[str] = [str(icons_root)]

    if sys.platform.startswith("linux"):
        hicolor = Path.home() / ".local" / "share" / "icons" / "hicolor"
        svg_dst = hicolor / "scalable" / "apps" / "splitch.svg"
        svg_dst.parent.mkdir(parents=True, exist_ok=True)
        svg_dst.write_text(render_svg_text(theme=eff_theme), encoding="utf-8")
        installed_paths.append(str(svg_dst))

        for px in (48, 64, 128, 256):
            png_dst = hicolor / f"{px}x{px}" / "apps" / "splitch.png"
            png_dst.parent.mkdir(parents=True, exist_ok=True)
            png_dst.write_bytes(render_png_bytes(px, "plain", theme=eff_theme))
            installed_paths.append(str(png_dst))

        # Also place top-level fallback `~/.local/share/icons/splitch.png` per XDG Icon Theme Spec
        top_icon = Path.home() / ".local" / "share" / "icons" / "splitch.png"
        top_icon.parent.mkdir(parents=True, exist_ok=True)
        top_icon.write_bytes(render_png_bytes(256, "plain", theme=eff_theme))
        installed_paths.append(str(top_icon))

        # Remove stale icon-theme.cache if hicolor has no index.theme (prevents GTK/KDE empty cache hits)
        stale_cache = hicolor / "icon-theme.cache"
        if stale_cache.is_file() and not (hicolor / "index.theme").is_file():
            with contextlib.suppress(OSError):
                stale_cache.unlink()

        # Use explicit absolute cache-busted 256px PNG path in .desktop files so every DE/WM/Qt/GTK
        # renders the active theme color immediately without icon-name cache staleness
        abs_icon_path = str(icons_root / f"splitch-256-{tag}.png")
        entry_text = _linux_desktop_entry_content(bin_path, abs_icon_path)
        app_desktop = Path.home() / ".local" / "share" / "applications" / "splitch.desktop"
        _atomic_write_text(app_desktop, entry_text, mode=0o755)
        installed_paths.append(str(app_desktop))

        if desktop_icon:
            desk_dir = _xdg_desktop_dir()
            if desk_dir.is_dir():
                desk_file = desk_dir / "splitch.desktop"
                _atomic_write_text(desk_file, entry_text, mode=0o755)
                installed_paths.append(str(desk_file))
                if shutil.which("gio"):
                    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                        subprocess.run(
                            ["gio", "set", "-t", "string", str(desk_file), "metadata::trusted", "true"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=3.0,
                            check=False,
                        )

        _refresh_linux_icon_caches(app_desktop.parent)

    elif sys.platform == "darwin":
        app_dir = Path.home() / "Applications" / "Splitch.app"
        macos_dir = app_dir / "Contents" / "MacOS"
        res_dir = app_dir / "Contents" / "Resources"
        macos_dir.mkdir(parents=True, exist_ok=True)
        res_dir.mkdir(parents=True, exist_ok=True)
        (res_dir / "splitch.png").write_bytes(render_png_bytes(256, "plain", theme=eff_theme))
        (app_dir / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            "  <key>CFBundleName</key><string>Splitch</string>\n"
            "  <key>CFBundleDisplayName</key><string>Splitch</string>\n"
            "  <key>CFBundleIdentifier</key><string>ai.splitch.desktop</string>\n"
            f"  <key>CFBundleVersion</key><string>{VERSION}</string>\n"
            "  <key>CFBundleExecutable</key><string>splitch-launcher</string>\n"
            "  <key>CFBundleIconFile</key><string>splitch.png</string>\n"
            "  <key>LSUIElement</key><true/>\n"
            "</dict></plist>\n",
            encoding="utf-8",
        )
        launcher = macos_dir / "splitch-launcher"
        launcher.write_text(f'#!/usr/bin/env sh\nexec "{bin_path}" launch "$@"\n', encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        installed_paths.append(str(app_dir))

    elif sys.platform == "win32":
        ico = _write_windows_themed_ico(icons_root, theme=eff_theme)
        installed_paths.append(str(ico))
        appdata = os.getenv("APPDATA", "")
        if appdata:
            sm_lnk = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Splitch.lnk"
            _create_windows_shortcut(sm_lnk, bin_path, "launch", ico, "Splitch Local AI Control Plane")
            installed_paths.append(str(sm_lnk))
        if desktop_icon:
            desk_lnk = Path.home() / "Desktop" / "Splitch.lnk"
            _create_windows_shortcut(desk_lnk, bin_path, "launch", ico, "Splitch Local AI Control Plane")
            installed_paths.append(str(desk_lnk))
        _notify_windows_shell_icons()

    auto_path = set_tray_autostart(autostart, splitch_bin=bin_path)
    if auto_path:
        installed_paths.append(str(auto_path))

    return {
        "ok": True,
        "platform": sys.platform,
        "config": cfg.model_dump(),
        "installed_paths": installed_paths,
    }


def uninstall_desktop_integration() -> dict:
    """Remove all Splitch desktop shortcuts, icons, and login autostart entries cleanly."""
    removed: list[str] = []
    candidates: list[Path] = [
        Path.home() / ".local" / "share" / "applications" / "splitch.desktop",
        _xdg_desktop_dir() / "splitch.desktop",
        Path.home() / ".config" / "autostart" / "splitch-tray.desktop",
        Path.home() / ".local" / "share" / "icons" / "splitch.png",
        Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "splitch.svg",
        Path.home() / ".local" / "share" / "icons" / "hicolor" / "48x48" / "apps" / "splitch.png",
        Path.home() / ".local" / "share" / "icons" / "hicolor" / "64x64" / "apps" / "splitch.png",
        Path.home() / ".local" / "share" / "icons" / "hicolor" / "128x128" / "apps" / "splitch.png",
        Path.home() / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "splitch.png",
        Path.home() / "Library" / "LaunchAgents" / "com.splitch.tray.plist",
    ]
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA", "")
        if appdata:
            candidates.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Splitch.lnk")
            candidates.append(
                Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Splitch Tray.lnk"
            )
        candidates.append(Path.home() / "Desktop" / "Splitch.lnk")

    for p in candidates:
        if p.is_file():
            with contextlib.suppress(OSError):
                p.unlink()
                removed.append(str(p))

    mac_app = Path.home() / "Applications" / "Splitch.app"
    if mac_app.is_dir():
        shutil.rmtree(mac_app, ignore_errors=True)
        removed.append(str(mac_app))

    return {"ok": True, "removed": removed}


def desktop_integration_status() -> dict:
    cfg = load_desktop_config()
    tray_pid = read_live_pid(default_data_root() / "tray.pid")
    router_pid = read_live_pid(default_data_root() / "router.pid")
    app_menu = (
        (Path.home() / ".local" / "share" / "applications" / "splitch.desktop").is_file()
        if sys.platform.startswith("linux")
        else (Path.home() / "Applications" / "Splitch.app").is_dir()
        if sys.platform == "darwin"
        else False
    )
    desktop_shortcut = (_xdg_desktop_dir() / ("Splitch.lnk" if sys.platform == "win32" else "splitch.desktop")).is_file()
    return {
        "desktop_applicable": is_desktop_applicable(),
        "platform": sys.platform,
        "app_menu_installed": app_menu,
        "desktop_icon_installed": desktop_shortcut,
        "tray_running": tray_pid is not None,
        "tray_pid": tray_pid,
        "managed_router_pid": router_pid,
        "config": cfg.model_dump(),
    }


def read_live_pid(pid_file: Path) -> int | None:
    if not pid_file.is_file():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        if pid > 0 and psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            if proc.status() != psutil.STATUS_ZOMBIE:
                return pid
    except (OSError, ValueError, psutil.Error):
        pass
    with contextlib.suppress(OSError):
        pid_file.unlink()
    return None


def write_pid_file(pid_file: Path, pid: int) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{pid}\n", encoding="utf-8")


def send_desktop_notification(title: str, body: str) -> None:
    """Best-effort native desktop notification across Linux, macOS, and Windows."""
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["notify-send", "-a", "Splitch", "-i", "splitch", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                check=False,
            )
    elif sys.platform == "darwin" and shutil.which("osascript"):
        safe_t = title.replace('"', '\\"')
        safe_b = body.replace('"', '\\"')
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe_b}" with title "{safe_t}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                check=False,
            )


def copy_to_clipboard(text: str) -> bool:
    """Copy `text` to OS clipboard (Wayland `wl-copy`, X11 `xclip`/`xsel`, macOS `pbcopy`, Win `clip`)."""
    cmds: list[list[str]] = []
    if sys.platform.startswith("linux"):
        if os.getenv("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
            cmds.append(["wl-copy"])
        if shutil.which("xclip"):
            cmds.append(["xclip", "-selection", "clipboard"])
        if shutil.which("xsel"):
            cmds.append(["xsel", "--clipboard", "--input"])
    elif sys.platform == "darwin" and shutil.which("pbcopy"):
        cmds.append(["pbcopy"])
    elif sys.platform == "win32" and shutil.which("clip"):
        cmds.append(["clip"])

    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, input=text, text=True, timeout=3.0, check=False)
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


def open_url_in_browser(url: str) -> bool:
    if sys.platform.startswith("linux") and shutil.which("xdg-open"):
        with contextlib.suppress(OSError):
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
    if sys.platform == "darwin" and shutil.which("open"):
        with contextlib.suppress(OSError):
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return bool(webbrowser.open(url))


def open_path_in_file_manager(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("linux") and shutil.which("xdg-open"):
        with contextlib.suppress(OSError):
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    elif sys.platform == "darwin" and shutil.which("open"):
        with contextlib.suppress(OSError):
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform == "win32":
        with contextlib.suppress(OSError):
            os.startfile(str(path))  # type: ignore[attr-defined]


def _check_router_healthy(url: str, timeout: float = 1.5) -> bool:
    try:
        r = httpx.get(f"{url.rstrip('/')}/health", timeout=timeout)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def start_tray_detached(router_url: str | None = None) -> int | None:
    """Launch `splitch tray` detached if not already running; returns PID."""
    pid_file = default_data_root() / "tray.pid"
    existing = read_live_pid(pid_file)
    if existing is not None:
        return existing
    bin_path = resolve_splitch_bin()
    cmd = [bin_path, "tray"]
    if router_url:
        cmd.extend(["--url", router_url])
    log_dir = default_data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_fh = (log_dir / "tray.log").open("ab")
    try:
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_fh,
            "stderr": log_fh,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kwargs)
        return proc.pid
    except OSError:
        return None
    finally:
        log_fh.close()


def ensure_splitch_running(cfg: DesktopConfig) -> tuple[bool, str]:
    """Ensure target Splitch router (or local agent in remote mode) is running."""
    url = cfg.router_url.rstrip("/")

    if cfg.mode == "remote":
        if cfg.local_service and sys.platform.startswith("linux") and shutil.which("systemctl"):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                st = subprocess.run(
                    ["systemctl", "--user", "is-active", cfg.local_service],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
                if st.stdout.strip() != "active":
                    subprocess.run(
                        ["systemctl", "--user", "start", cfg.local_service],
                        timeout=6.0,
                        check=False,
                    )
        if _check_router_healthy(url, timeout=2.0):
            return True, f"Connected to remote Splitch router at {url}"
        return False, f"Remote Splitch router unreachable at {url}"

    if _check_router_healthy(url):
        return True, f"Splitch already running at {url}"

    # Try systemd user unit first if installed
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        unit = Path.home() / ".config" / "systemd" / "user" / "splitch-router.service"
        if unit.is_file():
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(["systemctl", "--user", "start", "splitch-router.service"], timeout=6.0, check=False)
                for _ in range(30):
                    if _check_router_healthy(url, timeout=0.8):
                        return True, f"Started splitch-router.service at {url}"
                    time.sleep(0.35)

    # Otherwise spawn detached `splitch up`
    send_desktop_notification("Splitch", "Starting local AI router…")
    bin_path = resolve_splitch_bin()
    log_dir = default_data_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_fh = (log_dir / "router.log").open("ab")
    try:
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_fh,
            "stderr": log_fh,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([bin_path, "up"], **kwargs)
        write_pid_file(default_data_root() / "router.pid", proc.pid)
    except OSError as exc:
        return False, f"Failed to launch Splitch: {exc}"
    finally:
        log_fh.close()

    for _ in range(42):
        if _check_router_healthy(url, timeout=0.8):
            return True, f"Splitch ready at {url}"
        time.sleep(0.35)
    return False, f"Timed out waiting for Splitch at {url} (check {log_dir / 'router.log'})"


def launch_splitch_gui(
    *,
    router_url: str | None = None,
    start_tray: bool = True,
    open_browser: bool = True,
) -> tuple[bool, str]:
    """Start Splitch if not running, launch system tray, and open Web UI in default browser."""
    cfg = load_desktop_config()
    if router_url:
        cfg.router_url = router_url.rstrip("/")
        if not any(h in cfg.router_url for h in ("127.0.0.1", "localhost")):
            cfg.mode = "remote"

    ok, msg = ensure_splitch_running(cfg)
    if start_tray and cfg.auto_tray and is_desktop_applicable():
        start_tray_detached(cfg.router_url)
    if open_browser:
        open_url_in_browser(cfg.router_url)
    if not ok:
        send_desktop_notification("Splitch", msg)
    return ok, msg


def restart_splitch_service() -> tuple[bool, str]:
    """Gracefully restart managed local Splitch router/agent service."""
    cfg = load_desktop_config()
    if cfg.mode == "remote":
        if cfg.local_service and sys.platform.startswith("linux") and shutil.which("systemctl"):
            res = subprocess.run(
                ["systemctl", "--user", "restart", cfg.local_service],
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
            if res.returncode == 0:
                send_desktop_notification("Splitch", f"Restarted {cfg.local_service}")
                return True, f"Restarted {cfg.local_service}"
            return False, res.stderr.strip() or f"Failed to restart {cfg.local_service}"
        return True, "Remote mode: no local service configured"

    stop_splitch_graceful(stop_tray=False)
    time.sleep(0.4)
    return ensure_splitch_running(cfg)


def stop_splitch_graceful(*, stop_tray: bool = True) -> list[str]:
    """Gracefully terminate managed Splitch router, local agent service, and optionally tray."""
    cfg = load_desktop_config()
    stopped: list[str] = []

    # 1. Stop managed `splitch up` process tree via SIGTERM -> wait -> SIGKILL
    router_pid_file = default_data_root() / "router.pid"
    rpid = read_live_pid(router_pid_file)
    if rpid is not None:
        try:
            proc = psutil.Process(rpid)
            children = proc.children(recursive=True)
            proc.terminate()
            for ch in children:
                with contextlib.suppress(psutil.Error):
                    ch.terminate()
            _, alive = psutil.wait_procs([proc, *children], timeout=5.0)
            for p in alive:
                with contextlib.suppress(psutil.Error):
                    p.kill()
            stopped.append(f"managed router (PID {rpid})")
        except psutil.Error:
            pass
        with contextlib.suppress(OSError):
            router_pid_file.unlink()

    # 2. Stop user systemd services if active
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        units = ["splitch-router.service", "splitch-agent.service"]
        if cfg.local_service and cfg.local_service not in units:
            units.append(cfg.local_service)
        for unit in units:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                st = subprocess.run(
                    ["systemctl", "--user", "is-active", unit],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
                if st.stdout.strip() == "active":
                    subprocess.run(["systemctl", "--user", "stop", unit], timeout=8.0, check=False)
                    stopped.append(unit)

    # 3. Optionally stop tray daemon
    if stop_tray:
        tray_pid_file = default_data_root() / "tray.pid"
        tpid = read_live_pid(tray_pid_file)
        if tpid is not None and tpid != os.getpid():
            with contextlib.suppress(OSError):
                os.kill(tpid, signal.SIGTERM)
                stopped.append(f"tray (PID {tpid})")
            with contextlib.suppress(OSError):
                tray_pid_file.unlink()

    if stopped:
        send_desktop_notification("Splitch", f"Stopped: {', '.join(stopped)}")
    return stopped
