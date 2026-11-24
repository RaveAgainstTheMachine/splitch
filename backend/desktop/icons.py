"""
Zero-dependency Splitch icon rasterizer (PNG, ARGB32 SNI Pixmap, Windows ICO, macOS ICNS, SVG).

Renders the Splitch sheared-S badge synchronized with the user's active Appearance
theme (color scheme, surface preset, accent hex) and an optional live status dot
(`ok` emerald, `warn` amber, `offline` red, or `plain`) at arbitrary sizes.
Works with pure Python stdlib (`struct` + `zlib`) so icon generation never fails
even when Pillow is absent.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import math
import os
import re
import struct
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

StatusBadge = Literal["plain", "ok", "warn", "offline"]
RgbTuple = tuple[int, int, int]

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_BADGE_COLORS: dict[StatusBadge, RgbTuple | None] = {
    "plain": None,
    "ok": (52, 211, 153),       # Emerald #34d399
    "warn": (245, 158, 11),     # Amber #f59e0b
    "offline": (248, 113, 113), # Red #f87171
}


@dataclass(frozen=True)
class IconPalette:
    is_light: bool
    accent_hex: str
    surface: str
    bg: RgbTuple
    bg_hex: str
    border: RgbTuple
    border_hex: str
    upper_start: RgbTuple
    upper_start_hex: str
    upper_end: RgbTuple
    upper_end_hex: str
    lower_start: RgbTuple
    lower_start_hex: str
    lower_mid: RgbTuple
    lower_mid_hex: str
    lower_end: RgbTuple
    lower_end_hex: str
    tag: str


def _clamp_byte(n: float) -> int:
    return max(0, min(255, round(n)))


def _hex_to_rgb(hex_color: str) -> RgbTuple:
    clean = hex_color.lstrip("#")
    return (int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16))


def _rgb_to_hex(rgb: RgbTuple) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _mix_rgb(a: RgbTuple, b: RgbTuple, ratio_a: float) -> RgbTuple:
    ra = max(0.0, min(1.0, ratio_a))
    rb = 1.0 - ra
    return (
        _clamp_byte(a[0] * ra + b[0] * rb),
        _clamp_byte(a[1] * ra + b[1] * rb),
        _clamp_byte(a[2] * ra + b[2] * rb),
    )


def normalize_accent_hex(accent: str | None) -> str:
    raw = (accent or "").strip().lower()
    return raw if _HEX_COLOR_RE.fullmatch(raw) else ""


def _extract_theme_fields(theme: Any) -> tuple[str, str, str, bool]:
    if theme is None:
        return "system", "", "default", False
    if isinstance(theme, dict):
        scheme = str(theme.get("scheme") or "system").strip().lower()
        accent = normalize_accent_hex(str(theme.get("accent") or ""))
        surface = str(theme.get("surface") or "default").strip().lower()
        resolved_light = bool(theme.get("resolved_light", False))
    else:
        scheme = str(getattr(theme, "scheme", "system") or "system").strip().lower()
        accent = normalize_accent_hex(str(getattr(theme, "accent", "") or ""))
        surface = str(getattr(theme, "surface", "default") or "default").strip().lower()
        resolved_light = bool(getattr(theme, "resolved_light", False))
    if scheme not in ("system", "dark", "light"):
        scheme = "system"
    if surface not in ("default", "oled", "slate", "charcoal"):
        surface = "default"
    return scheme, accent, surface, resolved_light


def resolve_icon_palette(theme: Any = None) -> IconPalette:
    """Mirror `dashboard/src/utils/favicon.ts` color math 1:1 for all desktop & tray icons."""
    scheme, raw_accent, surface, resolved_light = _extract_theme_fields(theme)
    is_light = scheme == "light" or (scheme == "system" and resolved_light)
    accent_hex = raw_accent or ("#5b21b6" if is_light else "#a78bfa")
    accent_rgb = _hex_to_rgb(accent_hex)

    if is_light:
        bg_hex = "#f6f7fb"
        border_hex = "#d7dbe8"
        upper_start_hex = "#121521"
        upper_end_hex = "#262a3d"
        upper_start = _hex_to_rgb(upper_start_hex)
        upper_end = _hex_to_rgb(upper_end_hex)
    else:
        if surface == "oled":
            bg_hex, border_hex = "#000000", "#222536"
        elif surface == "slate":
            bg_hex, border_hex = "#0b1120", "#334155"
        elif surface == "charcoal":
            bg_hex, border_hex = "#141416", "#383842"
        else:
            bg_hex, border_hex = "#0a0b10", "#262a3d"
        upper_start_hex = "#ffffff"
        upper_start = (255, 255, 255)
        upper_end = _mix_rgb(accent_rgb, (255, 255, 255), 0.18)
        upper_end_hex = _rgb_to_hex(upper_end)

    lower_start = _mix_rgb(accent_rgb, (255, 255, 255), 0.82)
    lower_end = _mix_rgb(accent_rgb, (15, 23, 42), 0.72)

    key = f"{int(is_light)}:{accent_hex}:{surface}"
    tag = hashlib.sha256(key.encode("ascii")).hexdigest()[:8]

    return IconPalette(
        is_light=is_light,
        accent_hex=accent_hex,
        surface=surface,
        bg=_hex_to_rgb(bg_hex),
        bg_hex=bg_hex,
        border=_hex_to_rgb(border_hex),
        border_hex=border_hex,
        upper_start=upper_start,
        upper_start_hex=upper_start_hex,
        upper_end=upper_end,
        upper_end_hex=upper_end_hex,
        lower_start=lower_start,
        lower_start_hex=_rgb_to_hex(lower_start),
        lower_mid=accent_rgb,
        lower_mid_hex=accent_hex,
        lower_end=lower_end,
        lower_end_hex=_rgb_to_hex(lower_end),
        tag=tag,
    )


def theme_tag(theme: Any = None) -> str:
    """Deterministic 8-char hash uniquely identifying the effective icon palette."""
    return resolve_icon_palette(theme).tag


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


def _blend(
    dst: tuple[int, int, int, int],
    src: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    sa = src[3] / 255.0
    if sa <= 0.0:
        return dst
    if sa >= 1.0:
        return src
    da = dst[3] / 255.0
    out_a = sa + da * (1.0 - sa)
    if out_a <= 0.0:
        return (0, 0, 0, 0)
    r = int((src[0] * sa + dst[0] * da * (1.0 - sa)) / out_a)
    g = int((src[1] * sa + dst[1] * da * (1.0 - sa)) / out_a)
    b = int((src[2] * sa + dst[2] * da * (1.0 - sa)) / out_a)
    return (min(255, r), min(255, g), min(255, b), min(255, int(out_a * 255)))


def _cubic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    steps: int = 36,
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / float(steps)
        mt = 1.0 - t
        x = (
            mt * mt * mt * p0[0]
            + 3.0 * mt * mt * t * p1[0]
            + 3.0 * mt * t * t * p2[0]
            + t * t * t * p3[0]
        )
        y = (
            mt * mt * mt * p0[1]
            + 3.0 * mt * mt * t * p1[1]
            + 3.0 * mt * t * t * p2[1]
            + t * t * t * p3[1]
        )
        pts.append((x, y))
    return pts


def _build_smooth_s_segments() -> tuple[tuple[float, float, float, float, float], ...]:
    """Precompute segment geometry `(x1, y1, dx, dy, inv_l2)` for fast squared-distance lookup."""
    segs = [
        ((44.0, 21.5), (44.0, 15.8), (39.0, 13.0), (32.0, 13.0)),
        ((32.0, 13.0), (24.5, 13.0), (20.0, 16.2), (20.0, 21.5)),
        ((20.0, 21.5), (20.0, 27.0), (25.0, 29.2), (32.5, 31.0)),
        ((32.5, 31.0), (40.0, 32.8), (45.0, 35.2), (45.0, 42.0)),
        ((45.0, 42.0), (45.0, 48.0), (39.5, 51.0), (31.5, 51.0)),
        ((31.5, 51.0), (24.0, 51.0), (19.0, 48.0), (19.0, 42.5)),
    ]
    pts: list[tuple[float, float]] = []
    for idx, (p0, p1, p2, p3) in enumerate(segs):
        sampled = _cubic_bezier(p0, p1, p2, p3, steps=20)
        pts.extend(sampled if idx == 0 else sampled[1:])
    compiled: list[tuple[float, float, float, float, float]] = []
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        dx = x2 - x1
        dy = y2 - y1
        l2 = dx * dx + dy * dy
        compiled.append((x1, y1, dx, dy, (1.0 / l2) if l2 > 0.0 else 0.0))
    return tuple(compiled)


_S_SEGS: tuple[tuple[float, float, float, float, float], ...] = _build_smooth_s_segments()


def _s_curve_dist(x: float, y: float) -> float:
    # Fast bounding-box rejection around the S stroke (x in [12, 52], y in [6, 58])
    if x < 12.0 or x > 52.0 or y < 6.0 or y > 58.0:
        return 99.0
    best_sq = 9999.0
    for x1, y1, dx, dy, inv_l2 in _S_SEGS:
        px = x - x1
        py = y - y1
        if inv_l2 > 0.0:
            t = (px * dx + py * dy) * inv_l2
            if t < 0.0:
                t = 0.0
            elif t > 1.0:
                t = 1.0
            qx = px - t * dx
            qy = py - t * dy
        else:
            qx = px
            qy = py
        d2 = qx * qx + qy * qy
        best_sq = min(best_sq, d2)
    return math.sqrt(best_sq)


def render_rgba_pixels(
    size: int,
    status: StatusBadge = "plain",
    *,
    theme: Any = None,
) -> bytearray:
    """Render a crisp `size x size` RGBA pixel buffer of the Splitch icon."""
    pal = resolve_icon_palette(theme)
    buf = bytearray(size * size * 4)
    scale = 64.0 / float(size)
    half_stroke = 5.15
    badge_rgb = _BADGE_COLORS[status]
    bg_r, bg_g, bg_b = pal.bg
    bd_r, bd_g, bd_b = pal.border
    us_r, us_g, us_b = pal.upper_start
    ue_r, ue_g, ue_b = pal.upper_end
    ls_r, ls_g, ls_b = pal.lower_start
    lm_r, lm_g, lm_b = pal.lower_mid
    le_r, le_g, le_b = pal.lower_end

    for py in range(size):
        vy = (py + 0.5) * scale
        cy = 14.0 if vy < 14.0 else (min(vy, 50.0))
        dy_c = vy - cy
        upper_zone = vy < 30.6
        lower_zone = vy > 33.4
        row_off = py * size * 4
        for px in range(size):
            vx = (px + 0.5) * scale

            # Rounded-square background (rx=14 in 64x64)
            cx = 14.0 if vx < 14.0 else (min(vx, 50.0))
            d_corner = math.hypot(vx - cx, dy_c)
            bg_cov = (14.0 - d_corner) / scale + 0.5
            if bg_cov <= 0.0:
                continue
            bg_cov = min(bg_cov, 1.0)

            alpha_bg = int(bg_cov * 255)
            if d_corner > 12.2:
                col = (bd_r, bd_g, bd_b, alpha_bg)
            else:
                col = (bg_r, bg_g, bg_b, alpha_bg)

            # Sheared upper half vs accent-tinted lower half
            if upper_zone:
                ux = vx + 1.8
                in_bar = (12.5 <= ux <= 41.5) and (28.8 <= vy <= 30.6)
                if in_bar:
                    cov = 1.0
                else:
                    d_s = _s_curve_dist(ux, vy)
                    cov = (half_stroke - d_s) / scale + 0.5
                if cov > 0.0:
                    cov = min(cov, 1.0)
                    tu = max(0.0, min(1.0, (vy - 8.0) / 24.0))
                    ur = int(_lerp(us_r, ue_r, tu))
                    ug = int(_lerp(us_g, ue_g, tu))
                    ub = int(_lerp(us_b, ue_b, tu))
                    col = _blend(col, (ur, ug, ub, int(cov * 255)))
            elif lower_zone:
                lx = vx - 1.8
                in_bar = (22.5 <= lx <= 51.5) and (33.4 <= vy <= 35.2)
                if in_bar:
                    cov = 1.0
                else:
                    d_s = _s_curve_dist(lx, vy)
                    cov = (half_stroke - d_s) / scale + 0.5
                if cov > 0.0:
                    cov = min(cov, 1.0)
                    tl = max(0.0, min(1.0, (vy - 33.4) / 22.0))
                    if tl <= 0.5:
                        k = tl * 2.0
                        sr = int(_lerp(ls_r, lm_r, k))
                        sg = int(_lerp(ls_g, lm_g, k))
                        sb = int(_lerp(ls_b, lm_b, k))
                    else:
                        k = (tl - 0.5) * 2.0
                        sr = int(_lerp(lm_r, le_r, k))
                        sg = int(_lerp(lm_g, le_g, k))
                        sb = int(_lerp(lm_b, le_b, k))
                    col = _blend(col, (sr, sg, sb, int(cov * 255)))

            # Optional status dot in bottom-right corner with surface cutout ring
            if badge_rgb is not None:
                db = math.hypot(vx - 51.0, vy - 51.0)
                if db <= 11.8:
                    ring_cov = max(0.0, min(1.0, (11.5 - db) / scale + 0.5))
                    col = _blend(col, (bg_r, bg_g, bg_b, int(ring_cov * 255)))
                if db <= 9.2:
                    dot_cov = max(0.0, min(1.0, (8.6 - db) / scale + 0.5))
                    col = _blend(col, (badge_rgb[0], badge_rgb[1], badge_rgb[2], int(dot_cov * 255)))

            idx = row_off + px * 4
            buf[idx] = col[0]
            buf[idx + 1] = col[1]
            buf[idx + 2] = col[2]
            buf[idx + 3] = col[3]
    return buf


def render_argb32_pixmap(
    size: int,
    status: StatusBadge = "plain",
    *,
    theme: Any = None,
) -> tuple[int, int, bytes]:
    """Return `(width, height, argb32_big_endian_bytes)` for StatusNotifierItem `IconPixmap`."""
    rgba = render_rgba_pixels(size, status, theme=theme)
    argb = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        argb[i] = rgba[i + 3]      # A
        argb[i + 1] = rgba[i]      # R
        argb[i + 2] = rgba[i + 1]  # G
        argb[i + 3] = rgba[i + 2]  # B
    return size, size, bytes(argb)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Atomically write binary file via same-directory temp file + `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
        if sys.platform != "win32":
            with contextlib.suppress(OSError):
                tmp_path.chmod(mode)
        os.replace(tmp_path, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    """Atomically write UTF-8 text file via same-directory temp file + `os.replace`."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


@functools.lru_cache(maxsize=96)
def _render_png_cached(
    size: int,
    status: StatusBadge,
    is_light: bool,
    accent_hex: str,
    surface: str,
) -> bytes:
    theme_dict = {
        "scheme": "light" if is_light else "dark",
        "accent": accent_hex,
        "surface": surface,
        "resolved_light": is_light,
    }
    rgba = render_rgba_pixels(size, status, theme=theme_dict)
    stride = size * 4
    raw_rows = bytearray()
    for y in range(size):
        raw_rows.append(0)  # Filter type 0 (None)
        raw_rows.extend(rgba[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def render_png_bytes(
    size: int,
    status: StatusBadge = "plain",
    *,
    theme: Any = None,
) -> bytes:
    """Encode a valid RGBA PNG using pure Python stdlib (`zlib` + `struct`), cached per palette."""
    pal = resolve_icon_palette(theme)
    return _render_png_cached(size, status, pal.is_light, pal.accent_hex, pal.surface)


def render_ico_bytes(status: StatusBadge = "plain", *, theme: Any = None) -> bytes:
    """Build a multi-resolution Windows `.ico` (16/32/48/64/256px PNG-encoded frames for HiDPI)."""
    sizes = (16, 32, 48, 64, 256)
    pngs = [render_png_bytes(s, status, theme=theme) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = bytearray()
    for s, png in zip(sizes, pngs, strict=True):
        entries.extend(
            struct.pack(
                "<BBBBHHII",
                s if s < 256 else 0,
                s if s < 256 else 0,
                0,
                0,
                1,
                32,
                len(png),
                offset,
            )
        )
        offset += len(png)
    return header + bytes(entries) + b"".join(pngs)


def render_icns_bytes(status: StatusBadge = "plain", *, theme: Any = None) -> bytes:
    """Build a native Apple `.icns` container with multi-resolution PNG chunks (16–256px)."""
    chunk_specs: tuple[tuple[bytes, int], ...] = (
        (b"icp4", 16),
        (b"icp5", 32),
        (b"icp6", 64),
        (b"ic07", 128),
        (b"ic08", 256),
    )
    body = bytearray()
    for tag_bytes, px in chunk_specs:
        png = render_png_bytes(px, status, theme=theme)
        body.extend(tag_bytes)
        body.extend(struct.pack(">I", 8 + len(png)))
        body.extend(png)
    return b"icns" + struct.pack(">I", 8 + len(body)) + bytes(body)


def render_svg_text(*, theme: Any = None) -> str:
    """
    Return a QtSvg / SVG-Tiny-1.2 & librsvg compatible SVG of the Splitch icon.
    Uses explicit width/height and direct filled paths (no `<clipPath>` or root `fill="none"`).
    """
    pal = resolve_icon_palette(theme)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 64 64">\n'
        "  <defs>\n"
        '    <linearGradient id="splitch-upper" x1="0" y1="0" x2="1" y2="1">\n'
        f'      <stop offset="0%" stop-color="{pal.upper_start_hex}" />\n'
        f'      <stop offset="100%" stop-color="{pal.upper_end_hex}" />\n'
        "    </linearGradient>\n"
        '    <linearGradient id="splitch-accent" x1="0" y1="0" x2="1" y2="1">\n'
        f'      <stop offset="0%" stop-color="{pal.lower_start_hex}" />\n'
        f'      <stop offset="50%" stop-color="{pal.lower_mid_hex}" />\n'
        f'      <stop offset="100%" stop-color="{pal.lower_end_hex}" />\n'
        "    </linearGradient>\n"
        "  </defs>\n"
        f'  <rect x="0" y="0" width="64" height="64" rx="14" ry="14" fill="{pal.bg_hex}" />\n'
        f'  <rect x="1" y="1" width="62" height="62" rx="13" ry="13" fill="none" stroke="{pal.border_hex}" stroke-width="1.6" />\n'
        "  <!-- Upper sheared S half -->\n"
        '  <path fill="url(#splitch-upper)" d="M 30.2 7.75 C 40.2 7.75 47.45 12.2 47.45 21.5 L 36.95 21.5 '
        "C 36.95 18.6 34.4 17.6 30.2 17.6 C 25.8 17.6 23.45 19.0 23.45 21.5 "
        'C 23.45 24.1 26.8 25.4 34.5 27.3 L 40.6 28.8 L 40.6 30.6 L 10.7 30.6 L 10.7 29.0 L 16.8 29.0 '
        'C 14.2 26.9 12.95 24.4 12.95 21.5 C 12.95 12.4 20.1 7.75 30.2 7.75 Z" />\n'
        "  <!-- Lower sheared S half (themed accent) -->\n"
        '  <path fill="url(#splitch-accent)" d="M 23.4 33.4 L 53.3 33.4 L 53.3 35.0 L 47.2 35.0 '
        "C 49.8 37.1 51.05 39.6 51.05 42.5 C 51.05 51.6 43.9 56.25 33.8 56.25 "
        "C 23.8 56.25 16.55 51.8 16.55 42.5 L 27.05 42.5 C 27.05 45.4 29.6 46.4 33.8 46.4 "
        'C 38.2 46.4 40.55 45.0 40.55 42.5 C 40.55 39.9 37.2 38.6 29.5 36.7 L 23.4 35.2 Z" />\n'
        "</svg>\n"
    )


def tray_icon_stem(status: StatusBadge, *, theme: Any = None) -> str:
    """Return cache-busting SNI icon stem (`splitch-ok-<tag>`) so DE panels reload immediately."""
    tag = theme_tag(theme)
    if status == "plain":
        return f"splitch-{tag}"
    return f"splitch-{status}-{tag}"


def prune_stale_icons(icons_dir: Path, *, keep_tag: str) -> list[Path]:
    """Remove obsolete hashed `splitch-*-<oldtag>.png/.ico` files while keeping `keep_tag`."""
    if not icons_dir.is_dir():
        return []
    keep = {
        "splitch.png",
        "splitch-256.png",
        "splitch-ok.png",
        "splitch-warn.png",
        "splitch-offline.png",
        "splitch.svg",
        "splitch.ico",
        f"splitch-{keep_tag}.png",
        f"splitch-256-{keep_tag}.png",
        f"splitch-ok-{keep_tag}.png",
        f"splitch-warn-{keep_tag}.png",
        f"splitch-offline-{keep_tag}.png",
        f"splitch-{keep_tag}.ico",
    }
    removed: list[Path] = []
    for entry in icons_dir.iterdir():
        if (
            entry.is_file()
            and entry.name.startswith("splitch-")
            and entry.suffix in (".png", ".ico")
            and entry.name not in keep
        ):
            with contextlib.suppress(OSError):
                entry.unlink()
                removed.append(entry)
    return removed


def ensure_tray_icon_theme(
    icons_dir: Path,
    *,
    theme: Any = None,
    prune: bool = True,
) -> Path:
    """
    Atomically materialize both canonical and cache-busted themed PNG/SVG icons for SNI
    `IconThemePath` and desktop shortcuts, optionally pruning stale hashed variants.
    """
    icons_dir.mkdir(parents=True, exist_ok=True)
    tag = theme_tag(theme)

    canonical: dict[str, tuple[int, StatusBadge]] = {
        "splitch.png": (64, "plain"),
        "splitch-256.png": (256, "plain"),
        "splitch-ok.png": (64, "ok"),
        "splitch-warn.png": (64, "warn"),
        "splitch-offline.png": (64, "offline"),
    }
    hashed: dict[str, tuple[int, StatusBadge]] = {
        f"splitch-{tag}.png": (64, "plain"),
        f"splitch-256-{tag}.png": (256, "plain"),
        f"splitch-ok-{tag}.png": (64, "ok"),
        f"splitch-warn-{tag}.png": (64, "warn"),
        f"splitch-offline-{tag}.png": (64, "offline"),
    }

    # Render each unique (size, status) once and atomically replace both canonical + hashed copies
    rendered: dict[tuple[int, StatusBadge], bytes] = {}
    for fname, key in {**hashed, **canonical}.items():
        if key not in rendered:
            rendered[key] = render_png_bytes(key[0], key[1], theme=theme)
        atomic_write_bytes(icons_dir / fname, rendered[key])

    atomic_write_text(icons_dir / "splitch.svg", render_svg_text(theme=theme))

    if prune:
        prune_stale_icons(icons_dir, keep_tag=tag)
    return icons_dir
