"""Cursor quota — the dashboard's own Connect-RPC call.

Cursor offers no official surface for an individual account: the docs put
usage in the web dashboard, and the Admin API is team-only. This is the
endpoint the dashboard itself calls, with the access token the IDE caches in
its local state database, or the token the `agent` CLI writes to its own
auth file. Undocumented and terms-adjacent — built on the owner's say-so,
parsed defensively because the field names carry no contract.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from engines.base import QuotaError


def _path_checks():
    """Imported on use. `TestRelocatedFallbackTeardown` blocks `ar3` and never
    calls quota, so this import must not run at module import. A later call
    that finds `ar3` blocked raises QuotaError instead of ImportError."""
    try:
        from ar3.fsio import is_dir, is_file
    except ImportError as exc:
        raise QuotaError("cursor quota cannot read the filesystem") from exc

    return is_dir, is_file

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
    is_dir, _is_file = _path_checks()
    if sys.platform != "linux" or not is_dir(_WSL_USERS_DIR):
        return []
    try:
        entries = list(_WSL_USERS_DIR.iterdir())
    except OSError:
        return []
    return [
        profile / "AppData" / "Roaming"
        for profile in sorted(entries, key=lambda item: item.name)
        if is_dir(profile)
    ]


def state_db_candidates() -> list[Path]:
    """Every place the IDE token might be, most explicit first."""
    named = os.environ.get(STATE_DB_ENV, "").strip()
    if named:
        return [Path(named).expanduser()]
    roots = _APP_DATA_ROOTS.get(sys.platform, _LINUX_ROOTS) + _wsl_candidates()
    return [root.joinpath(*_STATE_DB_SUFFIX) for root in roots]


def cli_auth_candidates() -> list[Path]:
    """Where the `agent` CLI writes `auth.json`. Linux uses the file by
    default. macOS uses it when `AGENT_CLI_CREDENTIAL_STORE=file` (the
    Keychain is the usual store there). Windows uses `%APPDATA%`."""
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return [root / "Cursor" / "auth.json"]
    if sys.platform == "darwin":
        return [Path.home() / ".cursor" / "auth.json"]
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return [root / "cursor" / "auth.json"]


def state_db() -> Path | None:
    """The first candidate that actually answers. Existing is not enough: a
    machine can carry several Windows profiles, and a Cursor installed under
    one of them that was never signed in has a database with no token in it.
    A profile this process cannot stat is skipped."""
    with_token = next((p for p in state_db_candidates() if _token_in(p)), None)
    if with_token is not None:
        return with_token
    _is_dir, is_file = _path_checks()
    return next((p for p in state_db_candidates() if is_file(p)), None)


# A WSL seat can reach the Windows IDE database, the CLI auth file, or
# neither. The note says which one answered.
_TOKEN_NOTES = {
    "database": "; token from the IDE state database",
    "file": "; token from the agent CLI's auth.json",
    "keychain": "; token from the agent CLI's Keychain item",
}


class _AuthRejected(Exception):
    def __init__(self, code: int):
        self.code = code


def _login_ok(token: str) -> bool:
    """A login that can be a header value: ASCII, printable, no whitespace."""
    return bool(token) and token.isascii() and all(
        ch.isprintable() and not ch.isspace() for ch in token
    )


def _fetch_quota(token: str, source: str) -> dict:
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
    unreachable = False
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise _AuthRejected(exc.code) from None
        raise QuotaError(
            f"cursor dashboard endpoint returned HTTP {exc.code}"
        ) from exc
    except Exception:
        unreachable = True
    if unreachable:
        raise QuotaError("cursor dashboard endpoint unreachable")
    result = parse_period_usage(payload, _membership_type())
    extra = _TOKEN_NOTES.get(source)
    if extra:
        result["note"] += extra
    return result


def quota() -> dict:
    rejected: list[str] = []
    found = False
    for token, source in _token_sources():
        found = True
        if not _login_ok(token):
            rejected.append(f"cursor login in {source} is malformed")
            continue
        try:
            result = _fetch_quota(token, source)
        except _AuthRejected as exc:
            rejected.append(f"{source} (HTTP {exc.code})")
            continue
        if rejected:
            result["note"] += "; skipped rejected " + ", ".join(rejected)
        return result
    if not found:
        raise _missing_login()
    if rejected and all(item.startswith("cursor login in ") for item in rejected):
        raise QuotaError("; ".join(rejected))
    raise QuotaError(
        "cursor dashboard rejected every stored login: " + "; ".join(rejected)
    )


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
    _is_dir, is_file = _path_checks()
    return bool(is_file(db) and _read_state(db, ACCESS_TOKEN_KEY))


def _state_value(key: str) -> str | None:
    db = state_db()
    return _read_state(db, key) if db is not None else None


def _read_state(db: Path, key: str) -> str | None:
    """`mode=ro` on a Windows `state.vscdb` opened from WSL raises
    `disk I/O error`: SQLite's lock and WAL open do not work on that drvfs
    file while the IDE has it. The retry is that error text only, not a path
    prefix: the same text on another platform would retry too, and any other
    SQLite error does not. `immutable=1` reads the main file without a
    lock. It ignores an uncheckpointed WAL; the CLI auth file covers a token
    that lives only there."""
    row = None
    opened = False
    for query in ("mode=ro", "mode=ro&immutable=1"):
        try:
            with contextlib.closing(
                sqlite3.connect(f"file:{db}?{query}", uri=True)
            ) as conn:
                row = conn.execute(
                    "SELECT value FROM ItemTable WHERE key = ?", (key,)
                ).fetchone()
            opened = True
            break
        except sqlite3.OperationalError as exc:
            # Only the WSL drvfs "disk I/O error" is worth the immutable
            # retry. `no such table` or `database is locked` must fall
            # through to the next store instead of trusting a stale main file.
            if query == "mode=ro" and "disk i/o error" in str(exc).lower():
                continue
            return None
        except sqlite3.Error:
            return None
    if not opened or row is None:
        return None
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value.strip('"') if isinstance(value, str) else None


KEYCHAIN_SERVICE = "cursor-access-token"
KEYCHAIN_ACCOUNT = "cursor-user"


def _keychain_token() -> str | None:
    """The `agent` CLI's usual macOS store. Linux and Windows keep the same
    login in `auth.json` (`cli_auth_candidates`); darwin looks here only after
    that file."""
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _cli_auth_token() -> str | None:
    for path in cli_auth_candidates():
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        token = data.get("accessToken")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def _token_sources():
    """Stored logins, in try order. Later stores are read only if an earlier
    one is missing or the dashboard rejected it."""
    token = _state_value(ACCESS_TOKEN_KEY)
    if token:
        yield token, "database"
    token = _cli_auth_token()
    if token:
        yield token, "file"
    token = _keychain_token()
    if token:
        yield token, "keychain"


def _missing_login() -> QuotaError:
    looked = ", ".join(str(p) for p in state_db_candidates())
    auth_looked = ", ".join(str(p) for p in cli_auth_candidates())
    found = state_db()
    db_clause = (
        f"Cursor state database has no access token: {found}"
        if found
        else f"no Cursor state database (looked in: {looked})"
    )
    keychain_clause = (
        f"; no `{KEYCHAIN_SERVICE}` Keychain item" if sys.platform == "darwin" else ""
    )
    return QuotaError(
        f"{db_clause}; no agent CLI auth file (looked in: {auth_looked})"
        f"{keychain_clause}"
        " — run `agent login`, or log in to the Cursor IDE on this machine"
        f" (or set {STATE_DB_ENV} to its state.vscdb); the `cursor-agent` "
        "CLI stores its login in that auth file and does not require the IDE"
    )


def _access_token() -> tuple[str, str]:
    """The first stored login and which store it came from."""
    for source in _token_sources():
        return source
    raise _missing_login()


def _membership_type() -> str | None:
    return _state_value("cursorAuth/stripeMembershipType")
