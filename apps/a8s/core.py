"""a8s core — paths, logging, Participant, leaf-level helpers.

This module has no a8s sibling imports. Everything else (registry, mailbox,
definitions, daemon, commands, cli) imports from here.

Mutable module-level state:
  PRINT_LOCK   — None at module load. `daemon.attached_loop` sets it to a
                 threading.Lock so concurrent log writes serialize. `out` and
                 `out_agent` reference it through this module so updates are
                 visible across imports (`import core; core.PRINT_LOCK = ...`).
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# `arkver` sits at the repo root and carries the suite semver. A copy of this
# tree relocated away from that root (the isolation container copies apps/r4t
# alone to /opt/r4t) still has to run: the version is a nicety, never a
# dependency, so a missing module degrades to "unknown" instead of killing the
# CLI on import.
sys.path.append(str(Path(__file__).resolve().parents[2]))
try:
    from arkver import version_line  # noqa: E402
except ImportError:
    def version_line(app: str) -> str:
        return f"{app} unknown (The Ark)"

from ark import envseam  # noqa: E402
from ark.home import app_home  # noqa: E402

# ---------- constants ----------

MARKER_FILES = {
    "CLAUDE.md": "claude",
    "GEMINI.md": "agy",
    "CODEX.md": "codex",
    # Copilot's native repo-instructions location. Operators with a
    # repo-wide `copilot-instructions.md` who don't intend the dir to be
    # an a8s agent should expect `a8s discover` to surface it as a
    # candidate — `discover` is read-only and only suggests, never adds.
    ".github/copilot-instructions.md": "copilot",
    "CURSOR.md": "cursor",
    # Tool-agnostic standard (https://agents.md/) adopted by 20+ tools.
    # Listed LAST so kind-specific markers above always win when both are
    # present; AGENTS.md alone falls through to OpenCode (the BYO-model
    # default — operator picks the actual provider via per-agent
    # opencode.json).
    "AGENTS.md": "opencode",
}

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

# File-transfer cap for FILE: payloads. Larger sources are dropped at routing
# time with a log line; agents needing larger payloads should use a side-
# channel (TempFile.org-style staging supports 100 MiB).
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB

# Path constants — computed once at module load.
SCRIPT_DIR = Path(__file__).resolve().parent
BIN_ROOT = SCRIPT_DIR.parent.parent
DEFINITIONS_DIR = SCRIPT_DIR / "definitions"
# a8s owns this reserved-env contract; ark.envseam holds the canonical names
# so r4t can refuse a rig config the same two vars without hand-listing them.
TELL_OUTBOX_DIR_ENV = envseam.TELL_OUTBOX_DIR_ENV
TELL_FILE_MAX_ENV = envseam.TELL_FILE_MAX_ENV
# Explicit path for `cmd_start`'s re-exec. After the modular split, `__file__`
# resolved inside any module would point at that module — not the entry script.
ENTRYPOINT = SCRIPT_DIR / "a8s.py"

# Mutable: `daemon.attached_loop` sets this to a threading.Lock so log writes
# from concurrent paths serialize. Read by `out` and `out_agent` below.
PRINT_LOCK: threading.Lock | None = None


# ---------- a8s state paths ----------

def resolve_a8s_home() -> Path:
    """State-root directory (registry, mailboxes, network, secrets).

    Resolution order:
      1. ``A8S_HOME`` if set (tests, sandboxes, explicit relocate)
      2. ``XDG_CONFIG_HOME/a8s`` (or ``~/.config/a8s``) if that directory
         already exists
      3. ``~/.a8s`` if that directory already exists (legacy)
      4. ``XDG_CONFIG_HOME/a8s`` (or ``~/.config/a8s``, default for new
         installs)

    Does not create the directory — callers that write should mkdir. See
    ``ark.home.app_home`` — every app in the suite resolves through it.
    """
    return app_home("a8s", os.environ.get("A8S_HOME"), legacy=Path.home() / ".a8s")


def _a8s_dir() -> Path:
    """State directory for the registry, mailboxes, and process logs.

    See ``resolve_a8s_home`` for path selection. Creates the directory on
    first use. Overridden wholesale via ``A8S_HOME`` (not ``A8S_DIR`` —
    that token is reserved for the install path in definition argv).
    """
    base = resolve_a8s_home()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _log_path() -> Path:
    """Process-scoped log: loop start/stop, registration, things without a
    specific agent context. Per-agent activity goes in `agent_log_path(name)`."""
    return _a8s_dir() / "log.txt"


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def canonical_name(name: str) -> str:
    """Canonical form of an agent or alias name: stripped, lowercase, validated
    against NAME_RE. Raises ValueError on invalid input. Used at registration
    boundaries (`a8s add`, `a8s alias`) so the on-disk directory key is the
    same regardless of input casing — eliminates the case-collision footgun
    where `claude` and `Claude` produced two separate agent dirs."""
    s = (name or "").strip().lower()
    if not s or not NAME_RE.fullmatch(s):
        raise ValueError(
            f"name must be alphanumeric (with -, _) and start with a letter or digit: {name!r}"
        )
    return s


def agent_dir(name: str) -> Path:
    """Per-agent internal directory under ~/.config/a8s/. Holds inbox/, trash/,
    log.txt, and the pid file."""
    return _a8s_dir() / "agents" / _safe_name(name)


def inbox_dir(name: str) -> Path:
    return agent_dir(name) / "inbox"


def inbox_tmp_dir(name: str) -> Path:
    """Maildir-style staging dir. `route_outboxes` writes routed copies here
    first and renames them into `inbox/` only after every recipient's stage
    succeeds — so a crash mid-fan-out leaves no partial state to be re-routed
    as duplicates."""
    return agent_dir(name) / "inbox.tmp"


def trash_dir(name: str) -> Path:
    return agent_dir(name) / "trash"


def agent_log_path(name: str) -> Path:
    return agent_dir(name) / "log.txt"


def pid_path(name: str) -> Path:
    return agent_dir(name) / "pid"


def detach_request_path(name: str) -> Path:
    """Per-agent detach-request file. A process that wants to take over <name>
    writes its own pid here, then polls for the holder to release. The holder's
    `attached_loop` checks this file at the top of each iteration and, when the
    request is from a different pid, releases ONLY <name> (not its other handled
    agents) and clears the request. This is how take-over moves a single agent
    between processes without orphaning the holder's siblings."""
    return agent_dir(name) / "detach-request"


def kill_request_path(name: str) -> Path:
    """Per-agent kill-request file. `a8s kill <name>` writes its pid here and
    SIGUSR1s the holder. The holder's iteration top releases just <name>;
    its SIGUSR1 handler additionally kills the in-flight wake subprocess
    group iff the current wake target matches — so a long-running LLM call
    for <name> dies immediately while siblings keep running."""
    return agent_dir(name) / "kill-request"


def last_active_path(name: str) -> Path:
    """Per-agent last-activity timestamp. Single-line ISO-8601 UTC string,
    updated at the start of every wake and at the end of every idle-invoke
    run. `attached_loop` reads it to decide whether the agent has been idle
    long enough to fire `definition.idle.invoke`. Persists across handler
    restarts so an idle that was about to fire still fires after a restart."""
    return agent_dir(name) / "last-active"


def read_last_active(name: str) -> datetime | None:
    """Returns the parsed timestamp, or None if the file is missing /
    unreadable / unparseable. None signals "no prior activity recorded";
    callers typically fall back to "now" and write the file."""
    p = last_active_path(name)
    if not p.is_file():
        return None
    try:
        s = p.read_text().strip()
    except OSError:
        return None
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def touch_last_active(name: str, when: datetime | None = None) -> None:
    """Write `when` (or now) into the agent's last-active file. Best-effort
    — disk failures don't propagate (a missed write means the next idle
    check might fire one cycle late)."""
    ts = (when or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    p = last_active_path(name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ts)
    except OSError:
        pass


def inbox_waiting_since_path(name: str) -> Path:
    """When the first inbox message of a burst arrived. `attached_loop` reads
    this to debounce wakes per `definition.pause` so closely-spaced tells can
    accumulate before invoke/batch runs."""
    return agent_dir(name) / "inbox-waiting-since"


def read_inbox_waiting_since(name: str) -> datetime | None:
    p = inbox_waiting_since_path(name)
    if not p.is_file():
        return None
    try:
        s = p.read_text().strip()
    except OSError:
        return None
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def touch_inbox_waiting_since(name: str, when: datetime | None = None) -> None:
    ts = (when or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    p = inbox_waiting_since_path(name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ts)
    except OSError:
        pass


def clear_inbox_waiting_since(name: str) -> None:
    try:
        inbox_waiting_since_path(name).unlink(missing_ok=True)
    except OSError:
        pass


def wake_retry_path(name: str) -> Path:
    """Per-agent wake-delivery retry record, written when a wake fails and its
    envelopes go back into the inbox. JSON: `{"unit": [<inbox filenames>],
    "attempts": N, "next_at": "<ISO-8601 UTC>"}`. `unit` identifies which
    delivery the attempt count belongs to — a different message (or batch)
    starts the count over. Cleared the moment a wake exits 0. Persists across
    handler restarts so a restart can't reset the backoff and spin a
    permanently broken CLI."""
    return agent_dir(name) / "wake-retry"


def read_wake_retry(name: str) -> dict | None:
    p = wake_retry_path(name)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_wake_retry(
    name: str, unit: list[str], attempts: int, next_at: datetime
) -> None:
    p = wake_retry_path(name)
    body = json.dumps({
        "unit": sorted(unit),
        "attempts": attempts,
        "next_at": next_at.isoformat().replace("+00:00", "Z"),
    })
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    except OSError:
        pass


def clear_wake_retry(name: str) -> None:
    try:
        wake_retry_path(name).unlink(missing_ok=True)
    except OSError:
        pass


def pending_dir(name: str) -> Path:
    """Ingested-but-not-yet-fully-routed messages. `route_outboxes` atomically
    moves each new file from `<root>/.outbox/` into here on every pass before
    parsing or producing any retry sidecars — the agent's outbox is one-way
    (agent writes, a8s renames out), and everything a8s does after the rename
    happens under ~/.config/a8s/. Sidecar metadata (`<file>.retry`) lives alongside
    the pending file in this dir."""
    return agent_dir(name) / "pending"


def retry_sidecar_path(pending_file: Path) -> Path:
    """Companion file to a pending message: `<file>.json.retry`. Tracks
    attempts, next-attempt time, and which configured remotes have already
    accepted the publish. Lifetime tied to the pending file — happy path
    deletes both, exhaustion moves the message to trash and unlinks the
    sidecar."""
    return pending_file.with_suffix(pending_file.suffix + ".retry")


def resolve_outbox_path(agent_root: Path, spec: str | None = None) -> Path:
    """Resolve an outbox directory from a definition `outbox_dir` value.

    Relative paths are under `agent_root`; absolute paths are used as-is.
    Default / omitted spec is `.outbox` under the agent root."""
    raw = (spec if spec is not None else ".outbox").strip() or ".outbox"
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (agent_root / p).resolve()


def outbox_dir(root: Path) -> Path:
    """Default outbox path: `<agent-root>/.outbox`."""
    return resolve_outbox_path(root)


def outbox_bundle_dir(outbox: Path, msg_id: str) -> Path:
    """Outgoing attachment bundle for one message — lives beside its JSON in
    `.outbox/<msg_id>/` until ingest moves it to pending."""
    return outbox / msg_id


def files_dir(root: Path) -> Path:
    """Default incoming attachment root: `<agent-root>/.files`."""
    return resolve_files_path(root)


def resolve_inbox_path(agent_root: Path, spec: str | None = None) -> Path:
    """Resolve a file-proxy inbox directory from definition `inbox_dir`.

    Relative paths are under `agent_root`; absolute paths are used as-is.
    Default / omitted spec is `.inbox` under the agent root."""
    raw = (spec if spec is not None else ".inbox").strip() or ".inbox"
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (agent_root / p).resolve()


def resolve_files_path(agent_root: Path, spec: str | None = None) -> Path:
    """Resolve an incoming-files directory from a definition `files_dir` value.

    Relative paths are under `agent_root`; absolute paths are used as-is.
    Default / omitted spec is `.files` under the agent root."""
    raw = (spec if spec is not None else ".files").strip() or ".files"
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (agent_root / p).resolve()


def inbound_bundle_dir(files_root: Path, msg_id: str) -> Path:
    """`<files_root>/<msg_id>/` — one bundle per message."""
    return files_root / msg_id


def pending_bundle_dir(name: str, msg_id: str) -> Path:
    return pending_dir(name) / msg_id


def registry_path() -> Path:
    return _a8s_dir() / "a8s.json"


def network_config_path() -> Path:
    """Configured remotes / services (non-secret). Absent → local-only."""
    return _a8s_dir() / "network.json"


def secrets_config_path() -> Path:
    """Secret overlay for remotes/services (mode 0600). Merged at load time."""
    return _a8s_dir() / "secrets.json"


def settings_path() -> Path:
    """`~/.config/a8s/settings.json` — operator settings (`a8s config`)."""
    return _a8s_dir() / "settings.json"


def user_definitions_dir() -> Path:
    """User-installed definition templates under ``~/.config/a8s/definitions/``.

    Bare names for ``a8s add`` / ``a8s define`` resolve here after the
    repo-bundled ``DEFINITIONS_DIR``. Install with ``a8s defs add``.
    """
    return _a8s_dir() / "definitions"


def conversations_path() -> Path:
    """Conversation archive under the resolved a8s state root."""
    return _a8s_dir() / "conversations.sqlite3"


def transactions_path() -> Path:
    """Routing transaction log under the resolved a8s state root.

    Separate from the conversation archive: breadcrumbs arrive several per
    message and are pruned on their own retention schedule.
    """
    return _a8s_dir() / "transactions.sqlite3"


def seen_ids_path() -> Path:
    """Single cluster-wide ring file holding the last MAX_SEEN_IDS message
    IDs the receive loops have written into local inboxes. Receive-side dedup
    lookups read this; appends rotate when the cap is hit. Cluster-wide (not
    per-agent) because a duplicate envelope can target any local agent and we
    only need to know whether we've ever delivered it."""
    return _a8s_dir() / "seen-ids"


# Receive-side dedup ring cap. 26 chars per ULID + newline = 27 bytes per row;
# 10k rows ≈ 270 KiB, comfortably below any sane filesystem block budget.
MAX_SEEN_IDS = 10000

# Per-message retry backoff. Index = number of failed attempts so far. After
# the schedule is exhausted (MAX_ATTEMPTS = len), the message is moved to trash
# with a "discarded after backoff exhausted" log line.
BACKOFF_SCHEDULE = [30, 60, 120, 300, 900, 1800, 3600, 21600, 86400]
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE)

# Wake-delivery retry backoff. Index = number of failed wakes so far for the
# same delivery. Deliberately shorter and shallower than BACKOFF_SCHEDULE: a
# redelivered envelope costs an LLM turn, so duplicates are expensive and a
# poison envelope must stop blocking the inbox quickly. MAX_WAKE_ATTEMPTS
# counts the first attempt too, so the schedule holds the retry delays only.
WAKE_RETRY_SCHEDULE = [30, 120, 600]
MAX_WAKE_ATTEMPTS = len(WAKE_RETRY_SCHEDULE) + 1


# ---------- general helpers ----------

def _preview(content: str, n: int = 80) -> str:
    """Single-line snippet of `content` for log readability."""
    s = (content or "").replace("\n", " ").replace("\r", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def unique_path(p: Path) -> Path:
    """Return `p` if it doesn't exist; otherwise `p.stem.<N><suffix>` where N
    is the smallest positive integer that doesn't collide."""
    if not p.exists():
        return p
    i = 1
    while True:
        candidate = p.with_name(f"{p.stem}.{i}{p.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------- logging ----------

def _ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _append(path: Path, ts_line: str) -> None:
    """Append `ts_line` (already timestamp-prefixed and newline-terminated) to
    `path`. Best-effort: a missing directory is created lazily; OSError swallows."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(ts_line)
    except OSError:
        pass


def _emit_supervisor(line: str) -> None:
    """Stdout + supervisor log (process-scoped events only)."""
    sys.stdout.write(line)
    sys.stdout.flush()
    ts_line = f"{_ts()} {line}"
    if not ts_line.endswith("\n"):
        ts_line += "\n"
    _append(_log_path(), ts_line)


def _emit_agent(name: str, line: str) -> None:
    """Stdout + per-agent log only. Does NOT write to the supervisor log —
    agent-scoped events live in `~/.config/a8s/agents/<NAME>/log.txt` and `a8s logs`
    reads them directly."""
    sys.stdout.write(line)
    sys.stdout.flush()
    ts_line = f"{_ts()} {line}"
    if not ts_line.endswith("\n"):
        ts_line += "\n"
    _append(agent_log_path(name), ts_line)


def out(text: str = "", end: str = "\n") -> None:
    """Process-scoped output (loop lifecycle, registration, etc.). For
    agent-scoped lines use `out_agent(name, ...)`."""
    line = text + end
    if PRINT_LOCK is not None:
        with PRINT_LOCK:
            _emit_supervisor(line)
    else:
        _emit_supervisor(line)


def out_agent(name: str, text: str = "", end: str = "\n") -> None:
    """Agent-scoped output. Lands in `~/.config/a8s/agents/<NAME>/log.txt`."""
    line = text + end
    if PRINT_LOCK is not None:
        with PRINT_LOCK:
            _emit_agent(name, line)
    else:
        _emit_agent(name, line)


# ---------- types ----------

@dataclass(frozen=True)
class Participant:
    name: str
    root: Path
    safe_dirs: tuple[Path, ...] = ()
    outbox: Path | None = None
    files: Path | None = None
    inbox: Path | None = None

    def outbox_path(self) -> Path:
        if self.outbox is not None:
            return self.outbox
        return outbox_dir(self.root)

    def files_path(self) -> Path:
        if self.files is not None:
            return self.files
        return files_dir(self.root)

    def inbox_path(self) -> Path:
        if self.inbox is not None:
            return self.inbox
        return resolve_inbox_path(self.root)

    def files_bundle_dir(self, msg_id: str) -> Path:
        return inbound_bundle_dir(self.files_path(), msg_id)
