"""Cursor quota — the dashboard's own Connect-RPC call.

Cursor offers no official surface for an individual account: the docs put
usage in the web dashboard, and the Admin API is team-only. This is the
endpoint the dashboard itself calls, with the access token the IDE already
caches in its local state database. Undocumented and terms-adjacent — built
on the owner's say-so, parsed defensively because the field names carry no
contract.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from engines.base import QuotaError

USAGE_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
)
TIMEOUT_S = 15

STATE_DB = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Cursor"
    / "User"
    / "globalStorage"
    / "state.vscdb"
)


def quota() -> dict:
    token = _access_token()
    body = json.dumps({}).encode("utf-8")
    request = urllib.request.Request(
        USAGE_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Connect-Protocol-Version": "1",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise QuotaError(f"cursor dashboard endpoint returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise QuotaError(f"cursor dashboard endpoint unreachable: {exc}") from exc
    return parse_period_usage(payload, _membership_type())


def parse_period_usage(payload: dict, plan: str | None) -> dict:
    """Shape verified live 2026-08-08: `planUsage` carries percent-used for
    the auto bucket, named-model API use, and the included total;
    `billingCycleEnd` is a millisecond-epoch string. Unofficial endpoint —
    parse what is present and degrade to unknown."""
    usage = payload.get("planUsage") or {}
    reset = _cycle_end(payload)
    buckets = []
    for key, label in (
        ("totalPercentUsed", "Included Total"),
        ("autoPercentUsed", "Included Auto"),
        ("apiPercentUsed", "Included API"),
    ):
        percent = usage.get(key)
        if not isinstance(percent, (int, float)):
            continue
        buckets.append(
            {
                "label": label,
                "remaining_fraction": max(0.0, 1.0 - percent / 100),
                "reset_time": reset,
            }
        )
    if not buckets:
        raise QuotaError(
            "cursor answered but no planUsage percent fields were present "
            f"(keys: {', '.join(sorted(payload)) or 'none'})"
        )
    note = "unofficial endpoint — fields carry no contract"
    if usage.get("bonusSpend"):
        note += f"; bonus spend beyond the included limit: {usage['bonusSpend']}"
    return {
        "origin": "live",
        "plan": payload.get("membershipType") or plan,
        "buckets": buckets,
        "note": note,
    }


def _cycle_end(payload: dict) -> str | None:
    value = payload.get("billingCycleEnd")
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    return None


def _state_value(key: str) -> str | None:
    if not STATE_DB.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value.strip('"') if isinstance(value, str) else None


def _access_token() -> str:
    token = _state_value("cursorAuth/accessToken")
    if not token:
        raise QuotaError(
            "no Cursor access token in the IDE state database — is Cursor "
            "installed and logged in on this machine?"
        )
    return token


def _membership_type() -> str | None:
    return _state_value("cursorAuth/stripeMembershipType")
