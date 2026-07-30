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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlite_store
from core import conversations_path, inbound_bundle_dir, out
from settings import get_int

__all__ = [
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
    "involves_agent",
    "load_agent_entries",
    "load_entries",
    "open_glow_stdout",
    "print_entries",
    "prune_conversations",
    "record",
    "write_block",
]

DEFAULT_HEADING_OUT = "## from {from} to {to} at {timestamp}"
DEFAULT_HEADING_IN = "### from {from} to {to} at {timestamp}"

HEADING_PLACEHOLDERS = ("from", "to", "timestamp", "date")


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
    return f"""heading templates:
  Outbound (--heading-out) and inbound (--heading-in) use Python str.format placeholders:
    {{from}}       sender name
    {{to}}         recipient or alias
    {{timestamp}}  ISO UTC timestamp from the message
    {{date}}       alias for {{timestamp}}

  Defaults:
    outbound: {DEFAULT_HEADING_OUT}
    inbound:  {DEFAULT_HEADING_IN}

  Multiline headings:
    - Shell quotes preserve embedded newlines in one argument
    - Multiple arguments after the flag join with newlines (one line each)
    - Use \\n and \\t escapes inside a single argument

  Message body and attachment lines are appended after the heading block.

examples:
  a8s convo neil-macbook -f --limit 10 --glow
  a8s convo bob --heading-out '**{{from}}**' '→ {{to}}' --limit 5
  a8s convo bob --heading-in "### {{from}}\\n_{{timestamp}}_"

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
    """Normalize a tell/inbox envelope into a conversation archive entry."""
    files = msg.get("files") or []
    filenames = [
        (e.get("filename") or "").strip()
        for e in files
        if isinstance(e, dict) and (e.get("filename") or "").strip()
    ]
    return {
        "date": (msg.get("date") or "").strip() or _now_iso(),
        "from": (msg.get("from") or "").strip(),
        "to": (msg.get("to") or "").strip(),
        "content": msg.get("content", ""),
        "files": filenames,
        "id": (msg.get("id") or "").strip(),
        "recipients": list(recipients or []),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def load_entries() -> list[dict[str, Any]]:
    path = conversations_path()
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT entry_json FROM messages ORDER BY seq"
            ).fetchall()
        for (raw,) in rows:
            entry = _decode_entry(raw)
            if entry is not None:
                entries.append(entry)
    except (OSError, sqlite3.Error):
        return []
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
    limit: int,
    *,
    through_seq: int | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    if limit < 1:
        return []
    params: list[Any] = [_name_key(agent)]
    through = ""
    if through_seq is not None:
        through = "AND m.seq <= ?"
        params.append(through_seq)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT m.seq, m.entry_json
        FROM messages AS m
        JOIN message_agents AS a ON a.seq = m.seq
        WHERE a.agent_key = ? {through}
        ORDER BY m.seq DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    rows.reverse()
    return _rows_to_entries(rows)


def load_agent_entries(agent: str, *, limit: int) -> list[dict[str, Any]]:
    if limit < 1 or not conversations_path().is_file():
        return []
    try:
        with closing(_connect()) as conn:
            return [entry for _, entry in _latest_agent_entries(conn, agent, limit)]
    except (OSError, sqlite3.Error):
        return []


def _insert_entry(entry: dict[str, Any], msg_id: str | None) -> None:
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO messages(message_id, entry_json) VALUES (?, ?)",
            (msg_id, json.dumps(entry, ensure_ascii=False)),
        )
        if cursor.rowcount == 0:
            return
        seq = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO message_agents(seq, agent_key) VALUES (?, ?)",
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
            "timestamp": ts,
            "date": ts,
        }
    )


def _attachment_lines(agent: str, entry: dict[str, Any]) -> list[str]:
    names = [str(name).strip() for name in (entry.get("files") or []) if str(name).strip()]
    if not names:
        return []
    msg_id = (entry.get("id") or "").strip()
    bundle_root: Path | None = None
    if msg_id:
        from registry import find_participant, participants_from_registry

        participant = find_participant(participants_from_registry(), agent)
        if participant is not None:
            bundle_root = inbound_bundle_dir(participant.files_path(), msg_id)
    lines: list[str] = []
    for name in names:
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
) -> str:
    """Return markdown for the last `limit` messages involving `agent`."""
    if limit < 1:
        return ""
    rows = load_agent_entries(agent, limit=limit)
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
) -> None:
    """Print the last `limit` messages, then emit rows after a sequence cursor."""
    glow_stream = None
    if glow_theme is not None:
        try:
            glow_stream = open_glow_stdout(glow_theme)
        except FileNotFoundError:
            print("a8s convo: glow not found on PATH", file=sys.stderr)

    try:
        with closing(_connect()) as conn:
            conn.execute("BEGIN")
            cursor = int(
                conn.execute("SELECT COALESCE(MAX(seq), 0) FROM messages").fetchone()[0]
            )
            rows = _latest_agent_entries(conn, agent, limit, through_seq=cursor)
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
            with closing(_connect()) as conn:
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
