"""Devin quota — the interactive `/usage` view, read through a terminal.

Devin CLI 3000.10.67 reports the plan's allowance in one place only: the
`/usage` slash command of an interactive session, which draws a Daily and a
Weekly bar with percent used and the reset. Print mode serves slash commands
without a model turn but refuses `/usage`, `devin auth status` names the plan
and no allowance, and API v3 reports ACUs spent per organization, not what a
seat has left.

So this module starts `devin` in a pseudo-terminal in an empty temporary
directory, types `/usage`, reads the two bars off the screen, and ends the
process group. No prompt reaches a model, so no turn is spent, and r4t never
reads the CLI's credentials: the CLI fetches its own numbers.
`--respect-workspace-trust false` keeps the trust dialog away, so no directory
is added to the CLI's trusted list; the CLI still writes its own log file for
the launch, as any start does. The screen is not a contract, so the parse
is defensive: one bar is enough, and a screen with no usable bar is a
QuotaError. POSIX only: Windows has no `pty`.
"""
from __future__ import annotations

import os
import re
import select
import shutil
import struct
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone

from engines.base import QuotaError

BOOT_TIMEOUT_S = 20
USAGE_TIMEOUT_S = 20
SCREEN_ROWS, SCREEN_COLS = 50, 120

_ESCAPES = re.compile(
    r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[()][A-Za-z0-9]"
    r"|\x1b[=>78]"
)
_READY = "Ask Devin"
_BAR = re.compile(
    r"\b(Daily|Weekly)\b[^%\n]{0,80}?(\d{1,3}(?:\.\d+)?)%\s*used"
    r"\s*·\s*resets\s+(in\s+(?:\d+\s*[dhm]\s*)+|[A-Z][a-z]{2}\s+\d{1,2},\s*"
    r"\d{1,2}:\d{2}\s*[AP]M\s*\(UTC[+-]\d{1,2}(?::\d{2})?\))"
)
_PLAN = re.compile(r"·\s*([A-Za-z][A-Za-z0-9 +]*?)\s*·\s*\d{1,3}%\s*remaining")
_FAILURES = (
    "No quota data available",
    "Timed out fetching quota data",
    "Failed to fetch quota",
    "Not logged in",
)
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}


def quota() -> dict:
    return parse_usage_screen(_usage_screen(), now=time.time())


def screen_text(raw: bytes) -> str:
    return _ESCAPES.sub("", raw.decode("utf-8", "replace")).replace("\r", "\n")


def parse_usage_screen(text: str, *, now: float) -> dict:
    """The newest Daily and Weekly bars on the screen. The view redraws as it
    fills in, so a later match replaces an earlier one."""
    bars: dict[str, tuple[float, str]] = {}
    for match in _BAR.finditer(text):
        bars[match.group(1)] = (float(match.group(2)), match.group(3))
    if not bars:
        failure = next((f for f in _FAILURES if f in text), None)
        raise QuotaError(
            f"devin /usage: {failure}" if failure
            else "devin /usage drew no Daily or Weekly bar"
        )
    buckets = [
        {
            "label": f"{name} Limit",
            "remaining_fraction": max(0.0, min(1.0, 1.0 - used / 100)),
            "reset_time": reset_time(phrase, now=now),
        }
        for name in ("Daily", "Weekly")
        if name in bars
        for used, phrase in [bars[name]]
    ]
    plans = _PLAN.findall(text)
    return {
        "origin": "live",
        "plan": plans[-1].strip() if plans else None,
        "buckets": buckets,
        "note": "read from the interactive /usage view; the screen carries no contract",
    }


def reset_time(phrase: str, *, now: float) -> str | None:
    """`in 6h 50m` counts from `now`; `Oct 18, 1:00 AM (UTC-7)` names no
    year, so it is the first such moment no more than a day in the past."""
    phrase = " ".join(phrase.split())
    if phrase.startswith("in "):
        units = dict(
            (unit, int(n)) for n, unit in re.findall(r"(\d+)\s*([dhm])", phrase)
        )
        delta = timedelta(
            days=units.get("d", 0), hours=units.get("h", 0), minutes=units.get("m", 0)
        )
        when = datetime.fromtimestamp(now + delta.total_seconds(), tz=timezone.utc)
        return when.replace(second=0, microsecond=0).isoformat()
    match = re.match(
        r"([A-Z][a-z]{2}) (\d{1,2}), ?(\d{1,2}):(\d{2}) ?([AP])M \(UTC([+-]\d{1,2})(?::(\d{2}))?\)",
        phrase,
    )
    if not match or match.group(1) not in _MONTHS:
        return None
    month, day, hour, minute, half, off_h, off_m = match.groups()
    hour = int(hour) % 12 + (12 if half == "P" else 0)
    sign = -1 if off_h.startswith("-") else 1
    zone = timezone(sign * timedelta(hours=abs(int(off_h)), minutes=int(off_m or 0)))
    year = datetime.fromtimestamp(now, tz=zone).year
    for candidate_year in (year - 1, year, year + 1):
        try:
            when = datetime(candidate_year, _MONTHS[month], int(day), hour, int(minute), tzinfo=zone)
        except ValueError:
            continue
        if when.timestamp() >= now - 86400:
            return when.astimezone(timezone.utc).isoformat()
    return None


def _usage_screen() -> str:
    try:
        import fcntl
        import pty  # noqa: F401  (POSIX-only; its absence is the Windows answer)
        import termios
    except ImportError as exc:
        raise QuotaError(
            "devin quota reads the interactive /usage view through a "
            "pseudo-terminal, which this platform does not have"
        ) from exc
    from ar3.proc import terminate_group

    binary = shutil.which("devin")
    if binary is None:
        raise QuotaError("devin is not on PATH")
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", SCREEN_ROWS, SCREEN_COLS, 0, 0))
    env = {**os.environ, "TERM": os.environ.get("TERM") or "xterm-256color"}
    with tempfile.TemporaryDirectory(prefix="r4t-devin-quota-") as cwd:
        try:
            proc = subprocess.Popen(
                [binary, "--respect-workspace-trust", "false"],
                cwd=cwd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(slave)
            raise QuotaError(f"cannot run devin: {exc}") from exc
        os.close(slave)
        raw = bytearray()
        try:
            if not _read_until(master, raw, BOOT_TIMEOUT_S, lambda t: _READY in t):
                raise QuotaError(
                    f"devin's interactive prompt did not appear in {BOOT_TIMEOUT_S}s"
                )
            mark = len(raw)
            os.write(master, b"/usage")
            _read_until(master, raw, 1.0, lambda t: False)
            os.write(master, b"\r")
            _read_until(
                master,
                raw,
                USAGE_TIMEOUT_S,
                lambda t: {m.group(1) for m in _BAR.finditer(t)} == {"Daily", "Weekly"}
                or any(f in t for f in _FAILURES),
                start=mark,
            )
        finally:
            terminate_group(proc)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            os.close(master)
        return screen_text(bytes(raw[mark:]))


def _read_until(fd: int, raw: bytearray, seconds: float, done, *, start: int = 0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return False
            if not chunk:
                return False
            raw.extend(chunk)
            if done(screen_text(bytes(raw[start:]))):
                return True
    return False
