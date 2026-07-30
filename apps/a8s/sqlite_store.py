"""Connection discipline shared by the a8s SQLite stores.

The conversation archive and the transaction log are both written by several
processes at once (router, wake handlers, network receive loops), so the WAL
setup and busy-retry policy live here instead of drifting apart in two copies.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Sequence, TypeVar

__all__ = ["BUSY_TIMEOUT_MS", "connect", "retry_busy"]

BUSY_TIMEOUT_MS = 5000
_BUSY_RETRIES = 6
_BUSY_BACKOFF = 0.05

_INIT_LOCK = threading.Lock()

_T = TypeVar("_T")


def _is_busy(err: sqlite3.Error) -> bool:
    return (err.sqlite_errorcode & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def retry_busy(op: Callable[[], _T]) -> _T:
    """Run `op`, retrying while SQLite reports contention.

    `busy_timeout` covers statements that wait on a lock, but the WAL/journal
    transition returns SQLITE_BUSY without ever invoking the busy handler, so
    the setup path needs an explicit retry to stay durable under concurrent
    writers.
    """
    for attempt in range(_BUSY_RETRIES - 1):
        try:
            return op()
        except sqlite3.Error as e:
            if not _is_busy(e):
                raise
        time.sleep(_BUSY_BACKOFF * (attempt + 1))
    return op()


def _needs_schema(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is None
    )


def _initialize(conn: sqlite3.Connection, schema: Sequence[str], table: str) -> None:
    """Create the schema so other connections see all of it at once.

    The statements run in one explicit transaction rather than through
    `executescript`, which commits between statements — a concurrent writer
    could otherwise find the probed table already there and a later one not.
    """
    if not _needs_schema(conn, table):
        return
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in schema:
            conn.execute(statement)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def connect(
    path: Path,
    schema: Sequence[str],
    *,
    table: str,
    foreign_keys: bool = False,
) -> sqlite3.Connection:
    """Open `path` in WAL mode, creating `schema` when `table` is absent.

    `table` is the read-only probe: its presence means setup already ran, so
    the common path never takes a write lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if _needs_schema(conn, table):
            with _INIT_LOCK:
                retry_busy(lambda: _initialize(conn, schema, table))
    except sqlite3.Error:
        conn.close()
        raise
    return conn
