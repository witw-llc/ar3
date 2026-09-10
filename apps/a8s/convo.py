"""SQLite conversation archive for routed messages shown by `a8s convo`.

One record per logical message (alias fan-out stores the alias in `to` and
lists local deliverees in `recipients`). Inserts never prune history.
`a8s update` retains the newest `convo_max_rows` entries during housekeeping.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sqlite_store
from core import conversations_path, inbound_bundle_dir, out
from receipts import parse_duration, parse_stamp
from settings import get_int

from ar3 import clock
from ar3.ulid import is_ulid

__all__ = [
    "ConversationArchiveError",
    "DEFAULT_HEADING_IN",
    "DEFAULT_HEADING_OUT",
    "HEADING_PLACEHOLDERS",
    "convo_help_epilog",
    "decode_template",
    "entry_from_message",
    "extract_heading_templates",
    "format_conversation",
    "format_entry",
    "follow_conversation",
    "hold_open",
    "involves_agent",
    "load_agent_entries",
    "load_agent_records",
    "load_entries",
    "open_for_read",
    "open_glow_stdout",
    "print_entries",
    "prune_conversations",
    "record",
    "sender_keys",
    "sent_by",
    "write_block",
]

DEFAULT_HEADING_OUT = "## from {from} to {to} at {timestamp}"
DEFAULT_HEADING_IN = "### from {from} to {to} at {timestamp}"

HEADING_PLACEHOLDERS = ("from", "to", "timestamp", "date", "utc", "ulid")


def decode_template(text: str) -> str:
    return text.replace("\\n", "\n").replace("\\t", "\t")


def _argv_looks_like_option(arg: str) -> bool:
    return arg.startswith("-") and arg != "-"


def extract_heading_templates(argv: list[str]) -> tuple[list[str], str | None, str | None]:
    """Pull --heading-out/in (multi-token) out of argv before argparse."""
    rest: list[str] = []
    heading_out: str | None = None
    heading_in: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--heading-out":
            if i + 1 >= len(argv):
                raise ValueError("--heading-out requires a template")
            heading_out, i = _consume_template(argv, i + 1)
            continue
        if arg == "--heading-in":
            if i + 1 >= len(argv):
                raise ValueError("--heading-in requires a template")
            heading_in, i = _consume_template(argv, i + 1)
            continue
        rest.append(arg)
        i += 1
    return rest, heading_out, heading_in


def _consume_template(argv: list[str], start: int) -> tuple[str, int]:
    parts: list[str] = []
    i = start
    while i < len(argv) and not _argv_looks_like_option(argv[i]):
        parts.append(argv[i])
        i += 1
    if not parts:
        raise ValueError("template requires at least one line")
    return decode_template("\n".join(parts)), i


def convo_help_epilog() -> str:
    return f"""filters:
  --from NAME    show only messages sent by NAME (case-insensitive; repeat for several
                 senders). The limit counts matching messages, so --from bob --limit 10
                 shows bob's last ten however much other traffic sits between them.

  --since CURSOR show only rows after CURSOR, one of:
                   a message ulid   rows with a greater seq than that row -- delivery
                                    order, so a late arrival dated before that row is
                                    never dropped
                   an ISO timestamp rows after the last-inserted row dated at or before
                   or bare date     it (before it for a bare date, so the whole day is
                                    in, midnight included) -- can still miss a message that itself arrives
                                    late dated at or before the cursor; a ulid cursor
                                    cannot have that failure. A value with no offset,
                                    a bare date included, is read in this machine's
                                    local zone
                   a duration       rows dated within that span of now, e.g. 2h, 30m, 3d
                 On its own --since drains: every row after the cursor, oldest first.
                 Add --limit N for the oldest N of them and continue from the newest
                 ulid you were handed -- either way nothing between the cursor and the
                 window is skipped. Composes with --from. Nothing new prints nothing
                 and exits 0.

heading templates:
  Outbound (--heading-out) and inbound (--heading-in) use Python str.format placeholders:
    {{from}}       sender name
    {{to}}         recipient or alias
    {{timestamp}}  the message's time in this machine's zone, e.g. 2026-08-16 13:22:04 PDT
    {{date}}       alias for {{timestamp}}
    {{utc}}        the same instant as stored: ISO 8601 UTC
    {{ulid}}       the message's own id, empty string when the row has none

  Defaults:
    outbound: {DEFAULT_HEADING_OUT}
    inbound:  {DEFAULT_HEADING_IN}

  Multiline headings:
    - Shell quotes preserve embedded newlines in one argument
    - Multiple arguments after the flag join with newlines (one line each)
    - Use \\n and \\t escapes inside a single argument

  Message body and attachment lines are appended after the heading block.

output:
  --json         one JSON object per row (ulid, seq, from, to, utc, content, files,
                 files_unavailable), newline-delimited, in the same seq order as the
                 markdown view -- pairs with --since: record the newest ulid, ask
                 --since <it> next time. files_unavailable carries one
                 {{filename, error, detail}} object per attachment the transfer could
                 not deliver, so a lost file is never read as a delivered one.

examples:
  a8s convo my-desktop -f --limit 10 --glow
  a8s convo my-desktop -f --from ares
  a8s convo bob --heading-out '**{{from}}**' '→ {{to}}' --limit 5
  a8s convo bob --heading-in "### {{from}}\\n_{{timestamp}}_"
  a8s convo my-desktop --since 01J8X9K2QZ5VJ0G3R7T6M4N8FP --json

environment:
  A8S_GLOW=<theme>    default glow theme (auto, dark, light, dracula, …); --glow overrides
"""


def _name_key(name: str) -> str:
    return (name or "").strip().lower()


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS messages (
        seq INTEGER PRIMARY KEY,
        message_id TEXT,
        entry_json TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS messages_message_id
        ON messages(message_id)
        WHERE message_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS message_agents (
        seq INTEGER NOT NULL REFERENCES messages(seq) ON DELETE CASCADE,
        agent_key TEXT NOT NULL,
        PRIMARY KEY (seq, agent_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS message_agents_agent_seq
        ON message_agents(agent_key, seq)
    """,
)


class ConversationArchiveError(RuntimeError):
    pass


def _connect() -> sqlite3.Connection:
    return sqlite_store.connect(
        conversations_path(), _SCHEMA, table="messages", foreign_keys=True
    )


def hold_open() -> sqlite3.Connection:
    """The connection a running node keeps on this store for its lifetime.

    A `mode=ro` reader cannot create `-wal` / `-shm`, so it can read the
    archive only while some connection holds that pair open. The writers open
    per write and close again, so between two deliveries the side files are
    simply gone and a seat with read+execute on the a8s home is told "unable
    to open database file" about a store that is fine — intermittent, and
    nothing the reader can fix. A node holds this open instead.

    It is a connection of its own rather than the writers' reused: writes come
    from the router, the wake handlers and the receive loops on whatever
    thread reaches them first, and one shared connection would trade a
    per-write open for a lock on every write across all of them. Idle and
    outside any transaction, this one takes no lock and does not stop a
    writer's `wal_checkpoint(TRUNCATE)`.
    """
    return sqlite_store.hold(_connect())


def open_for_read() -> sqlite3.Connection:
    """The archive, or a `ConversationArchiveError` naming why it is not there.

    A reader that answers "no rows" when it means "I could not look" is the
    one failure mode this store must not have: a seat whose sandbox could read
    the registry but not the archive ran `a8s convo` on its heartbeat, got
    nothing and exit 0, and read it as no mail. Two delivered messages were
    sitting in the file it could not open.

    Reading never creates the store and never initializes one. `_connect`
    would — `sqlite_store.connect` makes the file and the schema — and an
    empty database left behind by a failed read turns the next read into a
    truthful "no rows" about a store that never held anything. The file being
    present is not enough to make that claim either: a zero-byte file and a
    database holding somebody else's tables both open, and both mean nothing
    was archived here.
    """
    path = conversations_path()
    if not path.is_file():
        raise ConversationArchiveError(f"no conversation store at {path}")
    try:
        conn = sqlite_store.connect_read_only(path, table="messages")
    except (OSError, sqlite3.Error) as e:
        raise ConversationArchiveError(f"cannot read {path}: {e}") from e
    if conn is None:
        raise ConversationArchiveError(f"cannot read {path}: not a conversation store")
    return conn


def sender_keys(senders: list[str] | None) -> set[str]:
    return {key for name in (senders or []) if (key := _name_key(str(name)))}


def sent_by(entry: dict[str, Any], keys: set[str]) -> bool:
    """True when no sender filter is active or the entry came from one of them."""
    return not keys or _name_key(entry.get("from", "")) in keys


def _entry_agents(entry: dict[str, Any]) -> set[str]:
    names = [entry.get("from", ""), entry.get("to", "")]
    names.extend(entry.get("recipients") or [])
    return {key for name in names if (key := _name_key(str(name)))}


def _decode_entry(raw: str) -> dict[str, Any] | None:
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return entry if isinstance(entry, dict) else None


def involves_agent(entry: dict[str, Any], agent: str) -> bool:
    key = _name_key(agent)
    if not key:
        return False
    if _name_key(entry.get("from", "")) == key:
        return True
    if _name_key(entry.get("to", "")) == key:
        return True
    recipients = entry.get("recipients") or []
    return any(_name_key(r) == key for r in recipients)


def entry_from_message(msg: dict[str, Any], *, recipients: list[str] | None = None) -> dict[str, Any]:
    """Normalize a tell/inbox envelope into a conversation archive entry.

    An entry that names a file the transfer could not deliver carries `error`
    and `detail`. Those are kept: reducing every entry to a bare filename made
    a lost attachment indistinguishable from a delivered one, and the archive
    is written once, so what is dropped here can never be recovered.
    """
    files = msg.get("files") or []
    filenames = [
        (e.get("filename") or "").strip()
        for e in files
        if isinstance(e, dict) and (e.get("filename") or "").strip()
    ]
    unavailable = [
        {
            "filename": (e.get("filename") or "").strip(),
            "detail": str(e.get("detail") or e.get("error") or "").strip(),
        }
        for e in files
        if isinstance(e, dict) and e.get("error") and (e.get("filename") or "").strip()
    ]
    entry = {
        "date": (msg.get("date") or "").strip() or _now_iso(),
        "from": (msg.get("from") or "").strip(),
        "to": (msg.get("to") or "").strip(),
        "content": msg.get("content", ""),
        "files": filenames,
        "id": (msg.get("id") or "").strip(),
        "recipients": list(recipients or []),
    }
    if unavailable:
        entry["files_unavailable"] = unavailable
    return entry


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def load_entries() -> list[dict[str, Any]]:
    try:
        with closing(open_for_read()) as conn:
            rows = conn.execute(
                "SELECT entry_json FROM messages ORDER BY seq"
            ).fetchall()
    except (OSError, sqlite3.Error) as e:
        raise ConversationArchiveError(f"cannot read {conversations_path()}: {e}") from e
    entries: list[dict[str, Any]] = []
    for (raw,) in rows:
        entry = _decode_entry(raw)
        if entry is not None:
            entries.append(entry)
    return entries


def _rows_to_entries(rows: list[tuple[int, str]]) -> list[tuple[int, dict[str, Any]]]:
    entries: list[tuple[int, dict[str, Any]]] = []
    for seq, raw in rows:
        entry = _decode_entry(raw)
        if entry is not None:
            entries.append((int(seq), entry))
    return entries


def _latest_agent_entries(
    conn: sqlite3.Connection,
    agent: str,
    limit: int | None,
    *,
    through_seq: int | None = None,
    seq_floor: int | None = None,
    date_floor: datetime | None = None,
    senders: list[str] | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    """Rows involving `agent`, oldest-first, capped at `limit` (None = no cap).

    A floor turns the walk around. Without one this is a tail view and the
    cap has to keep the *newest* `limit` rows, so the scan runs newest-first
    and the result is reversed at the end. With a floor the caller is reading
    forward from a cursor, and the cap has to keep the *oldest* `limit` rows
    past it — anything else drops the messages between the cursor and the
    window, which is the one thing a cursor exists to prevent.
    """
    if limit is not None and limit < 1:
        return []
    params: list[Any] = [_name_key(agent)]
    clauses: list[str] = []
    if through_seq is not None:
        clauses.append("m.seq <= ?")
        params.append(through_seq)
    if seq_floor is not None:
        clauses.append("m.seq > ?")
        params.append(seq_floor)
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""
    forward = seq_floor is not None or date_floor is not None
    sql = f"""
        SELECT m.seq, m.entry_json
        FROM messages AS m
        JOIN message_agents AS a ON a.seq = m.seq
        WHERE a.agent_key = ?{extra}
        ORDER BY m.seq {"ASC" if forward else "DESC"}
    """
    keys = sender_keys(senders)
    # A sender filter, or a date floor decided row by row after decoding,
    # walks the cursor lazily instead: the limit must count matches, not
    # rows scanned, so a SQL LIMIT here would cut the scan before either
    # filter had a chance to reject anything.
    if limit is not None and not keys and date_floor is None:
        sql += " LIMIT ?"
        params.append(limit)
    found: list[tuple[int, dict[str, Any]]] = []
    for seq, raw in conn.execute(sql, params):
        entry = _decode_entry(raw)
        if entry is None or not sent_by(entry, keys):
            continue
        if date_floor is not None:
            stamp = parse_stamp(entry.get("date") or "")
            if stamp is None or stamp < date_floor:
                continue
        found.append((int(seq), entry))
        if limit is not None and len(found) >= limit:
            break
    if not forward:
        found.reverse()
    return found


def load_agent_entries(
    agent: str, *, limit: int, senders: list[str] | None = None
) -> list[dict[str, Any]]:
    if limit < 1:
        return []
    try:
        with closing(open_for_read()) as conn:
            return [
                entry
                for _, entry in _latest_agent_entries(
                    conn, agent, limit, senders=senders
                )
            ]
    except (OSError, sqlite3.Error) as e:
        raise ConversationArchiveError(f"cannot read {conversations_path()}: {e}") from e


def _since_floor(
    conn: sqlite3.Connection, since: str
) -> tuple[int | None, datetime | None]:
    """Resolve a `--since` cursor into `(seq_floor, date_floor)`; exactly one is set.

    A **ulid** cursor resolves to `seq_floor`: the seq of the row that ulid
    names. Every row inserted after it has a strictly greater seq no matter
    how out of order its own `date` reads — the late-delivery case — so a
    ulid cursor can never drop one.

    A **timestamp or bare date** cursor also resolves to `seq_floor`, computed
    as the seq of the *last-inserted* row (across the whole store, not just
    this agent's) whose stored `date` is at or before it — strictly before it
    for a bare date, which names a whole day and so includes the row on its
    local midnight. That is an
    approximation, not an identity, and it has exactly the failure mode a
    ulid cursor does not: a message dated at or before the cursor that has
    not arrived yet when this call runs — the late-delivery case again — will
    itself become tomorrow's floor instead of a row found after it, because
    it is then the newest row with `date <= cursor`. It is swallowed, not
    surfaced. Record `{ulid}` as the cursor, not a timestamp, to close this.

    A **duration** cursor (`2h`, `30m`, `3d`, or bare seconds) resolves to
    `date_floor`: `now` minus the duration, compared as an instant against
    each row's own parsed `date`. No seq is involved, so the late-delivery
    question does not arise — a message dated inside the window is included
    whenever this runs, whatever its insertion order.

    Both date forms compare **instants**, never the spellings. A stored date
    may be written with any fractional precision and any offset, so `14:00Z`
    sorts after `14:00:00.000000Z` as text while naming the earlier moment;
    every comparison here parses both sides first, and a row whose date does
    not parse at all takes part in no date comparison.

    A cursor that carries no offset — a bare date included — is read in this
    machine's local zone, so `--since 2026-09-10` starts at local midnight,
    the day the delegator means. `receipts.parse_stamp` keeps presuming UTC:
    its other callers read *stored* stamps, which are written in UTC.
    """
    cursor = since.strip()
    if is_ulid(cursor):
        row = conn.execute(
            "SELECT seq FROM messages WHERE message_id = ?", (cursor.upper(),)
        ).fetchone()
        if row is None:
            raise ConversationArchiveError(f"--since: no message with id {cursor}")
        return int(row[0]), None
    try:
        seconds = parse_duration(cursor)
    except ValueError:
        seconds = None
    if seconds is not None:
        return None, datetime.now(timezone.utc) - timedelta(seconds=seconds)
    try:
        dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(
            f"--since: cannot parse {since!r} as a ulid, timestamp, or duration"
        ) from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    # A timestamp is a point the reader has already seen through, so the
    # floor is the last row at or before it. A bare date names a day the
    # reader wants whole, midnight included, so its floor is the last row
    # strictly before that midnight.
    inclusive = _is_bare_date(cursor)
    floor_seq = 0
    for seq, raw in conn.execute("SELECT seq, entry_json FROM messages ORDER BY seq DESC"):
        entry = _decode_entry(raw)
        if entry is None:
            continue
        stamp = parse_stamp(entry.get("date") or "")
        if stamp is None:
            continue
        if stamp < dt or (stamp == dt and not inclusive):
            floor_seq = int(seq)
            break
    return floor_seq, None


def _is_bare_date(cursor: str) -> bool:
    try:
        date.fromisoformat(cursor)
    except ValueError:
        return False
    return len(cursor) == 10


def load_agent_records(
    agent: str,
    *,
    limit: int | None,
    senders: list[str] | None = None,
    since: str | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    """Entries for `agent` in display (seq) order, each paired with its own
    `seq` — what a `--json` row needs alongside the rendered fields, and what
    a `--since <ulid>` cursor is resolved against.

    `since=None` behaves like `load_agent_entries` — the newest `limit` rows,
    just with `seq` kept on each row. With a cursor the rows come from the
    other end: the **oldest** `limit` rows past it, and `limit=None` drains
    every one of them. A poller that saves the newest ulid it was handed and
    asks for `--since <that>` next time therefore loses nothing, whether it
    drains or pages.

    See `_since_floor` for what the three cursor forms mean and for the
    timestamp cursor's late-delivery gap that a ulid cursor closes.
    """
    if limit is not None and limit < 1:
        return []
    try:
        with closing(open_for_read()) as conn:
            seq_floor = date_floor = None
            if since is not None:
                seq_floor, date_floor = _since_floor(conn, since)
            return _latest_agent_entries(
                conn,
                agent,
                limit,
                senders=senders,
                seq_floor=seq_floor,
                date_floor=date_floor,
            )
    except (OSError, sqlite3.Error) as e:
        raise ConversationArchiveError(f"cannot read {conversations_path()}: {e}") from e


def _merge_into_stored(stored_json: str, entry: dict[str, Any]) -> str | None:
    """Fold a later delivery into the stored row, or None when it adds nothing.

    One message id is one row, but each recipient downloads separately and a
    deferred recipient records long after an immediate one. Two things from
    that later write must reach the row:

    `recipients`, because the row is the record of who the message reached.
    Attaching the name to `message_agents` alone makes the index and the entry
    disagree — the lookup finds the row for a name the row does not list, so
    `involves_agent` denies what the query just asserted, and the rendered row
    names only whoever happened to be written first.

    `files_unavailable`, because leaving the first writer's clean view in place
    is how a lost file keeps being described as a delivered one.
    """
    stored = _decode_entry(stored_json)
    if stored is None:
        return None
    changed = False

    known_to = list(stored.get("recipients") or [])
    seen_keys = {_name_key(str(name)) for name in known_to}
    for name in entry.get("recipients") or []:
        key = _name_key(str(name))
        if key and key not in seen_keys:
            seen_keys.add(key)
            known_to.append(name)
            changed = True
    if changed:
        stored["recipients"] = known_to

    known_lost = stored.get("files_unavailable") or []
    seen_lost = {(e.get("filename"), e.get("detail")) for e in known_lost}
    added = [
        e
        for e in (entry.get("files_unavailable") or [])
        if (e.get("filename"), e.get("detail")) not in seen_lost
    ]
    if added:
        stored["files_unavailable"] = known_lost + added
        changed = True

    return json.dumps(stored, ensure_ascii=False) if changed else None


def _insert_entry(entry: dict[str, Any], msg_id: str | None) -> None:
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO messages(message_id, entry_json) VALUES (?, ?)",
            (msg_id, json.dumps(entry, ensure_ascii=False)),
        )
        if cursor.rowcount:
            seq = int(cursor.lastrowid)
        else:
            # The id is already archived — a second recipient of the same
            # message, or a deferred one landing after the immediate batch.
            # Returning here dropped that recipient out of the archive
            # entirely, so `a8s convo <name>` had nothing for it.
            row = conn.execute(
                "SELECT seq, entry_json FROM messages WHERE message_id = ?",
                (msg_id,),
            ).fetchone()
            if row is None:
                return
            seq = int(row[0])
            merged = _merge_into_stored(str(row[1]), entry)
            if merged is not None:
                conn.execute(
                    "UPDATE messages SET entry_json = ? WHERE seq = ?", (merged, seq)
                )
        conn.executemany(
            "INSERT OR IGNORE INTO message_agents(seq, agent_key) VALUES (?, ?)",
            [(seq, key) for key in sorted(_entry_agents(entry))],
        )


def record(msg: dict[str, Any], *, recipients: list[str]) -> None:
    """Append one logical message when delivery completes (local inbox, remote
    receive, or outbound remote publish). `recipients` lists local deliverees
    for routed/RECEIVED_REMOTE rows, or the logical `to` name for outbound
    remote-only sends. Retention is applied by `a8s update`, never here.
    """
    if not recipients:
        return
    entry = entry_from_message(msg, recipients=recipients)
    msg_id = entry.get("id") or None
    try:
        sqlite_store.retry_busy(lambda: _insert_entry(entry, msg_id))
    except (OSError, sqlite3.Error) as e:
        label = msg_id or "without-id"
        out(f"WARN conversation archive failed id={label}: {e}")


def prune_conversations(max_rows: int | None = None) -> int:
    """Retain the newest configured number of rows and return rows removed."""
    keep = max_rows if max_rows is not None else get_int("convo_max_rows")
    if keep < 1:
        raise ValueError("max_rows must be positive")
    try:
        with closing(_connect()) as conn:
            with conn:
                before = int(
                    conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                )
                removed = 0
                if before > keep:
                    cutoff = conn.execute(
                        "SELECT seq FROM messages ORDER BY seq DESC LIMIT 1 OFFSET ?",
                        (keep - 1,),
                    ).fetchone()
                    if cutoff is not None:
                        conn.execute(
                            "DELETE FROM messages WHERE seq < ?", (int(cutoff[0]),)
                        )
                        removed = before - keep
            conn.execute("PRAGMA optimize")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return removed
    except (OSError, sqlite3.Error) as e:
        raise ConversationArchiveError(str(e)) from e


def _format_heading(template: str, entry: dict[str, Any]) -> str:
    ts = (entry.get("date") or "").strip()
    return template.format(
        **{
            "from": entry.get("from", ""),
            "to": entry.get("to", ""),
            "timestamp": clock.stamp(ts, seconds=True),
            "date": clock.stamp(ts, seconds=True),
            "utc": ts,
            "ulid": (entry.get("id") or "").strip(),
        }
    )


def _attachment_lines(agent: str, entry: dict[str, Any]) -> list[str]:
    """One line per attachment. A file the transfer lost says so and says why.

    The bare-name line is not evidence of failure and must not read as one: a
    message this agent *sent* keeps its files in an outbox bundle this lookup
    does not search, and an inbound bundle is reaped after its retention
    window. Only an entry that arrived carrying an error is reported lost.
    """
    names = [str(name).strip() for name in (entry.get("files") or []) if str(name).strip()]
    lost = {
        str(e.get("filename") or "").strip(): str(e.get("detail") or "").strip()
        for e in (entry.get("files_unavailable") or [])
        if isinstance(e, dict) and str(e.get("filename") or "").strip()
    }
    if not names:
        return []
    msg_id = (entry.get("id") or "").strip()
    bundle_root: Path | None = None
    if msg_id and not all(name in lost for name in names):
        from registry import find_participant, participants_from_registry

        participant = find_participant(participants_from_registry(), agent)
        if participant is not None:
            bundle_root = inbound_bundle_dir(participant.files_path(), msg_id)
    lines: list[str] = []
    for name in names:
        if name in lost:
            detail = lost[name]
            lines.append(
                f"- ATTACHMENT UNAVAILABLE: {name}" + (f": {detail}" if detail else "")
            )
            continue
        if bundle_root is not None:
            path = bundle_root / name
            if path.is_file():
                lines.append(f"- attachment: {path}")
                continue
        lines.append(f"- attachment: {name}")
    return lines


def format_entry(
    agent: str,
    entry: dict[str, Any],
    *,
    heading_out: str = DEFAULT_HEADING_OUT,
    heading_in: str = DEFAULT_HEADING_IN,
) -> str:
    agent_key = _name_key(agent)
    sent = _name_key(entry.get("from", "")) == agent_key
    heading = _format_heading(heading_out if sent else heading_in, entry)
    content = entry.get("content", "")
    block = heading
    if content:
        block = f"{heading}\n\n{content}"
    file_lines = _attachment_lines(agent, entry)
    if file_lines:
        joined = "\n".join(file_lines)
        block = f"{block}\n\n{joined}" if block else joined
    return block


def open_glow_stdout(theme: str = "auto"):
    from glow_util import open_glow_stdout as _open

    return _open(theme)


def write_block(block: str, glow_stream: object | None) -> None:
    if not block:
        return
    if glow_stream is not None:
        glow_stream.write(block + "\n\n")
        # Each convo entry is complete markdown. Force a final flush so an
        # unclosed fence (common in agent replies) cannot hold the message
        # in GlowStream's buffer until Ctrl+C.
        finalize = getattr(glow_stream, "finalize", None)
        if callable(finalize):
            finalize()
        return
    print(block, flush=True)
    print(flush=True)


def print_entries(
    agent: str,
    entries: list[dict[str, Any]],
    *,
    glow_stream: object | None = None,
    heading_out: str = DEFAULT_HEADING_OUT,
    heading_in: str = DEFAULT_HEADING_IN,
) -> None:
    for entry in entries:
        block = format_entry(agent, entry, heading_out=heading_out, heading_in=heading_in)
        write_block(block, glow_stream)


def format_conversation(
    agent: str,
    *,
    limit: int = 10,
    heading_out: str = DEFAULT_HEADING_OUT,
    heading_in: str = DEFAULT_HEADING_IN,
    senders: list[str] | None = None,
) -> str:
    """Return markdown for the last `limit` messages involving `agent`."""
    if limit < 1:
        return ""
    rows = load_agent_entries(agent, limit=limit, senders=senders)
    parts = [
        format_entry(agent, entry, heading_out=heading_out, heading_in=heading_in)
        for entry in rows
    ]
    return "\n\n".join(parts)


def follow_conversation(
    agent: str,
    *,
    limit: int = 10,
    heading_out: str = DEFAULT_HEADING_OUT,
    heading_in: str = DEFAULT_HEADING_IN,
    poll_interval: float = 1.0,
    glow_theme: str | None = None,
    senders: list[str] | None = None,
) -> None:
    """Print the last `limit` messages, then emit rows after a sequence cursor."""
    keys = sender_keys(senders)
    glow_stream = None
    if glow_theme is not None:
        try:
            glow_stream = open_glow_stdout(glow_theme)
        except FileNotFoundError:
            print("a8s convo: glow not found on PATH", file=sys.stderr)

    try:
        with closing(open_for_read()) as conn:
            conn.execute("BEGIN")
            cursor = int(
                conn.execute("SELECT COALESCE(MAX(seq), 0) FROM messages").fetchone()[0]
            )
            rows = _latest_agent_entries(
                conn, agent, limit, through_seq=cursor, senders=senders
            )
            conn.commit()
        print_entries(
            agent,
            [entry for _, entry in rows],
            glow_stream=glow_stream,
            heading_out=heading_out,
            heading_in=heading_in,
        )

        while True:
            time.sleep(poll_interval)
            with closing(open_for_read()) as conn:
                conn.execute("BEGIN")
                bounds = conn.execute(
                    "SELECT MIN(seq), COALESCE(MAX(seq), 0) FROM messages"
                ).fetchone()
                minimum = int(bounds[0]) if bounds[0] is not None else None
                high_water = int(bounds[1])
                reset = bool(cursor and high_water < cursor)
                query_cursor = 0 if reset else cursor
                rows = conn.execute(
                    """
                    SELECT m.seq, m.entry_json
                    FROM messages AS m
                    JOIN message_agents AS a ON a.seq = m.seq
                    WHERE a.agent_key = ? AND m.seq > ? AND m.seq <= ?
                    ORDER BY m.seq
                    """,
                    (_name_key(agent), query_cursor, high_water),
                ).fetchall()
                conn.commit()
            if reset:
                print(
                    "a8s convo: conversation archive sequence reset "
                    f"from {cursor} to {high_water}; following from the beginning",
                    file=sys.stderr,
                    flush=True,
                )
            elif minimum is not None and cursor and minimum > cursor + 1:
                print(
                    "a8s convo: conversation housekeeping advanced past "
                    f"{minimum - cursor - 1} row(s); messages may have been missed",
                    file=sys.stderr,
                    flush=True,
                )
            for _, entry in _rows_to_entries(rows):
                if not sent_by(entry, keys):
                    continue
                print_entries(
                    agent,
                    [entry],
                    glow_stream=glow_stream,
                    heading_out=heading_out,
                    heading_in=heading_in,
                )
            cursor = high_water
    finally:
        if glow_stream is not None:
            glow_stream.close()
