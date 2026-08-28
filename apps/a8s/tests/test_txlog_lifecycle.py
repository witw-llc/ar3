"""Runner-lifecycle rows in the transaction log — RUN_START/RUN_STOP bracket
one `attached_loop` invocation, WAKE_START/WAKE_RETURN bracket one wake per
envelope (batches get one row per envelope, not one for the batch), and
HEARTBEAT is a throttled liveness pulse for a resident loop. Together these
are what makes a dead or deaf dispatcher visible from `a8s tx` — the
alive-but-deaf incident had none of them.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import txlog

from core import Participant, inbox_dir
from daemon import attached_loop
from mailbox import ensure_mailboxes
from registry import save_registry


def _main_thread_only(patched):
    """Scope an Event.wait monkeypatch to the loop under test.

    These tests replace `threading.Event.wait` process-wide to script the
    attached loop's iteration clock, but the runner's watchdog thread waits
    on the same event class — an unscoped patch runs the script from that
    thread too, racing the loop it is scripting (double refills, early
    stops). Any thread but the main one falls through to the real wait."""
    real_wait = threading.Event.wait

    def gated(self, timeout=None):
        if threading.current_thread() is not threading.main_thread():
            return real_wait(self, timeout)
        return patched(self, timeout)

    return gated


def _register(tmp_path: Path, fixtures_dir: Path, name: str = "A") -> Path:
    d = tmp_path / name.lower()
    d.mkdir()
    save_registry({name: {"root": str(d), "definition": str(fixtures_dir / "mock.json")}})
    ensure_mailboxes(Participant(name, d))
    return d


class TestRunStartStop:
    def test_step_brackets_one_run_start_and_run_stop(self, fake_home, tmp_path, fixtures_dir):
        _register(tmp_path, fixtures_dir)

        rc = attached_loop(["A"], 0.05, single_pass=True)
        assert rc == 0

        starts = txlog.read_recent(events=["RUN_START"], limit=10)
        stops = txlog.read_recent(events=["RUN_STOP"], limit=10)
        assert len(starts) == 1
        assert len(stops) == 1
        detail = starts[0][1]["detail"]
        assert "agents=A" in detail
        assert f"pid={os.getpid()}" in detail
        assert "mode=step" in detail
        assert starts[0][1]["from"] == "A"
        assert stops[0][1]["detail"] == "single pass complete"

    def test_drain_mode_stops_with_deadline_reason(self, fake_home, tmp_path, fixtures_dir):
        _register(tmp_path, fixtures_dir)

        rc = attached_loop(["A"], 0.01, single_pass=False, drain_seconds=0.05)
        assert rc == 0

        starts = txlog.read_recent(events=["RUN_START"], limit=10)
        stops = txlog.read_recent(events=["RUN_STOP"], limit=10)
        assert "mode=drain" in starts[-1][1]["detail"]
        assert stops[-1][1]["detail"] == "deadline"

    def test_run_mode_recorded_for_a_resident_loop(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod

        monkeypatch.setenv("A8S_WATCHDOG_WEDGE_SECONDS", "0")
        _register(tmp_path, fixtures_dir)

        def stop_immediately(self, timeout=None):
            daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(stop_immediately))
        rc = attached_loop(["A"], 0.01, single_pass=False)
        assert rc == 0

        starts = txlog.read_recent(events=["RUN_START"], limit=10)
        stops = txlog.read_recent(events=["RUN_STOP"], limit=10)
        assert "mode=run" in starts[-1][1]["detail"]
        assert stops[-1][1]["detail"] == "stop-signal"


class TestWakeStartReturn:
    def test_single_wake_rows_carry_the_envelope_stem(self, fake_home, tmp_path, fixtures_dir):
        _register(tmp_path, fixtures_dir)
        msg_id = "01WAKESTARTONE"
        (inbox_dir("A") / f"{msg_id}.json").write_text(json.dumps({
            "id": msg_id, "date": "2026-04-29T12:00:00Z",
            "from": "Y", "to": "A", "content": "hi", "files": [],
        }))

        rc = attached_loop(["A"], 0.05, single_pass=True)
        assert rc == 0

        events = txlog.read_events(msg_id)
        kinds = [e["event"] for e in events]
        assert kinds == ["WAKE_START", "WAKE_RETURN"]
        assert events[0]["to"] == "A"
        assert "wake pid=" in events[0]["detail"]
        assert "batch" not in events[0]["detail"]
        assert events[1]["detail"] == "exit 0"

    def test_batch_wake_emits_one_wake_start_row_per_envelope(
        self, fake_home, tmp_path, fixtures_dir
    ):
        d = tmp_path / "b"
        d.mkdir()
        save_registry({"B": {"root": str(d), "definition": str(fixtures_dir / "mock-batch.json")}})
        ensure_mailboxes(Participant("B", d))
        ids = [f"batchmsg{i}" for i in range(3)]
        for i, msg_id in enumerate(ids):
            (inbox_dir("B") / f"{msg_id}.json").write_text(json.dumps({
                "id": msg_id, "date": "2026-04-28T14:30:00.000000Z",
                "from": "A", "to": "B", "content": f"m-{i}", "files": [],
            }))

        rc = attached_loop(["B"], 0.1, single_pass=True)
        assert rc == 0

        for msg_id in ids:
            events = txlog.read_events(msg_id)
            starts = [e for e in events if e["event"] == "WAKE_START"]
            returns = [e for e in events if e["event"] == "WAKE_RETURN"]
            assert len(starts) == 1
            assert len(returns) == 1
            assert "batch=3" in starts[0]["detail"]
            assert returns[0]["detail"] == "exit 0"

    def test_nonzero_exit_still_gets_a_wake_return_row(
        self, fake_home, tmp_path, fixtures_dir
    ):
        d = tmp_path / "a"
        d.mkdir()
        defn = {"invoke": [sys.executable, str(fixtures_dir / "mock_flaky_cli.py"), "MSG:$MESSAGE"]}
        defp = tmp_path / "def.json"
        defp.write_text(json.dumps(defn))
        save_registry({"A": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("A", d))
        msg_id = "01WAKEFAIL"
        (inbox_dir("A") / f"{msg_id}.json").write_text(json.dumps({
            "id": msg_id, "date": "2026-04-29T12:00:00Z",
            "from": "Y", "to": "A", "content": "will fail", "files": [],
        }))

        rc = attached_loop(["A"], 0.05, single_pass=True)
        assert rc == 0

        events = txlog.read_events(msg_id)
        wake_return = next(e for e in events if e["event"] == "WAKE_RETURN")
        assert wake_return["detail"] == "exit 3"


class TestHeartbeat:
    def test_heartbeat_row_emitted_when_due(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod

        monkeypatch.setenv("A8S_TXLOG_HEARTBEAT_SECONDS", "0.05")
        monkeypatch.setenv("A8S_WATCHDOG_WEDGE_SECONDS", "0")
        _register(tmp_path, fixtures_dir)

        calls = 0

        def slow_wait(self, timeout=None):
            nonlocal calls
            calls += 1
            time.sleep(0.03)
            if calls >= 6:
                daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(slow_wait))
        rc = attached_loop(["A"], 0.01, single_pass=False)
        assert rc == 0

        rows = txlog.read_recent(events=["HEARTBEAT"], limit=50)
        assert rows, "no HEARTBEAT row was written"
        assert rows[0][1]["detail"] == "idle"
        assert rows[0][1]["from"] == "A"

    def test_disabled_by_default_zero_knob(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod

        monkeypatch.setenv("A8S_TXLOG_HEARTBEAT_SECONDS", "0")
        monkeypatch.setenv("A8S_WATCHDOG_WEDGE_SECONDS", "0")
        _register(tmp_path, fixtures_dir)

        calls = 0

        def slow_wait(self, timeout=None):
            nonlocal calls
            calls += 1
            time.sleep(0.02)
            if calls >= 4:
                daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(slow_wait))
        rc = attached_loop(["A"], 0.01, single_pass=False)
        assert rc == 0

        assert txlog.read_recent(events=["HEARTBEAT"], limit=10) == []
