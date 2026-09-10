"""Connection discipline shared by the a8s SQLite stores.

The conversation archive and the transaction log are both written by several
processes at once (router, wake handlers, network receive loops), so the WAL
setup and busy-retry policy live here instead of drifting apart in two copies.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Sequence, TypeVar

__all__ = ["BUSY_TIMEOUT_MS", "connect", "connect_read_only", "hold", "retry_busy"]

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


def hold(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Make `conn` a connection that keeps the WAL side files present.

    SQLite creates `<store>-wal` and `<store>-shm` on the first statement
    that touches the database, and deletes them when the last connection
    closes. A `mode=ro` reader can create neither, so it can read a WAL store
    only while some other connection holds that pair open or the directory is
    writable. Reading the schema is what brings them into being; whoever
    keeps the connection keeps them there.
    """
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return conn


_SIDE_FILE_SUFFIXES = ("-wal", "-shm")


def _side_files_are_the_obstacle(path: Path, err: sqlite3.Error) -> bool:
    """True when the missing WAL side files could explain a read-only failure.

    `mode=ro` cannot create `<store>-wal` and `<store>-shm`, so a WAL store
    with neither present opens only if the directory is writable enough for
    SQLite to make them. When it is not, the open fails on a store that is
    otherwise perfectly readable — SQLITE_CANTOPEN on some builds,
    SQLITE_READONLY_DIRECTORY on others, and the raw text of either
    ("unable to open database file") sends the reader after the wrong file.
    A `-wal` present without its `-shm` is a different fault and stays with
    the message SQLite gave it.

    This is a **could**, not a proof. Whether the directory is writable is
    not tested: `os.access` answers true for any existing directory on
    Windows without reading its ACLs, and no production reader should be
    writing a probe file to find out. So the same codes are raised by faults
    that have nothing to do with the side files — an unreadable main file,
    for one — and the caller adds its sentence to SQLite's words rather than
    replacing them, phrased as the condition it cannot rule out.
    """
    if (err.sqlite_errorcode & 0xFF) not in (
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_READONLY,
    ):
        return False
    if not path.is_file():
        return False
    return not any(path.with_name(path.name + s).exists() for s in _SIDE_FILE_SUFFIXES)


def _read_only_failure(path: Path, err: sqlite3.Error) -> sqlite3.Error:
    if not _side_files_are_the_obstacle(path, err):
        return err
    return sqlite3.OperationalError(
        f"{err}; no WAL side files sit beside the store, and a reader that "
        "cannot create them cannot open it until a node holds the store "
        "open; is a node running on this machine?"
    )


def connect_read_only(path: Path, *, table: str) -> sqlite3.Connection | None:
    """Open `path` for reading, or None when it is not the store `table` names.

    `connect` creates what it does not find, which is right for a writer and
    wrong for a reader: a truncated file or a database belonging to something
    else comes back an initialized empty store, and the next read reports a
    truthful "no rows" about a history it has just replaced. `mode=ro` cannot
    create the file and cannot write a byte of it, so whatever the reader
    could not use is still there for whoever has to look at it.

    A WAL store whose `-wal` file outlived its `-shm` cannot be opened this
    way at all; SQLite raises, and the caller says which file and why. That is
    the answer this reader owes either way — the one thing it must not do is
    return an empty result it did not read. When neither side file is there,
    "unable to open database file" points the reader at a file that is
    probably fine, so SQLite's words keep their place and a sentence naming
    the condition they can hide is added after them
    (`_side_files_are_the_obstacle`).
    """
    # A8S_HOME (lib/ar3/home.py) is deliberately allowed to stay relative, but
    # as_uri() refuses a relative path outright — absolute() (not resolve(),
    # which would follow symlinks and rename the path in error messages) is
    # enough to make it URI-eligible without changing what the user sees.
    uri_path = path.absolute()
    try:
        conn = sqlite3.connect(
            f"{uri_path.as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000
        )
    except sqlite3.Error as e:
        raise _read_only_failure(path, e) from e
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        missing = _needs_schema(conn, table)
    except sqlite3.Error as e:
        conn.close()
        raise _read_only_failure(path, e) from e
    if missing:
        conn.close()
        return None
    return conn
