"""Per-message delivery receipts a sender can read without the machine log.

One JSON file per outbound ULID at ``<agent root>/.outbox/.receipts/<ULID>.json``.

**The router writes these, never the sender.** A sender that writes its own
receipt is asserting an outcome it cannot observe; the same filesystem rule
that makes `from` unforgeable makes the receipt unforgeable. Every writer here
is called from routing code — `mailbox._process_pending` and the receive path
in `network` — and `tell` / `tells` only read.

Receipts are derived state: they prune on their own knob and are safe to
delete. Nothing routes off them.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ar3.fsio import atomic_write_text
from ar3.ulid import is_ulid

from delivery_receipt import FAILURE_STAGES, TERMINAL_STAGES


RECEIPTS_DIRNAME = ".receipts"

# States the roll-up can report, worst-first. A body that landed while its
# attachment did not is a failure the sender has to see, so failure outranks
# delivery rather than being averaged with it.
STATE_ENQUEUED = "enqueued"
STATE_PUBLISHED = "published"
STATE_DEFERRED = "deferred"
STATE_DELIVERED = "delivered"
STATE_FAILED = "failed"
# Some node said it owns nobody by this name and no node has claimed the
# message. Never "delivered", never yet "failed" — the sender knows only that
# it is unconfirmed, and how long it has been that way.
STATE_UNCONFIRMED = "unconfirmed"


def receipts_dir(outbox: Path) -> Path:
    return outbox / RECEIPTS_DIRNAME


def _path(outbox: Path, msg_id: str) -> Path:
    return receipts_dir(outbox) / f"{msg_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read(outbox: Path, msg_id: str) -> dict | None:
    try:
        data = json.loads(_path(outbox, msg_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write(outbox: Path, record: dict) -> None:
    directory = receipts_dir(outbox)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            directory / f"{record['id']}.json", json.dumps(record, indent=2)
        )
    except OSError:
        # A receipt is a report about a delivery, never a step in one. A
        # read-only outbox must not turn into a routing failure.
        pass


def record_enqueued(outbox: Path, msg: dict) -> None:
    """First sight of an envelope in the sender's own outbox."""
    msg_id = str(msg.get("id") or "")
    if not is_ulid(msg_id):
        return
    if read(outbox, msg_id) is not None:
        return
    _write(outbox, {
        "id": msg_id,
        "to": str(msg.get("to") or ""),
        "queued_at": str(msg.get("date") or _now()),
        "state": STATE_ENQUEUED,
        "updated_at": _now(),
        "recipients": {},
    })


def record_event(
    outbox: Path,
    msg_id: str,
    stage: str,
    *,
    recipient: str = "",
    files: list[str] | None = None,
    detail: str = "",
    to: str = "",
    queued_at: str = "",
) -> None:
    """Merge one observed stage into the message's receipt.

    Called for every event the router sees for that ULID, including events
    that arrive out of order — the per-recipient block keeps the newest stage
    and the roll-up is recomputed from all of them, so a late `deferred`
    cannot un-deliver a message.
    """
    if not is_ulid(msg_id):
        return
    record = read(outbox, msg_id) or {
        "id": msg_id,
        "to": to,
        "queued_at": queued_at or _now(),
        "recipients": {},
    }
    if to and not record.get("to"):
        record["to"] = to
    recipients = record.get("recipients")
    if not isinstance(recipients, dict):
        recipients = {}
    stamp = _now()
    if stage == "no_local_recipient":
        # Recorded beside the recipients, never inside one: a node reporting
        # that it owns nobody must not overwrite the block of the node that
        # delivered.
        unclaimed = record.get("unclaimed")
        if not isinstance(unclaimed, list):
            unclaimed = []
        unclaimed.append({"at": stamp, "name": recipient, "detail": detail})
        record["unclaimed"] = unclaimed[-8:]
        record["recipients"] = recipients
        if record.get("state") not in (STATE_DELIVERED, STATE_FAILED):
            record["state"] = STATE_UNCONFIRMED
        record["updated_at"] = stamp
        _write(outbox, record)
        return
    if recipient:
        block = recipients.get(recipient)
        if not isinstance(block, dict):
            block = {"files": {}}
        per_file = block.get("files")
        if not isinstance(per_file, dict):
            per_file = {}
        if stage in ("attachment_fetched", "attachment_failed"):
            for name in files or []:
                per_file[name] = {"state": stage, "detail": detail}
            # The attachment leg never overwrites an inbox write, in either
            # direction the two legs can arrive: the body reaching the
            # recipient's inbox is true regardless of what its attachment
            # did, so `state` here (recipient_state) stays "inbox_write" once
            # that happens. The attachment's own outcome lives in `files`,
            # which `_roll_up` and `summary_line` read directly — that is
            # what stays sticky, not this block-level field.
            if block.get("state") != "inbox_write":
                block["state"] = stage
        else:
            block["state"] = stage
        block["at"] = stamp
        if detail:
            block["detail"] = detail
        block["files"] = per_file
        recipients[recipient] = block
    record["recipients"] = recipients
    record["state"] = _roll_up(stage, recipients, record.get("state", ""))
    record["updated_at"] = stamp
    _write(outbox, record)


def _roll_up(stage: str, recipients: dict, previous: str) -> str:
    """The one word for a message with several recipients.

    Worst-first: a body that landed while its attachment did not is a failure
    the sender has to act on, so failure outranks delivery instead of being
    averaged with it. Anything that reports nothing new — a fetched
    attachment, say — leaves the message where it was.

    Reads per-file evidence, not only each recipient's block-level `state`:
    the block's `state` can read "inbox_write" (truthful — the body did
    arrive) while one of its files reads "attachment_failed" (equally
    truthful — that file did not), and `record_event` above never lets one
    of those two facts erase the other regardless of which arrived first.
    A failure recorded on any file makes this sticky: nothing later can roll
    the message back up to delivered."""
    states = {
        block.get("state", "")
        for block in recipients.values()
        if isinstance(block, dict)
    }
    states.add(stage)
    file_states = {
        entry.get("state", "")
        for block in recipients.values()
        if isinstance(block, dict)
        for entry in (block.get("files") or {}).values()
        if isinstance(entry, dict)
    }
    if (states | file_states) & FAILURE_STAGES:
        return STATE_FAILED
    if "inbox_write" in states:
        return STATE_DELIVERED
    if "deferred" in states:
        return STATE_DEFERRED
    return previous or STATE_ENQUEUED


def record_published(outbox: Path, msg_id: str, recipient: str, remote: str) -> None:
    """The envelope left this machine. Never promoted to delivered: a publish
    says the transport took it, not that anything on the far side read it."""
    if not is_ulid(msg_id):
        return
    record = read(outbox, msg_id) or {
        "id": msg_id, "to": recipient, "queued_at": _now(), "recipients": {},
    }
    if record.get("state") in (STATE_ENQUEUED, "", None):
        record["state"] = STATE_PUBLISHED
    record["published_at"] = _now()
    record["remote"] = remote
    record["updated_at"] = record["published_at"]
    _write(outbox, record)


def is_terminal(record: dict) -> bool:
    state = record.get("state", "")
    if state in (STATE_FAILED, STATE_DELIVERED):
        recipients = record.get("recipients")
        if isinstance(recipients, dict) and recipients:
            return all(
                block.get("state", "") in TERMINAL_STAGES
                for block in recipients.values()
                if isinstance(block, dict)
            )
        return state == STATE_FAILED
    return False


def summary_line(record: dict) -> str:
    """One line for `tells --sent`: ULID, state, age, recipient."""
    state = record.get("state", STATE_ENQUEUED)
    age = age_text(record.get("updated_at") or record.get("queued_at") or "")
    detail = ""
    if state == STATE_PUBLISHED and not record.get("recipients"):
        detail = f" (no receipt, {age})"
    if state == STATE_UNCONFIRMED:
        detail = f" (no node claimed it, {age})"
    failures = []
    for name, block in (record.get("recipients") or {}).items():
        if not isinstance(block, dict):
            continue
        # File-level evidence first: it is what `_roll_up` treats as sticky,
        # so it is what has to name the loss even when the block's own
        # `state` reads "inbox_write" (truthful — the body still arrived).
        file_failures = [
            entry.get("detail", "") or entry.get("state", "")
            for entry in (block.get("files") or {}).values()
            if isinstance(entry, dict) and entry.get("state") in FAILURE_STAGES
        ]
        if file_failures:
            failures.append(f"{name}: {'; '.join(file_failures)}")
        elif block.get("state") in FAILURE_STAGES:
            failures.append(f"{name}: {block.get('detail', '') or block.get('state', '')}")
    if failures:
        detail = " — " + "; ".join(failures)
    return f"{record.get('id', '?')}  {state}{detail}  {age}  -> {record.get('to', '?')}"


def parse_stamp(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_text(raw: str) -> str:
    stamp = parse_stamp(raw)
    if stamp is None:
        return "?"
    return duration_text((datetime.now(timezone.utc) - stamp).total_seconds())


def duration_text(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def list_recent(outbox: Path, since_s: float | None = None) -> list[dict]:
    directory = receipts_dir(outbox)
    if not directory.is_dir():
        return []
    cutoff = None if since_s is None else time.time() - since_s
    records: list[dict] = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return []
    for entry in entries:
        record = read(outbox, entry.stem)
        if record is None:
            continue
        if cutoff is not None:
            stamp = parse_stamp(record.get("queued_at", ""))
            if stamp is not None and stamp.timestamp() < cutoff:
                continue
        records.append(record)
    records.sort(key=lambda r: str(r.get("id", "")))
    return records


def parse_duration(raw: str) -> float:
    """`30s`, `10m`, `2h`, `7d`, or bare seconds. Raises ValueError."""
    text = str(raw).strip().lower()
    if not text:
        raise ValueError("empty duration")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    factor = 1
    if text[-1] in units:
        factor = units[text[-1]]
        text = text[:-1]
    value = float(text)
    if value < 0:
        raise ValueError("duration must not be negative")
    return value * factor
