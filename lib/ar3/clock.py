"""One display clock for every ar3 app.

The suite stores time in UTC and shows time in the machine's local zone. That
split is not a style preference, it is the only arrangement in which both jobs
are done correctly:

    A timestamp that is ever sorted, compared, merged, or pruned is UTC.
    A timestamp a human or a model reads is local, and always carries its zone.

Filenames are sort keys, so they stay UTC — a roster's day-log directory is a
shared artifact whose lexicographic order must be chronological order on every
machine that writes into it, and `r4t`'s retention pass string-compares those
names. Display is display: it is read once, by someone standing in one place,
and UTC is wrong for them.

The model is the reader this matters most for. An agent handed nothing but UTC
stamps concludes it lives in UTC, and then every relative word it writes —
today, tomorrow, this morning — resolves in the wrong day.

Zone source is the machine, which means `TZ` is honored for free: a container
or a VM that boots UTC is corrected with `rig.env: {"TZ": "America/Los_Angeles"}`
rather than with a knob this module would have to invent.

Nothing here is ever called from a writer.
"""
from __future__ import annotations

from datetime import datetime, timezone

DATE_FORMAT = "%Y-%m-%d %H:%M"
DATE_FORMAT_SECONDS = "%Y-%m-%d %H:%M:%S"
TIME_FORMAT_SECONDS = "%H:%M:%S"

# `%Z` gives `PDT` on macOS and Linux but `Pacific Daylight Time` on Windows,
# and a bare numeric offset (`+07`, `+0845`) for the zones that have no
# abbreviation at all. Only an alphabetic abbreviation of this length is worth
# printing as-is; everything else renders as `UTC-07:00`, which is longer,
# unambiguous, and needs no lookup table. There is no stdlib way to recover the
# local IANA key, and the suite's dependency rule forbids a package for it.
MAX_ABBREVIATION = 5


def local_now() -> datetime:
    """Now, aware, in the machine's local zone."""
    return datetime.now().astimezone()


def to_local(ts: str | datetime | None) -> datetime | None:
    """Convert a stored stamp to the machine's local zone.

    Accepts the suite's stored ISO forms (`2026-08-16T20:07:00Z`, an explicit
    offset, or a bare naive stamp) and `datetime` objects. A naive value is
    read as UTC, because everything this suite stores naive is stored UTC.
    Returns None for anything unparseable — a caller showing a foreign or
    corrupt stamp passes it through verbatim rather than inventing a time.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        text = str(ts).strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def zone_label(when: datetime | None = None) -> str:
    """The zone abbreviation for `when`, or a `UTC-07:00` offset when the
    platform has no short abbreviation to give."""
    dt = when or local_now()
    abbr = dt.strftime("%Z")
    if abbr.isalpha() and len(abbr) <= MAX_ABBREVIATION:
        return abbr
    offset = dt.strftime("%z") or "+0000"
    return f"UTC{offset[:3]}:{offset[3:5]}"


def stamp(ts: str | datetime | None = None, *, seconds: bool = False) -> str:
    """`2026-08-16 13:22 PDT` — a stored UTC stamp as its local reading.

    `ts` of None means now. `seconds=True` adds them, for log lines. An
    unparseable `ts` comes back as the caller passed it, so a display surface
    never swallows a value it could not read.
    """
    dt = local_now() if ts is None else to_local(ts)
    if dt is None:
        return str(ts)
    fmt = DATE_FORMAT_SECONDS if seconds else DATE_FORMAT
    return f"{dt.strftime(fmt)} {zone_label(dt)}"


def time_stamp(ts: str | datetime | None = None) -> str:
    """`13:22:04 PDT` — bare local time and zone, no date. For the ticker
    lines a reader watches live, where the date is today by construction."""
    dt = local_now() if ts is None else to_local(ts)
    if dt is None:
        return str(ts)
    return f"{dt.strftime(TIME_FORMAT_SECONDS)} {zone_label(dt)}"
