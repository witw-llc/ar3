"""Tests for txlog.py — transaction log in WAL SQLite."""
from __future__ import annotations

import re
import sqlite3

import pytest

from commands import cmd_transactions
from core import transactions_path
from txlog import (
    TransactionLogError,
    _one_line,
    _ts,
    log,
    prune_transactions,
    read_events,
    read_recent,
)


def _rows() -> list[tuple]:
    with sqlite3.connect(transactions_path()) as conn:
        return conn.execute(
            "SELECT timestamp, event, msg_id, sender, recipient, files, remote, detail"
            " FROM transactions ORDER BY seq"
        ).fetchall()


class TestTransactionsPath:
    def test_respects_a8s_home(self, fake_home, monkeypatch, tmp_path):
        custom = tmp_path / "custom-a8s"
        monkeypatch.setenv("A8S_HOME", str(custom))
        assert transactions_path() == custom / "transactions.sqlite3"

    def test_defaults_under_home(self, fake_home):
        assert transactions_path() == fake_home / ".a8s" / "transactions.sqlite3"


class TestOneLine:
    def test_collapses_newlines(self):
        assert _one_line("line1\nline2") == "line1 line2"

    def test_strips_carriage_returns(self):
        assert _one_line("a\rb") == "ab"

    def test_clean_string_unchanged(self):
        assert _one_line("nothing special") == "nothing special"


class TestTimestamp:
    def test_iso8601_format(self):
        ts = _ts()
        # e.g. 2026-05-28T14:30:01.123Z
        pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
        assert re.match(pattern, ts), f"Timestamp {ts!r} doesn't match ISO-8601 ms"

    def test_ends_with_z(self):
        assert _ts().endswith("Z")

    def test_milliseconds_are_three_digits(self):
        ts = _ts()
        ms_part = ts.split(".")[1].rstrip("Z")
        assert len(ms_part) == 3


class TestLogCreatesStore:
    def test_creates_database(self, fake_home):
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B")
        assert transactions_path().is_file()
        assert len(_rows()) == 1

    def test_appends_in_order(self, fake_home):
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B")
        log("PUBLISHED", msg_id="02DEF", sender="C", recipient="D")
        assert [row[1] for row in _rows()] == ["ROUTED", "PUBLISHED"]

    def test_creates_parent_dirs(self, fake_home, monkeypatch, tmp_path):
        deep = tmp_path / "deep" / "nested" / "a8s"
        monkeypatch.setenv("A8S_HOME", str(deep))
        log("ROUTED", msg_id="X", sender="A", recipient="B")
        assert (deep / "transactions.sqlite3").is_file()

    def test_uses_wal_journal(self, fake_home):
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B")
        with sqlite3.connect(transactions_path()) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


class TestLogFieldFormat:
    def test_stores_every_field(self, fake_home):
        log(
            "PUBLISHED",
            msg_id="01ABC",
            sender="A",
            recipient="B",
            files=["one.txt"],
            remote="mqtt",
            detail="hi",
        )
        assert _rows()[0][1:] == (
            "PUBLISHED",
            "01ABC",
            "A",
            "B",
            "one.txt",
            "mqtt",
            "hi",
        )

    def test_detail_truncated_at_default_max(self, fake_home):
        from settings import DEFAULTS

        limit = DEFAULTS["txlog_detail_max"]
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B", detail="x" * (limit + 100))
        assert len(_rows()[0][7]) == limit

    def test_detail_max_env_override(self, fake_home, monkeypatch):
        monkeypatch.setenv("A8S_TXLOG_DETAIL_MAX", "50")
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B", detail="y" * 200)
        assert len(_rows()[0][7]) == 50

    def test_detail_max_zero_unlimited(self, fake_home, monkeypatch):
        monkeypatch.setenv("A8S_TXLOG_DETAIL_MAX", "0")
        long_detail = "z" * 5000
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B", detail=long_detail)
        assert _rows()[0][7] == long_detail

    def test_multiline_detail_collapsed(self, fake_home):
        log("DROPPED", msg_id="01ABC", sender="A", recipient="B", detail="bad\nenvelope")
        assert _rows()[0][7] == "bad envelope"

    def test_tabs_in_names_are_preserved(self, fake_home):
        log("ROUTED", msg_id="01ABC", sender="A\tX", recipient="B\tY")
        assert _rows()[0][3:5] == ("A\tX", "B\tY")

    def test_files_none_produces_empty_field(self, fake_home):
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B", files=None)
        assert _rows()[0][5] == ""

    def test_files_list_produces_comma_joined(self, fake_home):
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B",
            files=["one.txt", "two.log", "three.py"])
        assert _rows()[0][5] == "one.txt,two.log,three.py"


class TestReadEvents:
    def test_filters_one_message_and_preserves_order(self, fake_home):
        log("PUBLISHED", msg_id="01ABC", sender="A", recipient="B")
        log("DROPPED", msg_id="02DEF", sender="C", recipient="D")
        log("DELIVERY_RECEIPT", msg_id="01ABC", sender="A", recipient="B")

        events = read_events("01abc")
        assert [event["event"] for event in events] == ["PUBLISHED", "DELIVERY_RECEIPT"]
        assert all(event["msg_id"] == "01ABC" for event in events)

    def test_returns_trace_field_names(self, fake_home):
        log(
            "PUBLISHED",
            msg_id="01ABC",
            sender="A",
            recipient="B",
            remote="mqtt",
            files=["x.txt"],
            detail="preview",
        )
        event = read_events("01ABC")[0]
        assert event["from"] == "A"
        assert event["to"] == "B"
        assert event["remote"] == "mqtt"
        assert event["files"] == "x.txt"
        assert event["detail"] == "preview"
        assert event["timestamp"].endswith("Z")

    def test_missing_store_returns_empty(self, fake_home):
        assert read_events("01ABC") == []
        assert not transactions_path().exists()

    def test_unknown_id_returns_empty(self, fake_home):
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B")
        assert read_events("09ZZZ") == []


class TestLogSwallowsErrors:
    def test_unwritable_path_does_not_raise(self, fake_home, monkeypatch, tmp_path):
        # Point to a path inside a file (impossible to mkdir)
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file, not a directory")
        monkeypatch.setenv("A8S_HOME", str(blocker / "subdir"))
        log("ROUTED", msg_id="X", sender="A", recipient="B")


class TestConcurrency:
    def test_concurrent_writers_do_not_lose_rows(self, fake_home):
        from concurrent.futures import ThreadPoolExecutor

        def write(i: int) -> None:
            log("ROUTED", msg_id=f"01JCONCURRENT{i:012d}", sender="A", recipient="B",
                detail=str(i))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(40)))
        rows = _rows()
        assert len(rows) == 40
        assert {row[7] for row in rows} == {str(i) for i in range(40)}

    def test_concurrent_processes_do_not_lose_rows(self, fake_home):
        import os
        import subprocess
        import sys
        from pathlib import Path

        a8s_dir = Path(__file__).resolve().parent.parent
        script = (
            "import sys, txlog\n"
            "start = int(sys.argv[1])\n"
            "for i in range(start, start + 10):\n"
            "    txlog.log('ROUTED', msg_id=f'01JPROC{i:012d}', detail=str(i))\n"
        )
        env = {
            **os.environ,
            "PYTHONPATH": str(a8s_dir),
            "A8S_HOME": str(transactions_path().parent),
        }
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(start)],
                cwd=str(a8s_dir),
                env=env,
            )
            for start in (0, 10, 20, 30)
        ]
        for proc in procs:
            assert proc.wait(timeout=60) == 0
        rows = _rows()
        assert len(rows) == 40
        assert {row[7] for row in rows} == {str(i) for i in range(40)}


class TestPrune:
    def test_retains_newest_rows(self, fake_home):
        for i in range(5):
            log("ROUTED", msg_id=f"01ABC{i}", sender="A", recipient="B", detail=f"m{i}")
        assert prune_transactions(3) == 2
        rows = _rows()
        assert [row[7] for row in rows] == ["m2", "m3", "m4"]

    def test_noop_below_cap(self, fake_home):
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B")
        assert prune_transactions(10) == 0
        assert len(_rows()) == 1

    def test_uses_setting_by_default(self, fake_home, monkeypatch):
        monkeypatch.setenv("A8S_TXLOG_MAX_ROWS", "2")
        for i in range(4):
            log("ROUTED", msg_id=f"01ABC{i}", sender="A", recipient="B", detail=f"m{i}")
        assert prune_transactions() == 2
        assert [row[7] for row in _rows()] == ["m2", "m3"]

    def test_rejects_non_positive_cap(self, fake_home):
        with pytest.raises(ValueError):
            prune_transactions(0)

    def test_wraps_sqlite_failure(self, fake_home, monkeypatch):
        log("ROUTED", msg_id="01ABC", sender="A", recipient="B")

        def boom(*a, **kw):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(sqlite3, "connect", boom)
        with pytest.raises(TransactionLogError):
            prune_transactions(1)


class TestLogIntegrationWithRoute:
    """Verify that route_outboxes produces a ROUTED row in the txlog."""

    def test_route_produces_routed_row(self, fake_home, tmp_path):
        from core import Participant
        from mailbox import _write_outbox, ensure_mailboxes, route_outboxes
        from registry import save_registry

        a_root = tmp_path / "a"; a_root.mkdir()
        b_root = tmp_path / "b"; b_root.mkdir()
        save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
        a = Participant("A", a_root)
        b = Participant("B", b_root)
        ensure_mailboxes(a)
        ensure_mailboxes(b)

        _write_outbox("A", a.root, "B", "hello txlog", [])
        route_outboxes([a, b], all_agents=[a, b])

        routed = [row for row in _rows() if row[1] == "ROUTED"]
        assert len(routed) >= 1
        assert routed[0][3] == "A"
        assert routed[0][4] == "B"


class TestReadRecent:
    def _seed(self):
        log("ROUTED", msg_id="01A", sender="Alice", recipient="Bob", detail="one")
        log("PUBLISHED", msg_id="01A", sender="Alice", recipient="Bob", remote="r1")
        log("DROPPED", msg_id="01B", sender="Carol", recipient="Bob", detail="two")
        log("DISCARDED", msg_id="01C", sender="Alice", recipient="Dave", detail="three")

    def test_returns_chronological_with_seq(self, fake_home):
        self._seed()
        rows = read_recent()
        assert [e["event"] for _s, e in rows] == [
            "ROUTED",
            "PUBLISHED",
            "DROPPED",
            "DISCARDED",
        ]
        assert [s for s, _e in rows] == sorted(s for s, _e in rows)

    def test_limit_keeps_the_newest(self, fake_home):
        self._seed()
        rows = read_recent(limit=2)
        assert [e["event"] for _s, e in rows] == ["DROPPED", "DISCARDED"]

    def test_filters_by_event_case_insensitively(self, fake_home):
        self._seed()
        rows = read_recent(events=["dropped", "DISCARDED"])
        assert [e["msg_id"] for _s, e in rows] == ["01B", "01C"]

    def test_filters_by_sender_and_recipient(self, fake_home):
        self._seed()
        assert len(read_recent(senders=["alice"])) == 3
        assert len(read_recent(recipients=["dave"])) == 1
        assert len(read_recent(senders=["alice"], recipients=["bob"])) == 2

    def test_filters_by_msg_id(self, fake_home):
        self._seed()
        assert [e["event"] for _s, e in read_recent(msg_id="01A")] == [
            "ROUTED",
            "PUBLISHED",
        ]

    def test_after_seq_returns_everything_newer_ignoring_limit(self, fake_home):
        self._seed()
        rows = read_recent(limit=1)
        cursor = rows[-1][0]
        log("ROUTED", msg_id="01D", sender="Alice", recipient="Bob")
        log("ROUTED", msg_id="01E", sender="Alice", recipient="Bob")
        fresh = read_recent(limit=1, after_seq=cursor)
        assert [e["msg_id"] for _s, e in fresh] == ["01D", "01E"]

    def test_after_seq_respects_filters(self, fake_home):
        self._seed()
        cursor = read_recent(limit=1)[-1][0]
        log("ROUTED", msg_id="01D", sender="Zed", recipient="Bob")
        log("DROPPED", msg_id="01E", sender="Alice", recipient="Bob")
        fresh = read_recent(after_seq=cursor, senders=["alice"])
        assert [e["msg_id"] for _s, e in fresh] == ["01E"]

    def test_missing_store_is_empty(self, fake_home):
        assert read_recent() == []


class TestCmdTransactions:
    def test_prints_recent_rows(self, fake_home, capsys):
        log("ROUTED", msg_id="01A", sender="Alice", recipient="Bob", detail="hello")
        assert cmd_transactions([]) == 0
        out = capsys.readouterr().out
        assert "ROUTED" in out
        assert "01A" in out
        assert "from=Alice" in out
        assert "to=Bob" in out
        assert "detail=hello" in out

    def test_event_filter(self, fake_home, capsys):
        log("ROUTED", msg_id="01A", sender="Alice", recipient="Bob")
        log("DISCARDED", msg_id="01B", sender="Alice", recipient="Bob")
        assert cmd_transactions(["--event", "discarded"]) == 0
        out = capsys.readouterr().out
        assert "01B" in out
        assert "01A" not in out

    def test_rejects_unknown_event(self, fake_home, capsys):
        assert cmd_transactions(["--event", "NOPE"]) == 2
        assert "unknown event" in capsys.readouterr().err

    def test_rejects_non_positive_limit(self, fake_home, capsys):
        assert cmd_transactions(["--limit", "0"]) == 2
        assert "--limit must be a positive integer" in capsys.readouterr().err

    def test_empty_store_reports_and_exits_one(self, fake_home, capsys):
        assert cmd_transactions([]) == 1
        assert "no transaction events" in capsys.readouterr().err

    def test_empty_filter_says_no_match(self, fake_home, capsys):
        log("ROUTED", msg_id="01A", sender="Alice", recipient="Bob")
        assert cmd_transactions(["--from", "nobody"]) == 1
        assert "no matching transaction events" in capsys.readouterr().err

    def test_msg_filter_uppercases(self, fake_home, capsys):
        log("ROUTED", msg_id="01ABC", sender="Alice", recipient="Bob")
        assert cmd_transactions(["--msg", "01abc"]) == 0
        assert "01ABC" in capsys.readouterr().out

    def test_follow_starts_after_the_backlog(self, fake_home, capsys, monkeypatch):
        log("ROUTED", msg_id="01OLD", sender="Alice", recipient="Bob")
        calls = {"n": 0}

        def fake_sleep(_interval):
            calls["n"] += 1
            if calls["n"] == 1:
                log("ROUTED", msg_id="01NEW", sender="Alice", recipient="Bob")
                return
            raise KeyboardInterrupt

        monkeypatch.setattr("time.sleep", fake_sleep)
        assert cmd_transactions(["-f"]) == 0
        out = capsys.readouterr().out
        assert out.count("01OLD") == 1
        assert out.count("01NEW") == 1

    def test_follow_on_empty_filter_does_not_replay_history(
        self, fake_home, capsys, monkeypatch
    ):
        log("ROUTED", msg_id="01OLD", sender="Alice", recipient="Bob")

        def fake_sleep(_interval):
            raise KeyboardInterrupt

        monkeypatch.setattr("time.sleep", fake_sleep)
        assert cmd_transactions(["-f", "--event", "DISCARDED"]) == 0
        assert "01OLD" not in capsys.readouterr().out
