"""Ollama quota — local models have no cloud quota to run out of."""
from __future__ import annotations


def quota() -> dict:
    return {
        "origin": "live",
        "plan": "local",
        "buckets": [
            {"label": "Local", "remaining_fraction": 1.0, "reset_time": None}
        ],
        "note": "local models — the machine is the only limit",
    }
