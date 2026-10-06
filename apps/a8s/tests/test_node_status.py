"""`a8s ps` STATUS and activity columns, and what `a8s stop` does to a node's
backoff. The real-process tests for `stop --force` live in test_stop_force.py."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import txlog
from commands import (
    _duration_left,
    _hard_kill,
    _hard_kill_targets,
    cmd_ps,
    cmd_restart,
    cmd_stop,
)
from core import (
    inbox_dir,
    mark_dead_letter,
    pid_path,
    read_dead_letters,
    read_wake_retry,
    trash_dir,
    transactions_path,
    wake_pid_path,
    write_wake_pid,
    write_wake_retry,
)
from cli import dispatch
from registry import save_registry


def _in(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


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


class TestDurationLeft:
    @pytest.mark.parametrize("seconds,shown", [
        (0.2, "1s"), (29.4, "30s"), (59.0, "59s"), (60.0, "1m"), (61.0, "2m"),
        (119.0, "2m"), (600.0, "10m"), (3600.0, "1h"),
    ])
    def test_rounds_up_to_the_unit(self, seconds, shown):
        assert _duration_left(seconds) == shown


class TestPsStatus:
    def test_ok_when_no_backoff_is_armed(self, node, capsys):
        assert cmd_ps([]) == 0
        out = capsys.readouterr().out
        assert "STATUS" in out
        assert _row(out, "alpha")[3] == "ok"

    def test_a_running_backoff_shows_what_is_left_and_the_attempt(self, node, capsys):
        write_wake_retry(node, ["a.json"], 2, _in(120))
        cmd_ps([])
        assert "failed, backoff 2m (attempt 3/4)" in capsys.readouterr().out

    def test_the_time_shown_is_what_remains(self, node, capsys):
        write_wake_retry(node, ["a.json"], 1, _in(30))
        write_wake_retry("beta", ["a.json"], 3, _in(5))
        cmd_ps([])
        out = capsys.readouterr().out
        assert "failed, backoff 30s (attempt 2/4)" in out
        assert "failed, backoff 5s (attempt 4/4)" in out

    def test_an_elapsed_backoff_is_ok(self, node, capsys):
        write_wake_retry(node, ["a.json"], 2, _in(-5))
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[3] == "ok"

    def test_a_corrupt_record_is_ok(self, node, capsys):
        from core import wake_retry_path

        wake_retry_path(node).write_text("{not json")
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[3] == "ok"

    def test_a_wake_in_flight_is_busy(self, node, capsys):
        write_wake_pid(node, os.getpid(), ["a.json"])
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[3] == "busy"

    def test_a_dead_wake_record_is_not_busy(self, node, capsys):
        wake_pid_path(node).write_text(json.dumps({"pid": 2**22 + 12345, "start": "", "unit": []}))
        cmd_ps([])
        assert _row(capsys.readouterr().out, "alpha")[3] == "ok"

    def test_quiet_is_still_names_only(self, node, capsys):
        write_wake_retry(node, ["a.json"], 2, _in(120))
        cmd_ps(["-q"])
        assert capsys.readouterr().out.splitlines() == ["alpha", "beta"]


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
        assert _row(out, "alpha")[4] == "3"
        assert _row(out, "beta")[4] == "1"

    def test_only_a_landing_counts(self, node, capsys):
        for event in ("ENQUEUED", "WAKE_START", "WAKE_RETURN", "PROXY_DELIVERED", "NOT_LOCAL"):
            _seed(event, "alpha", "01A")
        cmd_ps([])
        out = capsys.readouterr().out
        assert _row(out, "alpha")[4] == "0"
        assert _row(out, "alpha")[5] == "-"

    def test_the_last_message_is_local_time_with_its_zone(self, node, capsys, zone):
        zone("America/Los_Angeles")
        _seed("ROUTED", "alpha", "01A", age_hours=24 * 200)
        txlog_stamp = "2026-07-01T12:00:00.000Z"
        _seed("ROUTED", "alpha", "01B", age_hours=1)
        with sqlite3.connect(transactions_path()) as conn:
            conn.execute("UPDATE transactions SET timestamp = ? WHERE msg_id = '01B'", (txlog_stamp,))
        cmd_ps([])
        out = capsys.readouterr().out
        assert "2026-07-01 05:00 PDT" in out
        # An old newest message still shows when the node last heard.
        assert _row(out, "alpha")[4] == "0"

    def test_no_log_reads_as_nothing(self, node, capsys):
        assert not transactions_path().exists()
        assert cmd_ps([]) == 0
        row = _row(capsys.readouterr().out, "alpha")
        assert row[4] == "0" and row[5] == "-"


@pytest.fixture
def stoppable(fake_home, tmp_path, monkeypatch):
    """A node whose handler detaches on the first SIGTERM, with an armed
    backoff, a dead letter in trash and a returned message in the inbox."""
    root = tmp_path / "root"
    root.mkdir()
    save_registry({"alpha": {"root": str(root)}})
    pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
    pid_path("alpha").write_text(str(os.getppid()))
    write_wake_retry("alpha", ["a.json"], 2, _in(300))
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


class TestStopClearsTheBackoff:
    def test_stop_clears_the_record_and_says_so(self, stoppable, capsys):
        assert cmd_stop([stoppable]) == 0
        assert read_wake_retry(stoppable) is None
        assert "alpha: backoff cleared" in capsys.readouterr().out

    def test_stop_leaves_trash_and_dead_letters_alone(self, stoppable):
        assert cmd_stop([stoppable]) == 0
        assert (trash_dir(stoppable) / "dead.json").is_file()
        assert read_dead_letters(stoppable) == ["dead.json"]
        assert not (inbox_dir(stoppable) / "dead.json").exists()

    def test_retry_still_returns_the_dead_letters_after_a_stop(self, stoppable, capsys):
        cmd_stop([stoppable])
        assert dispatch("retry", [stoppable], 1.0) == 0
        assert (inbox_dir(stoppable) / "dead.json").is_file()
        assert read_dead_letters(stoppable) == []

    def test_a_stop_with_no_backoff_prints_no_such_line(self, stoppable, capsys):
        from core import clear_wake_retry

        clear_wake_retry(stoppable)
        assert cmd_stop([stoppable]) == 0
        assert "backoff" not in capsys.readouterr().out

    def test_a_stop_that_timed_out_keeps_the_record(self, stoppable, monkeypatch, capsys):
        for mod in ("commands", "daemon", "core"):
            monkeypatch.setattr(f"{mod}.os.kill", lambda pid, sig: None)
        monkeypatch.setattr("commands.STOP_WAIT_S", 0.05)
        assert cmd_stop([stoppable]) == 1
        assert read_wake_retry(stoppable) is not None

    def test_restart_clears_it_too(self, stoppable, monkeypatch, capsys):
        class FakeProc:
            pid = 4242

        monkeypatch.setattr("commands.subprocess.Popen", lambda *a, **k: FakeProc())
        assert cmd_restart([stoppable]) == 0
        assert read_wake_retry(stoppable) is None


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

    def test_a_handler_that_never_detaches_is_killed_and_its_mail_returned(
        self, fake_home, tmp_path, monkeypatch, capsys
    ):
        root = tmp_path / "root"
        root.mkdir()
        save_registry({"alpha": {"root": str(root)}})
        pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
        pid_path("alpha").write_text(str(os.getppid()))
        write_wake_pid("alpha", os.getpid(), ["m.json"])
        trash_dir("alpha").mkdir(parents=True, exist_ok=True)
        (trash_dir("alpha") / "m.json").write_text("{}")
        write_wake_retry("alpha", ["m.json"], 1, _in(300))

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
        assert (inbox_dir("alpha") / "m.json").is_file()
        assert not (trash_dir("alpha") / "m.json").exists()
        assert not wake_pid_path("alpha").exists()
        assert read_wake_retry("alpha") is None
        out = capsys.readouterr()
        assert "killing it" in out.err
        assert "returned 1 message(s) from the killed wake" in out.out

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
