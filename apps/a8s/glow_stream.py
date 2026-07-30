"""Incremental markdown rendering via glow — backs `a8s convo --glow`."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time


def _in_fenced_code_block(text: str) -> bool:
    return text.count("```") % 2 == 1


def _paragraph_flush_end(text: str) -> int:
    best = 0
    start = 0
    while True:
        idx = text.find("\n\n", start)
        if idx == -1:
            break
        best = idx + 2
        start = idx + 2
    return best


def safe_markdown_flush_end(text: str) -> int:
    """Bytes safe to render without waiting for more input."""
    if not text:
        return 0

    if _in_fenced_code_block(text):
        fence_start = text.rfind("```")
        before = text[:fence_start]
        if before.count("```") % 2 == 0:
            prose_flush = _paragraph_flush_end(before)
            if prose_flush:
                return prose_flush
        return 0

    best = _paragraph_flush_end(text)
    if best < len(text):
        tail = text[best:]
        if tail.rstrip().endswith("```") and tail.count("```") >= 2:
            return len(text)
    return best


def resolve_glow_style(theme: str = "auto") -> str:
    if theme != "auto":
        return theme

    cli_theme = os.environ.get("CLITHEME", "").strip().split(":")[0]
    if cli_theme in ("dark", "light"):
        return cli_theme

    lum = _query_terminal_background_luminance()
    if lum is not None:
        return "light" if lum >= 32768 else "dark"

    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg:
        bg = colorfgbg.rsplit(";", 1)[-1]
        if bg.isdigit():
            n = int(bg)
            if n in (0, 1, 2, 3, 4, 5, 6, 8):
                return "dark"
            if n in (7, 9, 10, 11, 12, 13, 14, 15):
                return "light"

    return "dark"


def _parse_osc11_rgb(data: bytes) -> tuple[int, int, int] | None:
    text = data.decode("latin-1", errors="replace")
    match = re.search(r"11;(?:rgb:)?([^\\a\x1b]+)", text)
    if not match:
        return None
    spec = match.group(1).strip()
    if spec.startswith("#"):
        hex_rgb = spec[1:]
        if len(hex_rgb) < 6:
            return None
        parts = [hex_rgb[i:i + 2] for i in (0, 2, 4)]
    else:
        parts = spec.split("/")
        if len(parts) != 3:
            return None

    try:
        rgb = tuple(_osc_color_component(part) for part in parts)
    except ValueError:
        return None
    return rgb


def _osc_color_component(part: str) -> int:
    part = part.strip().lower()
    if not part:
        return 0
    if len(part) == 1:
        part = part * 4
    elif len(part) == 2:
        part = part * 2
    elif len(part) == 3:
        part = f"{part[0]}{part[0]}{part[1]}{part[1]}{part[2]}{part[2]}"
    return int(part[:4], 16)


def _query_terminal_background_luminance() -> int | None:
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return None
    try:
        import select
        import termios
        import tty as tty_mod
    except ImportError:
        return None

    fd = sys.stdin.fileno()
    old = None
    reply = b""
    try:
        old = termios.tcgetattr(fd)
        tty_mod.setraw(fd, termios.TCSANOW)
        os.write(sys.stdout.fileno(), b"\033]11;?\007")
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.02)
            if not ready:
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            reply += chunk
            if b"\a" in reply or b"\x1b\\" in reply:
                break
    except OSError:
        return None
    finally:
        if old is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except OSError:
                pass

    rgb = _parse_osc11_rgb(reply)
    if rgb is None:
        return None
    r, g, b = rgb
    return (299 * r + 587 * g + 114 * b) // 1000


def _glow_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("CLICOLOR_FORCE", "1")
    env.setdefault("COLORTERM", "truecolor")
    return env


def _glow_width() -> int | None:
    if not sys.stdout.isatty():
        return None
    try:
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    except OSError:
        return None
    if cols <= 0:
        return None
    return min(cols, 120)


class GlowStream:
    """Render markdown incrementally via glow at safe chunk boundaries."""

    def __init__(self, stream, glow_bin: str, theme: str = "auto"):
        self._stream = stream
        self._glow = glow_bin
        self._style = resolve_glow_style(theme)
        self._width = _glow_width()
        self._buffer = ""
        self._rendered = 0
        self._closed = False

    def write(self, text: str) -> int:
        if not text or self._closed:
            return 0
        self._buffer += text
        self._flush_safe()
        return len(text)

    def flush(self) -> None:
        self._flush_safe(final=False)

    def finalize(self) -> None:
        self._flush_safe(final=True)
        # Drop rendered prefix so long-lived streams (a8s convo -f) do not grow
        # without bound after each forced per-entry finalize.
        if self._rendered:
            self._buffer = self._buffer[self._rendered :]
            self._rendered = 0

    def _flush_safe(self, final: bool = False) -> None:
        while True:
            pending = self._buffer[self._rendered:]
            end = len(pending) if final else safe_markdown_flush_end(pending)
            if not end:
                break
            chunk = pending[:end]
            self._rendered += end
            rendered = self._run_glow(chunk)
            if rendered and self._stream:
                try:
                    self._stream.write(rendered)
                    if not rendered.endswith("\n"):
                        self._stream.write("\n")
                    self._stream.flush()
                except (BrokenPipeError, OSError):
                    self._stream = None
                    self._closed = True
                    break

    def _run_glow(self, markdown: str) -> str:
        cmd = [self._glow, "--style", self._style]
        if self._width:
            cmd.extend(["-w", str(self._width)])
        cmd.append("-")
        try:
            proc = subprocess.run(
                cmd,
                input=markdown,
                capture_output=True,
                text=True,
                check=False,
                env=_glow_subprocess_env(),
            )
        except OSError:
            return markdown
        if proc.returncode != 0 or not proc.stdout:
            return markdown
        return proc.stdout

    def close(self) -> None:
        if self._closed:
            return
        self.finalize()
        self._closed = True


def open_glow_stream(stream, theme: str = "auto") -> GlowStream:
    glow_bin = shutil.which("glow")
    if not glow_bin:
        raise FileNotFoundError("glow not found on PATH")
    return GlowStream(stream, glow_bin, theme)
