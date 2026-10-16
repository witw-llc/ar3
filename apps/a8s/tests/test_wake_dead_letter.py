"""A failed wake is a dead letter at once — exit 0 is the only ack.

`_settle_wake` is exercised directly for the exact record it leaves. The
failure modes (nonzero exit, timeout kill, missing binary, unexpanded vars)
go through `attached_loop` with real subprocesses, because what matters is
what the wake actually does.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import txlog
from core import (
    Participant,
    agent_dir,
    agent_log_path,
    count_dead_letters,
    dead_letters_dir,
    inbox_dir,
    mark_dead_letter,
    trash_dir,
)
from daemon import _settle_wake, attached_loop, wake_once
from mailbox import ensure_mailboxes
from registry import save_registry
from ar3.ulid import new as new_ulid


def _main_thread_only(patched):
    """Scope an Event.wait monkeypatch to the loop under test: the watchdog
    thread waits on the same event class and must keep the real wait."""
    real_wait = threading.Event.wait

    def gated(self, timeout=None):
        if threading.current_thread() is not threading.main_thread():
            return real_wait(self, timeout)
        return patched(self, timeout)

    return gated


def _read_log(name: str) -> str:
    p = agent_log_path(name)
    return p.read_text() if p.is_file() else ""


def _envelope(content: str) -> dict:
    return {
        "id": new_ulid(),
        "date": "2026-04-29T12:00:00Z",
        "from": "Y",
        "to": "A",
        "content": content,
        "files": [],
    }


def _queue_inbox(name: str, content: str) -> Path:
    env = _envelope(content)
    path = inbox_dir(name) / f"{env['id']}.json"
    path.write_text(json.dumps(env))
    return path


def _in_trash(name: str, content: str) -> Path:
    """An envelope sitting in trash, as a wake that just consumed it leaves it."""
    env = _envelope(content)
    trash_dir(name).mkdir(parents=True, exist_ok=True)
    path = trash_dir(name) / f"{env['id']}.json"
    path.write_text(json.dumps(env))
    return path


def _dead(name: str) -> list[str]:
    d = dead_letters_dir(name)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def _execs(name: str) -> int:
    return _read_log(name).count("] exec: ")


@pytest.fixture
def agent(fake_home, tmp_path):
    root = tmp_path / "a"
    root.mkdir()
    p = Participant("A", root)
    ensure_mailboxes(p)
    return p


class TestSettleWake:
    def test_exit_zero_acks(self, agent):
        env = _in_trash("A", "done")

        _settle_wake(agent, [env], 0)

        assert env.is_file()
        assert _dead("A") == []
        assert [e["event"] for e in txlog.read_events(env.stem)] == ["WAKE_RETURN"]

    def test_nonzero_exit_is_a_dead_letter_at_once(self, agent):
        env = _in_trash("A", "fails once")

        _settle_wake(agent, [env], 3)

        assert env.is_file(), "a dead letter stays in trash"
        assert not list(inbox_dir("A").glob("*.json"))
        assert _dead("A") == [env.name]
        assert f"wake failed (exit 3): 1 dead letter(s) left in trash: {env.stem}" in _read_log("A")
        events = txlog.read_events(env.stem)
        assert [e["event"] for e in events] == ["WAKE_RETURN", "DROPPED"]
        assert events[0]["detail"] == "exit 3"
        assert events[1]["detail"] == "wake failed (exit 3); dead letter in trash"

    def test_no_retry_record_is_written(self, agent):
        _settle_wake(agent, [_in_trash("A", "x")], 3)

        assert not (agent_dir("A") / "wake-retry").exists()

    def test_spawn_failure_is_a_dead_letter(self, agent):
        env = _in_trash("A", "never ran")

        _settle_wake(agent, [env], None)

        assert _dead("A") == [env.name]
        assert "wake failed (spawn failed)" in _read_log("A")

    def test_explicit_reason_reaches_the_log(self, agent):
        _settle_wake(agent, [_in_trash("A", "bad vars")], None, reason="undefined a8s var: $MODEL")

        assert "wake failed (undefined a8s var: $MODEL)" in _read_log("A")

    def test_a_failed_batch_dead_letters_every_envelope_in_one_line(self, agent):
        envs = [_in_trash("A", f"batch-{i}") for i in range(3)]

        _settle_wake(agent, envs, 3)

        assert _dead("A") == sorted(e.name for e in envs)
        assert not list(inbox_dir("A").glob("*.json"))
        log = _read_log("A")
        assert log.count("dead letter(s) left in trash") == 1
        assert "3 dead letter(s)" in log
        for env in envs:
            assert [e["event"] for e in txlog.read_events(env.stem)] == ["WAKE_RETURN", "DROPPED"]


class TestWakeOnceRefusals:
    """A message a wake cannot even start for is a dead letter, so `a8s ps`
    and `a8s health` count it."""

    def test_an_unreadable_inbox_file_is_a_dead_letter(self, agent):
        path = inbox_dir("A") / f"{new_ulid()}.json"
        path.write_text("not json")
        assert wake_once(agent, path) is False
        assert not path.exists()
        assert _dead("A") == [path.name]
        assert (trash_dir("A") / path.name).is_file()
        rows = txlog.read_events(path.stem)
        assert [e["event"] for e in rows] == ["DROPPED"]
        assert "inbox parse error" in rows[0]["detail"]
        assert count_dead_letters("A", datetime.now(timezone.utc) - timedelta(hours=1)) == 1

    def test_a_definition_that_will_not_load_is_a_dead_letter(self, agent, tmp_path):
        save_registry({"A": {"root": str(agent.root), "definition": str(tmp_path / "missing.json")}})
        path = _queue_inbox("A", "hello")
        assert wake_once(agent, path) is False
        assert _dead("A") == [path.name]
        rows = txlog.read_events(path.stem)
        assert [e["event"] for e in rows] == ["DROPPED"]
        assert "definition did not load" in rows[0]["detail"]
        assert _execs("A") == 0


class TestCountDeadLetters:
    def test_counts_only_the_window(self, agent):
        mark_dead_letter("A", "new.json")
        mark_dead_letter("A", "old.json")
        old = time.time() - 25 * 3600
        os.utime(dead_letters_dir("A") / "old.json", (old, old))

        since = datetime.now(timezone.utc) - timedelta(hours=24)
        assert count_dead_letters("A", since) == 1

    def test_no_directory_is_zero(self, agent):
        assert count_dead_letters("A", datetime.now(timezone.utc)) == 0

    def test_markers_older_than_a_week_are_deleted(self, agent):
        mark_dead_letter("A", "recent.json")
        mark_dead_letter("A", "stale.json")
        stale = time.time() - 8 * 86400
        os.utime(dead_letters_dir("A") / "stale.json", (stale, stale))

        assert count_dead_letters("A", datetime.now(timezone.utc) - timedelta(days=30)) == 1
        assert _dead("A") == ["recent.json"]


# ---------- failure modes end-to-end through attached_loop ----------

def _register(tmp_path: Path, definition: dict) -> Participant:
    root = tmp_path / "a"
    root.mkdir(exist_ok=True)
    defp = tmp_path / "def.json"
    defp.write_text(json.dumps(definition))
    save_registry({"A": {"root": str(root), "definition": str(defp)}})
    p = Participant("A", root)
    ensure_mailboxes(p)
    return p


def _flaky_def(fixtures_dir: Path, **extra) -> dict:
    return {
        "invoke": [sys.executable, str(fixtures_dir / "mock_flaky_cli.py"), "MSG:$MESSAGE"],
        **extra,
    }


class TestExit127NamesThePath:
    def test_the_log_names_the_path_and_where_it_came_from(
        self, fake_home, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("A8S_WAKE_PATH", "/machine/bin:/usr/bin")
        p = _register(tmp_path, {"invoke": ["x", "$MESSAGE"]})
        _settle_wake(p, [_in_trash("A", "needs node")], 127)
        log = _read_log("A")
        assert "exit 127 means a program was not found" in log
        assert "the terminals a8s has run in: /machine/bin:/usr/bin" in log
        assert "Run `a8s restart A` from a terminal where the program runs." in log

    def test_a_declared_path_is_named_as_the_source(self, fake_home, tmp_path):
        p = _register(
            tmp_path, {"invoke": ["x", "$MESSAGE"], "env": {"PATH": "/node/bin"}}
        )
        _settle_wake(p, [_in_trash("A", "needs node")], 127)
        log = _read_log("A")
        assert "came from `definition.env`: /node/bin" in log
        assert "a8s restart" not in log

    def test_a_login_shell_node_names_the_rc_files(self, fake_home, tmp_path):
        p = _register(tmp_path, {"invoke": ["x", "$MESSAGE"], "wake_shell": "login"})
        _settle_wake(p, [_in_trash("A", "needs node")], 127)
        assert "login shell" in _read_log("A")

    def test_another_exit_code_says_nothing_about_the_path(self, fake_home, tmp_path):
        p = _register(tmp_path, {"invoke": ["x", "$MESSAGE"]})
        _settle_wake(p, [_in_trash("A", "plain failure")], 3)
        assert "exit 127" not in _read_log("A")


class TestFailureModesDeadLetter:
    def test_nonzero_exit(self, fake_home, tmp_path, fixtures_dir):
        _register(tmp_path, _flaky_def(fixtures_dir))
        msg = _queue_inbox("A", "the crash")

        attached_loop(["A"], 0.05, single_pass=True)

        assert not list(inbox_dir("A").glob("*.json"))
        assert (trash_dir("A") / msg.name).is_file()
        assert _dead("A") == [msg.name]
        assert "wake failed (exit 3)" in _read_log("A")

    def test_missing_binary(self, fake_home, tmp_path):
        _register(tmp_path, {"invoke": [str(tmp_path / "no-such-cli"), "$MESSAGE"]})
        msg = _queue_inbox("A", "bad definition")

        attached_loop(["A"], 0.05, single_pass=True)

        assert not list(inbox_dir("A").glob("*.json"))
        assert _dead("A") == [msg.name]
        log = _read_log("A")
        assert "command not found" in log
        assert "wake failed (spawn failed)" in log

    def test_undefined_var(self, fake_home, tmp_path, fixtures_dir):
        _register(
            tmp_path,
            {"invoke": [sys.executable, str(fixtures_dir / "mock_cli.py"), "MODEL:$MODEL|$MESSAGE"]},
        )
        msg = _queue_inbox("A", "unexpanded")

        attached_loop(["A"], 0.05, single_pass=True)

        assert not list(inbox_dir("A").glob("*.json"))
        assert _dead("A") == [msg.name]
        assert "wake failed (undefined a8s var: $MODEL)" in _read_log("A")

    def test_timeout_kill(self, fake_home, tmp_path, fixtures_dir, monkeypatch):
        import daemon as daemon_mod

        monkeypatch.setenv("MOCK_SLEEP", "5")
        _register(
            tmp_path,
            {
                "invoke": [sys.executable, str(fixtures_dir / "mock_slow_cli.py"), "MSG:$MESSAGE"],
                "max_wake_seconds": 0.25,
            },
        )
        msg = _queue_inbox("A", "hangs forever")

        waits = 0

        def stop_once_settled(self, timeout=None):
            nonlocal waits
            waits += 1
            if "wake failed" in _read_log("A") or waits >= 60:
                if daemon_mod._STOP_EVENT is not None:
                    daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(stop_once_settled))
        attached_loop(["A"], 0.05, single_pass=False)

        log = _read_log("A")
        assert "max wake time" in log
        assert "wake failed" in log
        assert not list(inbox_dir("A").glob("*.json"))
        assert _dead("A") == [msg.name]
        assert _execs("A") == 1

    def test_batch_failure_dead_letters_the_whole_batch(
        self, fake_home, tmp_path, fixtures_dir
    ):
        _register(
            tmp_path,
            {
                "pause": 0,
                "invoke": [sys.executable, str(fixtures_dir / "mock_flaky_cli.py"), "SINGLE"],
                "batch": {
                    "invoke": [sys.executable, str(fixtures_dir / "mock_flaky_cli.py"), "BATCH"],
                    "limit": 5,
                },
            },
        )
        msgs = [_queue_inbox("A", f"batch-{i}") for i in range(3)]

        attached_loop(["A"], 0.05, single_pass=True)

        assert not list(inbox_dir("A").glob("*.json"))
        assert "batch exec:" in _read_log("A")
        assert _dead("A") == sorted(m.name for m in msgs)


class TestNothingTriesAgain:
    def test_each_message_is_tried_once(self, fake_home, tmp_path, fixtures_dir):
        _register(tmp_path, _flaky_def(fixtures_dir))
        for i in range(3):
            _queue_inbox("A", f"msg-{i}")

        for _ in range(3):
            attached_loop(["A"], 0.01, single_pass=True)

        assert _execs("A") == 3
        assert not list(inbox_dir("A").glob("*.json"))
        assert len(_dead("A")) == 3

    def test_the_next_message_wakes_at_once(self, fake_home, tmp_path, fixtures_dir, monkeypatch):
        ok_file = tmp_path / "cli-fixed"
        monkeypatch.setenv("MOCK_OK_FILE", str(ok_file))
        _register(tmp_path, _flaky_def(fixtures_dir))
        first = _queue_inbox("A", "fails")
        attached_loop(["A"], 0.01, single_pass=True)

        ok_file.write_text("ok")
        _queue_inbox("A", "works")
        attached_loop(["A"], 0.01, single_pass=True)

        assert _execs("A") == 2
        assert _dead("A") == [first.name]
        assert "MOCK-CLI: MSG:works" in _read_log("A")
        assert "MOCK-CLI: MSG:fails" not in _read_log("A")
