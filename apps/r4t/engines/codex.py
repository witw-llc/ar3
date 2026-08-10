"""Codex quota — the CLI's own app-server protocol.

`codex app-server` speaks newline-delimited JSON-RPC on stdio and ships a
schema-documented `account/rateLimits/read` method: a read-only account call
that costs no model quota. The sandbox flags keep the child harmless even if
the handshake goes sideways. Window identity comes from `windowDurationMins`,
never from the primary/secondary slot — sessions have been observed with
either or both populated.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading

from engines.base import QuotaError, iso_from_unix, window_label

TIMEOUT_S = 15


def quota() -> dict:
    if not shutil.which("codex"):
        raise QuotaError("codex is not on PATH")
    raw = _rpc_rate_limits()
    return parse_rate_limits(raw)


def _rpc_rate_limits() -> dict:
    # stdin must stay open until the answer lands: the server treats EOF as
    # "session over" and exits mid-flight if the requests are one-shot piped.
    try:
        proc = subprocess.Popen(
            ["codex", "-s", "read-only", "-a", "untrusted", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as exc:
        raise QuotaError(f"cannot run codex app-server: {exc}") from exc
    answer: dict = {}

    def read_answer() -> None:
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 2:
                answer.update(message)
                return

    reader = threading.Thread(target=read_answer, daemon=True)
    reader.start()
    try:
        for request in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "r4t", "version": "0"}},
            },
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": None,
            },
        ):
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
        reader.join(timeout=TIMEOUT_S)
    except (OSError, BrokenPipeError) as exc:
        raise QuotaError(f"codex app-server pipe broke: {exc}") from exc
    finally:
        proc.kill()
    if not answer:
        raise QuotaError(
            f"codex app-server did not answer account/rateLimits/read in "
            f"{TIMEOUT_S}s (is this CLI logged in? try: codex login status)"
        )
    if "error" in answer:
        raise QuotaError(
            f"account/rateLimits/read: {answer['error'].get('message')}"
        )
    return answer.get("result") or {}


def parse_rate_limits(result: dict) -> dict:
    snapshot = result.get("rateLimits") or {}
    buckets = []
    for window in (snapshot.get("primary"), snapshot.get("secondary")):
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        resets = window.get("resetsAt")
        buckets.append(
            {
                "label": window_label(window.get("windowDurationMins")),
                "remaining_fraction": (
                    max(0.0, 1.0 - used / 100) if isinstance(used, (int, float)) else None
                ),
                "reset_time": iso_from_unix(resets) if resets else None,
            }
        )
    if not buckets:
        raise QuotaError("codex answered without rate-limit windows")
    return {
        "origin": "live",
        "plan": snapshot.get("planType"),
        "buckets": buckets,
        "note": None,
    }
