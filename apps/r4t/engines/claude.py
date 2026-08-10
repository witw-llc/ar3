"""Claude Code quota — the endpoint behind the interactive `/usage` view.

There is no first-party scriptable surface; this is the OAuth usage endpoint
the CLI itself reads, community-documented. The claude-code User-Agent is
load-bearing — without it the request lands in a punitive rate bucket. The
token comes from wherever this machine's CLI keeps it: the macOS Keychain
item, the credentials file, or the environment. An expired token is an error
with a remedy, not a refresh — the refresh flow belongs to the CLI.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from engines.base import QuotaError

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
TIMEOUT_S = 15

BUCKET_LABELS = {
    "five_hour": "Five Hour Limit",
    "seven_day": "Weekly Limit",
    "seven_day_opus": "Weekly Limit (Opus)",
    "seven_day_sonnet": "Weekly Limit (Sonnet)",
    "seven_day_fable": "Weekly Limit (Fable)",
}


def quota() -> dict:
    token = _oauth_token()
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"claude-code/{_cli_version()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise QuotaError(
                "usage endpoint rejected the token (expired?) — run claude "
                "interactively once to refresh it"
            ) from exc
        raise QuotaError(f"usage endpoint returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise QuotaError(f"usage endpoint unreachable: {exc}") from exc
    result = parse_usage(payload)
    # One account can hold seats in several workspaces (a Team and a personal
    # Max, say) and a token answers for exactly one — name it, so nobody
    # mistakes whose buckets these are.
    result["plan"] = _workspace(token) or result["plan"]
    return result


def _workspace(token: str) -> str | None:
    request = urllib.request.Request(
        PROFILE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"claude-code/{_cli_version()}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            profile = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None
    org = profile.get("organization") or {}
    name = org.get("name")
    tier = org.get("rate_limit_tier")
    if not name:
        return None
    return f"{name} ({tier})" if tier else name


LIMIT_KIND_LABELS = {
    "session": "Five Hour Limit",
    "weekly_all": "Weekly Limit",
}


def parse_usage(payload: dict) -> dict:
    """Shape verified live 2026-08-08. The `limits` array is the product
    surface — the same rows the /usage panel draws, including model-scoped
    weeklies (`weekly_scoped` + scope.model.display_name, e.g. Fable). Fall
    back to the older top-level utilization windows when `limits` is absent,
    so aged deployments of the endpoint still parse."""
    buckets = []
    for limit in payload.get("limits") or []:
        if not isinstance(limit, dict):
            continue
        kind = limit.get("kind")
        label = LIMIT_KIND_LABELS.get(kind)
        if label is None:
            scoped = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
            label = (
                f"Weekly Limit ({scoped})"
                if kind == "weekly_scoped" and scoped
                else str(kind or "Quota").replace("_", " ").title()
            )
        percent = limit.get("percent")
        buckets.append(
            {
                "label": label,
                "remaining_fraction": (
                    max(0.0, 1.0 - percent / 100)
                    if isinstance(percent, (int, float))
                    else None
                ),
                "reset_time": limit.get("resets_at"),
                "severity": limit.get("severity"),
            }
        )
    if not buckets:
        for key, window in payload.items():
            if not isinstance(window, dict) or "utilization" not in window:
                continue
            utilization = window.get("utilization")
            buckets.append(
                {
                    "label": BUCKET_LABELS.get(key, key.replace("_", " ").title()),
                    "remaining_fraction": (
                        max(0.0, 1.0 - utilization / 100)
                        if isinstance(utilization, (int, float))
                        else None
                    ),
                    "reset_time": window.get("resets_at"),
                }
            )
    if not buckets:
        raise QuotaError("usage endpoint answered without any utilization windows")
    return {"origin": "live", "plan": None, "buckets": buckets, "note": None}


def _oauth_token() -> str:
    creds = _keychain_credentials() or _file_credentials()
    if creds:
        token = (creds.get("claudeAiOauth") or {}).get("accessToken")
        if token:
            return token
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env
    raise QuotaError(
        "no Claude Code OAuth token found (Keychain, ~/.claude/.credentials.json, "
        "or CLAUDE_CODE_OAUTH_TOKEN) — is this machine logged in?"
    )


def _keychain_credentials() -> dict | None:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return None


def _file_credentials() -> dict | None:
    try:
        return json.loads(
            (Path.home() / ".claude" / ".credentials.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None


def _cli_version() -> str:
    try:
        proc = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10
        )
        word = proc.stdout.strip().split()[0]
        if word and word[0].isdigit():
            return word
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    return "2.0.0"
