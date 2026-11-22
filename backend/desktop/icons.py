"""
Zero-dependency Splitch icon rasterizer (PNG, ARGB32 SNI Pixmap, Windows ICO).

Renders the Splitch sheared-S obsidian badge with an optional live status dot
(`ok` emerald, `warn` amber, `offline` red, or `plain`) at arbitrary sizes.
Works with pure Python stdlib (`struct` + `zlib`) so icon generation never fails
even when Pillow is absent.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path
from typing import Literal

StatusBadge = Literal["plain", "ok", "warn", "offline"]

_BADGE_COLORS: dict[StatusBadge, tuple[int, int, int] | None] = {
    "plain": None,
    "ok": (52, 211, 153),      # Emerald #34d399
    "warn": (245, 158, 11),    # Amber #f59e0b
    "offline": (248, 113, 113),  # Red #f87171
}


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


def _dist_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


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


def _build_smooth_s_points() -> tuple[tuple[float, float], ...]:
    """Sample exact cubic Beziers of the Splitch S curve in 64x64 viewBox space."""
    segs = [
        ((44.0, 21.5), (44.0, 15.8), (39.0, 13.0), (32.0, 13.0)),
        ((32.0, 13.0), (24.5, 13.0), (20.0, 16.2), (20.0, 21.5)),
        ((20.0, 21.5), (20.0, 27.0), (25.0, 29.2), (32.5, 31.0)),
        ((32.5, 31.0), (40.0, 32.8), (45.0, 35.2), (45.0, 42.0)),
        ((45.0, 42.0), (45.0, 48.0), (39.5, 51.0), (31.5, 51.0)),
        ((31.5, 51.0), (24.0, 51.0), (19.0, 48.0), (19.0, 42.5)),
    ]
    out: list[tuple[float, float]] = []
    for idx, (p0, p1, p2, p3) in enumerate(segs):
        pts = _cubic_bezier(p0, p1, p2, p3, steps=20)
        out.extend(pts if idx == 0 else pts[1:])
    return tuple(out)


_S_POINTS: tuple[tuple[float, float], ...] = _build_smooth_s_points()


def _s_curve_dist(x: float, y: float) -> float:
    best = 999.0
    for i in range(len(_S_POINTS) - 1):
        x1, y1 = _S_POINTS[i]
        x2, y2 = _S_POINTS[i + 1]
        d = _dist_segment(x, y, x1, y1, x2, y2)
        best = min(best, d)
    return best


def render_rgba_pixels(size: int, status: StatusBadge = "plain") -> bytearray:
    """Render a crisp `size x size` RGBA pixel buffer of the Splitch icon."""
    buf = bytearray(size * size * 4)
    scale = 64.0 / float(size)
    half_stroke = 5.15
    badge_rgb = _BADGE_COLORS[status]

    for py in range(size):
        vy = (py + 0.5) * scale
        for px in range(size):
            vx = (px + 0.5) * scale

            # Rounded-square obsidian background (rx=14 in 64x64)
            cx = max(14.0, min(50.0, vx))
            cy = max(14.0, min(50.0, vy))
            d_corner = math.hypot(vx - cx, vy - cy)
            bg_cov = max(0.0, min(1.0, (14.0 - d_corner) / scale + 0.5))
            if bg_cov <= 0.0:
                continue

            # Subtle border ring
            if d_corner > 12.2:
                col = (46, 51, 74, int(bg_cov * 255))
            else:
                col = (10, 11, 16, int(bg_cov * 255))

            # Sheared upper half (white) vs lower half (amber #f59e0b -> #fbbf24)
            if vy < 30.6:
                d_s = _s_curve_dist(vx + 1.8, vy)
                # Horizontal top shear bar
                in_bar = (12.5 <= vx + 1.8 <= 41.5) and (28.8 <= vy <= 30.6)
                cov = 1.0 if in_bar else max(0.0, min(1.0, (half_stroke - d_s) / scale + 0.5))
                if cov > 0.0:
                    col = _blend(col, (245, 246, 252, int(cov * 255)))
            elif vy > 33.4:
                d_s = _s_curve_dist(vx - 1.8, vy)
                in_bar = (22.5 <= vx - 1.8 <= 51.5) and (33.4 <= vy <= 35.2)
                cov = 1.0 if in_bar else max(0.0, min(1.0, (half_stroke - d_s) / scale + 0.5))
                if cov > 0.0:
                    t = (vy - 33.4) / 22.0
                    sr = int(_lerp(251, 217, t))
                    sg = int(_lerp(191, 119, t))
                    sb = int(_lerp(36, 6, t))
                    col = _blend(col, (sr, sg, sb, int(cov * 255)))

            # Optional status dot in bottom-right corner with dark cutout ring
            if badge_rgb is not None:
                db = math.hypot(vx - 51.0, vy - 51.0)
                if db <= 11.8:
                    ring_cov = max(0.0, min(1.0, (11.5 - db) / scale + 0.5))
                    col = _blend(col, (10, 11, 16, int(ring_cov * 255)))
                if db <= 9.2:
                    dot_cov = max(0.0, min(1.0, (8.6 - db) / scale + 0.5))
                    col = _blend(col, (badge_rgb[0], badge_rgb[1], badge_rgb[2], int(dot_cov * 255)))

            idx = (py * size + px) * 4
            buf[idx] = col[0]
            buf[idx + 1] = col[1]
            buf[idx + 2] = col[2]
            buf[idx + 3] = col[3]
    return buf


def render_argb32_pixmap(size: int, status: StatusBadge = "plain") -> tuple[int, int, bytes]:
    """Return `(width, height, argb32_big_endian_bytes)` for StatusNotifierItem `IconPixmap`."""
    rgba = render_rgba_pixels(size, status)
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


def render_png_bytes(size: int, status: StatusBadge = "plain") -> bytes:
    """Encode a valid RGBA PNG using pure Python stdlib (`zlib` + `struct`)."""
    rgba = render_rgba_pixels(size, status)
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


def render_ico_bytes(status: StatusBadge = "plain") -> bytes:
    """Build a multi-resolution Windows `.ico` containing PNG-encoded 32x32 and 64x64 images."""
    sizes = (32, 64)
    pngs = [render_png_bytes(s, status) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = bytearray()
    for s, png in zip(sizes, pngs, strict=True):
        entries.extend(
            struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0, 0, 0, 1, 32, len(png), offset)
        )
        offset += len(png)
    return header + bytes(entries) + b"".join(pngs)


def render_svg_text() -> str:
    """
    Return a QtSvg / SVG-Tiny-1.2 & librsvg compatible SVG of the Splitch icon.
    Uses explicit width/height and direct filled paths (no `<clipPath>` or root `fill="none"`).
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 64 64">\n'
        "  <defs>\n"
        '    <linearGradient id="splitch-amber" x1="0" y1="0" x2="1" y2="1">\n'
        '      <stop offset="0%" stop-color="#fbbf24" />\n'
        '      <stop offset="55%" stop-color="#f59e0b" />\n'
        '      <stop offset="100%" stop-color="#d97706" />\n'
        "    </linearGradient>\n"
        "  </defs>\n"
        '  <rect x="0" y="0" width="64" height="64" rx="14" ry="14" fill="#0a0b10" />\n'
        '  <rect x="1" y="1" width="62" height="62" rx="13" ry="13" fill="none" stroke="#2e334a" stroke-width="1.6" />\n'
        "  <!-- Upper sheared S half (white) -->\n"
        '  <path fill="#f5f6fc" d="M 30.2 7.75 C 40.2 7.75 47.45 12.2 47.45 21.5 L 36.95 21.5 '
        "C 36.95 18.6 34.4 17.6 30.2 17.6 C 25.8 17.6 23.45 19.0 23.45 21.5 "
        'C 23.45 24.1 26.8 25.4 34.5 27.3 L 40.6 28.8 L 40.6 30.6 L 10.7 30.6 L 10.7 29.0 L 16.8 29.0 '
        'C 14.2 26.9 12.95 24.4 12.95 21.5 C 12.95 12.4 20.1 7.75 30.2 7.75 Z" />\n'
        "  <!-- Lower sheared S half (amber) -->\n"
        '  <path fill="url(#splitch-amber)" d="M 23.4 33.4 L 53.3 33.4 L 53.3 35.0 L 47.2 35.0 '
        "C 49.8 37.1 51.05 39.6 51.05 42.5 C 51.05 51.6 43.9 56.25 33.8 56.25 "
        "C 23.8 56.25 16.55 51.8 16.55 42.5 L 27.05 42.5 C 27.05 45.4 29.6 46.4 33.8 46.4 "
        'C 38.2 46.4 40.55 45.0 40.55 42.5 C 40.55 39.9 37.2 38.6 29.5 36.7 L 23.4 35.2 Z" />\n'
        "</svg>\n"
    )


def ensure_tray_icon_theme(icons_dir: Path) -> Path:
    """Materialize PNG status icons for SNI `IconThemePath` and desktop shortcuts."""
    icons_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, tuple[int, StatusBadge]] = {
        "splitch.png": (64, "plain"),
        "splitch-256.png": (256, "plain"),
        "splitch-ok.png": (64, "ok"),
        "splitch-warn.png": (64, "warn"),
        "splitch-offline.png": (64, "offline"),
    }
    for fname, (px, badge) in mapping.items():
        target = icons_dir / fname
        target.write_bytes(render_png_bytes(px, badge))
    (icons_dir / "splitch.svg").write_text(render_svg_text(), encoding="utf-8")
    return icons_dir
