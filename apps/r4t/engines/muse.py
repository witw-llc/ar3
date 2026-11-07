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
import math
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from ar3.proc import launch_options, terminate_group

from engines.base import LiveQuotaError, QuotaError, QuotaExhausted, QuotaInvalidated, iso_from_unix, window_label
from rig import merge_env

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
PENDING_COMPLETIONS_LIMIT = 16


def quota(live: bool = False) -> dict:
    if live:
        return live_quota()
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
            **launch_options(),
        )
    except OSError as exc:
        raise QuotaError(f"cannot run muse serve: {exc}") from exc
    responses: dict[int, dict] = {}
    observed: dict = {}
    completed = threading.Event()
    session_id = None
    pending_completions: dict[str, dict] = {}

    def terminal_rank(params: dict) -> int:
        if params.get("terminal") != "failed":
            return 0
        error = params.get("error")
        return 2 if isinstance(error, dict) and isinstance(error.get("quota"), dict) else 1

    def accept_completion(params: dict) -> None:
        turn_id = (responses.get(4, {}).get("result") or {}).get("turnId")
        if turn_id and params.get("turnId") == turn_id:
            previous = observed.get("completion")
            if previous is None or terminal_rank(params) > terminal_rank(previous):
                observed["completion"] = params
            completed.set()

    def read_stream() -> None:
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in message:
                responses[message["id"]] = message
                if message["id"] == 4:
                    turn_id = (message.get("result") or {}).get("turnId")
                    if isinstance(turn_id, str) and turn_id in pending_completions:
                        accept_completion(pending_completions[turn_id])
                    pending_completions.clear()
                continue
            method = message.get("method")
            if method == "usage/changed":
                params = message.get("params")
                if isinstance(params, dict):
                    observed["usage"] = params
            elif method == "turn/completed":
                params = message.get("params")
                if (isinstance(params, dict) and session_id
                        and params.get("sessionId") == session_id):
                    if 4 in responses:
                        accept_completion(params)
                    else:
                        turn_id = params.get("turnId")
                        if not isinstance(turn_id, str) or not turn_id:
                            continue
                        previous = pending_completions.get(turn_id)
                        if previous is not None:
                            if terminal_rank(params) > terminal_rank(previous):
                                pending_completions[turn_id] = params
                        elif len(pending_completions) < PENDING_COMPLETIONS_LIMIT:
                            pending_completions[turn_id] = params
                        else:
                            observed["completion_overflow"] = True
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
        if observed.get("completion_overflow"):
            raise QuotaInvalidated("muse probe completion limit exceeded; quota unavailable")
        completion = observed.get("completion", {})
        if completion.get("terminal") == "failed":
            return {"failed_turn": completion}
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
                observed["usage"] = result["usage"]
        if observed.get("completion_overflow"):
            raise QuotaInvalidated("muse probe completion limit exceeded; quota unavailable")
        completion = observed.get("completion", {})
        if completion.get("terminal") == "failed":
            return {"failed_turn": completion}
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
    failure = result.get("failed_turn")
    if isinstance(failure, dict):
        error = failure.get("error")
        detail = error.get("quota") if isinstance(error, dict) else None
        if not isinstance(detail, dict):
            raise QuotaError("muse probe turn failed without a quota observation")
        if error.get("kind") != "modelError" or detail.get("limitKind") != "tierLimit":
            raise QuotaExhausted("muse reported quota exhaustion with an unsupported scope")
        now = time.time() * 1000
        reset = detail.get("resetsAtMs")
        if (isinstance(reset, bool) or not isinstance(reset, int)
                or not now < reset <= now + LIVE_MAX_RESET_MS):
            raise QuotaExhausted("muse subscription quota exhausted; reset time unavailable")
        return {
            "origin": "live",
            "plan": None,
            "buckets": [{
                "label": "Subscription Limit",
                "remaining_fraction": 0.0,
                "reset_time": iso_from_unix(reset / 1000),
            }],
            "note": "muse reported subscription quota exhausted on the probe turn",
        }
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


# The no-turn live read: `muse usage --json` (Muse Code 1.4.13+) prints the
# subscription window, weekly block and tier the provider reported, with the
# time of the observation, and starts no model turn. A reading is accepted only
# when it was minted by this very invocation.
LIVE_TIMEOUT_S = 25
VERSION_TIMEOUT_S = 5
LIVE_OUTPUT_LIMIT = 65536
LIVE_SKEW_MS = 2000
LIVE_MAX_RESET_MS = 400 * 24 * 3600 * 1000
MIN_USAGE_VERSION = (1, 4, 13)
UPGRADE_HINT = (
    "muse usage needs Muse Code 1.4.13 or later — upgrade muse"
)
# Provider text is never matched against a pattern and relayed: a reason or
# hint is named only when it is one of these codes, else omitted.
_TIER = re.compile(r"[A-Za-z0-9_.\- ]{1,40}")
KNOWN_CODES = frozenset({"no_allowance", "server_sent_no_usage"})


def live_account_problem(env: dict | None = None) -> str | None:
    """Why the account `muse usage` answers for may not be the requested one,
    else None. The native binary reads `$XDG_CONFIG_HOME/muse/auth.json` (or
    `$HOME/.config/muse/auth.json`), an API-key mode, or the macOS Keychain;
    it ignores `MUSE_AUTH_PATH`, which only the launcher honors. Names the
    variable, never its value."""
    source = os.environ if env is None else env

    def value(name: str) -> str:
        return (source.get(name) or "").strip()

    for name in ("META_API_KEY", "TBH_CREDENTIAL_BACKEND"):
        if value(name):
            return (
                f"{name} is set, so muse may not authenticate with the "
                f"subscription login this check reads"
            )
    home = value("HOME") or value("USERPROFILE") or os.path.expanduser("~")
    config = value("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    auth = value("MUSE_AUTH_PATH")
    if auth and os.path.realpath(auth) != os.path.realpath(
        os.path.join(config, "muse", "auth.json")
    ):
        return (
            "MUSE_AUTH_PATH names a credentials file the muse binary does "
            "not read"
        )
    if sys.platform == "darwin" and os.path.realpath(home) != os.path.realpath(
        _passwd_home()
    ):
        return (
            "HOME differs from the user's home directory, and the macOS "
            "Keychain login belongs to the user, not to HOME"
        )
    return None


def _passwd_home() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return os.path.expanduser("~")


def live_quota() -> dict:
    path = shutil.which("muse")
    if not path:
        raise LiveQuotaError("muse is not on PATH")
    # Nesting: this child runs in its own session, so a caller that kills
    # r4t's group does not reach it. The inner bounds stay strictly inside
    # any outer one instead: 5s + 25s plus two teardown graces, well under
    # a8s's 60s limit.
    # MUSE_NO_AUTO_UPDATE keeps the launcher from forking its background
    # updater, which would mutate the install during a read and be killed
    # half-done by the group teardown.
    env = merge_env(os.environ, {"MUSE_NO_AUTO_UPDATE": "1"})
    code, text, _s, _f = _run_bounded([path, "--version"], VERSION_TIMEOUT_S, env)
    version = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if code != 0 or not version or tuple(map(int, version.groups())) < MIN_USAGE_VERSION:
        raise LiveQuotaError(f"muse is too old or unidentifiable — {UPGRADE_HINT}", code="upgrade_required")
    code, raw, started_ms, finished_ms = _run_bounded(
        [path, "usage", "--json"], LIVE_TIMEOUT_S, env
    )
    if code != 0:
        raise LiveQuotaError(f"muse usage exited {code} — {UPGRADE_HINT}")
    return parse_live_usage(raw, started_ms, finished_ms)


def _run_bounded(argv: list[str], timeout: float, env: dict) -> tuple:
    """(exit code, stdout, started_ms, finished_ms). Output goes to a temp
    file, not a pipe, so a descendant holding it cannot hang the read; the
    whole process group is ended however the call finished. Stderr is
    discarded: provider text never reaches a reason."""
    started_ms = int(time.time() * 1000)
    with tempfile.TemporaryFile(mode="w+b") as out:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.DEVNULL,
                env=env,
                **launch_options(new_session=True),
            )
        except OSError:
            raise LiveQuotaError("cannot run muse") from None
        code = None
        try:
            try:
                _wait_exit(proc, timeout)
            except subprocess.TimeoutExpired:
                raise LiveQuotaError(
                    f"muse {argv[1]} did not answer in {timeout}s".replace(".0s", "s")
                ) from None
        finally:
            code = _end_group(proc)
        finished_ms = int(time.time() * 1000)
        return code, _head(out), started_ms, finished_ms


KQUEUE_SLICE_S = 0.1


def _kqueue_exit(proc, timeout: float) -> bool:
    """True once the leader has exited, seen with kqueue NOTE_EXIT, which does
    not reap. The filter is registered again every slice: a zombie refused with
    ESRCH has exited, and an exit that fell between lookup and attach is caught
    by the next registration. Nothing else may reap the child (SIGCHLD not
    ignored, no `waitpid(-1)`)."""
    deadline = time.monotonic() + max(0.0, timeout)
    kq = select.kqueue()
    try:
        event = select.kevent(
            proc.pid, filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT,
        )
        while True:
            try:
                kq.control([event], 0, 0)
            except ProcessLookupError:
                return True
            wait = max(0.0, min(KQUEUE_SLICE_S, deadline - time.monotonic()))
            for got in kq.control(None, 1, wait):
                if got.ident == proc.pid and got.fflags & select.KQ_NOTE_EXIT \
                        and not got.flags & select.KQ_EV_ERROR:
                    return True
            if time.monotonic() >= deadline:
                return False
    finally:
        kq.close()


def _wait_exit(proc, timeout: float) -> None:
    """Return once the leader has exited, waiting at most `timeout`. The leader
    is observed without being reaped, so its zombie keeps the pid and the group
    id reserved until `_end_group` has signalled the group: `os.waitid` with
    WNOWAIT where it exists (Linux, macOS on Python 3.13+), else kqueue
    NOTE_EXIT (older macOS). With neither (and on Windows) it is an ordinary
    wait that reaps the leader."""
    if hasattr(os, "waitid"):
        deadline = time.monotonic() + timeout
        while True:
            try:
                done = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                proc.poll()  # reaped elsewhere: record it so nothing is signalled by pid
                return
            if done is not None:
                return
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            time.sleep(0.02)
    if hasattr(select, "kqueue"):
        if not _kqueue_exit(proc, timeout):
            raise subprocess.TimeoutExpired(proc.args, timeout)
        return
    proc.wait(timeout=timeout)


TEARDOWN_GRACE_S = 0.5
REAP_TIMEOUT_S = 2


def _end_group(proc) -> int:
    """End the process group however the call finished: a child that outlives
    the leader holds the output file and the account's resources. Returns the
    leader's exit code.

    A leader not yet reaped (the `waitid` or kqueue path, or a timeout) still
    owns its pid, so the group is signalled by `terminate_group`: SIGTERM, a
    grace, SIGKILL, and only then is the leader reaped, by a bounded wait.

    A leader already reaped (neither `waitid` nor kqueue, or reaped elsewhere)
    no longer owns its pid, so nothing is signalled: descendants that outlive
    it are not reached, as on Windows."""
    if proc.returncode is None:
        terminate_group(proc, grace_seconds=TEARDOWN_GRACE_S)
        try:
            return proc.wait(timeout=REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise LiveQuotaError("muse did not exit after teardown") from None
    return proc.returncode


def _head(handle) -> str:
    handle.seek(0)
    data = handle.read(LIVE_OUTPUT_LIMIT + 1)
    if len(data) > LIVE_OUTPUT_LIMIT:
        raise LiveQuotaError("muse usage output exceeded the size bound")
    return data.decode("utf-8", "replace")


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _live_bucket(block, label: str, as_of: float) -> dict:
    if not isinstance(block, dict):
        raise LiveQuotaError(f"muse usage {label} block is malformed")
    used = _number(block.get("usedPercent"))
    resets = _number(block.get("resetsAtMs"))
    if used is None or used < 0:
        raise LiveQuotaError(f"muse usage {label} usedPercent is malformed")
    if resets is None or not as_of < resets <= as_of + LIVE_MAX_RESET_MS:
        raise LiveQuotaError(f"muse usage {label} resetsAtMs is malformed")
    return {
        "label": label,
        "remaining_fraction": round(max(0.0, 1.0 - used / 100), 6),
        "reset_time": iso_from_unix(resets / 1000),
    }


def parse_live_usage(raw: str, started_ms: float, finished_ms: float) -> dict:
    """`muse usage --json` output as an engine answer, or QuotaError. Anything
    short of a fresh minted observation is unavailable: no cache, no stale
    reading, no allowance."""
    try:
        data = json.loads(raw)
    except ValueError:
        raise LiveQuotaError("muse usage answered non-JSON") from None
    if not isinstance(data, dict) or data.get("schemaVersion") != 1 or isinstance(
        data.get("schemaVersion"), bool
    ):
        raise LiveQuotaError("muse usage answered an unsupported schema")
    allowance = data.get("allowance")
    if not isinstance(allowance, dict):
        reason = data.get("reason")
        hint = data.get("hint")
        detail = " — ".join(
            v for v in (reason, hint) if isinstance(v, str) and v in KNOWN_CODES
        )
        raise LiveQuotaError(
            "muse usage reported no allowance" + (f": {detail}" if detail else "")
        )
    if allowance.get("source") != "mint":
        raise LiveQuotaError("muse usage allowance is not a fresh observation")
    as_of = _number(allowance.get("asOfMs"))
    if as_of is None:
        raise LiveQuotaError("muse usage allowance has no valid asOfMs")
    if as_of < started_ms - LIVE_SKEW_MS:
        raise LiveQuotaError("muse usage allowance predates this request (stale)")
    if as_of > finished_ms + LIVE_SKEW_MS:
        raise LiveQuotaError("muse usage allowance is dated in the future")
    window = allowance.get("window")
    weekly = allowance.get("weekly")
    if window is None and weekly is None:
        raise LiveQuotaError("muse usage allowance carried no windows")
    buckets = []
    if window is not None:
        minutes = window.get("windowDurationMins") if isinstance(window, dict) else None
        label = (
            window_label(minutes)
            if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0
            else "Window Limit"
        )
        buckets.append(_live_bucket(window, label, as_of))
    if weekly is not None:
        buckets.append(_live_bucket(weekly, "Weekly Limit", as_of))
    tier = allowance.get("tier")
    plan = tier if isinstance(tier, str) and _TIER.fullmatch(tier) else None
    requested = iso_from_unix(as_of / 1000)
    return {
        "origin": "live",
        "plan": plan,
        "buckets": buckets,
        "requested_at": requested,
        "note": f"requested at {requested} via `muse usage` — no model turn was made",
    }
