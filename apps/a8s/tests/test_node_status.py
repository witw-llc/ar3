"""`a8s ps` STATUS and activity columns, and what `a8s stop` does to a node's
wake. The real-process tests for `stop --force` live in test_stop_force.py."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import txlog
from commands import (
    _hard_kill,
    _hard_kill_targets,
    cmd_ps,
    cmd_restart,
    cmd_stop,
)
from core import (
    dead_letters_dir,
    inbox_dir,
    mark_dead_letter,
    pid_path,
    trash_dir,
    transactions_path,
    wake_pid_path,
    write_last_wake,
    write_wake_pid,
)
from registry import save_registry


@pytest.fixture
def node(fake_home, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    save_registry({"alpha": {"root": str(root)}, "beta": {"root": str(root)}})
    for name in ("alpha", "beta"):
        pid_path(name).parent.mkdir(parents=True, exist_ok=True)
        pid_path(name).write_text(str(os.getpid()))
    return "alpha"


def _row(out: str, name: str) -> list[str]:
    return next(line for line in out.splitlines() if line.startswith(name)).split()


def _seed(event: str, recipient: str, msg_id: str, *, age_hours: float = 0.0) -> None:
    txlog.log(event, msg_id=msg_id, sender="x", recipient=recipient)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    with sqlite3.connect(transactions_path()) as conn:
        conn.execute(
            "UPDATE transactions SET timestamp = ? WHERE seq = (SELECT MAX(seq) FROM transactions)",
            (stamp,),
        )


class TestPsStatus:
    def test_ok_when_nothing_failed(self, node, capsys):
        assert cmd_ps([]) == 0
        out = capsys.readouterr().out
        assert "STATUS" in out
        assert _row(out, "alpha")[3] == "idle"

    def test_dead_letter_markers_do_not_change_status(self, node, capsys):
        mark_dead_letter(node, "a.json")
        mark_dead_letter(node, "b.json")
        cmd_ps([])
        out = capsys.readouterr().out
        assert "dead letter" not in out
        assert _row(out, "alpha")[3] == "idle"

    def test_failed_column_counts_wake_failures_of_the_last_day(self, node, capsys):
        _seed("WAKE_FAILED", "alpha", "", age_hours=1)
        _seed("WAKE_FAILED", "ALPHA", "", age_hours=5)
        _seed("WAKE_FAILED", "alpha", "", age_hours=25)
        cmd_ps([])
        out = capsys.readouterr().out
        assert "FAILED" in out
        row = _row(out, "alpha")
        assert row[3:5] == ["idle", "2"]
        assert _row(out, "beta")[4] == "-"

    def test_failed_column_is_a_dash_when_none(self, node, capsys):
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[4] == "-"

    def test_a_failed_last_wake_is_fail_until_the_next_one_succeeds(self, node, capsys):
        write_last_wake(node, False)
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[3] == "fail"
        write_last_wake(node, True)
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[3] == "idle"

    def test_a_wake_in_flight_is_busy_whatever_the_last_outcome(self, node, capsys):
        write_last_wake(node, False)
        write_wake_pid(node, os.getpid(), ["a.json"])
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[3] == "busy"

    def test_a_wake_in_flight_is_busy(self, node, capsys):
        write_wake_pid(node, os.getpid(), ["a.json"])
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[3] == "busy"

    def test_a_dead_wake_record_is_not_busy(self, node, capsys):
        wake_pid_path(node).write_text(json.dumps({"pid": 2**22 + 12345, "start": "", "unit": []}))
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[3] == "idle"

    def test_quiet_is_still_names_only(self, node, capsys):
        mark_dead_letter(node, "a.json")
        cmd_ps(["-q"])
        assert capsys.readouterr().out.splitlines() == ["alpha", "beta"]


class TestPsActivity:
    def test_counts_distinct_messages_landed_in_the_last_day(self, node, capsys):
        _seed("ROUTED", "alpha", "01A", age_hours=1)
        _seed("RECEIVED_REMOTE", "alpha", "01B", age_hours=2)
        _seed("RECEIVED_REMOTE", "alpha", "01B", age_hours=2)
        _seed("ROUTED", "ALPHA", "01C", age_hours=0.5)
        _seed("ROUTED", "alpha", "01D", age_hours=30)
        _seed("ROUTED", "beta", "01E", age_hours=1)
        cmd_ps([])
        out = capsys.readouterr().out
        assert _row(out, "alpha")[6] == "3"
        assert _row(out, "beta")[6] == "1"

    def test_only_a_landing_counts(self, node, capsys):
        for event in ("ENQUEUED", "WAKE_START", "WAKE_RETURN", "PROXY_DELIVERED", "NOT_LOCAL"):
            _seed(event, "alpha", "01A")
        cmd_ps([])
        out = capsys.readouterr().out
        assert _row(out, "alpha")[6] == "0"

    def test_the_last_message_is_local_time_with_its_zone(self, node, capsys, zone):
        zone("America/Los_Angeles")
        _seed("ROUTED", "alpha", "01A", age_hours=24 * 200)
        txlog_stamp = "2026-07-01T12:00:00.000Z"
        _seed("ROUTED", "alpha", "01B", age_hours=1)
        with sqlite3.connect(transactions_path()) as conn:
            conn.execute("UPDATE transactions SET timestamp = ? WHERE msg_id = '01B'", (txlog_stamp,))
        txlog.log("ROUTED", msg_id="01C", sender="alpha", recipient="beta")
        with sqlite3.connect(transactions_path()) as conn:
            conn.execute("UPDATE transactions SET timestamp = ? WHERE msg_id = '01C'", (txlog_stamp,))
        cmd_ps([])
        out = capsys.readouterr().out
        assert _row(out, "alpha")[7:9] == ["2026-07-01", "05:00"] or "2026-07-01 05:00 PDT" in out
        # An old newest message still shows when the node last spoke.
        assert _row(out, "alpha")[6] == "0"

    def test_no_log_reads_as_nothing(self, node, capsys):
        assert not transactions_path().exists()
        assert cmd_ps([]) == 0
        row = _row(capsys.readouterr().out, "alpha")
        assert row[6] == "0" and row[7] == "-"

    def test_last_sent_is_the_nodes_own_newest_outbox_event(self, node, capsys, zone):
        zone("UTC")
        # Mail landing for alpha never moves LAST SENT; what alpha wrote does,
        # whether it was delivered, published, or dropped.
        _seed("ROUTED", "alpha", "01A", age_hours=1)
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[7] == "-"
        for event, msg_id in (("ENQUEUED", "01B"), ("DROPPED", "01C"), ("PUBLISHED", "01D")):
            txlog.log(event, msg_id=msg_id, sender="Alpha", recipient="beta")
        stamp = "2026-07-02T09:30:00.000Z"
        with sqlite3.connect(transactions_path()) as conn:
            conn.execute("UPDATE transactions SET timestamp = ? WHERE msg_id = '01D'", (stamp,))
            conn.execute("UPDATE transactions SET timestamp = '2026-07-01T00:00:00.000Z' WHERE msg_id IN ('01B', '01C')")
        cmd_ps([])
        out = capsys.readouterr().out
        assert "2026-07-02 09:30 UTC" in out
        assert _row(out, "beta")[7] == "-"

    def test_definition_comes_from_the_registry(self, node, capsys):
        reg = {"alpha": {"root": str(inbox_dir("alpha").parent), "definition": "claude"},
               "beta": {"root": str(inbox_dir("beta").parent)}}
        save_registry(reg)
        cmd_ps([])
        out = capsys.readouterr().out
        assert _row(out, "alpha")[5] == "claude"
        assert _row(out, "beta")[5] == "default"


@pytest.fixture
def stoppable(fake_home, tmp_path, monkeypatch):
    """A node whose handler detaches on the first SIGTERM, with a dead letter
    in trash."""
    root = tmp_path / "root"
    root.mkdir()
    save_registry({"alpha": {"root": str(root)}})
    pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
    pid_path("alpha").write_text(str(os.getppid()))
    trash_dir("alpha").mkdir(parents=True, exist_ok=True)
    (trash_dir("alpha") / "dead.json").write_text("{}")
    mark_dead_letter("alpha", "dead.json")

    def fake_kill(pid, sig):
        if sig == 0:
            if not pid_path("alpha").is_file():
                raise ProcessLookupError()
            return
        pid_path("alpha").unlink(missing_ok=True)

    for mod in ("commands", "daemon", "core"):
        monkeypatch.setattr(f"{mod}.os.kill", fake_kill)
    gone = lambda pid: pid_path("alpha").is_file()
    for mod in ("commands", "daemon", "core"):
        monkeypatch.setattr(f"{mod}._pid_alive", gone)
    monkeypatch.setattr("commands.STOP_POLL_S", 0.01)
    return "alpha"


class TestStopLeavesDeadLettersDead:
    def test_stop_leaves_trash_and_dead_letters_alone(self, stoppable, capsys):
        assert cmd_stop([stoppable]) == 0
        assert (trash_dir(stoppable) / "dead.json").is_file()
        assert (dead_letters_dir(stoppable) / "dead.json").is_file()
        assert not (inbox_dir(stoppable) / "dead.json").exists()
        assert "dead letter" not in capsys.readouterr().out

    def test_restart_leaves_them_dead_too(self, stoppable, monkeypatch, capsys):
        class FakeProc:
            pid = 4242

        monkeypatch.setattr("commands.subprocess.Popen", lambda *a, **k: FakeProc())
        assert cmd_restart([stoppable]) == 0
        assert (trash_dir(stoppable) / "dead.json").is_file()
        assert not (inbox_dir(stoppable) / "dead.json").exists()

    def test_retry_is_not_a_verb(self, stoppable, capsys):
        from cli import main

        assert main(["retry", stoppable]) == 2
        assert "unknown command: 'retry'" in capsys.readouterr().err
        assert not (inbox_dir(stoppable) / "dead.json").exists()


class TestForceEscalation:
    def test_posix_kills_the_wake_group_then_the_handler(self):
        assert _hard_kill_targets(True, 10, 20) == [("group", 20), ("pid", 10)]

    def test_windows_ends_the_wake_process_then_the_handler(self):
        assert _hard_kill_targets(False, 10, 20) == [("pid", 20), ("pid", 10)]

    def test_no_wake_means_only_the_handler(self):
        assert _hard_kill_targets(True, 10, None) == [("pid", 10)]
        assert _hard_kill_targets(False, 10, None) == [("pid", 10)]

    def test_a_group_target_signals_the_group(self, monkeypatch):
        sent = []
        monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append(("killpg", pid, sig)), raising=False)
        monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(("kill", pid, sig)))
        _hard_kill([("group", 20), ("pid", 10)])
        sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        assert sent == [("killpg", 20, sig), ("kill", 10, sig)]

    def test_a_target_that_is_already_gone_is_not_an_error(self, monkeypatch):
        def gone(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(os, "kill", gone)
        _hard_kill([("pid", 10)])

    def test_a_handler_that_never_detaches_is_killed_and_its_mail_dead_lettered(
        self, fake_home, tmp_path, monkeypatch, capsys
    ):
        root = tmp_path / "root"
        root.mkdir()
        save_registry({"alpha": {"root": str(root)}})
        pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
        pid_path("alpha").write_text(str(os.getppid()))
        trash_dir("alpha").mkdir(parents=True, exist_ok=True)
        (trash_dir("alpha") / "01KILLED.json").write_text("{}")
        write_wake_pid("alpha", os.getpid(), ["01KILLED.json"])

        killed = []

        def fake_hard_kill(targets):
            killed.extend(targets)
            pid_path("alpha").unlink(missing_ok=True)

        deaf = lambda pid, sig: None
        for mod in ("commands", "daemon", "core"):
            monkeypatch.setattr(f"{mod}.os.kill", deaf)
        monkeypatch.setattr("commands._pid_alive", lambda pid: pid_path("alpha").is_file())
        monkeypatch.setattr("daemon._pid_alive", lambda pid: pid_path("alpha").is_file())
        monkeypatch.setattr("commands._hard_kill", fake_hard_kill)
        monkeypatch.setattr("commands.STOP_POLL_S", 0.01)
        monkeypatch.setattr("commands.STOP_FORCE_GRACE_S", 0.1)

        assert cmd_stop(["alpha", "--force"]) == 0
        assert ("pid", os.getppid()) in killed
        assert any(k in ("group", "pid") and pid == os.getpid() for k, pid in killed)
        assert (trash_dir("alpha") / "01KILLED.json").is_file()
        assert not (inbox_dir("alpha") / "01KILLED.json").exists()
        assert (dead_letters_dir("alpha") / "01KILLED.json").is_file()
        assert not wake_pid_path("alpha").exists()
        events = txlog.read_events("01KILLED")
        assert [e["event"] for e in events] == ["DROPPED"]
        assert "killed by `a8s stop --force`" in events[0]["detail"]
        out = capsys.readouterr()
        assert "killing it" in out.err
        assert "alpha: the killed wake's 1 message(s) are dead letters: 01KILLED" in out.out

    def test_a_plain_stop_never_kills(self, fake_home, tmp_path, monkeypatch, capsys):
        root = tmp_path / "root"
        root.mkdir()
        save_registry({"alpha": {"root": str(root)}})
        pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
        pid_path("alpha").write_text(str(os.getppid()))
        deaf = lambda pid, sig: None
        for mod in ("commands", "daemon", "core"):
            monkeypatch.setattr(f"{mod}.os.kill", deaf)
        monkeypatch.setattr("commands._pid_alive", lambda pid: True)
        monkeypatch.setattr("daemon._pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "commands._hard_kill", lambda targets: pytest.fail("plain stop must not kill")
        )
        monkeypatch.setattr("commands.STOP_POLL_S", 0.01)
        monkeypatch.setattr("commands.STOP_WAIT_S", 0.1)
        assert cmd_stop(["alpha"]) == 1


class TestStatusProbe:
    """`status.invoke`: a node's own answer to `busy` / `resident`."""

    def _probed(self, node, tmp_path, script: str) -> None:
        import sys
        defn = tmp_path / "probed.json"
        defn.write_text(json.dumps({
            "invoke": [sys.executable, "-c", "pass"],
            "status": {"invoke": [sys.executable, "-c", script, "$RECIPIENT"]},
        }))
        save_registry({
            "alpha": {"root": str(tmp_path / "root"), "definition": str(defn)},
            "beta": {"root": str(tmp_path / "root")},
        })

    def _status(self, capsys) -> str:
        cmd_ps([])
        return _row(capsys.readouterr().out, "alpha")[3]

    def test_build_status_command(self):
        from definitions import build_status_command
        assert build_status_command({"invoke": ["x"]}, "n1") is None
        assert build_status_command({"status": {}}, "n1") is None
        defn = {"status": {"invoke": ["r4t", "status", "--agent", "$RECIPIENT", "--x=$X?"]}}
        assert build_status_command(defn, "n1") == ["r4t", "status", "--agent", "n1"]
        assert build_status_command(defn, "n1", vars={"X": "1"})[-1] == "--x=1"

    @pytest.mark.parametrize("word", ["busy", "resident"])
    def test_probe_word_is_the_status(self, node, tmp_path, capsys, word):
        self._probed(node, tmp_path, f"print('{word} extra words')")
        assert self._status(capsys) == word

    @pytest.mark.parametrize("script", [
        "print('garbage')",
        "pass",
        "import sys; print('busy'); sys.exit(1)",
    ])
    def test_anything_else_falls_back(self, node, tmp_path, capsys, script):
        self._probed(node, tmp_path, script)
        assert self._status(capsys) == "idle"

    def test_fallback_keeps_fail(self, node, tmp_path, capsys):
        self._probed(node, tmp_path, "print('garbage')")
        write_last_wake(node, False)
        assert self._status(capsys) == "fail"

    def test_a_probe_past_the_timeout_falls_back(self, node, tmp_path, capsys, monkeypatch):
        import daemon
        monkeypatch.setattr(daemon, "STATUS_PROBE_SECONDS", 0.5)
        self._probed(node, tmp_path, "import time; time.sleep(30)")
        assert self._status(capsys) == "idle"

    def _probe_with_pipe_holder(self, node, tmp_path, leader: str) -> "pathlib.Path":
        """A probe whose child keeps the stdout pipe for 30 s; the leader
        exits at once or stays. Returns the file the child's pid lands in."""
        marker = tmp_path / "child.pid"
        tail = "import time; time.sleep(30)" if leader == "stays" else "pass"
        self._probed(node, tmp_path, (
            "import subprocess, sys; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            f"open({str(marker)!r}, 'w').write(str(p.pid)); "
            f"print('busy', flush=True); {tail}"
        ))
        return marker

    @pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
    @pytest.mark.parametrize("leader", ["exits", "stays"])
    def test_a_child_holding_stdout_does_not_stall_ps(self, node, tmp_path, capsys, monkeypatch, leader):
        """A child the probe spawned keeps the stdout pipe for 30 s, whether
        the leader exits at once or stays. Killing the leader alone would
        leave `ps` waiting on the pipe; the group ends and the child goes
        with it."""
        import daemon
        import time
        monkeypatch.setattr(daemon, "STATUS_PROBE_SECONDS", 0.5)
        marker = self._probe_with_pipe_holder(node, tmp_path, leader)
        started = time.monotonic()
        assert self._status(capsys) == "idle"
        assert time.monotonic() - started < 5.0
        child = int(marker.read_text())
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)

    def test_a_late_eof_does_not_extend_the_deadline(self, node, tmp_path, capsys, monkeypatch):
        """The leader prints and closes stdout at 0.4 s and exits at 0.8 s,
        past the 0.5 s deadline. A wait that got its own budget after the
        read would accept that answer at 0.8 s; one budget refuses it at the
        deadline and falls back."""
        import daemon
        import time
        monkeypatch.setattr(daemon, "STATUS_PROBE_SECONDS", 0.5)
        self._probed(node, tmp_path, (
            "import os, sys, time; time.sleep(0.4); print('busy', flush=True); "
            "os.close(1); time.sleep(0.4)"
        ))
        started = time.monotonic()
        assert self._status(capsys) == "idle"
        assert time.monotonic() - started < 1.5

    @pytest.mark.parametrize("leader", ["exits", "stays"])
    def test_the_deadline_holds_when_only_the_leader_can_be_ended(
        self, node, tmp_path, capsys, monkeypatch, leader
    ):
        """Windows has no group to end: the child keeps the pipe after the
        leader is killed. The deadline must still return `ps`, so nothing on
        the ps side may wait on the pipe or its stream (a reader blocked in
        the buffered stream holds its lock, and a `close` would wait on it).
        Leader-only termination is forced here; the child is ended by the
        test afterwards."""
        import daemon
        import signal
        import time
        monkeypatch.setattr(daemon, "STATUS_PROBE_SECONDS", 0.5)
        monkeypatch.setattr(daemon, "terminate_group", lambda proc, **_: proc.kill())
        marker = self._probe_with_pipe_holder(node, tmp_path, leader)
        started = time.monotonic()
        try:
            assert self._status(capsys) == "idle"
            assert time.monotonic() - started < 5.0
        finally:
            try:
                os.kill(int(marker.read_text()), getattr(signal, "SIGKILL", signal.SIGTERM))
            except (OSError, ValueError):
                pass

    def test_a_missing_program_falls_back(self, node, tmp_path, capsys):
        defn = tmp_path / "probed.json"
        defn.write_text(json.dumps({"status": {"invoke": ["/nonexistent/prog"]}}))
        save_registry({"alpha": {"root": str(tmp_path / "root"), "definition": str(defn)}})
        assert self._status(capsys) == "idle"

    def test_a_live_wake_wins_before_the_probe(self, node, tmp_path, capsys):
        self._probed(node, tmp_path, "print('resident')")
        write_wake_pid(node, os.getpid(), ["a.json"])
        assert self._status(capsys) == "busy"

    def test_the_probe_runs_in_the_node_root(self, node, tmp_path, capsys):
        self._probed(node, tmp_path,
                     "import os; print('busy' if os.getcwd().endswith('root') else 'x')")
        assert self._status(capsys) == "busy"
