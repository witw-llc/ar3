"""Queued resident input, and the text a busy session submits for it later.

a8s renders each wake's prompt with an age measured at wake time. A resident
session holds that prompt while an earlier turn runs, so by submission the
age is stale. The submission text keeps every original absolute stamp,
re-dates the relative age to the moment of submission, and adds one line when
the wait was long. Held input is never dropped: reading conversation history
during the earlier turn does not prove the work was done.
"""
from __future__ import annotations

import re
import time

# Above the 20 s handshake deadlines and the bridge's poll cadence, so a wait
# this long means an earlier turn ran. That turn was long enough to read the
# message through conversation history. It is also where the age unit changes
# from seconds to minutes, so the note always names at least one minute.
HELD_NOTE_SECONDS = 60

_UNITS = (
    ("week", 7 * 86400), ("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1),
)
_UNIT_SECONDS = dict(_UNITS)
_AGE = r"\((\d+) (second|minute|hour|day|week)s? ago\)(?=: )"
# The single-message header: the first "(N units ago)" on the first line,
# before its first ": ", so the message body is never touched.
_HEADER_AGE = re.compile(r"\A((?:(?!: )[^\n])*?)" + _AGE)
# A batch prompt's per-message block line.
_BATCH_AGE = re.compile(r"(^----\n[^\s:]+ sent )" + _AGE, re.MULTILINE)


def _now() -> float:
    return time.time()


class QueuedText(str):
    """The bridge's combined prompt for one inbox drain. `parts` keeps each
    message with the wall-clock time its enqueuer stored."""

    parts: tuple[tuple[str, float | None], ...] = ()

    @classmethod
    def from_items(cls, items: list[dict]) -> QueuedText:
        text = cls("\n\n".join(item["message"] for item in items))
        text.parts = tuple(
            (item["message"],
             float(item["queued_at"])
             if isinstance(item.get("queued_at"), (int, float)) else None)
            for item in items
        )
        return text


def _age(seconds: int) -> str:
    unit, size = next((u, s) for u, s in _UNITS if seconds >= s or u == "second")
    n = seconds // size
    return f"{n} {unit}{'' if n == 1 else 's'} ago"


def _redate(message: str, waited: float) -> str:
    extra = int(waited)
    if extra <= 0:
        return message

    def shift(m: re.Match[str]) -> str:
        seconds = int(m.group(2)) * _UNIT_SECONDS[m.group(3)] + extra
        return f"{m.group(1)}({_age(seconds)})"

    redated, n = _HEADER_AGE.subn(shift, message, count=1)
    if n:
        return redated
    return _BATCH_AGE.sub(shift, message)


def held_note(waited: float) -> str:
    minutes = int(waited) // 60
    unit = "minute" if minutes == 1 else "minutes"
    return (
        f"[This input was held {minutes} {unit} while an earlier turn ran. "
        "It may already appear in conversation history you read; "
        "check before you act on it twice.]"
    )


def submission_text(text: str, now: float | None = None) -> str:
    """The text to submit for held input `text`. Plain text, and queued text
    whose parts carry no time, pass through unchanged."""
    parts = getattr(text, "parts", ())
    if not parts:
        return str(text)
    now = _now() if now is None else now
    waits = [max(0.0, now - at) for _, at in parts if at is not None]
    body = "\n\n".join(
        message if at is None else _redate(message, now - at) for message, at in parts
    )
    oldest = max(waits, default=0.0)
    if oldest >= HELD_NOTE_SECONDS:
        return f"{held_note(oldest)}\n{body}"
    return body
