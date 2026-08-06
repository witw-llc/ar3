"""Thread ledger — the conversation label that survives a batch turn.

A thread is a conversation label, not a budget. It exists so a reply can
be attributed to the exchange it answers, so the originator can be tracked
(answer-the-originator closure), and so a thread that goes quiet without
its originator hearing back can wake the leader. It never gates delivery:
every inbound message enqueues regardless of a thread's status.

A thread has one terminal disposition: `close_task`, when the originator has
had a substantive reply.

An `ingress` thread came from outside the garden, and it is owed nothing.
Beyond the wall a8s posts messages to nodes; it carries no notion of a reply
being expected, and giving it one would put a decision point on every node of
a network r4t does not own. So the obligation graph stops at the wall: an
ingress thread is labelled and traced like any other, the leader answers it or
does not at its own judgment, and the quiet sweep never second-guesses that.
What the sweep does own is the inside, where both ends are r4t's and a member
that never answered its originator is a real dropped ball.

`ingress` is stamped at birth from which side of the wall the sender is on —
not from anything the sender says about itself. `creator` is whatever `from`
the message carried and `r4t dispatch --from` accepts any string, so wire
claims never move the ledger.

The thread id + hop travel as structured fields on the r4t-message
(`dispatch.py`), never as a text header — there is no serialize/parse step
inside the walls. Hop counts are stamped for telemetry (and the tree) but
never cut a message.
"""
from __future__ import annotations

from pathlib import Path

from state import atomic_write_json, roster_dir, utc_now
from ulid import new as new_ulid

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


def new_thread_id() -> str:
    return new_ulid()


# ---------- ledger ----------

def tasks_dir(node: str) -> Path:
    return roster_dir(node) / "tasks"


def task_path(node: str, task_id: str) -> Path:
    return tasks_dir(node) / f"{task_id}.json"


def load_task(node: str, task_id: str) -> dict | None:
    path = task_path(node, task_id)
    if not path.is_file():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_task(node: str, task: dict) -> None:
    task["updated_at"] = utc_now()
    atomic_write_json(task_path(node, task["id"]), task)


def new_task(task_id: str, creator: str, *, ingress: bool = False) -> dict:
    now = utc_now()
    return {
        "id": task_id,
        "creator": creator,
        "created_at": now,
        "updated_at": now,
        "status": STATUS_OPEN,
        "answered": False,
        "ingress": ingress,
    }


def ensure_task(
    node: str, task_id: str, creator: str, *, ingress: bool = False
) -> dict:
    """The ledger for a thread, opened if this is the thread's first message.
    `ingress` is stamped at birth from the one fact the router cannot be lied
    to about — which side of the wall the sender is on."""
    task = load_task(node, task_id)
    if task is None:
        task = new_task(task_id, creator, ingress=ingress)
        save_task(node, task)
    return task


def close_task(node: str, task_id: str) -> None:
    """Mark a thread closed: its originator has had a substantive reply."""
    task = load_task(node, task_id)
    if task is None or task.get("status") == STATUS_CLOSED:
        return
    task["status"] = STATUS_CLOSED
    task["answered"] = True
    save_task(node, task)


def list_tasks(node: str) -> list[dict]:
    root = tasks_dir(node)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(root.glob("*.json")):
        task = load_task(node, path.stem)
        if task is not None:
            out.append(task)
    return out


# ---------- expiry (idle maintenance) ----------

def last_activity(task: dict) -> float:
    """Unix timestamp of the ledger's last write (0.0 when unparsable)."""
    from datetime import datetime

    raw = task.get("updated_at") or task.get("created_at") or ""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def expire_tasks(node: str, older_than_seconds: float) -> list[str]:
    """Delete thread ledgers idle longer than `older_than_seconds`."""
    from datetime import datetime, timezone

    cutoff = datetime.now(timezone.utc).timestamp() - older_than_seconds
    removed: list[str] = []
    for task in list_tasks(node):
        if last_activity(task) >= cutoff:
            continue
        try:
            task_path(node, task["id"]).unlink()
        except OSError:
            continue
        removed.append(task["id"])
    return removed
