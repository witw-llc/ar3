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
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from engines.base import QuotaError

USAGE_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
)
TIMEOUT_S = 15

# Where the IDE keeps its state database, per platform. The suffix is the same
# everywhere; only the application-data root moves. `R4T_CURSOR_STATE_DB` names
# the file outright, for a machine no rule reaches.
STATE_DB_ENV = "R4T_CURSOR_STATE_DB"
_STATE_DB_SUFFIX = ("Cursor", "User", "globalStorage", "state.vscdb")
_APP_DATA_ROOTS = {
    "darwin": [Path.home() / "Library" / "Application Support"],
    "win32": [Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")],
}
_LINUX_ROOTS = [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")]
# A WSL shell reaches the Windows side under /mnt/c, and a seat there commonly
# runs the CLI on Linux while the IDE — the only thing that holds the token —
# is installed on Windows. Measured working from such a seat, so it is worth
# looking rather than declaring the case unreachable. Every Windows profile is
# a candidate: which one holds the login is not knowable from this side.
_WSL_USERS_DIR = Path("/mnt/c/Users")


def _wsl_candidates() -> list[Path]:
    if sys.platform != "linux" or not _WSL_USERS_DIR.is_dir():
        return []
    try:
        profiles = sorted(p for p in _WSL_USERS_DIR.iterdir() if p.is_dir())
    except OSError:
        return []
    return [p / "AppData" / "Roaming" for p in profiles]


def state_db_candidates() -> list[Path]:
    """Every place the token might be, most explicit first."""
    named = os.environ.get(STATE_DB_ENV, "").strip()
    if named:
        return [Path(named).expanduser()]
    roots = _APP_DATA_ROOTS.get(sys.platform, _LINUX_ROOTS) + _wsl_candidates()
    return [root.joinpath(*_STATE_DB_SUFFIX) for root in roots]


def state_db() -> Path | None:
    """The first candidate that actually answers. Existing is not enough: a
    machine can carry several Windows profiles, and a Cursor installed under
    one of them that was never signed in has a database with no token in it."""
    return next(
        (p for p in state_db_candidates() if _token_in(p)),
        next((p for p in state_db_candidates() if p.is_file()), None),
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


ACCESS_TOKEN_KEY = "cursorAuth/accessToken"


def _token_in(db: Path) -> bool:
    return bool(db.is_file() and _read_state(db, ACCESS_TOKEN_KEY))


def _state_value(key: str) -> str | None:
    db = state_db()
    return _read_state(db, key) if db is not None else None


def _read_state(db: Path, key: str) -> str | None:
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
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
    token = _state_value(ACCESS_TOKEN_KEY)
    if not token:
        looked = ", ".join(str(p) for p in state_db_candidates())
        found = state_db()
        raise QuotaError(
            (
                f"Cursor state database has no access token: {found}"
                if found
                else f"no Cursor state database (looked in: {looked})"
            )
            + " — the token comes from the Cursor IDE, which the `cursor-agent` "
            f"CLI alone does not install (try: log in to the IDE on this "
            f"machine, or set {STATE_DB_ENV} to its state.vscdb)"
        )
    return token


def _membership_type() -> str | None:
    return _state_value("cursorAuth/stripeMembershipType")
