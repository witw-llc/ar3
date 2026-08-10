"""Copilot quota — the entitlement endpoint every IDE extension reads.

GitHub documents no user-scoped quota API; `/copilot_internal/user` is the
internal endpoint the official extensions poll, reachable with a plain
GitHub OAuth token. `gh` supplies both the token and the transport. On
token-based-billing seats the fraction fields are degenerate
(`unlimited: true`, 0/0/100) — the answer there is cumulative credits spent
plus the reset date, and the fraction is None on purpose.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from engines.base import QuotaError

TIMEOUT_S = 15

BUCKET_LABELS = {
    "chat": "Chat",
    "completions": "Completions",
    "premium_interactions": "Premium Requests",
}


def quota() -> dict:
    if not shutil.which("gh"):
        raise QuotaError("gh is not on PATH (the token and transport come from gh)")
    try:
        proc = subprocess.run(
            ["gh", "api", "/copilot_internal/user"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise QuotaError(f"gh api did not answer in {TIMEOUT_S}s") from exc
    except OSError as exc:
        raise QuotaError(f"cannot run gh: {exc}") from exc
    if proc.returncode != 0:
        raise QuotaError(
            f"gh api /copilot_internal/user failed: {proc.stderr.strip() or 'no detail'}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise QuotaError("copilot endpoint returned non-JSON") from exc
    return parse_user(payload)


def parse_user(payload: dict) -> dict:
    reset = payload.get("quota_reset_date_utc") or payload.get("quota_reset_date")
    snapshots = payload.get("quota_snapshots") or {}
    buckets = []
    note = None
    for key, snap in snapshots.items():
        if not isinstance(snap, dict):
            continue
        unlimited = bool(snap.get("unlimited"))
        percent = snap.get("percent_remaining")
        buckets.append(
            {
                "label": BUCKET_LABELS.get(key, key),
                "remaining_fraction": (
                    None
                    if unlimited or not isinstance(percent, (int, float))
                    else max(0.0, min(1.0, percent / 100))
                ),
                "reset_time": reset,
            }
        )
        if unlimited and snap.get("credits_used"):
            spent = f"{key}: {snap['credits_used']} credits used this cycle"
            note = f"{note}; {spent}" if note else spent
    if not buckets:
        raise QuotaError("copilot endpoint answered without quota_snapshots")
    return {
        "origin": "live",
        "plan": payload.get("copilot_plan"),
        "buckets": buckets,
        "note": note,
    }
