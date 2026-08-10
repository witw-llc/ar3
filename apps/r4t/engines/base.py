"""Shared vocabulary for engine components."""
from __future__ import annotations

from datetime import datetime, timezone


class QuotaError(Exception):
    """The check could not answer. The message says what to do about it."""


def iso_from_unix(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def window_label(minutes: int | None) -> str:
    if minutes == 300:
        return "Five Hour Limit"
    if minutes == 10080:
        return "Weekly Limit"
    if minutes == 43200:
        return "Monthly Limit"
    return f"{minutes} Min Limit" if minutes else "Quota"
