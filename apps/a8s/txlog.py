"""A8S transaction log — routing breadcrumbs for `a8s trace <ULID>`.

One row per routing event in WAL SQLite at ``transactions.sqlite3`` under the
a8s state root. Not a message store: full bodies live in `.inbox` and the
conversation archive, and `detail` holds only a short preview.

Designed for debugging message flow end-to-end: trace a msg_id from
sender outbox -> local routing -> file transfer -> remote publish -> remote
receive -> recipient wake. `a8s trace` is the stable interface; nothing outside
this module reads the rows.

The log also carries runner lifecycle (`RUN_START`, `RUN_STOP`, `WAKE_START`,
`WAKE_RETURN`, `HEARTBEAT`, `WEDGE`) so a dead or deaf dispatcher is visible
from `a8s tx`.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Literal

import sqlite_store
from core import transactions_path
from settings import DEFAULTS, get_int, get_setting

__all__ = [
    "EVENTS",
    "TransactionLogError",
    "log",
    "prune_transactions",
    "read_events",
    "read_recent",
]

Event = Literal[
    "ROUTED",
    "RECEIVED_REMOTE",
    "RESOLVED_REMOTE",
    "NOT_LOCAL",
    "RECEIPT_PUBLISHED",
    "DELIVERY_RECEIPT",
    "FILE_DELIVERED",
    "FILE_UPLOAD_FAILED",
    "PUBLISHED",
    "DISCARDED",
    "DROPPED",
    "PROXY_DELIVERED",
    "RUN_START",
    "RUN_STOP",
    "WAKE_START",
    "WAKE_RETURN",
    "HEARTBEAT",
    "WEDGE",
]

EVENTS: tuple[str, ...] = (
    "ROUTED",
    "RECEIVED_REMOTE",
    "RESOLVED_REMOTE",
    "NOT_LOCAL",
    "RECEIPT_PUBLISHED",
    "DELIVERY_RECEIPT",
    "FILE_DELIVERED",
    "FILE_UPLOAD_FAILED",
    "PUBLISHED",
    "DISCARDED",
    "DROPPED",
    "PROXY_DELIVERED",
    "RUN_START",
    "RUN_STOP",
    "WAKE_START",
    "WAKE_RETURN",
    "HEARTBEAT",
    "WEDGE",
)

# Event dict keys, in trace display order. `from`/`to` are SQL keywords, so
# the columns are named `sender`/`recipient` and mapped back here.
FIELDS = (
    "timestamp",    # ISO-8601 UTC with milliseconds
    "event",        # event type (see Event literal above)
    "msg_id",       # envelope ULID
    "from",         # sender participant name
    "to",           # recipient participant name (or alias)
    "files",        # comma-separated filenames (or empty)
    "remote",       # remote id involved (or empty)
    "detail",       # short free-text (preview, error, etc.)
)

_COLUMNS = "timestamp, event, msg_id, sender, recipient, files, remote, detail"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS transactions (
        seq INTEGER PRIMARY KEY,
        timestamp TEXT NOT NULL,
        event TEXT NOT NULL,
        msg_id TEXT NOT NULL COLLATE NOCASE,
        sender TEXT NOT NULL,
        recipient TEXT NOT NULL,
        files TEXT NOT NULL,
        remote TEXT NOT NULL,
        detail TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS transactions_msg_id
        ON transactions(msg_id, seq)
    """,
)


class TransactionLogError(RuntimeError):
    pass


def _connect() -> sqlite3.Connection:
    return sqlite_store.connect(transactions_path(), _SCHEMA, table="transactions")


def _ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _one_line(val: str) -> str:
    """Collapse newlines so each event renders as one `a8s trace` line."""
    return val.replace("\n", " ").replace("\r", "")


def _detail_max() -> int:
    try:
        limit = int(get_setting("txlog_detail_max"))
    except (TypeError, ValueError):
        return int(DEFAULTS["txlog_detail_max"])
    return limit if limit >= 0 else int(DEFAULTS["txlog_detail_max"])


def log(
    event: Event,
    *,
    msg_id: str = "",
    sender: str = "",
    recipient: str = "",
    files: list[str] | None = None,
    remote: str = "",
    detail: str = "",
) -> None:
    """Append one transaction row. Never raises — errors are swallowed.

    A breadcrumb that cannot be written must not disturb the delivery path it
    is describing, and warning per failure would flood the router's output.
    """
    try:
        detail_max = _detail_max()
        row = (
            _ts(),
            event,
            msg_id,
            sender,
            recipient,
            ",".join(files) if files else "",
            remote,
            _one_line(detail if detail_max <= 0 else detail[:detail_max]),
        )

        def insert() -> None:
            with closing(_connect()) as conn, conn:
                conn.execute(
                    f"INSERT INTO transactions({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )

        sqlite_store.retry_busy(insert)
    except (OSError, sqlite3.Error):
        pass


def read_events(msg_id: str) -> list[dict[str, str]]:
    """Return transaction events correlated to one message ULID."""
    if not transactions_path().is_file():
        return []
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM transactions WHERE msg_id = ? ORDER BY seq",
                (msg_id,),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    return [dict(zip(FIELDS, row)) for row in rows]


def read_recent(
    *,
    limit: int = 20,
    events: list[str] | None = None,
    senders: list[str] | None = None,
    recipients: list[str] | None = None,
    msg_id: str = "",
    after_seq: int | None = None,
) -> list[tuple[int, dict[str, str]]]:
    """Return `(seq, event)` pairs in chronological order, newest `limit` last.

    With `after_seq` the limit does not apply: every matching row after that
    cursor comes back, which is what `a8s transactions -f` polls for.
    """
    if not transactions_path().is_file():
        return []
    where: list[str] = []
    params: list[object] = []
    for column, values in (
        ("event", events),
        ("sender", senders),
        ("recipient", recipients),
    ):
        wanted = [v.strip().lower() for v in (values or []) if v.strip()]
        if wanted:
            where.append(f"LOWER({column}) IN ({','.join('?' * len(wanted))})")
            params.extend(wanted)
    if msg_id:
        where.append("msg_id = ?")
        params.append(msg_id)
    if after_seq is not None:
        where.append("seq > ?")
        params.append(after_seq)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    if after_seq is None:
        sql = f"SELECT seq, {_COLUMNS} FROM transactions {clause} ORDER BY seq DESC LIMIT ?"
        params.append(max(limit, 0))
    else:
        sql = f"SELECT seq, {_COLUMNS} FROM transactions {clause} ORDER BY seq"
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
    except (OSError, sqlite3.Error):
        return []
    if after_seq is None:
        rows.reverse()
    return [(int(row[0]), dict(zip(FIELDS, row[1:]))) for row in rows]


def last_heard() -> dict[str, str]:
    """Newest arrival per remote sender: `{name: utc_stamp}`.

    Arrival is the only evidence this log can offer about a remote being
    alive. `PUBLISHED` records that we handed a message to the transport, not
    that anything on the far side read it — a remote that is down and a remote
    that is fine produce the same row. `RECEIVED_REMOTE` cannot be written
    unless the far side actually spoke.

    Names fold case-insensitively, matching how the registry resolves them:
    `Robin` and `robin` are one remote, stamped by whichever spoke last and
    spelled the way that newest arrival spelled it.

    This reads the transaction log, so it sees only what retention has kept.
    A remote whose rows have aged out of `txlog_max_rows` drops off the list
    until it speaks again — the log is an event record, not a roster.
    """
    if not transactions_path().is_file():
        return {}
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT sender, MAX(timestamp) FROM transactions "
                "WHERE event = 'RECEIVED_REMOTE' AND sender != '' "
                "GROUP BY sender"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}
    heard: dict[str, tuple[str, str]] = {}
    for sender, ts in rows:
        if not sender or not ts:
            continue
        key = str(sender).lower()
        newest = heard.get(key)
        if newest is None or str(ts) > newest[1]:
            heard[key] = (str(sender), str(ts))
    return {name: stamp for name, stamp in heard.values()}


def prune_transactions(max_rows: int | None = None) -> int:
    """Retain the newest configured number of rows and return rows removed."""
    keep = max_rows if max_rows is not None else get_int("txlog_max_rows")
    if keep < 1:
        raise ValueError("max_rows must be positive")
    try:
        with closing(_connect()) as conn:
            with conn:
                before = int(
                    conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
                )
                removed = 0
                if before > keep:
                    cutoff = conn.execute(
                        "SELECT seq FROM transactions ORDER BY seq DESC LIMIT 1 OFFSET ?",
                        (keep - 1,),
                    ).fetchone()
                    if cutoff is not None:
                        conn.execute(
                            "DELETE FROM transactions WHERE seq < ?", (int(cutoff[0]),)
                        )
                        removed = before - keep
            conn.execute("PRAGMA optimize")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return removed
    except (OSError, sqlite3.Error) as e:
        raise TransactionLogError(str(e)) from e
