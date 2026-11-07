"""Copilot — the one place that knows how to talk to the CLI for everything
that is not composing an argv.

Two surfaces live here. **Quota** reads the entitlement endpoint every IDE
extension polls: GitHub documents no user-scoped quota API, and
`/copilot_internal/user` answers to a plain OAuth token. On token-based-billing
seats the fraction fields are degenerate (`unlimited: true`, 0/0/100) — the
answer there is cumulative credits spent plus the reset date, and the fraction
is None on purpose.

**Per-turn spend** reads the JSON `--usage-output-file` writes at the end of a
run. It is the only per-turn accounting this engine hands over without the
operator configuring anything, and it is what every other measurement here is
built on: the OTEL exporter is richer but an organisation telemetry policy
takes it away on roughly one run in three, while the usage file survives even
r4t's own SIGTERM-then-SIGKILL teardown (all measured 2026-09-02; the wiki's
Engine-Copilot page holds the tables).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ar3.proc import launch_options

from engines.base import LiveQuotaError, QuotaError
from rig import (
    Launch, RigError, cli_stem, engine_home, launch_version, merge_env, preset_cli,
    resolve_launch,
)
from state import r4t_home

TIMEOUT_S = 15
PROBE_TIMEOUT_S = 20

# 1 AI credit = 1e9 nano-AIU, and GitHub bills a credit at $0.01.
NANO_AIU_PER_CREDIT = 1e9

USER_ENDPOINT = "https://api.github.com/copilot_internal/user"
# copilot's own auth precedence, from `copilot help environment`.
TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

USAGE_FLAG = "--usage-output-file"
# Where a turn's instruments land. Both files are written INSIDE the directory
# the turn runs in when the caller is the roster, because that is the one place
# writable by the child under every isolation mode r4t offers — a container
# bind-mounts it rw at the same path, and `run_as` probes it for write before
# the turn starts. Dot-named and removed on the way out; a leftover from a
# killed turn is cleared on the way in, so a turn can never read the previous
# turn's numbers.
USAGE_BASENAME = ".r4t-copilot-usage.json"
OTEL_BASENAME = ".r4t-copilot-otel.jsonl"
# Setting this one variable both enables OpenTelemetry and selects the file
# exporter (`copilot help monitoring`), so a turn can arm the exporter for
# itself and leave the operator's own sessions untouched.
OTEL_ENV = "COPILOT_OTEL_FILE_EXPORTER_PATH"
MAX_CREDITS_FLAG = "--max-ai-credits"
# copilot refuses a smaller cap (`copilot help limits`). The cap is SOFT
# either way: usage is known only after a response returns, so a response can
# overshoot it and the next model call is what gets blocked.
MIN_AI_CREDITS = 30
# The first build that accepts --usage-output-file; 1.0.80 rejects it as an
# unknown option, which is how the boundary was found.
USAGE_FILE_MIN_VERSION = (1, 0, 82)

BUCKET_LABELS = {
    "chat": "Chat",
    "completions": "Completions",
    "premium_interactions": "Premium Requests",
}


def _gh_user() -> dict:
    """The endpoint through `gh`, which supplies both the token and the
    transport when it is installed and signed in."""
    try:
        proc = subprocess.run(
            ["gh", "api", "/copilot_internal/user"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            **launch_options(),
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
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise QuotaError("copilot endpoint returned non-JSON") from exc


def _config_payload(env: dict | None = None) -> dict:
    """`~/.copilot/config.json`, which is JSONC — it opens with a `// User
    settings` comment line — so the comments come out before json reads it."""
    try:
        text = (copilot_home(env) / "config.json").read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def token(env: dict | None = None) -> str | None:
    """The OAuth token to call the endpoint with, or None.

    `copilot help environment` gives the CLI's own precedence and this follows
    it: COPILOT_GITHUB_TOKEN, then GH_TOKEN, then GITHUB_TOKEN, then the login
    the CLI stored for itself. The stored form is `copilotTokens` in the config
    file, keyed by host on the seat where it was read; both the bare-string and
    the keyed shapes are accepted because only one of them has been seen. The
    value is returned and never logged, printed or put in an error message.
    """
    source = merge_env(os.environ if env is None else env)
    for name in TOKEN_ENV_VARS:
        value = (source.get(name) or "").strip()
        if value:
            return value
    stored = _config_payload(env).get("copilotTokens")
    if isinstance(stored, str):
        return stored.strip() or None
    if isinstance(stored, dict):
        for value in stored.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                inner = value.get("token") or value.get("oauth_token")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
    return None


def _direct_user(auth: str) -> dict:
    """The endpoint over HTTPS, stdlib only. Every failure is re-raised as a
    QuotaError whose message names the endpoint and never the token."""
    request = urllib.request.Request(
        USER_ENDPOINT,
        headers={
            "Authorization": f"token {auth}",
            "Accept": "application/json",
            "User-Agent": "r4t",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise QuotaError(
            f"{USER_ENDPOINT} answered {exc.code} — the stored token may have "
            f"expired; sign in again"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise QuotaError(f"cannot reach {USER_ENDPOINT}: {exc.reason if hasattr(exc, 'reason') else exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise QuotaError("copilot endpoint returned non-JSON") from exc


def account_scoped(env: dict | None) -> bool:
    """Whether `env` points copilot at a different account than the router's:
    another `COPILOT_HOME`, or a token variable that differs. Such a rig must
    never be answered with the router's `gh` login or the router's stored
    token."""
    if env is None:
        return False
    env = merge_env(env)
    own = merge_env(os.environ)
    if engine_home("copilot", env) != engine_home("copilot", None):
        return True
    return any(
        (env.get(name) or "") != (own.get(name) or "") for name in TOKEN_ENV_VARS
    )


def account_key(env: dict | None) -> str | None:
    """The cache key for the account `env` reads. "" for the router's own
    account. For a rig that names its own, a short hash of the credential it
    resolves (`token(env)`: the environment's token variables, else the login
    stored in its home), so two rigs in one home with different tokens, or one
    home whose login was replaced, never share a reading. The raw token is
    never stored, printed or used as a file name. None when the rig names its
    own account but no credential can be read: the identity is unknown, so
    nothing may be cached or served for it."""
    if not account_scoped(env):
        return ""
    credential = token(env)
    if credential is None:
        return None
    return "-" + hashlib.sha256(f"copilot\0{credential}".encode()).hexdigest()[:16]


def quota(env: dict | None = None, live: bool = False) -> dict:
    """(`engines.quota` hands a quota check that names an `env` parameter the
    spawned environment of the rig it asks about.)

    The entitlement endpoint, through whichever transport this machine has.

    `gh` is a convenience, not a prerequisite. A seat authenticated to Copilot
    throughout was answering `gh is not on PATH` on a machine that held a
    perfectly good token, which is a wrong answer rather than a missing one, so
    a token from the environment or from copilot's own stored login carries the
    call directly when `gh` cannot.

    `env` is the spawned environment of a rig that names its own account
    (`COPILOT_HOME`, a token variable). Then `gh`, which signs in as whoever
    the router is, is not asked: only the token that environment resolves
    answers, and none is an error naming the home, never a fallback to the
    router's account.

    `live` is the node-health read: the account is the one copilot itself
    resolves from this process's environment (`token(env)`: its own variable
    precedence, then the login stored under its `COPILOT_HOME`), always over
    the direct endpoint. `gh` is never asked, because it signs in through its
    own store and can answer for another account than the CLI's.
    """
    if live:
        auth = token(env)
        if auth is None:
            raise LiveQuotaError(
                "no Copilot token is readable in this environment — sign in "
                "with `copilot login`, or set COPILOT_GITHUB_TOKEN"
            )
        return parse_user(_direct_user(auth))
    scoped = account_scoped(env)
    if not scoped and shutil.which("gh"):
        return parse_user(_gh_user())
    auth = token(env)
    if auth is None:
        where = f" in {engine_home('copilot', env)}" if scoped else ""
        raise QuotaError(
            f"gh is not on PATH and no Copilot token is readable{where} (set "
            f"{' / '.join(TOKEN_ENV_VARS)}, or sign in with `copilot` so it "
            "stores one)"
            if not scoped else
            f"no Copilot token is readable{where} for this rig's account (set "
            f"{' / '.join(TOKEN_ENV_VARS)} in the rig's env, or sign in with "
            f"`copilot` under that COPILOT_HOME); the router's login is not "
            f"used for another account"
        )
    return parse_user(_direct_user(auth))


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
        spent = snap.get("credits_used")
        buckets.append(
            {
                "label": BUCKET_LABELS.get(key, key),
                "remaining_fraction": (
                    None
                    if unlimited or not isinstance(percent, (int, float))
                    else max(0.0, min(1.0, percent / 100))
                ),
                # A numerator with no denominator, and on a token-based seat
                # the only real signal: the fractions there are degenerate
                # (0/0/100 whatever has been spent). Carried as a number as
                # well as inside the note, so something can trend it.
                "credits_used": spent if isinstance(spent, (int, float)) else None,
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


# --- per-turn spend ---------------------------------------------------------

def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(g) for g in match.groups()) if match else None


_usage_support: dict[tuple[str, int, int], bool] = {}


def supports_usage_file(launch: Launch) -> bool:
    """Whether the CLI this launch starts accepts `--usage-output-file`.

    Probed once per process per executable, `--help` first: this Mac's 1.0.82
    lists the flag, so the cheapest probe is also the direct one. `--help` is a
    floor rather than a census on this CLI — flags run that it never lists — so
    a help text without the flag falls back to the version boundary instead of
    concluding no. Passing the flag speculatively is the alternative, and it is
    worse: an unknown option aborts the run before the turn starts, and
    `run.py` streams the child's stdout rather than capturing it, so nothing
    could see the error to retry on.

    `launch` is the resolved launch the turn will spawn (see `rig.Launch`): the
    probe runs THAT file under THAT environment, so a rig whose invoke names an
    absolute copilot is never vouched for by the one on PATH. An isolated or
    unresolved launch has no host file to ask, so the flag is not added.
    """
    found = launch.binary
    if found is None or launch.isolated:
        return False
    try:
        info = os.stat(found)
    except OSError:
        return False
    # Keyed by the file's identity, so a replaced or upgraded binary at the same
    # path is asked again. The probe's own env and cwd are not part of the key:
    # a shim that answers differently by environment is a documented residual.
    key = (found, info.st_mtime_ns, info.st_size)
    if key not in _usage_support:
        if USAGE_FLAG in _probe_text(launch, ["--help"]):
            _usage_support[key] = True
        else:
            version = _version_tuple(_probe_text(launch, ["--version"]))
            _usage_support[key] = bool(
                version and version >= USAGE_FILE_MIN_VERSION
            )
    return _usage_support[key]


def _probe_text(launch: Launch, args: list[str]) -> str:
    """One no-turn probe of the launch's CLI, its output as text: the probed
    file, the launch's cwd and env. A binary that cannot answer yields the empty
    string: every caller here is best-effort by design."""
    done = launch.probe(args, timeout=PROBE_TIMEOUT_S)
    return "" if done is None else (done.stdout or "") + (done.stderr or "")


def max_credits_problem(credits: int) -> str | None:
    """Why copilot will not take this spend fuse, or None."""
    if credits < MIN_AI_CREDITS:
        return (
            f"copilot takes no {MAX_CREDITS_FLAG} below {MIN_AI_CREDITS}, "
            f"and {credits} is below it"
        )
    return None


def _token_count(details: dict, key: str) -> int | None:
    entry = details.get(key)
    return entry.get("tokenCount") if isinstance(entry, dict) else None


def copilot_home(env: dict | None = None) -> Path:
    """Where the CLI keeps its state, as the spawned environment `env` sees it
    (this process's when None): a non-empty `COPILOT_HOME`, else the child's
    HOME plus `.copilot` — `rig.engine_home`, the one resolver."""
    return engine_home("copilot", env)


def session_exists(session_id: str, env: dict | None = None) -> bool:
    """Whether this session has already been founded in the home `env` names.
    `--session-id` names the directory too, so the store answers directly and
    a pinned run can be located on disk without a discovery step."""
    return (copilot_home(env) / "session-state" / session_id).is_dir()


def read_usage(path: Path) -> dict | None:
    """One turn's spend, from the JSON copilot writes at the end of a run.
    None when the file was never written — the run predates the flag, or the
    process died to a bare SIGKILL, the one teardown that loses it."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    nano = payload.get("totalNanoAiu")
    details = payload.get("tokenDetails")
    details = details if isinstance(details, dict) else {}
    model = payload.get("currentModel")
    metrics = payload.get("modelMetrics")
    metrics = metrics.get(model) if isinstance(metrics, dict) and model else None
    return {
        "credits": (
            round(nano / NANO_AIU_PER_CREDIT, 4)
            if isinstance(nano, (int, float))
            else None
        ),
        "model": model,
        "premium_requests": payload.get("totalPremiumRequestCost"),
        "input_tokens": _token_count(details, "input"),
        "output_tokens": _token_count(details, "output"),
        "cache_read_tokens": _token_count(details, "cache_read"),
        "cache_write_tokens": _token_count(details, "cache_write"),
        # Five minutes past the call, re-stamped by a read: the sliding cache
        # window, reported by the CLI rather than inferred.
        "cache_expires_at": (
            metrics.get("cacheExpiresAt") if isinstance(metrics, dict) else None
        ),
    }


def format_spend(spend: dict) -> str:
    """The one line a turn's spend earns on stderr."""
    credits = spend.get("credits")
    parts = [
        f"{credits:.2f} credits" if isinstance(credits, (int, float)) else "credits ?",
        str(spend.get("model") or "model ?"),
        f"cache write {spend.get('cache_write_tokens') or 0} / "
        f"read {spend.get('cache_read_tokens') or 0}",
    ]
    return "spend: " + " · ".join(parts)


# --- OpenTelemetry, best-effort ---------------------------------------------
#
# The exporter is the richest instrument this engine has: per model call it
# reports input, output, cache-read and cache-write tokens next to that call's
# credit cost, which nothing else on this engine reports at all. It is also the
# one r4t must never depend on. An organisation telemetry policy, fetched from
# the server after the environment variable is honoured, reconfigures the
# exporter from `file` to `otlp-http` before the SDK initialises; the run exits
# 0, the spans go to the organisation's collector, and no file appears. Whether
# the policy lands before or after SDK init is a race, so on a managed seat the
# file arrives on roughly two runs in three. Read the absence as an absence.

def otel_dir() -> Path:
    return r4t_home() / "copilot" / "otel"


def otel_path(stamp: str) -> Path:
    """Where a turn's exported spans are kept. Durable, unlike the usage file:
    the JSONL is the raw record and nothing here parses it yet."""
    return otel_dir() / f"{stamp}.jsonl"


def read_otel(path: Path) -> int | None:
    """How many records the exporter wrote, or None when it wrote no file."""
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return None


def format_otel(path: Path, records: int | None) -> str:
    """The one line a turn's exporter earns. An absent file is reported, not
    raised: it is the expected outcome on a seat under a telemetry policy."""
    if records is None:
        return "otel: not written (org telemetry policy or race)"
    return f"otel: {path} ({records} records)"


# --- denial telemetry --------------------------------------------------------
#
# A headless copilot turn that touches a path or URL it has no rule for is
# denied ("unattended fallback") and carries on. The denial sits in the
# session's `events.jsonl`; nothing in the exit code, the reply or the a8s log
# shows it. This reads it back, once, after the turn.
#
# It is a DIAGNOSTIC and never a gate. It cannot change an exit code, it tells
# nobody, and a record that cannot be proven complete says so instead of
# printing a comfortable zero. The summary is bounded metadata only: counts,
# a fixed tool set, a slug for the denial kind, at most ten event ids. No
# argument, command, path, URL or rule text ever leaves the file: a rule
# message is reduced to the fixed tool its head names and a count.
#
# What a denial is (live, copilot 1.0.105, 2026-11-02). Two shapes, and a
# reader that knew only the first would print "0 (complete)" over the second:
#   - no approval rule, headless: `permission.requested`, then
#     `permission.completed` with result kind
#     `denied-no-approval-rule-and-could-not-request-from-user` and
#     `decisionSource: unattended_fallback`, then a failed `tool.execution_complete`;
#   - a deny RULE (`--deny-tool=shell(touch)`): NO `permission.*` record at
#     all, only `tool.execution_complete` with `success: false` and
#     `error.code: "denied"`, whose message names the rule.
# Both are folded by tool call id. The `data` payloads are real captures; the
# line ENVELOPE around them (`type`, `data`, an optional `id`) is inferred, as
# is the tool-name map. A record that does not match what is demanded here is
# `unknown-shape`, which is never complete.

EVENTS_BASENAME = "events.jsonl"
SESSION_DIR = "session-state"
# The one build whose event schema has been read. Newer or unknown builds can
# add an event family that carries denials this reader ignores by design.
SCHEMA_VERIFIED = (1, 0, 105)
MAX_SLICE_BYTES = 8 * 1024 * 1024
MAX_EVENT_IDS = 10
MAX_COUNT = 1000
MAX_CLASSES = 10

# Fixed vocabulary: nothing from the file reaches a summary except through it.
# In the order a summary names them: a turn that fails several conditions
# reports the first.
REASONS = (
    "not-started",
    "not-armed",
    "home-unknown",
    "session-unknown",
    "reader-error",
    "file-replaced",
    "truncated",
    "file-grew",
    "unverified-version",
    "shared-pin",
    "oversize",
    "partial-line",
    "unknown-shape",
    "no-shutdown",
)
TOOLS = ("shell", "read", "write", "url", "mcp", "memory", "other")
_ID = re.compile(r"[A-Za-z0-9-]{1,64}\Z")
# The field's own denial kind is 54 characters, so the slug has room for it.
_CLASS = re.compile(r"[a-z0-9-]{1,64}\Z")


def _slug(value: object, pattern: re.Pattern) -> str | None:
    return value if isinstance(value, str) and pattern.match(value) else None


def _empty_summary(session: str | None, reason: str, observed: int | None = None) -> dict:
    return {
        "observed": observed,
        "complete": False,
        "reason": reason,
        "by_tool": {},
        "by_class": {},
        "event_ids": [],
        "rule_tools": [],
        "rules_omitted": 0,
        # Validated here too: a summary built before the session was checked
        # (an unknown home) must not carry an unchecked string out.
        "session": _slug(session, _ID),
    }


def unknown_summary(reason: str) -> dict:
    """`unknown (<reason>)` for a turn whose denials were not read: `not-started`
    (it was refused or failed before its instruments existed) or `not-armed`
    (it had no denial probe)."""
    return _seal(_empty_summary(None, reason))


def _seal(summary: object) -> dict:
    """The last gate before a summary leaves this module. Every field is
    checked against the vocabulary it is allowed to hold; a summary that does
    not fit is replaced by `reader-error`, never trimmed into something that
    still looks right. Nothing but fixed names, slugs, ids and counts passes."""
    try:
        if not isinstance(summary, dict) or set(summary) != set(_empty_summary(None, "x")):
            raise _Shape
        observed = summary["observed"]
        if observed is not None and not _count(observed, MAX_COUNT):
            raise _Shape
        reason = summary["reason"]
        if reason is not None and reason not in REASONS:
            raise _Shape
        if summary["complete"] is True:
            if reason is not None or observed is None or summary["session"] is None:
                raise _Shape
        elif summary["complete"] is not False or reason is None:
            raise _Shape
        for field, allowed, limit in (("by_tool", None, len(TOOLS)), ("by_class", _CLASS, MAX_CLASSES + 1)):
            table = summary[field]
            if not isinstance(table, dict) or len(table) > limit:
                raise _Shape
            for key, value in table.items():
                known = key in TOOLS if allowed is None else _slug(key, allowed) is not None
                if not known or not _count(value, MAX_COUNT):
                    raise _Shape
        if observed is not None and (
            sum(summary["by_tool"].values()) > observed
            or sum(summary["by_class"].values()) != sum(summary["by_tool"].values())
        ):
            raise _Shape
        ids = summary["event_ids"]
        if not isinstance(ids, list) or len(ids) > MAX_EVENT_IDS or len(set(ids)) != len(ids):
            raise _Shape
        if any(_slug(i, _ID) is None for i in ids):
            raise _Shape
        tools = summary["rule_tools"]
        if not isinstance(tools, list) or any(t not in TOOLS for t in tools) or len(set(tools)) != len(tools):
            raise _Shape
        if not _count(summary["rules_omitted"], MAX_COUNT):
            raise _Shape
        if summary["session"] is not None and _slug(summary["session"], _ID) is None:
            raise _Shape
    except (_Shape, TypeError):
        return _empty_summary(
            summary.get("session") if isinstance(summary, dict) else None, "reader-error"
        )
    return summary


def _count(value: object, ceiling: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= ceiling


class _Shape(Exception):
    """A record this reader does not know."""


# Copilot names its tools in the log (`bash`, `view`); the permission request
# names a coarser kind (`shell`, `read`). Either maps into the fixed TOOLS set.
_TOOL_BY_NAME = {
    "bash": "shell", "shell": "shell", "powershell": "shell",
    "view": "read", "read": "read", "glob": "read", "grep": "read",
    "edit": "write", "create": "write", "write": "write",
    "str_replace_editor": "write",
    "web_fetch": "url", "fetch": "url", "url": "url",
    "memory": "memory", "store_memory": "memory",
}
_CALL = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
# A deny rule's message names the rules that matched. Rule text is
# operator-written and can carry anything (`shell(echo TOKEN)`,
# `write(notes.txt)`, `url(host)`): none of it is kept. Only the fixed tool
# the rule's head names survives, and a count of the rules left out.
_RULES_QUOTED = re.compile(r"`([^`\n]{1,80})`")
_RULE_HEAD = re.compile(r"([a-z][a-z0-9_-]{0,15})(?:\(.*\))?\Z", re.S)
MAX_RULES = 50
RULE_CLASS = "denied-by-rule"


def _rule_tool(rule: str) -> str | None:
    """The fixed tool a rule's head names, or None. The body is never read."""
    found = _RULE_HEAD.match(rule)
    return found.group(1) if found and found.group(1) in TOOLS and found.group(1) != "other" else None


def _tool_of(label: object) -> str:
    return _TOOL_BY_NAME.get(label, label if label in TOOLS else "other") if isinstance(label, str) else "other"


# Event kinds a slice may carry that this reader deliberately does not read.
# They hold no permission decision and no tool outcome (copilot 1.0.105 live
# captures). Every OTHER kind is either read and strictly validated below, or
# it is unknown, and an unknown kind is never part of a "complete" proof.
# The shutdown types seen in live captures. Another value is not proof that the
# session ended the way a finished turn does.
SHUTDOWN_TYPES = frozenset({"routine"})
# Failure codes of a tool record that are shown not to be a permission
# decision. None has been captured yet, so any failure that is not a denial is
# not proven to be one; live evidence extends this set.
NON_DENIAL_CODES: frozenset[str] = frozenset()
IGNORABLE_KINDS = frozenset({
    "system.message", "user.message",
    "assistant.turn_start", "assistant.turn_end", "assistant.message",
    "assistant.usage",
    "session.model_change", "session.resume", "session.usage_record",
    "session.usage_checkpoint", "session.auto_mode_resolved",
    "session.permissions_changed",
})


def parse_events(data: bytes, session: str | None = None) -> dict:
    """Read the complete lines of an events slice.

    A denial is one of two records, and a turn can carry either. The
    no-approval-rule fallback writes `permission.completed` (a `denied*`
    result) and then a failed `tool.execution_complete`; a deny RULE writes
    only the failed `tool.execution_complete` (`error.code` "denied"), no
    `permission.*` record at all (live, 1.0.105). Both are folded by tool call
    id so the fallback is counted once.

    Two questions are answered separately. COUNT: a denial is detected on its
    own fields, so a record with a missing, malformed, contradicted or
    unmatched id is kept as an uncorrelated denial, never dropped
    (`_lower_bound`). PROOF: the `proof` entry lists the positive facts that
    "complete" needs: every line parsed (`lines`), every kind either read and
    strictly validated or in `IGNORABLE_KINDS` (`shapes`), every id consistent
    (`correlation`), a valid shutdown (`shutdown`), no bound hit (`bounds`).
    Validation runs on every recognized record before any success or
    non-denial fast path. Anything not proven is not complete.

    `session`, when given, is checked against the attribution fields the
    records carry. Pure: no file, no environment."""
    proof = {"lines": True, "shapes": True, "correlation": True}
    starts: dict[str, str] = {}
    session_starts = shutdowns = 0
    requests: dict[str, tuple[str, str | None]] = {}
    completions: list[dict] = []
    execs: list[dict] = []
    rule_tools: set[str] = set()
    omitted = 0

    def attributed(owner: object) -> bool:
        """A record's own session field, when it carries one."""
        return session is None or _slug(owner, _ID) == session

    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            # A complete line that is not JSON could be any record, a denial
            # included: coverage cannot be proven.
            proof["lines"] = False
            continue
        kind = event.get("type") if isinstance(event, dict) else None
        if not isinstance(kind, str):
            proof["lines"] = False
            continue
        ident = _slug(event.get("id"), _ID)
        body = event.get("data")
        try:
            if kind == "session.shutdown":
                # Evidence that the session ended on its own terms: an object
                # that names how. `data: null` is not that.
                if not isinstance(body, dict) or not isinstance(body.get("shutdownType"), str) \
                        or body["shutdownType"] not in SHUTDOWN_TYPES:
                    raise _Shape
                shutdowns += 1
            elif kind == "session.start":
                if not isinstance(body, dict) or _slug(body.get("sessionId"), _ID) is None:
                    raise _Shape
                if not attributed(body["sessionId"]):
                    raise _Shape
                session_starts += 1
            elif kind == "tool.execution_start":
                call = _slug(body.get("toolCallId") if isinstance(body, dict) else None, _CALL)
                if call is None or not isinstance(body.get("toolName"), str):
                    raise _Shape
                tool = _tool_of(body["toolName"])
                if starts.setdefault(call, tool) != tool:
                    proof["correlation"] = False
            elif kind == "tool.execution_complete":
                if not isinstance(body, dict) or not isinstance(body.get("success"), bool):
                    raise _Shape
                call = _slug(body.get("toolCallId"), _CALL)
                denied, label = False, "other"
                if not body["success"]:
                    error = body.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    if not isinstance(code, str):
                        raise _Shape
                    if not code.startswith("denied") and code not in NON_DENIAL_CODES:
                        # A failure of a kind nobody has shown to be unrelated
                        # to permissions might be a denial under another name.
                        proof["shapes"] = False
                    if code.startswith("denied"):
                        denied = True
                        message = error.get("message")
                        message = message if isinstance(message, str) else ""
                        marker = "denied due to the following rules"
                        if marker in message:
                            label = RULE_CLASS
                            for rule in _RULES_QUOTED.findall(message.split(marker, 1)[1]):
                                omitted += 1
                                tool = _rule_tool(rule)
                                if tool is not None:
                                    rule_tools.add(tool)
                        else:
                            label = _slug(code, _CLASS) or "other"
                execs.append({"call": call, "denied": denied, "label": label, "id": ident})
                if call is None:
                    # Required of every outcome, not only a denied one.
                    raise _Shape
            elif kind.startswith("permission."):
                if not isinstance(body, dict):
                    raise _Shape
                request = _slug(body.get("requestId"), _ID)
                if kind == "permission.requested":
                    asked = body.get("permissionRequest")
                    if request is None or request in requests or not isinstance(asked, dict):
                        raise _Shape
                    if not isinstance(asked.get("kind"), str):
                        raise _Shape
                    if asked.get("toolCallId") is not None and _slug(asked["toolCallId"], _CALL) is None:
                        raise _Shape
                    # Attribution is part of the proof, so the field is required.
                    if _slug(body.get("ownerSessionId"), _ID) is None or not attributed(body["ownerSessionId"]):
                        raise _Shape
                    requests[request] = (_tool_of(asked["kind"]), _slug(asked.get("toolCallId"), _CALL))
                elif kind == "permission.completed":
                    result = body.get("result")
                    verdict = result.get("kind") if isinstance(result, dict) else None
                    if not isinstance(verdict, str):
                        raise _Shape
                    denied = verdict.startswith("denied")
                    if not denied and (not verdict.startswith("approved") or request is None):
                        raise _Shape
                    completions.append({
                        "request": request, "call": _slug(body.get("toolCallId"), _CALL),
                        "denied": denied, "label": _slug(verdict, _CLASS) or "other",
                        "id": ident,
                    })
                    if denied and (request is None or completions[-1]["call"] is None):
                        raise _Shape
                else:
                    raise _Shape
            elif kind not in IGNORABLE_KINDS:
                raise _Shape
        except _Shape:
            proof["shapes"] = False

    if session_starts > 1 or shutdowns > 1:
        proof["shapes"] = False
    # A request nobody answered has no recorded outcome: it may be a denial.
    answered = {c["request"] for c in completions}
    if set(requests) - answered:
        proof["shapes"] = False
    # And an answer to a request never seen has lost its attribution.
    if answered - set(requests):
        proof["shapes"] = False
    # A tool call that started and never recorded an outcome may be a denial
    # whose record is missing.
    finished = {e["call"] for e in execs}
    if set(starts) - finished:
        proof["shapes"] = False

    denied: dict[str, dict] = {}
    anon_exec: list[dict] = []
    anon_perm: list[dict] = []

    def entry(call: str, ident: str | None) -> dict:
        found = denied.setdefault(call, {"perm": None, "exec": None, "tool": None, "id": None})
        if found["id"] is None:
            found["id"] = ident
        return found

    # Permission records, resolved per request. A request and its completions
    # name one operation: a completion that names another call, completions of
    # one request that disagree about the call or the verdict, or a call no
    # tool ever started cannot establish a distinct operation. Such records
    # keep their denial but lose their id.
    by_request: dict[str | None, list[dict]] = {}
    for record in completions:
        by_request.setdefault(record["request"], []).append(record)
    for request, group in by_request.items():
        asked_tool, asked_call = requests.get(request, ("other", None))
        calls = {c["call"] or asked_call for c in group} - {None}
        consistent = (
            request is not None
            and len(calls) <= 1
            and len({c["denied"] for c in group}) == 1
            and all(c["call"] in (None, asked_call) or asked_call is None for c in group)
            and all(call in starts for call in calls)
        )
        if not consistent:
            proof["correlation"] = False
        for record in group:
            if not record["denied"]:
                continue
            call = record["call"] or asked_call
            if consistent and call is not None:
                found = entry(call, record["id"])
                found["perm"], found["tool"] = record["label"], asked_tool
            else:
                anon_perm.append({"id": record["id"], "label": record["label"], "tool": asked_tool})
                proof["correlation"] = False

    # Tool records. An outcome for a call no tool started has an id nobody
    # can vouch for; one call recorded both denied and not is contradictory.
    outcomes: dict[str, set[bool]] = {}
    for record in execs:
        call = record["call"]
        if call is not None:
            outcomes.setdefault(call, set()).add(record["denied"])
        if call is not None and call not in starts:
            proof["correlation"] = False
        if not record["denied"]:
            continue
        if call is not None and call in starts:
            entry(call, record["id"])["exec"] = record["label"]
        else:
            anon_exec.append({"id": record["id"], "label": record["label"], "tool": "other"})
            proof["correlation"] = False
    if any(len(v) > 1 for v in outcomes.values()):
        proof["correlation"] = False

    count, extra = _lower_bound(denied, anon_exec, anon_perm)
    by_tool: dict[str, int] = {}
    by_class: dict[str, int] = {}
    ids: list[str] = []

    def tally(tool: str, label: str, ident: str | None) -> None:
        by_tool[tool] = by_tool.get(tool, 0) + 1
        if label not in by_class and len(by_class) >= MAX_CLASSES:
            label = "other"
        by_class[label] = by_class.get(label, 0) + 1
        if ident and ident not in ids:
            ids.append(ident)

    # Past the cap the breakdowns stop with the count, so they always sum to
    # what `observed` says.
    entries = [
        (found["tool"] if found["tool"] not in (None, "other") else starts.get(call, "other"),
         found["perm"] or found["exec"] or "other", found["id"])
        for call, found in denied.items()
    ] + [(r["tool"], r["label"], r["id"]) for r in extra]
    for tool, label, ident in entries[:MAX_COUNT]:
        tally(tool, label, ident)
    return {
        "ids": ids[:MAX_EVENT_IDS],
        "count": count,
        "by_tool": dict(sorted(by_tool.items())),
        "by_class": dict(sorted(by_class.items())),
        "rule_tools": sorted(rule_tools),
        "rules_omitted": min(omitted, MAX_RULES),
        "shutdown": shutdowns >= 1,
        "unknown_shape": not all(proof.values()),
        "proof": {**proof, "shutdown": shutdowns == 1, "bounds": count <= MAX_COUNT},
    }


def _lower_bound(
    denied: dict[str, dict], anon_exec: list[dict], anon_perm: list[dict]
) -> tuple[int, list[dict]]:
    """How many distinct calls were denied AT LEAST, and the record that stands
    for them when only uncorrelated ones were seen.

    The rule: `max(unique correlated calls, 1 if any uncorrelated denial)`.
    Uncorrelated records are never added to the correlated count, because
    nothing proves them disjoint: one call can write a `permission.completed`
    without a recoverable id and then a `tool.execution_complete` that has
    one, and a replayed or duplicated record repeats itself. Counting them
    separately could claim two calls where one happened. Read this way the
    figure is a true lower bound, an observed denial is never zero, and the
    caller marks coverage incomplete whenever an uncorrelated one exists."""
    anonymous = anon_exec or anon_perm
    if denied or not anonymous:
        return len(denied), []
    return 1, anonymous[:1]


# The positive checklist behind "complete". Each name is the reason reported
# when that fact is NOT proven. A fact missing from the evidence is unproven:
# there is no way to reach "complete" except by proving every line below.
PROOF = (
    "unverified-version",   # a schema-verified build
    "shared-pin",           # this turn alone wrote the session
    "file-replaced",        # the file read is the file the turn wrote
    "truncated",            # and it was read in full
    "file-grew",            # and it did not grow while it was read
    "oversize",             # no bound was hit
    "partial-line",         # the slice ends on a whole line
    "unknown-shape",        # every line parsed, valid, ids consistent
    "no-shutdown",          # exactly one valid shutdown
)


def _unproven(proven: dict[str, bool]) -> list[str]:
    """The ONE place "complete" is decided: the reasons in `PROOF` that were
    not proven, in report order. Empty means complete. A missing entry is
    not proven."""
    order = {name: i for i, name in enumerate(REASONS)}
    return sorted((name for name in PROOF if proven.get(name) is not True), key=order.__getitem__)


def format_denials(summary: dict) -> str:
    """The one line a turn's denials earn. Zero is printed only when the
    reader proved full coverage; an incomplete read says "at least" for what
    it saw, and says "unknown" when it saw nothing or could not look."""
    observed = summary.get("observed")
    reason = summary.get("reason")
    if summary.get("complete") and isinstance(observed, int):
        return f"denied: {observed} (complete)"
    if isinstance(observed, int) and observed > 0:
        return f"denied: at least {observed} (incomplete: {reason})"
    if isinstance(observed, int):
        return f"denied: unknown (incomplete: {reason})"
    return f"denied: unknown ({reason})"


@dataclass
class DenialProbe:
    """One turn's denial reader. `session` is the id the turn runs under:
    minted here and passed as `--session-id` (`minted`), or the caller's pin.
    `bind` runs just before the spawn and snapshots the events file; `collect`
    runs after it and never raises."""

    session: str
    minted: bool
    lock_held: bool = False
    bound: bool = False
    _early: str | None = None
    _path: Path | None = None
    _start: tuple[int, int, int] | None = None
    _attached_before: bool = False
    _version: tuple[int, ...] | None = None

    @property
    def flags(self) -> list[str]:
        return ["--session-id", self.session] if self.minted else []

    def bind(self, launch: Launch) -> None:
        """Snapshot the events file in the home the CHILD will use. Anything
        that stops this from being known is recorded, not raised: an unknown
        home stays unknown and the host's home is never read in its place.

        `launch` is the resolved launch this turn spawns (see `rig.Launch`).
        The home is read from `launch.env`, the environment the child gets, so
        it cannot diverge from the child's; the version gate asks
        `launch.binary`, the file the probes and the spawn share; and an
        isolated launch has no home this process can read."""
        self.bound = True
        try:
            self._bind(launch)
        except Exception:
            self._early = "reader-error"

    def _bind(self, launch: Launch) -> None:
        # The launch's env is built by `merge_env` and is canonical; a launch
        # made from a caller's dict is canonicalized here, so a `copilot_home`
        # alias on Windows names the same home the child gets.
        env = merge_env(launch.env)
        if launch.isolated:
            # A run_as user or a container keeps its own home; this process
            # cannot see it.
            self._early = "home-unknown"
            return
        named = (env.get("COPILOT_HOME") or "").strip()
        if not named and not (env.get("HOME") or "").strip():
            self._early = "home-unknown"
            return
        try:
            home = engine_home("copilot", env)
        except RigError:
            # A relative or `~` home is refused upstream before a turn spawns;
            # read here it is a home this process cannot name.
            self._early = "home-unknown"
            return
        if not home.is_absolute() or _slug(self.session, _ID) is None:
            self._early = "home-unknown" if not home.is_absolute() else "session-unknown"
            return
        folder = home / SESSION_DIR / self.session
        self._path = folder / EVENTS_BASENAME
        self._attached_before = folder.exists()
        try:
            st = self._path.stat()
            self._start = (st.st_dev, st.st_ino, st.st_size)
        except FileNotFoundError:
            self._start = None
        # Only the CLI itself is asked its version. Any other executable is a
        # wrapper or a stand-in that r4t cannot vouch for, and running it
        # with `--version` would be a second invocation nobody asked for: it
        # stays unverified.
        if cli_stem(launch.binary or launch.argv0) == preset_cli("copilot"):
            self._version = launch_version(launch)

    def collect(self, expect: bool = False) -> dict | None:
        """The turn's denial summary, never raising. `bind` never ran: None,
        or with `expect` an `unknown (not-started)` record, for a caller whose
        stored record would otherwise keep the previous turn's. Any failure
        inside is `reader-error`, and every summary passes `_seal`."""
        if not self.bound:
            return _empty_summary(self.session, "not-started") if expect else None
        try:
            return _seal(self._collect())
        except Exception:
            return _empty_summary(self.session, "reader-error")

    def _collect(self) -> dict:
        if self._early is not None:
            return _empty_summary(self.session, self._early)
        # One descriptor, opened without blocking and without following a
        # link, and everything below is checked on THAT descriptor: a path
        # replaced after a stat (a FIFO, another file) is never read by
        # accident, and what was read is the file whose identity was checked.
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            fd = os.open(self._path, flags)
        except FileNotFoundError:
            return _empty_summary(self.session, "session-unknown")
        except OSError:
            return _empty_summary(self.session, "reader-error")
        proven: dict[str, bool] = {}
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return _empty_summary(self.session, "reader-error")
            proven["unverified-version"] = (
                self._version is not None and self._version[:3] == SCHEMA_VERIFIED
            )
            # Attribution: only a session this turn founded can be told apart
            # from a native CLI attached to the same pin, and a founded pin is
            # only this turn's when r4t's per-session lock was held for it.
            proven["shared-pin"] = not self._attached_before and (self.minted or self.lock_held)
            offset = 0
            proven["file-replaced"] = True
            proven["truncated"] = True
            if self._start is not None:
                dev, ino, size = self._start
                if (st.st_dev, st.st_ino) != (dev, ino):
                    proven["file-replaced"] = False
                elif st.st_size < size:
                    proven["truncated"] = False
                else:
                    offset = size
            length = max(st.st_size - offset, 0)
            want = min(length, MAX_SLICE_BYTES)
            chunks: list[bytes] = []
            os.lseek(fd, offset, os.SEEK_SET)
            while want > sum(map(len, chunks)):
                piece = os.read(fd, min(1 << 20, want - sum(map(len, chunks))))
                if not piece:
                    break
                chunks.append(piece)
            data = b"".join(chunks)
            after = os.fstat(fd)
            if len(data) < want or after.st_size < offset + len(data):
                proven["truncated"] = False
            # The byte budget was fixed by the first fstat. A file that grew
            # since holds records this slice never saw; the count so far is
            # kept as a lower bound and nothing is read again.
            proven["file-grew"] = after.st_size <= st.st_size
            # The path must still be the file that was read.
            try:
                now = os.stat(self._path)
                if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
                    proven["file-replaced"] = False
            except OSError:
                proven["file-replaced"] = False
        finally:
            os.close(fd)
        proven["oversize"] = length <= MAX_SLICE_BYTES
        whole = data.rfind(b"\n") + 1
        proven["partial-line"] = whole == len(data)
        data = data[:whole]
        parsed = parse_events(data, self.session)
        proven["unknown-shape"] = not parsed["unknown_shape"]
        proven["no-shutdown"] = parsed["proof"]["shutdown"]
        count = parsed["count"]
        if count > MAX_COUNT:
            count = MAX_COUNT
            proven["oversize"] = False
        reasons = _unproven(proven)
        return {
            "observed": count,
            "complete": not reasons,
            "reason": reasons[0] if reasons else None,
            "by_tool": parsed["by_tool"],
            "by_class": parsed["by_class"],
            "event_ids": parsed["ids"],
            "rule_tools": parsed["rule_tools"],
            "rules_omitted": parsed["rules_omitted"],
            "session": self.session,
        }


# --- the per-turn instrument lifecycle --------------------------------------
#
# Two callers compose a turn's argv in different places — `engine run` from the
# preset template (engines/run.py), the roster from `Rig.argv` (dispatch.py) —
# and both have to arm the same instruments, name them in argv and environment,
# and read them back afterwards. That lifecycle lives here once. A preset says
# whether it has one with `"instruments": "copilot"` in the preset table;
# `ollama-copilot` deliberately names none, because the launcher owns the head
# of its argv and flags spliced next to the binary would reach `ollama` rather
# than the CLI behind it.

def turn_stamp() -> str:
    """A name for one turn's kept artifacts: UTC to the second plus this
    process's pid, so two turns started in the same second do not collide."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"


@dataclass
class TurnInstruments:
    """What one turn carries and reads back. Every field is None on an engine
    with no instruments, so each method is inert there rather than each caller
    carrying a special case."""

    usage_file: Path | None = None
    otel_file: Path | None = None
    max_credits: int | None = None
    denials: DenialProbe | None = None

    @property
    def armed(self) -> bool:
        return self.usage_file is not None or self.otel_file is not None

    def flags(self) -> list[str]:
        """The argv tokens naming this turn's instruments and its spend fuse,
        for whichever caller composed the rest of the argv to splice next to
        the binary."""
        argv: list[str] = []
        if self.usage_file is not None:
            argv += [USAGE_FLAG, str(self.usage_file)]
        if self.max_credits is not None:
            argv += [MAX_CREDITS_FLAG, str(self.max_credits)]
        if self.denials is not None:
            argv += self.denials.flags
        return argv

    def pass_env(self) -> dict[str, str]:
        """Names that have to cross an OS isolation boundary for this turn.
        The wrapper keeps only what it is told to keep, so an exporter armed
        in the router's environment and not named here would be armed for
        nobody."""
        return {} if self.otel_file is None else {OTEL_ENV: str(self.otel_file)}

    def env_for(self, env: dict[str, str] | None) -> dict[str, str] | None:
        """The child's environment with the exporter armed for this turn only.
        `None` in and nothing armed out keeps a caller's inherit-my-environment
        contract; arming it has to materialize the map to add one name."""
        if not self.pass_env():
            return env
        return merge_env(env if env is not None else os.environ, self.pass_env())

    def measure(self, *, denials_only: bool = False, expect_denials: bool = False) -> tuple[dict, list[str]]:
        """What the turn measured, and the lines a caller reports it with.
        Spend and spans have the same shape wherever they are reported.

        Never raises: a diagnostic section that fails is left out. A turn that
        did not finish (a spawn failure, an interruption) asks for
        `denials_only`: what the session already wrote is still evidence, but
        spend and spans of a turn that never returned are not read."""
        record: dict = {}
        lines: list[str] = []
        if self.denials is not None:
            summary = self.denials.collect(expect_denials)
            if summary is not None:
                lines.append(format_denials(summary))
                record["denials"] = summary
        if denials_only:
            return record, lines
        sections: list[tuple[dict, list[str]]] = []
        try:
            if self.otel_file is not None:
                kept = self._keep_spans()
                records = read_otel(kept) if kept is not None else None
                note = format_otel(kept, records)
                sections.append(({"otel": {
                    "path": str(kept) if kept is not None else None,
                    "records": records,
                    "note": note,
                }}, [note]))
        except Exception:
            pass
        try:
            if self.usage_file is not None:
                spend = read_usage(self.usage_file)
                if spend is not None:
                    sections.append(({"spend": spend}, [format_spend(spend)]))
        except Exception:
            pass
        # The historical order: spans, spend, then denials.
        merged: dict = {}
        out: list[str] = []
        for fields, section_lines in sections:
            merged.update(fields)
            out += section_lines
        if "denials" in record:
            merged["denials"] = record["denials"]
            out += lines
        return merged, out

    def _keep_spans(self) -> Path | None:
        """Exported spans moved out of the turn's scratch into the durable
        store. The scratch may be a member's own working directory, and a
        member's workdir is not r4t's filing cabinet."""
        if self.otel_file is None or not self.otel_file.is_file():
            return None
        kept = otel_path(turn_stamp())
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.otel_file), str(kept))
        return kept

    def clear(self) -> None:
        for path in (self.usage_file, self.otel_file):
            if path is not None:
                path.unlink(missing_ok=True)


def instrument_env(kind: str | None, scratch: Path) -> dict[str, str]:
    """The environment a turn of this kind carries for its instruments,
    computed before the turn's launch is built so the launch's env is final:
    `turn_instruments` names the same exporter path and checks the launch holds
    it."""
    if kind != "copilot":
        return {}
    return {OTEL_ENV: str(Path(scratch) / OTEL_BASENAME)}


def _denial_probe(session: str | None, lock_held: bool, launch: Launch) -> DenialProbe | None:
    """The turn's denial reader. A pinned turn reads the pin; any other turn
    mints a UUID and passes it as `--session-id`, so the events file is known
    before the CLI starts instead of discovered afterwards. Never both: a pin
    is already a `--session-id` or `--resume` in the argv. A build too old for
    the usage flag (1.0.82, the oldest the pin was measured on) is not handed
    a flag it may reject: an unknown option aborts the turn."""
    if session:
        return DenialProbe(session=session, minted=False, lock_held=lock_held)
    if not supports_usage_file(launch):
        return None
    return DenialProbe(session=str(uuid.uuid4()), minted=True)


@contextmanager
def turn_instruments(
    kind: str | None,
    *,
    scratch: Path,
    max_credits: int | None = None,
    launch: Launch | None = None,
    session: str | None = None,
    lock_held: bool = False,
) -> Iterator[TurnInstruments]:
    """Arm one turn's instruments in `scratch`, and clear them afterwards.

    `kind` is the preset's `instruments` value — None, or anything this module
    does not answer for, yields an inert set. The usage flag is 1.0.82+ and is
    feature-detected rather than assumed, so an older binary still gets the
    exporter and the fuse; the exporter needs no flag at all.
    """
    if kind != "copilot":
        yield TurnInstruments()
        return
    if launch is not None and launch.env.get(OTEL_ENV) != str(scratch / OTEL_BASENAME):
        # The launch's env is final and carries the exporter path; one that
        # does not would spawn without the instrument it is about to read.
        raise RuntimeError(
            "the launch was built without this turn's exporter path "
            "(copilot.instrument_env)"
        )
    launch = launch or resolve_launch("", {}, cwd=scratch)
    instruments = TurnInstruments(
        usage_file=(scratch / USAGE_BASENAME) if supports_usage_file(launch) else None,
        otel_file=scratch / OTEL_BASENAME,
        max_credits=max_credits,
        denials=_denial_probe(session, lock_held, launch),
    )
    # Before, not only after: a turn killed hard leaves its files behind, and
    # reading those would report the previous turn's spend as this one's.
    instruments.clear()
    try:
        yield instruments
    finally:
        instruments.clear()
