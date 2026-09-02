"""Tests for the alive-but-deaf incident: a wake's grandchild inheriting the
stdout pipe's write end must never wedge the runner.

`daemon.py` reads a wake's stdout from a dedicated reader thread and only
ever drains its queue with a bounded deadline (`wake_drain_grace_seconds`).
These tests exercise that mechanism directly (no bash fixtures — plain
`sys.executable -c "..."` scripts, no shell, no signals, so they run the same
way on Windows) plus the alive-but-deaf watchdog that recovers a wedged loop.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core
from core import Participant, agent_log_path, inbox_dir
from daemon import attached_loop
from mailbox import ensure_mailboxes
from registry import save_registry
from ar3.ulid import new as new_ulid

import daemon as daemon_mod


def _read_log(name: str) -> str:
    p = agent_log_path(name)
    return p.read_text() if p.is_file() else ""


def _queue_inbox(name: str, content: str) -> str:
    msg_id = new_ulid()
    (inbox_dir(name) / f"{msg_id}.json").write_text(json.dumps({
        "id": msg_id,
        "date": "2026-04-29T12:00:00Z",
        "from": "Y",
        "to": name,
        "content": content,
        "files": [],
    }))
    return msg_id


class TestWedgeRegression:
    """The incident itself: the wake's own child exits immediately, but a
    grandchild it spawned inherits the stdout pipe's write end and holds it
    open. Without the bounded drain, `for line in proc.stdout:` never sees
    EOF and the runner blocks forever."""

    def _grandchild_holds_stdout_def(
        self, tmp_path: Path, *, sleep_seconds: float, pidfile: Path | None = None
    ) -> Path:
        grandchild = "import time\n"
        if pidfile is not None:
            grandchild = (
                "import os, pathlib, sys\n"
                f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                "import time\n"
            )
        grandchild += f"time.sleep({sleep_seconds})\n"
        script = (
            "import subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        )
        defp = tmp_path / "wedge.json"
        defp.write_text(json.dumps({"invoke": [sys.executable, "-c", script]}))
        return defp

    def test_wake_settles_well_under_the_grandchilds_lifetime(
        self, fake_home, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("A8S_WAKE_DRAIN_GRACE_SECONDS", "0.5")
        d = tmp_path / "a"
        d.mkdir()
        pidfile = tmp_path / "grandchild.pid"
        # Long enough that a natural exit inside the test's own deadlines
        # can't be mistaken for the group kill this test is verifying.
        defp = self._grandchild_holds_stdout_def(tmp_path, sleep_seconds=30, pidfile=pidfile)
        save_registry({"A": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("A", d))
        _queue_inbox("A", "wedge test")

        # `run_with_prefix` calls `_drain_wake_stdout_after_exit` by its bare
        # module-global name, so patching the module attribute here is enough
        # to capture the reader thread it drains right before the drain
        # timeout's terminate_group + close land.
        captured: dict[str, threading.Thread | None] = {}
        orig_drain = daemon_mod._drain_wake_stdout_after_exit

        def spying_drain(name: str) -> None:
            captured["thread"] = daemon_mod._WAKE_STDOUT_THREAD
            orig_drain(name)

        monkeypatch.setattr(daemon_mod, "_drain_wake_stdout_after_exit", spying_drain)

        started = time.monotonic()
        rc = attached_loop(["A"], 0.05, single_pass=True)
        elapsed = time.monotonic() - started

        assert rc == 0
        assert elapsed < 4.0, f"runner blocked for {elapsed:.1f}s — the wedge fix regressed"
        assert not list(inbox_dir("A").glob("*.json")), "exit 0 from the child must still ack"
        log = _read_log("A")
        assert "stdout stayed open" in log
        assert "suspected inherited handle" in log

        thread = captured.get("thread")
        assert thread is not None
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "reader thread never joined after the drain-timeout group kill"

        if sys.platform != "win32":
            deadline = time.monotonic() + 3.0
            while not pidfile.is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert pidfile.is_file(), "grandchild never started"
            gc_pid = int(pidfile.read_text())

            deadline = time.monotonic() + 2.0
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(gc_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            assert not alive, "grandchild survived the drain-timeout group kill"


class TestReaderThreadPump:
    """The reader thread + queue draining, exercised directly (no bash
    fixtures) so it also documents the exit-code and ordering contract."""

    def test_lines_land_in_order_and_exit_code_propagates(self, fake_home, tmp_path):
        script = (
            "import sys, time\n"
            "print('first', flush=True)\n"
            "time.sleep(0.05)\n"
            "print('second', flush=True)\n"
            "sys.exit(7)\n"
        )
        d = tmp_path / "a"
        d.mkdir()
        completed: dict[str, int | None] = {}
        started = daemon_mod._start_wake_subprocess(
            "A",
            [sys.executable, "-c", script],
            d,
            on_complete=lambda rc: completed.setdefault("rc", rc),
        )
        assert started
        deadline = time.monotonic() + 5.0
        while "rc" not in completed and time.monotonic() < deadline:
            daemon_mod._service_in_flight_wake()
            time.sleep(0.02)

        assert completed.get("rc") == 7
        log = _read_log("A")
        assert log.index("first") < log.index("second")

    def test_wake_start_and_wake_return_rows_carry_the_envelope_stem(
        self, fake_home, tmp_path, fixtures_dir
    ):
        d = tmp_path / "a"
        d.mkdir()
        defn = {"invoke": [sys.executable, str(fixtures_dir / "mock_cli.py"), "one", "two"]}
        defp = tmp_path / "def.json"
        defp.write_text(json.dumps(defn))
        save_registry({"A": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("A", d))
        msg_id = _queue_inbox("A", "hi")

        rc = attached_loop(["A"], 0.05, single_pass=True)
        assert rc == 0

        import txlog

        events = txlog.read_events(msg_id)
        kinds = [e["event"] for e in events]
        assert "WAKE_START" in kinds
        assert "WAKE_RETURN" in kinds
        wake_start = next(e for e in events if e["event"] == "WAKE_START")
        assert wake_start["to"] == "A"
        assert "wake pid=" in wake_start["detail"]
        wake_return = next(e for e in events if e["event"] == "WAKE_RETURN")
        assert wake_return["to"] == "A"
        assert wake_return["detail"] == "exit 0"
        assert not list(inbox_dir("A").glob("*.json"))


class TestStuckReaderSweep:
    """Finding 2's backstop: when a drain-timeout's own `terminate_group`
    doesn't free a reader thread (simulated here by patching it to a no-op —
    the real gap is Windows, which has no group to target, or a POSIX
    straggler that survives the SIGKILL-window race), the thread is retained
    and swept before the next wake spawn rather than accumulating silently."""

    @pytest.fixture(autouse=True)
    def _reset_stuck_readers(self):
        daemon_mod._STUCK_WAKE_READERS = []
        yield
        daemon_mod._STUCK_WAKE_READERS = []

    def _wedge_cmd(self, pidfile: Path) -> list[str]:
        grandchild = (
            "import os, pathlib, sys, time\n"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(9999)\n"
        )
        script = (
            "import subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        )
        return [sys.executable, "-c", script]

    def test_sweep_reports_then_drops_a_stuck_reader(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.setenv("A8S_WAKE_DRAIN_GRACE_SECONDS", "0.2")
        monkeypatch.setattr(daemon_mod, "terminate_group", lambda *a, **k: None)

        d = tmp_path / "a"
        d.mkdir()
        pidfile = tmp_path / "grandchild.pid"

        completed: dict[str, int | None] = {}
        started = daemon_mod._start_wake_subprocess(
            "A", self._wedge_cmd(pidfile), d,
            on_complete=lambda rc: completed.setdefault("rc", rc),
        )
        assert started
        deadline = time.monotonic() + 5.0
        while "rc" not in completed and time.monotonic() < deadline:
            daemon_mod._service_in_flight_wake()
            time.sleep(0.02)
        assert completed.get("rc") == 0
        assert len(daemon_mod._STUCK_WAKE_READERS) == 1, "drain timeout never retained the stuck reader"

        outputs: list[str] = []
        monkeypatch.setattr(core, "out", lambda text="", end="\n": outputs.append(text))

        started2 = daemon_mod._start_wake_subprocess("A", [sys.executable, "-c", "pass"], d)
        assert started2
        deadline = time.monotonic() + 5.0
        while daemon_mod._wake_in_flight() and time.monotonic() < deadline:
            daemon_mod._service_in_flight_wake()
            time.sleep(0.02)
        assert not daemon_mod._wake_in_flight()
        assert any("stuck wake stdout reader" in o for o in outputs), (
            "sweep never reported the stuck reader before the next spawn"
        )
        assert len(daemon_mod._STUCK_WAKE_READERS) == 1, "sweep dropped a still-alive entry"

        # SIGTERM (not the platform-quirky signal 0 used elsewhere as a pure
        # liveness probe) actually terminates on both POSIX and Windows —
        # `os.kill`'s Windows support is documented for SIGTERM specifically
        # (it calls `TerminateProcess`), so no platform guard is needed here.
        stuck_thread = daemon_mod._STUCK_WAKE_READERS[0][0]
        deadline = time.monotonic() + 3.0
        while not pidfile.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.is_file(), "grandchild never started"
        gc_pid = int(pidfile.read_text())
        os.kill(gc_pid, signal.SIGTERM)
        stuck_thread.join(timeout=3.0)
        assert not stuck_thread.is_alive(), "reader thread never unblocked once its grandchild died"

        started3 = daemon_mod._start_wake_subprocess("A", [sys.executable, "-c", "pass"], d)
        assert started3
        deadline = time.monotonic() + 5.0
        while daemon_mod._wake_in_flight() and time.monotonic() < deadline:
            daemon_mod._service_in_flight_wake()
            time.sleep(0.02)
        assert daemon_mod._STUCK_WAKE_READERS == [], "sweep never dropped the now-dead reader"


class TestWatchdog:
    """Unit-level exercise of the alive-but-deaf watchdog: a stale loop beat
    plus an old addressed inbox message is the wedge predicate; recovery is
    scoped to the current wake's own process group only."""

    @pytest.fixture(autouse=True)
    def _reset_daemon_globals(self, fake_home):
        yield
        daemon_mod._STOP_EVENT = None
        daemon_mod._CURRENT_WAKE_PROC = None
        daemon_mod._CURRENT_WAKE_NAME = None
        daemon_mod._WAKE_STARTED_MONO = None
        daemon_mod._WAKE_MAX_SECONDS = None
        daemon_mod._LOOP_BEAT_MONO = None
        daemon_mod._LOOP_BEAT_WALL = None

    def _queue_stale_mail(self, tmp_path: Path) -> None:
        d = tmp_path / "a"
        d.mkdir()
        ensure_mailboxes(Participant("A", d))
        old_msg = inbox_dir("A") / "old.json"
        old_msg.write_text(json.dumps({
            "id": "old", "date": "2026-04-29T12:00:00Z",
            "from": "Y", "to": "A", "content": "stuck", "files": [],
        }))
        old_ts = time.time() - 5
        os.utime(old_msg, (old_ts, old_ts))

    def _spawn_alive_wake(self) -> subprocess.Popen:
        # `start_new_session=True` matches how `_start_wake_subprocess` spawns
        # every real wake — `terminate_group` resolves the pgid from the pid,
        # and without a session of its own this pid's pgid IS the test
        # runner's, which would make a group-kill take out pytest itself.
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )

    def test_active_turn_is_not_touched(self, fake_home, tmp_path, monkeypatch):
        """A wake still inside its own budget must never be closed or killed
        just because the loop beat went stale — Finding 1's P1: a slow
        `route_outboxes` upload can hold an iteration past the wedge window
        with a perfectly healthy turn running underneath it."""
        monkeypatch.setenv("A8S_WATCHDOG_WEDGE_SECONDS", "0.2")
        self._queue_stale_mail(tmp_path)

        kills: list[object] = []
        monkeypatch.setattr(daemon_mod, "terminate_group", lambda *a, **k: kills.append(a))

        running = self._spawn_alive_wake()
        daemon_mod._CURRENT_WAKE_PROC = running
        daemon_mod._CURRENT_WAKE_NAME = "A"
        daemon_mod._WAKE_STARTED_MONO = time.monotonic() - 3
        daemon_mod._WAKE_MAX_SECONDS = 120.0
        daemon_mod._STOP_EVENT = threading.Event()
        daemon_mod._LOOP_BEAT_MONO = time.monotonic() - 5
        daemon_mod._LOOP_BEAT_WALL = datetime.now(timezone.utc) - timedelta(seconds=5)

        watchdog = threading.Thread(
            target=daemon_mod._watchdog_loop, args=(["A"], "A"), daemon=True
        )
        watchdog.start()
        try:
            import txlog

            deadline = time.monotonic() + 3.0
            rows: list[tuple[int, dict]] = []
            while time.monotonic() < deadline:
                rows = txlog.read_recent(events=["WEDGE"], limit=10)
                if rows:
                    break
                time.sleep(0.05)
            assert rows, "no WEDGE row was written for the active-turn case"
            assert "not intervening" in rows[-1][1]["detail"]

            assert not kills, "watchdog killed a wake still inside its budget"
            assert running.poll() is None, "watchdog killed a healthy in-flight turn"
            assert not running.stdout.closed, "watchdog closed stdout of a healthy in-flight turn"
        finally:
            daemon_mod._STOP_EVENT.set()
            watchdog.join(timeout=2.0)
            if running.poll() is None:
                running.kill()
            running.wait(timeout=5)

    def test_overrun_wake_gets_group_killed(self, fake_home, tmp_path, monkeypatch):
        """A wake that has overrun `max_wake_seconds` while the loop itself is
        too stale to enforce it must still get killed — the watchdog stands in
        for `_check_wake_timeout` in that case."""
        monkeypatch.setenv("A8S_WATCHDOG_WEDGE_SECONDS", "0.2")
        self._queue_stale_mail(tmp_path)

        kills: list[object] = []
        monkeypatch.setattr(daemon_mod, "terminate_group", lambda *a, **k: kills.append(a))

        running = self._spawn_alive_wake()
        daemon_mod._CURRENT_WAKE_PROC = running
        daemon_mod._CURRENT_WAKE_NAME = "A"
        daemon_mod._WAKE_STARTED_MONO = time.monotonic() - 10
        daemon_mod._WAKE_MAX_SECONDS = 1.0
        daemon_mod._STOP_EVENT = threading.Event()
        daemon_mod._LOOP_BEAT_MONO = time.monotonic() - 5
        daemon_mod._LOOP_BEAT_WALL = datetime.now(timezone.utc) - timedelta(seconds=5)

        watchdog = threading.Thread(
            target=daemon_mod._watchdog_loop, args=(["A"], "A"), daemon=True
        )
        watchdog.start()
        try:
            deadline = time.monotonic() + 3.0
            while not kills and time.monotonic() < deadline:
                time.sleep(0.02)
            assert kills, "watchdog never killed a wake that overran its budget"

            # Simulate the loop resuming so the recovery attempt's grace
            # window resolves as "recovered" instead of waiting out its 30s.
            daemon_mod._LOOP_BEAT_MONO = time.monotonic()
            daemon_mod._LOOP_BEAT_WALL = datetime.now(timezone.utc)

            import txlog

            deadline = time.monotonic() + 3.0
            rows: list[tuple[int, dict]] = []
            while time.monotonic() < deadline:
                rows = txlog.read_recent(events=["WEDGE"], limit=10)
                if rows:
                    break
                time.sleep(0.05)
            assert rows, "no WEDGE row was written"
            assert "recovered" in rows[-1][1]["detail"]

            # A fresh beat means the loop isn't stale any more — no further
            # detection fires even though the (now-stale) inbox file is
            # still sitting there.
            before = len(txlog.read_recent(events=["WEDGE"], limit=50))
            time.sleep(0.5)
            after = len(txlog.read_recent(events=["WEDGE"], limit=50))
            assert after == before
        finally:
            daemon_mod._STOP_EVENT.set()
            watchdog.join(timeout=2.0)
            if running.poll() is None:
                running.kill()
            running.wait(timeout=5)

    def test_disabled_when_knob_is_zero(self, fake_home, monkeypatch):
        monkeypatch.setenv("A8S_WATCHDOG_WEDGE_SECONDS", "0")
        daemon_mod._STOP_EVENT = threading.Event()
        daemon_mod._LOOP_BEAT_MONO = time.monotonic() - 1000
        watchdog = threading.Thread(
            target=daemon_mod._watchdog_loop, args=([], "A"), daemon=True
        )
        watchdog.start()
        watchdog.join(timeout=2.0)
        assert not watchdog.is_alive(), "a zero knob must return immediately, not poll forever"
