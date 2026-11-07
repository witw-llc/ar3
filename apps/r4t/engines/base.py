"""Shared vocabulary for engine components."""
from __future__ import annotations

from datetime import datetime, timezone


class QuotaError(Exception):
    """The check could not answer. The message says what to do about it."""


class QuotaInvalidated(QuotaError):
    """Current evidence invalidates any saved quota reading."""


class QuotaExhausted(QuotaInvalidated):
    """A typed quota refusal that cannot safely become a reusable gauge.

    An earlier snapshot must not override this provider evidence.
    """


class LiveQuotaError(QuotaError):
    """A live-read failure whose fixed text is safe to publish: it carries no
    provider body, stderr, path or credential. Only this subtype (and
    QuotaNotApplicable) reaches `quota --live` output verbatim; any other
    QuotaError is replaced by a generic engine-specific line there."""


    def __init__(self, message: str, *, code: str = "query_failed"):
        super().__init__(message)
        self.code = code


class QuotaNotApplicable(LiveQuotaError):
    """The engine or name has no subscription quota to read, as opposed to a
    reading that failed. Only the live path distinguishes the two."""


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
