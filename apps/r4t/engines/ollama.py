"""Ollama quota — local models have no cloud quota to run out of."""
from __future__ import annotations

from engines.base import QuotaNotApplicable


def quota(live: bool = False) -> dict:
    if live:
        raise QuotaNotApplicable(
            "ollama runs local models and has no subscription quota to read"
        )
    return {
        "origin": "live",
        "plan": "local",
        "buckets": [
            {"label": "Local", "remaining_fraction": 1.0, "reset_time": None}
        ],
        "note": "local models — the machine is the only limit",
    }
