"""Out-of-repo roster state under ~/.config/r4t/rosters/<node>/ (honors
XDG_CONFIG_HOME; relocate wholesale with R4T_HOME, mirroring how a8s
honors A8S_HOME).

    rosters/<node>/
    ├── agents/<name>/history.md   rolling conversation memory (messages only), ~8KB cap
    ├── agents/<name>/queue/       durable inbound queue — one envelope per file,
    │                              ULID-named; a turn drains the whole queue at once
    ├── agents/<name>/.lock        PID lockfile — one turn per agent at a time
    ├── agents/<name>/.turn.json   in-flight turn: thread/hop/sender;
    │                              a leftover file with no live lock = crashed turn
    ├── agents/<name>/meta.json    last inbound / last completed turn bookkeeping
    ├── agents/<name>/staging/     per-turn $TELL_OUTBOX_DIR — envelopes the agent
    │                              sent this turn, released by dispatch afterwards
    ├── agents/<name>/delivered/   per-turn bundles of inbound attachment copies
    │                              for isolated turns (most recent 50 kept)
    ├── agents/<name>/mcp/         config the `mcp` knob hands the harness to read
    │                              (rig.py); readable behind an isolation boundary,
    │                              and the one dir a container mounts for it
    ├── agents/<name>/live.log     the running turn's harness output, teed live
    │                              (truncated at turn start; a gemba attach tails it)
    ├── agents/<name>/turns/       per-turn capture — one markdown file per turn
    │                              (full prompt + raw output; most recent 50 kept)
    ├── tasks/<id>.json            thread ledger (see tasks.py)
    ├── dead-letter/               undeliverable mail (unknown recipient, malformed)
    ├── buckets.json               per-member + roster spend budgets (turns, not tokens)
    ├── rotation.json              per-rig round-robin index for harness pools
    ├── last-turn-start            cadence stamp for the roster throttle
    ├── log/<date>.md              full I/O transcript, append-only; `r4t clear`
    │                              drops whole days past `log_retention_days`
    ├── velocity.csv               one row per harness turn, current month
    └── velocity-<month>.csv       finished months, rotated out by `r4t clear`
                                   and never pruned (the cost record)

One file sits ABOVE the rosters, at the R4T_HOME root — rig-buckets.json, the
machine-global rig spend buckets: a rig maps to a real subscription shared by
every roster on the machine, so its bucket cannot be per-roster.

Never inside the repo: the working tree is only touched by the harness
subprocesses themselves.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# The isolation test (tests/docker/run-as.sh) copies apps/r4t alone into a
# container with no repo root, so `ark` is not always reachable — unlike
# arkver's "unknown version" degrade, a ULID has no no-op fallback, so the
# except branch carries a working minimal reimplementation instead of a stub.
try:
    from ark.ulid import new as new_ulid
except ImportError:
    import secrets

    def new_ulid() -> str:
        alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
        rnd = int.from_bytes(secrets.token_bytes(10), "big")
        n = (ts_ms << 80) | rnd
        chars = [alphabet[(n >> (5 * i)) & 0x1F] for i in range(25, -1, -1)]
        return "".join(chars)

# Same relocation concern as `new_ulid` above; r4t carries no legacy config
# dir, so the fallback needs neither an override nor a migration path.
try:
    from ark.home import app_home as _app_home
except ImportError:
    def _app_home(app: str, env_override: str | None) -> Path:
        override = (env_override or "").strip()
        if override:
            return Path(override).expanduser()
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
        return base / app

# Same relocation concern again; the fallback keeps the same-dir-tmp +
# os.replace + cleanup-on-failure contract, minus the fsync/mode knobs no
# r4t call site needs.
try:
    from ark.fsio import atomic_write_text as _atomic_write
except ImportError:
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{new_ulid()}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

_queue_seq = itertools.count()

HISTORY_ENTRY_RE = re.compile(r"(?m)^(?=## )")
VELOCITY_HEADER = "timestamp,agent,rig,thread,hop,duration_seconds,exit_code\n"
DAY_LOG_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
VELOCITY_MONTH_RE = re.compile(r"(\d{4}-\d{2})-\d{2}T")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def r4t_home() -> Path:
    return _app_home("r4t", os.environ.get("R4T_HOME"))


def rosters_dir() -> Path:
    return r4t_home() / "rosters"


def roster_dir(node: str) -> Path:
    return rosters_dir() / node.strip().lower()


def agent_dir(node: str, name: str) -> Path:
    return roster_dir(node) / "agents" / name.strip().lower()


def known_rosters() -> list[str]:
    root = rosters_dir()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def root_path(node: str) -> Path:
    return roster_dir(node) / "root"


def stamp_root(node: str, root: Path) -> None:
    path = root_path(node)
    text = str(root)
    try:
        if path.is_file() and path.read_text(encoding="utf-8").strip() == text:
            return
    except OSError:
        pass
    _atomic_write_text(path, text + "\n")


def read_root(node: str) -> Path | None:
    try:
        text = root_path(node).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text) if text else None


def node_for_root(cwd: Path) -> str | None:
    """The roster whose stamped org-dir root — or, in a portable org, whose
    workplace repo — is cwd or an ancestor of it. The org-dir root matches
    first; the workplace is a fallback so that standing in the repo a portable
    roster works in also infers the node. A workplace shared by two org dirs (the
    A/B case) is ambiguous and matches neither, so the caller still asks for
    --node."""
    from org import load_org

    by_root: dict[Path, str] = {}
    by_workplace: dict[Path, str | None] = {}
    for node in known_rosters():
        root = read_root(node)
        if root is None:
            continue
        by_root[root] = node
        org = load_org(root)
        if org.is_portable:
            by_workplace[org.workplace] = (
                None if org.workplace in by_workplace else node
            )
    cwd = cwd.resolve()
    for candidate in (cwd, *cwd.parents):
        if candidate in by_root:
            return by_root[candidate]
    for candidate in (cwd, *cwd.parents):
        node = by_workplace.get(candidate)
        if node is not None:
            return node
    return None


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write(path, text)


def atomic_write_json(path: Path, payload: dict) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2))


# ---------- history ----------

def history_path(node: str, name: str) -> Path:
    return agent_dir(node, name) / "history.md"


def read_history(node: str, name: str) -> str:
    path = history_path(node, name)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _truncate_history(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    entries = [e for e in HISTORY_ENTRY_RE.split(text) if e.strip()]
    while len(entries) > 1 and len("".join(entries).encode("utf-8")) > max_bytes:
        entries.pop(0)
    return "".join(entries)


def append_history(
    node: str, name: str, entry: str, *, max_bytes: int = 8192
) -> None:
    current = read_history(node, name).rstrip()
    combined = (current + "\n\n" if current else "") + entry.strip() + "\n"
    _atomic_write_text(history_path(node, name), _truncate_history(combined, max_bytes))


def archive_history(node: str, name: str) -> Path | None:
    """Rename the member's history log to a timestamped sibling, so the next
    turn starts a fresh one and the prompt carries no transcript. Returns the
    archive path, or None when the member has no history. Never deletes — the
    log is the record of what was said."""
    path = history_path(node, name)
    if not path.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = path.with_name(f"history-{stamp}.md")
    os.replace(path, archive)
    return archive


# ---------- locks ----------

# Same relocation concern as the ark imports at the top; the container copy
# is POSIX-only, so the fallback keeps only the POSIX probe.
try:
    from ark.proc import pid_alive as _pid_alive
except ImportError:
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


class AgentLock:
    def __init__(self, node: str, name: str) -> None:
        self.path = agent_dir(node, name) / ".lock"
        self.acquired = False

    def acquire(self, rig: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"pid": os.getpid(), "rig": rig, "started": utc_now()}
        )
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = read_lock(self.path)
                if holder is not None and _pid_alive(int(holder.get("pid", 0) or 0)):
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            self.acquired = True
            return True
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self.acquired = False


class ProcessLock:
    """Exclusive PID lock for short cross-process state transactions."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "started": utc_now()})
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = read_lock(self.path)
                if holder is not None and _pid_alive(int(holder.get("pid", 0) or 0)):
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            self.acquired = True
            return True
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self.acquired = False


def admission_lock(node: str) -> ProcessLock:
    return ProcessLock(roster_dir(node) / ".admission.lock")


def task_lock(node: str, task_id: str) -> ProcessLock:
    return ProcessLock(roster_dir(node) / "tasks" / f".{task_id}.lock")


def read_lock(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def live_locks(node: str, *, prune: bool = True) -> list[dict]:
    """Scan agents/*/.lock; return live ones as dicts with an `agent` key.
    Dead-PID locks are removed when `prune` (they're stale by definition)."""
    agents_root = roster_dir(node) / "agents"
    if not agents_root.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(agents_root.iterdir()):
        lock_path = entry / ".lock"
        if not lock_path.is_file():
            continue
        data = read_lock(lock_path)
        pid = int((data or {}).get("pid", 0) or 0)
        if data is None or not _pid_alive(pid):
            if prune:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
            continue
        data["agent"] = entry.name
        out.append(data)
    return out


def count_rig_locks(node: str, rig: str) -> int:
    key = rig.lower()
    return sum(1 for lock in live_locks(node) if str(lock.get("rig", "")).lower() == key)


def prune_stale_locks(node: str) -> int:
    agents_root = roster_dir(node) / "agents"
    if not agents_root.is_dir():
        return 0
    before = sum(1 for e in agents_root.iterdir() if (e / ".lock").is_file())
    after = len(live_locks(node, prune=True))
    return max(0, before - after)


# ---------- durable member queue (batch invoke; nothing is ever dropped) ----------

def queue_dir(node: str, name: str) -> Path:
    return agent_dir(node, name) / "queue"


def _normalize_body(text: str) -> str:
    return " ".join((text or "").lower().split())


def list_queue(node: str, name: str) -> list[Path]:
    d = queue_dir(node, name)
    if not d.is_dir():
        return []
    return sorted(f for f in d.iterdir() if f.is_file() and f.name.endswith(".json"))


def read_queue(node: str, name: str) -> list[dict]:
    out: list[dict] = []
    for path in list_queue(node, name):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def queue_depth(node: str, name: str) -> int:
    return len(list_queue(node, name))


def enqueue(node: str, name: str, envelope: dict) -> Path:
    """Append an inbound envelope to a member's durable queue. Duplicate
    collapse (the only suppression left): if the NEWEST queued entry has the
    same sender and identical normalized body, bump its `repeats` count and
    re-stamp instead of adding a file — collapsing loses no information."""
    d = queue_dir(node, name)
    d.mkdir(parents=True, exist_ok=True)
    existing = list_queue(node, name)
    if existing:
        newest = existing[-1]
        try:
            prev = json.loads(newest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prev = None
        if (
            isinstance(prev, dict)
            and str(prev.get("from", "")).strip().lower()
            == str(envelope.get("from", "")).strip().lower()
            and _normalize_body(str(prev.get("body", "")))
            == _normalize_body(str(envelope.get("body", "")))
        ):
            prev["repeats"] = int(prev.get("repeats", 1) or 1) + 1
            prev["queued_at"] = utc_now()
            atomic_write_json(newest, prev)
            return newest
    env = dict(envelope)
    env.setdefault("id", new_ulid())
    env.setdefault("repeats", 1)
    env.setdefault("queued_at", utc_now())
    # The FILENAME orders the queue, so it must be monotonic in arrival order —
    # ULIDs are not, within a millisecond. A wall-clock nanosecond stamp plus a
    # process-local counter is: two enqueues in one process never collide, and
    # separate wake processes are genuinely ordered by wall time.
    path = d / f"{time.time_ns():020d}-{next(_queue_seq):06d}.json"
    atomic_write_json(path, env)
    return path


def claim_queue(node: str, name: str) -> list[dict]:
    """Read and remove every currently-queued envelope in arrival order.
    Called under the agent lock at turn start, so no two turns claim the same
    batch; envelopes arriving mid-turn are written after this snapshot and
    ride the next turn."""
    entries: list[dict] = []
    for path in list_queue(node, name):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            continue
        if isinstance(data, dict):
            entries.append(data)
        path.unlink(missing_ok=True)
    return entries


def members_with_queue(node: str) -> list[str]:
    root = roster_dir(node) / "agents"
    if not root.is_dir():
        return []
    out: list[str] = []
    for entry in sorted(root.iterdir()):
        q = entry / "queue"
        if q.is_dir() and any(
            f.is_file() and f.name.endswith(".json") for f in q.iterdir()
        ):
            out.append(entry.name)
    return out


# ---------- seat (the roster human's mailbox on the node) ----------

def seat_dir(node: str, name: str) -> Path:
    return roster_dir(node) / "seat" / name.strip().lower()


def seat_inbox_dir(node: str, name: str) -> Path:
    return seat_dir(node, name) / "inbox"


def seat_read_dir(node: str, name: str) -> Path:
    return seat_dir(node, name) / "read"


def park_seat_message(
    node: str,
    name: str,
    sender: str,
    content: str,
    *,
    files: list[dict] | None = None,
    bundle: Path | None = None,
) -> Path:
    """Park one message in the human's seat inbox. When the envelope carried
    attachments (`files` entries + the staged `bundle` dir), copy them into the
    seat's own storage so they outlive the per-turn staging dir; the parked
    JSON records filename + stored path for each."""
    envelope = {
        "id": new_ulid(),
        "from": sender,
        "to": name.strip().lower(),
        "content": content,
        "files": [],
        "parked_at": utc_now(),
    }
    if files and bundle is not None:
        store = seat_dir(node, name) / "files" / envelope["id"]
        for entry in files:
            src = bundle / str(entry.get("filename", ""))
            if not src.is_file():
                continue
            store.mkdir(parents=True, exist_ok=True)
            dest = store / src.name
            shutil.copyfile(src, dest)
            envelope["files"].append({"filename": src.name, "path": str(dest)})
    path = seat_inbox_dir(node, name) / f"{envelope['id']}.json"
    atomic_write_json(path, envelope)
    return path


def list_seat_messages(node: str, name: str, *, read: bool = False) -> list[Path]:
    root = seat_read_dir(node, name) if read else seat_inbox_dir(node, name)
    if not root.is_dir():
        return []
    return sorted(f for f in root.iterdir() if f.is_file() and f.name.endswith(".json"))


def mark_seat_read(node: str, name: str, path: Path) -> Path:
    dest_dir = seat_read_dir(node, name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    path.rename(dest)
    return dest


def seat_presence_path(node: str, name: str) -> Path:
    return seat_dir(node, name) / "presence"


def touch_seat_presence(node: str, name: str) -> None:
    p = seat_presence_path(node, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(p, str(os.getpid()))


def clear_seat_presence(node: str, name: str) -> None:
    try:
        seat_presence_path(node, name).unlink()
    except OSError:
        pass


def seat_attached(node: str, name: str) -> bool:
    try:
        pid = int(seat_presence_path(node, name).read_text().strip())
    except (OSError, ValueError):
        return False
    return pid > 0 and _pid_alive(pid)


# ---------- transcript log + velocity ----------

def append_log(node: str, text: str) -> None:
    log_dir = roster_dir(node) / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with (log_dir / f"{day}.md").open("a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n\n")


def recent_log_lines(node: str, *, days: int = 2) -> list[str]:
    """Raw lines from the last `days` UTC day log files, oldest first — a
    read-only view for chat/attach backfill. Never writes."""
    log_dir = roster_dir(node) / "log"
    if not log_dir.is_dir():
        return []
    lines: list[str] = []
    for path in sorted(log_dir.glob("*.md"))[-days:]:
        try:
            lines.extend(path.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
    return lines


def prune_day_logs(node: str, retention_days: int) -> list[str]:
    """Delete whole day-log files outside the retention window, keeping the
    most recent `retention_days` UTC days (today included). Whole files only:
    a surviving day stays byte-identical to what was written, so no partial
    truncation can ever corrupt a transcript mid-turn. `retention_days` of 0
    keeps every day forever. Returns the days dropped, oldest first."""
    if retention_days <= 0:
        return []
    log_dir = roster_dir(node) / "log"
    if not log_dir.is_dir():
        return []
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=retention_days - 1)
    ).strftime("%Y-%m-%d")
    dropped: list[str] = []
    for path in sorted(log_dir.glob("*.md")):
        if not DAY_LOG_RE.fullmatch(path.stem) or path.stem >= cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        dropped.append(path.stem)
    return dropped


def _csv_field(value: object) -> str:
    text = str(value)
    if any(c in text for c in ",\"\n"):
        return '"' + text.replace('"', '""') + '"'
    return text


def record_velocity(
    node: str,
    *,
    agent: str,
    rig: str,
    thread: str,
    hop: int,
    duration_seconds: float,
    exit_code: int,
) -> None:
    path = roster_dir(node) / "velocity.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.is_file()
    row = ",".join(
        _csv_field(v)
        for v in (
            utc_now(),
            agent,
            rig,
            thread,
            hop,
            f"{duration_seconds:.2f}",
            exit_code,
        )
    )
    with path.open("a", encoding="utf-8") as f:
        if fresh:
            f.write(VELOCITY_HEADER)
        f.write(row + "\n")


def rotate_velocity(node: str) -> list[str]:
    """Move rows from finished months out of velocity.csv into
    `velocity-<YYYY-MM>.csv` siblings, leaving the live file holding the
    current month. The monthly files are never pruned — turn economics is the
    record of what the roster cost, and a row per turn stays small — so rotation
    bounds what every reader parses without dropping anything. Returns the
    months archived, oldest first.

    The caller rotates only while no turn is live: a running turn appends to
    velocity.csv, and rewriting it underneath that append would lose the row."""
    path = roster_dir(node) / "velocity.csv"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return []
    if not lines or lines[0] != VELOCITY_HEADER:
        return []
    current = datetime.now(timezone.utc).strftime("%Y-%m")
    keep: list[str] = []
    by_month: dict[str, list[str]] = {}
    for row in lines[1:]:
        stamped = VELOCITY_MONTH_RE.match(row)
        if stamped and stamped.group(1) < current:
            by_month.setdefault(stamped.group(1), []).append(row)
        else:
            keep.append(row)
    if not by_month:
        return []
    for month, rows in sorted(by_month.items()):
        archive = path.with_name(f"velocity-{month}.csv")
        fresh = not archive.is_file()
        with archive.open("a", encoding="utf-8") as f:
            if fresh:
                f.write(VELOCITY_HEADER)
            f.writelines(rows)
    _atomic_write_text(path, VELOCITY_HEADER + "".join(keep))
    return sorted(by_month)


# ---------- per-turn state (staging outbox + crash evidence) ----------

def turn_path(node: str, name: str) -> Path:
    return agent_dir(node, name) / ".turn.json"


def write_turn(node: str, name: str, payload: dict) -> Path:
    path = turn_path(node, name)
    atomic_write_json(path, payload)
    return path


def read_turn(node: str, name: str) -> dict | None:
    path = turn_path(node, name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_turn(node: str, name: str) -> None:
    try:
        turn_path(node, name).unlink()
    except OSError:
        pass


# ---------- mission-review backoff (idle liveness) ----------

def mission_review_path(node: str) -> Path:
    return roster_dir(node) / "mission-review.json"


def read_mission_review(node: str) -> dict:
    path = mission_review_path(node)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_mission_review(node: str, data: dict) -> None:
    atomic_write_json(mission_review_path(node), data)


def staging_dir(node: str, name: str) -> Path:
    return agent_dir(node, name) / "staging"


def prepare_staging(node: str, name: str) -> Path:
    """Fresh per-turn staging outbox. Dispatch points the harness
    subprocess's $TELL_OUTBOX_DIR here, so the unmodified `tell` writes the
    agent's envelopes into a dir only this turn owns — attribution for
    free. Leftovers from a crashed turn are wiped, not released."""
    d = staging_dir(node, name)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def staged_envelopes(node: str, name: str) -> list[Path]:
    d = staging_dir(node, name)
    if not d.is_dir():
        return []
    return sorted(f for f in d.iterdir() if f.is_file() and f.name.endswith(".json"))


def delivered_dir(node: str, name: str) -> Path:
    return agent_dir(node, name) / "delivered"


def list_delivered_bundles(node: str, name: str) -> list[Path]:
    d = delivered_dir(node, name)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_dir())


def new_delivered_bundle(node: str, name: str) -> Path:
    """Fresh per-turn bundle for inbound attachment copies (dispatch marshals
    a8s ATTACHED FILE paths here when the org runs isolated, so the agent user
    can read them). The time-sortable stamp names the bundle; retention rides
    the same keep-most-recent policy as turn captures (TURN_RETENTION)."""
    bundle = delivered_dir(node, name) / turn_capture_stamp()
    bundle.mkdir(parents=True, exist_ok=True)
    for stale in list_delivered_bundles(node, name)[:-TURN_RETENTION]:
        shutil.rmtree(stale, ignore_errors=True)
    return bundle


# ---------- live turn output (a gemba attach tails this) ----------

def live_log_path(node: str, name: str) -> Path:
    return agent_dir(node, name) / "live.log"


def reset_live_log(node: str, name: str) -> Path:
    """Truncate the member's live turn log and return its path. Dispatch tees
    the turn's harness output here as it streams, so an attached read-only view
    can tail the turn AS IT COMES OUT instead of waiting for the turn to end."""
    path = live_log_path(node, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, "")
    return path


def read_live_log_tail(node: str, name: str, offset: int) -> tuple[str, int]:
    """Return (new text since `offset`, new offset). A shrunk file means the
    next turn truncated it, so reading restarts from the top."""
    path = live_log_path(node, name)
    if not path.is_file():
        return "", 0
    try:
        if path.stat().st_size < offset:
            offset = 0
        with path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            chunk = f.read()
            return chunk, f.tell()
    except OSError:
        return "", offset


# ---------- per-turn capture (prompt + raw output, one file per turn) ----------
#
# live.log holds only the LAST turn's raw output and no prompt at all, so a
# prompting problem cannot be diagnosed after the fact. Turn capture keeps the
# most recent TURN_RETENTION turns per member as standalone markdown files —
# the full assembled prompt and the full raw harness output, every turn,
# successes and timeouts alike. Long-term retention belongs to the day logs
# and `log_retention_days` (prune_day_logs).

TURN_RETENTION = 50


def turns_dir(node: str, name: str) -> Path:
    return agent_dir(node, name) / "turns"


def turn_capture_stamp() -> str:
    """Filesystem-safe, time-sortable stamp for a turn-capture filename."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def list_turn_captures(node: str, name: str) -> list[Path]:
    d = turns_dir(node, name)
    if not d.is_dir():
        return []
    return sorted(f for f in d.iterdir() if f.is_file() and f.name.endswith(".md"))


def write_turn_capture(node: str, name: str, stamp: str, thread: str, content: str) -> Path:
    """Write one turn-capture file and prune the member's turns/ dir to the
    most recent TURN_RETENTION. The filename sorts by time; the thread (or
    'batch' when a turn drained several threads) makes it identifiable."""
    d = turns_dir(node, name)
    d.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9_-]", "", thread) or "batch"
    path = d / f"{stamp}-{tag}.md"
    _atomic_write_text(path, content)
    for stale in list_turn_captures(node, name)[:-TURN_RETENTION]:
        try:
            stale.unlink()
        except OSError:
            pass
    return path


# ---------- dead letters ----------

def dead_letter_dir(node: str) -> Path:
    return roster_dir(node) / "dead-letter"


def record_dead_letter(
    node: str,
    *,
    reason: str,
    sender: str,
    to: str,
    thread: str,
    content: str,
    count: int = 1,
) -> Path:
    record_id = new_ulid()
    path = dead_letter_dir(node) / f"{record_id}.json"
    atomic_write_json(
        path,
        {
            "id": record_id,
            "time": utc_now(),
            "reason": reason,
            "count": count,
            "from": sender,
            "to": to,
            "thread": thread,
            "content": content[:2000],
        },
    )
    return path


def list_dead_letters(node: str) -> list[dict]:
    root = dead_letter_dir(node)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


# ---------- spend budgets (a turn costs 1 unit; empty = resting) ----------
#
# A bifurcated token bucket, refilled lazily by elapsed wall-clock time: each
# member has its own bucket, and the whole cell shares one. A turn costs 1
# member unit AND 1 cell unit, regardless of how many queued messages it
# consumes — batching is rewarded by construction. An empty bucket means the
# member is not runnable ("resting"); the queue simply holds. Nothing is muted,
# nothing is dropped.

CELL_BUDGET_KEY = "__cell__"


def fmt_budget(value: float) -> str:
    """Budget level as a clean number: 8.0 -> "8", 7.5 -> "7.5". Rounds to
    one decimal FIRST so a lazily-extrapolated 7.0004 renders "7", not "7.0"."""
    rounded = round(value, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.1f}"


def _read_buckets(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _bucket_level(
    entry: object, budget_max: float, earn_per_hour: float, now: float
) -> float:
    """Current balance for one stored bucket entry: the stored level plus
    whatever has been earned by elapsed wall-clock time since it was last
    written, capped at `budget_max`. An unseen/malformed entry starts full."""
    if not isinstance(entry, dict):
        return float(budget_max)
    try:
        level = float(entry.get("level", budget_max))
        at = float(entry.get("at", now))
    except (TypeError, ValueError):
        return float(budget_max)
    earned = max(0.0, now - at) / 3600.0 * earn_per_hour
    return min(float(budget_max), level + earned)


def buckets_path(node: str) -> Path:
    return roster_dir(node) / "buckets.json"


def read_buckets(node: str) -> dict:
    return _read_buckets(buckets_path(node))


def budget_level(
    node: str, key: str, budget_max: float, earn_per_hour: float, *, now: float | None = None
) -> float:
    now = time.time() if now is None else now
    return _bucket_level(read_buckets(node).get(key.lower()), budget_max, earn_per_hour, now)


def budget_charge(
    node: str,
    key: str,
    budget_max: float,
    earn_per_hour: float,
    amount: float = 1.0,
    *,
    now: float | None = None,
) -> float:
    now = time.time() if now is None else now
    level = budget_level(node, key, budget_max, earn_per_hour, now=now)
    new_level = max(0.0, level - amount)
    data = read_buckets(node)
    data[key.lower()] = {"level": round(new_level, 4), "at": now}
    atomic_write_json(buckets_path(node), data)
    return new_level


def budget_seconds_until(
    node: str,
    key: str,
    budget_max: float,
    earn_per_hour: float,
    target: float = 1.0,
    *,
    now: float | None = None,
) -> float:
    """Seconds until the bucket refills to `target` (0.0 when already there,
    inf when it never will because nothing is earned)."""
    level = budget_level(node, key, budget_max, earn_per_hour, now=now)
    if level >= target:
        return 0.0
    if earn_per_hour <= 0:
        return float("inf")
    return (target - level) / earn_per_hour * 3600.0


# ---------- rig spend bucket (MACHINE-GLOBAL: one subscription, many rosters) ----------
#
# A rig maps to a real subscription (an Antigravity plan good for ~20 prompts an
# hour, a Claude seat). Its ceiling is set ON THE RIG and binds every r4t roster
# on the machine, so one rig is safely shared across projects. This bucket
# therefore lives at the r4t_home ROOT, not under any one roster, and every roster
# node charges it concurrently — so the read-modify-write is serialized by a
# machine-global lock. The gate itself (check-then-charge) is best-effort across
# nodes; the charge clamps at zero and the queue holds, so a rare double-spend
# just runs one extra turn before the rig rests.

def rig_buckets_path() -> Path:
    return r4t_home() / "rig-buckets.json"


@contextmanager
def _rig_bucket_locked():
    """Serialize the machine-global rig-bucket read-modify-write across every
    charging node AND thread. Advisory flock (POSIX) / msvcrt.locking (Windows)
    blocks until free, works across processes and threads (each open() gets its
    own file description), and releases when the holder dies with the fd —
    no stale-lock reclaim to race against. Turn locks stay O_EXCL PID files."""
    path = r4t_home() / ".rig-buckets.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if sys.platform == "win32":
            while True:
                try:
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.01)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if sys.platform == "win32":
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def read_rig_buckets() -> dict:
    return _read_buckets(rig_buckets_path())


def rig_budget_level(
    rig: str, budget_max: float, earn_per_hour: float, *, now: float | None = None
) -> float:
    now = time.time() if now is None else now
    return _bucket_level(read_rig_buckets().get(rig.lower()), budget_max, earn_per_hour, now)


def rig_budget_charge(
    rig: str,
    budget_max: float,
    earn_per_hour: float,
    amount: float = 1.0,
    *,
    now: float | None = None,
) -> float:
    now = time.time() if now is None else now
    with _rig_bucket_locked():
        data = read_rig_buckets()
        level = _bucket_level(data.get(rig.lower()), budget_max, earn_per_hour, now)
        new_level = max(0.0, level - amount)
        data[rig.lower()] = {"level": round(new_level, 4), "at": now}
        atomic_write_json(rig_buckets_path(), data)
        return new_level


def rig_budget_drain(rig: str, *, now: float | None = None) -> None:
    """Empty the rig bucket outright — the whole rig rests until it refills.
    The blank-response quota signal uses this: an out-of-quota subscription is
    a rig-wide fact, so one drained bucket rests every member on that rig."""
    now = time.time() if now is None else now
    with _rig_bucket_locked():
        data = read_rig_buckets()
        data[rig.lower()] = {"level": 0.0, "at": now}
        atomic_write_json(rig_buckets_path(), data)


def rig_budget_seconds_until(
    rig: str,
    budget_max: float,
    earn_per_hour: float,
    target: float = 1.0,
    *,
    now: float | None = None,
) -> float:
    level = rig_budget_level(rig, budget_max, earn_per_hour, now=now)
    if level >= target:
        return 0.0
    if earn_per_hour <= 0:
        return float("inf")
    return (target - level) / earn_per_hour * 3600.0


# ---------- per-agent failure breaker ----------

def breaker_open(
    node: str, name: str, cap: int, cooldown_seconds: float
) -> tuple[bool, int]:
    """systemd-StartLimitBurst-style breaker: `cap` consecutive failed turns
    (nonzero exit or timeout, tracked in meta.json by dispatch) opens it.
    While open, turns are blocked until `cooldown_seconds` have passed since
    the last failure — then one probe turn is let through (half-open); a
    clean turn resets the count and closes it. Returns (blocked, count)."""
    meta = read_meta(node, name)
    count = int(meta.get("consecutive_failures", 0) or 0)
    if cap <= 0 or count < cap:
        return False, count
    raw = str(meta.get("last_failure_at", ""))
    try:
        last = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False, count
    return (time.time() - last) < cooldown_seconds, count


def clear_failures(node: str, name: str) -> None:
    update_meta(node, name, consecutive_failures=0)


# ---------- per-agent meta (idle recovery bookkeeping) ----------

def meta_path(node: str, name: str) -> Path:
    return agent_dir(node, name) / "meta.json"


def read_meta(node: str, name: str) -> dict:
    path = meta_path(node, name)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def update_meta(node: str, name: str, **fields) -> dict:
    meta = read_meta(node, name)
    meta.update(fields)
    atomic_write_json(meta_path(node, name), meta)
    return meta


# ---------- continue conversation record (founded / retired per member) ----------
#
# A continuing member's CLI conversation is keyed on the CLI binary in the turn
# directory. The record says which CLI the current conversation lives on and
# whether it has been retired (by an idle flush or a rig swap); a retired or
# absent record makes the next turn refound cold from state on disk.

def read_conversation(node: str, name: str) -> dict:
    data = read_meta(node, name).get("conversation")
    return data if isinstance(data, dict) else {}


def record_conversation(node: str, name: str, cli: str) -> dict:
    return update_meta(
        node, name,
        conversation={"cli": cli, "retired": False, "recorded_at": utc_now()},
    )


def retire_conversation(node: str, name: str) -> None:
    convo = read_conversation(node, name)
    if convo and not convo.get("retired"):
        convo["retired"] = True
        update_meta(node, name, conversation=convo)


# ---------- harness pool rotation ----------

def take_rotation(node: str, rig: str, pool_size: int) -> int:
    """Return the round-robin index for this rig's next turn and persist
    the advance. Single-variant rigs always get 0 without touching disk."""
    if pool_size <= 1:
        return 0
    path = roster_dir(node) / "rotation.json"
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            pass
    try:
        index = int(data.get(rig, 0)) % pool_size
    except (TypeError, ValueError):
        index = 0
    data[rig] = index + 1
    atomic_write_json(path, data)
    return index


# ---------- roster throttle cadence ----------

def last_turn_start_path(node: str) -> Path:
    return roster_dir(node) / "last-turn-start"


def stamp_last_turn_start(node: str) -> None:
    _atomic_write_text(last_turn_start_path(node), utc_now() + "\n")


def read_last_turn_start(node: str) -> float | None:
    path = last_turn_start_path(node)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError):
        return None


