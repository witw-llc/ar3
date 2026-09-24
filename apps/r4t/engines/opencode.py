"""OpenCode quota — a dispatcher, because opencode has none of its own.

OpenCode fronts other people's subscriptions and surfaces no remaining/reset
display for any of them; even its own paid product tracks usage only in the
web console. The honest check reads which providers this machine has
authenticated through opencode and runs *their* checks.
"""
from __future__ import annotations

import json
from pathlib import Path

from engines.base import QuotaError

AUTH_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"

# opencode provider id -> engine component that owns the real check.
DELEGATES = {
    "anthropic": "claude",
    "github-copilot": "copilot",
}


def quota() -> dict:
    providers = _providers()
    if not providers:
        raise QuotaError(
            "opencode has no stored provider credentials on this machine — "
            "the quota belongs to whichever provider backs it"
        )
    from engines import MODULES  # late: the registry imports this module

    buckets = []
    notes = []
    for provider in providers:
        engine = DELEGATES.get(provider)
        if engine is None:
            notes.append(f"{provider}: no quota check exists")
            continue
        # A fan-out read is not an opt-in to spend: a delegate whose live
        # check costs a turn (muse declares QUOTA_SPENDS_TURN) is pointed at
        # its own explicit command instead of being invoked.
        if getattr(MODULES[engine], "QUOTA_SPENDS_TURN", False):
            notes.append(
                f"{provider}: {engine}'s live read spends a turn — "
                f"`r4t engine {engine} quota` mints a reading on demand"
            )
            continue
        try:
            delegated = MODULES[engine].quota()
        except QuotaError as exc:
            notes.append(f"{provider}: {exc}")
            continue
        for bucket in delegated.get("buckets") or []:
            buckets.append({**bucket, "label": f"{provider} {bucket['label']}"})
    if not buckets and notes:
        raise QuotaError("; ".join(notes))
    return {
        "origin": "live",
        "plan": None,
        "buckets": buckets,
        "note": "; ".join(notes) or None,
    }


def _providers() -> list[str]:
    try:
        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return sorted(data) if isinstance(data, dict) else []
