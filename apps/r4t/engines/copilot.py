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

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from engines.base import QuotaError
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


def _config_payload() -> dict:
    """`~/.copilot/config.json`, which is JSONC — it opens with a `// User
    settings` comment line — so the comments come out before json reads it."""
    try:
        text = (copilot_home() / "config.json").read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def token() -> str | None:
    """The OAuth token to call the endpoint with, or None.

    `copilot help environment` gives the CLI's own precedence and this follows
    it: COPILOT_GITHUB_TOKEN, then GH_TOKEN, then GITHUB_TOKEN, then the login
    the CLI stored for itself. The stored form is `copilotTokens` in the config
    file, keyed by host on the seat where it was read; both the bare-string and
    the keyed shapes are accepted because only one of them has been seen. The
    value is returned and never logged, printed or put in an error message.
    """
    for name in TOKEN_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    stored = _config_payload().get("copilotTokens")
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


def quota() -> dict:
    """The entitlement endpoint, through whichever transport this machine has.

    `gh` is a convenience, not a prerequisite. A seat authenticated to Copilot
    throughout was answering `gh is not on PATH` on a machine that held a
    perfectly good token, which is a wrong answer rather than a missing one, so
    a token from the environment or from copilot's own stored login carries the
    call directly when `gh` cannot.
    """
    if shutil.which("gh"):
        return parse_user(_gh_user())
    auth = token()
    if auth is None:
        raise QuotaError(
            "gh is not on PATH and no Copilot token is readable (set "
            f"{' / '.join(TOKEN_ENV_VARS)}, or sign in with `copilot` so it "
            "stores one)"
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

def _probe(argv: list[str]) -> str:
    """One no-turn probe of the CLI, its output as text. A binary that cannot
    answer yields the empty string rather than an exception: every caller here
    is best-effort by design."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(g) for g in match.groups()) if match else None


_usage_support: dict[str, bool] = {}


def supports_usage_file(binary: str = "copilot") -> bool:
    """Whether the installed CLI accepts `--usage-output-file`.

    Probed once per process, `--help` first: this Mac's 1.0.82 lists the flag,
    so the cheapest probe is also the direct one. `--help` is a floor rather
    than a census on this CLI — flags run that it never lists — so a help text
    without the flag falls back to the version boundary instead of concluding
    no. Passing the flag speculatively is the alternative, and it is worse: an
    unknown option aborts the run before the turn starts, and `run.py` streams
    the child's stdout rather than capturing it, so nothing could see the error
    to retry on.
    """
    if binary not in _usage_support:
        if shutil.which(binary) is None:
            return False
        if USAGE_FLAG in _probe([binary, "--help"]):
            _usage_support[binary] = True
        else:
            version = _version_tuple(_probe([binary, "--version"]))
            _usage_support[binary] = bool(
                version and version >= USAGE_FILE_MIN_VERSION
            )
    return _usage_support[binary]


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


def copilot_home() -> Path:
    """Where the CLI keeps its state. `COPILOT_HOME` relocates the root."""
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def session_exists(session_id: str) -> bool:
    """Whether this session has already been founded on this machine.
    `--session-id` names the directory too, so the store answers directly and
    a pinned run can be located on disk without a discovery step."""
    return (copilot_home() / "session-state" / session_id).is_dir()


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
        return {**(env if env is not None else os.environ), **self.pass_env()}

    def measure(self) -> tuple[dict, list[str]]:
        """What the turn measured, and the lines a caller reports it with.
        Spend and spans have the same shape wherever they are reported."""
        record: dict = {}
        lines: list[str] = []
        if self.otel_file is not None:
            kept = self._keep_spans()
            records = read_otel(kept) if kept is not None else None
            note = format_otel(kept, records)
            lines.append(note)
            record["otel"] = {
                "path": str(kept) if kept is not None else None,
                "records": records,
                "note": note,
            }
        if self.usage_file is not None:
            spend = read_usage(self.usage_file)
            if spend is not None:
                lines.append(format_spend(spend))
                record["spend"] = spend
        return record, lines

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


@contextmanager
def turn_instruments(
    kind: str | None, *, scratch: Path, max_credits: int | None = None
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
    instruments = TurnInstruments(
        usage_file=(scratch / USAGE_BASENAME) if supports_usage_file() else None,
        otel_file=scratch / OTEL_BASENAME,
        max_credits=max_credits,
    )
    # Before, not only after: a turn killed hard leaves its files behind, and
    # reading those would report the previous turn's spend as this one's.
    instruments.clear()
    try:
        yield instruments
    finally:
        instruments.clear()
