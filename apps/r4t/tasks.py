"""Thread ledger — the conversation label that survives a batch turn.

A thread is a conversation label, not a budget. It exists so a reply can
be attributed to the exchange it answers, so the originator can be tracked
(answer-the-originator closure), and so a thread that goes quiet without
its originator hearing back can wake the leader. It never gates delivery:
every inbound message enqueues regardless of a thread's status.

A thread has two terminal dispositions, and both live here: `close_task` (the
originator got a substantive reply) and `close_without_reply` (the member
deliberately answered with silence — see `ack.py` and docs/r4t-ack.md). Either
way the quiet sweep stops seeing the thread, which is the whole point: an open
thread otherwise cannot tell deliberate silence from a dropped ball. An ack is
never prospective, so a new inbound on an ack-closed thread reopens the ledger
(`ensure_task`); a thread closed by a real answer stays closed.

A `relay` thread was opened by machine-classed external mail (`meta.class`
`auto` on the wire, #167) — an originator that is another cluster's relay, not
someone waiting on an answer. It carries a label like any other thread; what it
does not carry is owed attention, so the quiet sweep leaves it alone.

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


def new_task(task_id: str, creator: str, *, relay: bool = False) -> dict:
    now = utc_now()
    return {
        "id": task_id,
        "creator": creator,
        "created_at": now,
        "updated_at": now,
        "status": STATUS_OPEN,
        "answered": False,
        "relay": relay,
    }


def ensure_task(node: str, task_id: str, creator: str, *, relay: bool = False) -> dict:
    task = load_task(node, task_id)
    if task is None:
        task = new_task(task_id, creator, relay=relay)
        save_task(node, task)
    elif task.get("status") == STATUS_CLOSED and task.get("ack"):
        # An ack is never prospective: it ends the obligations the thread was
        # carrying, not the ones it has not carried yet. A new inbound on an
        # ack-closed thread therefore reopens the ledger, or the sweep would be
        # blind to that message forever. A thread closed by a real answer is
        # left alone — `close_task` keeps its meaning.
        task["status"] = STATUS_OPEN
        task["answered"] = False
        task.setdefault("ack_notes", []).append(
            {**task.pop("ack"), "superseded_at": utc_now()}
        )
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


def close_without_reply(
    node: str, task_id: str, *, member: str, reason: str, stated: str = ""
) -> None:
    """Mark a thread closed because its member deliberately said nothing —
    the terminal disposition of `ack.py`. `reason` is the task layer's own,
    re-derived from the ledger; `stated` is whatever the model claimed and is
    kept as color, never read back as a fact."""
    task = load_task(node, task_id)
    if task is None or task.get("status") == STATUS_CLOSED:
        return
    task["status"] = STATUS_CLOSED
    task["answered"] = True
    task["ack"] = {
        "member": member,
        "reason": reason,
        "stated": stated,
        "at": utc_now(),
    }
    save_task(node, task)


def note_ack(
    node: str, task_id: str, *, member: str, reason: str, stated: str = ""
) -> None:
    """Record a valid close proposal from a member that does NOT owe this
    thread's creator — the delegation-chain case, where one thread id is shared
    by every hop. The obligation stays open: only the member the creator is
    waiting on can end it."""
    task = load_task(node, task_id)
    if task is None:
        return
    task.setdefault("ack_notes", []).append(
        {"member": member, "reason": reason, "stated": stated, "at": utc_now()}
    )
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
