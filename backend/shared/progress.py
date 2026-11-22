"""
Zero-dependency modern terminal progress UI for Splitch CLI.

Features:
  - Smooth Unicode progress bars (`━`/`─`) with percentage, MB transferred, MB/s speed, and ETA
  - Live Braille spinners (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) with elapsed timer for indeterminate tasks
  - Native OSC 9;4 terminal progress reporting (Ghostty, Kitty, Windows Terminal, iTerm2, WezTerm)
  - Automatic non-TTY / `NO_COLOR` graceful degradation (clean single-line logs, no CR spam)
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Self, TextIO

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _fmt_bytes(num: float) -> str:
    if num >= 1024**3:
        return f"{num / (1024**3):.2f} GB"
    if num >= 1024**2:
        return f"{num / (1024**2):.1f} MB"
    if num >= 1024:
        return f"{num / 1024:.1f} KB"
    return f"{int(num)} B"


def _fmt_duration(seconds: float) -> str:
    if seconds < 1.0:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m{secs:02d}s"


class _NullDownloadBar:
    def advance(self, _delta: int) -> None:
        return None


class DownloadBar:
    """Streaming byte progress bar with MB/s, ETA, and OSC 9;4 updates."""

    def __init__(
        self,
        label: str,
        total_bytes: int | None,
        *,
        stream: TextIO,
        is_tty: bool,
        use_color: bool,
        osc: bool,
    ) -> None:
        self.label = label
        self.total = total_bytes if (total_bytes and total_bytes > 0) else None
        self.stream = stream
        self.is_tty = is_tty
        self.use_color = use_color
        self.osc = osc
        self.completed = 0
        self._start = time.monotonic()
        self._last_draw = 0.0
        self._frame = 0

    def _emit_osc(self, state: int, pct: int = 0) -> None:
        if self.osc:
            self.stream.write(f"\x1b]9;4;{state};{pct}\x1b\\")

    def _c(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.use_color else text

    def __enter__(self) -> Self:
        self._start = time.monotonic()
        if self.is_tty:
            self._emit_osc(1 if self.total else 3, 0)
            self._render(force=True)
        else:
            size_hint = f" ({_fmt_bytes(self.total)})" if self.total else ""
            self.stream.write(f"  ↓ {self.label}{size_hint}...\n")
            self.stream.flush()
        return self

    def advance(self, delta: int) -> None:
        if delta <= 0:
            return
        self.completed += delta
        if self.is_tty:
            self._render(force=False)

    def _render(self, *, force: bool = False, final: bool = False) -> None:
        now = time.monotonic()
        if not force and not final and (now - self._last_draw) < 0.065:
            return
        self._last_draw = now
        elapsed = max(0.001, now - self._start)
        speed = self.completed / elapsed
        cols = shutil.get_terminal_size((88, 24)).columns

        if self.total:
            ratio = min(1.0, self.completed / self.total)
            pct = int(ratio * 100)
            if not final:
                self._emit_osc(1, pct)
            eta_s = (self.total - self.completed) / speed if (speed > 0 and not final) else 0.0
            meta = (
                f" {pct:3d}%  {_fmt_bytes(self.completed)}/{_fmt_bytes(self.total)}"
                f"  {_fmt_bytes(speed)}/s"
                + (f"  {_fmt_duration(elapsed)}" if final else f"  ETA {_fmt_duration(eta_s)}")
            )
            prefix = f"  {'✓' if final else '↓'} {self.label}  "
            bar_w = max(10, min(28, cols - len(prefix) - len(meta) - 2))
            filled = round(bar_w * ratio)
            bar = self._c("35" if not final else "32", "━" * filled) + self._c(
                "90", "─" * (bar_w - filled)
            )
            icon = self._c("1;32", "✓") if final else self._c("1;35", "↓")
            line = f"  {icon} {self.label}  {bar}{self._c('90', meta) if final else meta}"
        else:
            spin = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
            self._frame += 1
            icon = self._c("1;32", "✓") if final else self._c("1;35", spin)
            meta = f" {_fmt_bytes(self.completed)}  {_fmt_bytes(speed)}/s  {_fmt_duration(elapsed)}"
            line = f"  {icon} {self.label} {meta}"

        self.stream.write(f"\r\x1b[2K{line}")
        if final:
            self.stream.write("\n")
        self.stream.flush()

    def __exit__(self, exc_type, _exc, _tb) -> None:
        elapsed = max(0.001, time.monotonic() - self._start)
        if self.is_tty:
            if exc_type is None:
                if self.total and self.completed < self.total:
                    self.completed = self.total
                self._render(force=True, final=True)
                self._emit_osc(0, 0)
            else:
                icon = self._c("1;31", "✗")
                self.stream.write(f"\r\x1b[2K  {icon} {self.label} ({_fmt_duration(elapsed)})\n")
                self._emit_osc(2, 100)
                self._emit_osc(0, 0)
            self.stream.flush()
        else:
            if exc_type is None:
                speed = self.completed / elapsed
                self.stream.write(
                    f"  ✓ {self.label} ({_fmt_bytes(self.completed)}, {_fmt_bytes(speed)}/s, {_fmt_duration(elapsed)})\n"
                )
            else:
                self.stream.write(f"  ✗ {self.label} failed\n")
            self.stream.flush()


class StepSpinner:
    """Animated Braille spinner for indeterminate CLI operations."""

    def __init__(
        self,
        label: str,
        *,
        done_label: str | None = None,
        stream: TextIO,
        is_tty: bool,
        use_color: bool,
        osc: bool,
    ) -> None:
        self.label = label
        self.done_label = done_label or label
        self.stream = stream
        self.is_tty = is_tty
        self.use_color = use_color
        self.osc = osc
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0

    def _c(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.use_color else text

    def _emit_osc(self, state: int, pct: int = 0) -> None:
        if self.osc:
            self.stream.write(f"\x1b]9;4;{state};{pct}\x1b\\")

    def _loop(self) -> None:
        idx = 0
        while not self._stop.is_set():
            frame = _SPINNER_FRAMES[idx % len(_SPINNER_FRAMES)]
            idx += 1
            elapsed = _fmt_duration(time.monotonic() - self._start)
            self.stream.write(
                f"\r\x1b[2K  {self._c('1;35', frame)} {self.label} {self._c('90', elapsed)}"
            )
            self.stream.flush()
            self._stop.wait(0.08)

    def __enter__(self) -> Self:
        self._start = time.monotonic()
        if self.is_tty:
            self._emit_osc(3, 0)
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        elapsed = _fmt_duration(time.monotonic() - self._start)
        if self.is_tty:
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=0.5)
            self._emit_osc(0, 0)
            if exc_type is None:
                self.stream.write(
                    f"\r\x1b[2K  {self._c('1;32', '✓')} {self.done_label} {self._c('90', elapsed)}\n"
                )
            else:
                self.stream.write(
                    f"\r\x1b[2K  {self._c('1;31', '✗')} {self.label} {self._c('90', elapsed)}\n"
                )
            self.stream.flush()
        else:
            mark = "✓" if exc_type is None else "✗"
            self.stream.write(f"  {mark} {self.done_label if exc_type is None else self.label} ({elapsed})\n")
            self.stream.flush()


class TerminalProgress:
    """High-level progress reporter passed into Splitch CLI operations."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        force_tty: bool | None = None,
        enable_osc: bool = True,
    ) -> None:
        self.stream = stream or sys.stderr
        if force_tty is None:
            try:
                tty = bool(self.stream.isatty()) and os.getenv("TERM", "") != "dumb"
            except (AttributeError, OSError):
                tty = False
        else:
            tty = force_tty
        self.is_tty = tty
        self.use_color = tty and not bool(os.getenv("NO_COLOR", "").strip())
        self.osc = tty and enable_osc

    @contextmanager
    def step(self, label: str, *, done_label: str | None = None) -> Iterator[StepSpinner]:
        with StepSpinner(
            label,
            done_label=done_label,
            stream=self.stream,
            is_tty=self.is_tty,
            use_color=self.use_color,
            osc=self.osc,
        ) as sp:
            yield sp

    @contextmanager
    def download(self, label: str, total_bytes: int | None = None) -> Iterator[DownloadBar]:
        with DownloadBar(
            label,
            total_bytes,
            stream=self.stream,
            is_tty=self.is_tty,
            use_color=self.use_color,
            osc=self.osc,
        ) as bar:
            yield bar

    def ok(self, msg: str) -> None:
        mark = "\x1b[1;32m✓\x1b[0m" if self.use_color else "✓"
        self.stream.write(f"  {mark} {msg}\n")
        self.stream.flush()


@contextmanager
def null_step(_label: str, *, done_label: str | None = None) -> Iterator[None]:
    del done_label
    yield None


@contextmanager
def null_download(_label: str, _total_bytes: int | None = None) -> Iterator[_NullDownloadBar]:
    yield _NullDownloadBar()
