"""Muse quota — the CLI's own MSP session host.

`muse serve` speaks newline-delimited JSON-RPC on stdio and ships a
schema-documented `usage/read` method (`muse schema generate-json-schema`
exports the exact wire contract offline). The answer is the host's
last-observed subscription window: a five-hour-class block and a rolling
weekly block in percent used, with reset stamps, the tier id, and the
observation's arrival stamp.

The catch is the observation is in-memory per host and minted by provider
traffic — a freshly spawned `muse serve` answers `{}`, and neither
`session/resume` nor the session log restores one. So a cold check mints the
observation itself: a session and one minimal turn (`denyUnmatched`
approvals, a throwaway workspace, `reasoningEffort: none`), after which
`usage/changed` carries the window and `usage/read` repeats it. That means a
live muse quota check *spends a probe turn* — unlike every other engine's
free read — and the turn's provider round-trip runs anywhere from seconds
to over a minute, so an interactive caller gets heartbeat lines on stderr
rather than a silent terminal. The snapshot fallback covers the hours
between checks. Verified against Muse Code 1.3.0.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from engines.base import QuotaError, iso_from_unix, window_label

# Muse's live read is the only one in the registry that is not free: a cold
# host mints the observation with a provider turn. `engines.quota` refuses to
# call `quota()` for callers that have not opted into the spend — they get
# the snapshot or a refusal instead.
QUOTA_SPENDS_TURN = True

TIMEOUT_S = 15
# The mint turn is one trivial prompt on the session's default (contributor)
# model — observed anywhere from ~5s to ~70s end to end, all of it provider
# queueing — but a congested provider is no reason to abandon a check that is
# already committed.
MINT_TIMEOUT_S = 120
MINT_PROMPT = "Reply with exactly: ok"
# A terminal that goes silent for the length of a provider round-trip reads
# as a hang; while a stage is open, say so every few seconds.
HEARTBEAT_S = 10


def quota() -> dict:
    if not shutil.which("muse"):
        raise QuotaError("muse is not on PATH")
    raw = _rpc_usage()
    return parse_usage(raw)


def _uuid7() -> str:
    """`commandId` on session/start and turn/start is checked as UUIDv7 —
    a v4 is rejected invalidParams."""
    ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    b = bytearray(os.urandom(16))
    b[0:6] = ms.to_bytes(6, "big")
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _rpc_usage() -> dict:
    # Same spawn-and-handshake shape as codex's app-server read: stdin stays
    # open until the answer lands, stderr goes to a file so an argv the
    # installed CLI rejects still leaves its complaint where the raise can
    # quote it. MSP wants `initialized` as a bare notification — an id makes
    # it a request and every later call fails `notInitialized`.
    try:
        errors = tempfile.TemporaryFile(mode="w+")
        proc = subprocess.Popen(
            ["muse", "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
    except OSError as exc:
        raise QuotaError(f"cannot run muse serve: {exc}") from exc
    responses: dict[int, dict] = {}
    observed: dict = {}
    completed = threading.Event()

    def read_stream() -> None:
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in message:
                responses[message["id"]] = message
                continue
            method = message.get("method")
            if method == "usage/changed":
                params = message.get("params")
                if isinstance(params, dict):
                    observed["usage"] = params
            elif method == "turn/completed":
                completed.set()

    def send(frame: dict) -> None:
        proc.stdin.write(json.dumps(frame) + "\n")
        proc.stdin.flush()

    def report(message: str) -> None:
        # Progress belongs to whoever is watching; `fuel()` callers and piped
        # output get silence and the same answer.
        if sys.stderr.isatty():
            print(f"muse: {message}", file=sys.stderr, flush=True)

    def wait_for(predicate, seconds: float, activity: str | None = None) -> bool:
        started = time.time()
        deadline = started + seconds
        beat = started + HEARTBEAT_S
        while time.time() < deadline:
            if predicate():
                return True
            if proc.poll() is not None:
                return False
            if activity and time.time() >= beat:
                report(f"{activity} ({int(time.time() - started)}s)")
                beat += HEARTBEAT_S
            time.sleep(0.05)
        return predicate()

    try:
        reader = threading.Thread(target=read_stream, daemon=True)
        reader.start()
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "r4t", "version": "0"}},
            }
        )
        send({"jsonrpc": "2.0", "method": "initialized"})
        # A spawned host is always cold — its observation is per-host memory —
        # so the session the mint needs is requested in the same batch rather
        # than after the inevitable `{}`. If a future muse persists usage and
        # `usage/read` answers anyway, the one extra session record is the
        # same litter the mint path already leaves.
        send({"jsonrpc": "2.0", "id": 2, "method": "usage/read"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/start",
                "params": {
                    "commandId": _uuid7(),
                    "approvalMode": "denyUnmatched",
                    "workspaceRoot": tempfile.gettempdir(),
                },
            }
        )
        if not wait_for(
            lambda: 2 in responses, TIMEOUT_S, "waiting on usage/read"
        ):
            raise QuotaError(
                f"muse serve did not answer usage/read in {TIMEOUT_S}s"
                + _stderr_hint(errors)
            )
        if "error" in responses[2]:
            raise QuotaError(f"usage/read: {responses[2]['error'].get('message')}")
        if "usage" in (responses[2].get("result") or {}):
            return responses[2]["result"]

        # The host has observed nothing — the only way forward is a provider
        # turn. The window lands with the provider's answer: `usage/changed`
        # can arrive while the turn still runs, so the wait below returns the
        # moment it does rather than holding out for `turn/completed`.
        if not wait_for(
            lambda: 3 in responses, TIMEOUT_S, "starting a session"
        ):
            raise QuotaError("muse serve did not answer session/start")
        session = (responses[3].get("result") or {}).get("session") or {}
        session_id = session.get("sessionId")
        if "error" in responses[3] or not session_id:
            raise QuotaError(
                "muse serve refused session/start: "
                + str(responses[3].get("error", "no sessionId"))
            )
        report(
            "cold host — minting the usage observation with one minimal "
            "turn; provider latency applies"
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "turn/start",
                "params": {
                    "commandId": _uuid7(),
                    "sessionId": session_id,
                    "reasoningEffort": "none",
                    "input": [{"type": "text", "text": MINT_PROMPT}],
                },
            }
        )
        wait_for(
            lambda: observed.get("usage") or completed.is_set()
            or ("error" in responses.get(4, {})),
            MINT_TIMEOUT_S,
            "waiting on the probe turn",
        )
        if not completed.is_set() and "error" not in responses.get(4, {}):
            # The observation may land mid-turn; cancel lets the host record
            # the turn closed instead of dying SIGKILL mid-stream.
            try:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "turn/cancel",
                        "params": {
                            "commandId": _uuid7(),
                            "sessionId": session_id,
                        },
                    }
                )
                wait_for(lambda: completed.is_set(), 3)
            except OSError:
                pass
        if "usage" not in observed and "error" not in responses.get(4, {}):
            # A completed turn without a usage frame, or a slow one that was
            # cancelled — ask once more before giving up.
            send({"jsonrpc": "2.0", "id": 5, "method": "usage/read"})
            wait_for(lambda: 5 in responses or "usage" in observed, TIMEOUT_S)
            result = responses.get(5, {}).get("result") or {}
            if "usage" in result:
                return result
        if "error" in responses.get(4, {}):
            raise QuotaError(
                f"turn/start: {responses[4]['error'].get('message')}"
            )
        if "usage" not in observed:
            raise QuotaError(
                "the mint turn produced no usage observation — the provider "
                "never sent a window for this host to report"
            )
        return {"usage": observed["usage"]}
    except OSError as exc:
        raise QuotaError(
            f"muse serve pipe broke: {exc}" + _stderr_hint(errors)
        ) from exc
    finally:
        proc.kill()
        errors.close()


def _stderr_hint(errors) -> str:
    errors.seek(0)
    complaint = errors.read().strip()
    return f": {_last_line(complaint)}" if complaint else ""


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _bucket(window: dict | None, label: str | None = None) -> dict | None:
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    resets = window.get("resetsAtMs")
    return {
        "label": label or window_label(window.get("windowDurationMins")),
        "remaining_fraction": (
            max(0.0, 1.0 - used / 100) if isinstance(used, (int, float)) else None
        ),
        "reset_time": iso_from_unix(resets / 1000) if resets else None,
    }


def parse_usage(result: dict) -> dict:
    """`usage/read`'s result (or a `usage/changed` frame). The `usage` member
    is omitted — never null — when the host has observed nothing."""
    usage = result.get("usage")
    if not isinstance(usage, dict):
        raise QuotaError(
            "this muse host has observed no usage — the subscription window "
            "is only learned from provider traffic"
        )
    buckets = [
        bucket
        for bucket in (
            _bucket(usage.get("window")),
            _bucket(usage.get("weekly"), "Weekly Limit"),
        )
        if bucket is not None
    ]
    if not buckets:
        raise QuotaError("muse usage observation carried no windows")
    observed = usage.get("observedAtMs")
    return {
        "origin": "live",
        "plan": usage.get("tier"),
        "buckets": buckets,
        "note": (
            f"observed {iso_from_unix(observed / 1000)} — muse reports the "
            "host's last-observed window, not a live read; a cold check "
            "mints it with a probe turn"
            if isinstance(observed, (int, float))
            else None
        ),
    }
