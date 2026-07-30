"""Wake-delivery retry (issue #152) — exit 0 is the only ack.

Two layers here. `_settle_wake` / `_wake_retry_ready` are exercised directly
because the schedule and the dead-letter cap are the semantics worth pinning
exactly. The failure MODES (nonzero exit, timeout kill, missing binary,
unexpanded vars) go through `attached_loop` with real subprocesses, because the
whole point of the issue is that mail survives what the wake actually does.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import (
    MAX_WAKE_ATTEMPTS,
    Participant,
    WAKE_RETRY_SCHEDULE,
    agent_log_path,
    inbox_dir,
    read_wake_retry,
    trash_dir,
    wake_retry_path,
    write_wake_retry,
)
from daemon import _settle_wake, _wake_retry_ready, attached_loop
from mailbox import ensure_mailboxes
from registry import save_registry
from ulid import new as new_ulid


def _read_log(name: str) -> str:
    p = agent_log_path(name)
    return p.read_text() if p.is_file() else ""


def _envelope(content: str) -> dict:
    msg_id = new_ulid()
    return {
        "id": msg_id,
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


@pytest.fixture
def agent(fake_home, tmp_path):
    root = tmp_path / "a"
    root.mkdir()
    p = Participant("A", root)
    ensure_mailboxes(p)
    return p


# ---------- _settle_wake semantics ----------

class TestSettleWake:
    def test_exit_zero_acks_and_clears_the_record(self, agent):
        env = _in_trash("A", "done")
        write_wake_retry("A", [env.name], 2, datetime.now(timezone.utc))

        _settle_wake(agent, [env], 0)

        assert env.is_file()
        assert not list(inbox_dir("A").glob("*.json"))
        assert read_wake_retry("A") is None

    def test_nonzero_exit_requeues_and_arms_backoff(self, agent):
        env = _in_trash("A", "retry me")
        before = datetime.now(timezone.utc)

        _settle_wake(agent, [env], 3)

        assert not env.exists()
        requeued = list(inbox_dir("A").glob("*.json"))
        assert [f.name for f in requeued] == [env.name]
        assert json.loads(requeued[0].read_text())["content"] == "retry me"

        record = read_wake_retry("A")
        assert record["attempts"] == 1
        assert record["unit"] == [env.name]
        next_at = datetime.fromisoformat(record["next_at"].replace("Z", "+00:00"))
        assert next_at >= before + timedelta(seconds=WAKE_RETRY_SCHEDULE[0])
        assert "wake failed (exit 3)" in _read_log("A")

    def test_spawn_failure_requeues(self, agent):
        env = _in_trash("A", "never ran")

        _settle_wake(agent, [env], None)

        assert [f.name for f in inbox_dir("A").glob("*.json")] == [env.name]
        assert "wake failed (spawn failed)" in _read_log("A")

    def test_explicit_reason_reaches_the_log(self, agent):
        env = _in_trash("A", "bad vars")

        _settle_wake(agent, [env], None, reason="undefined a8s var: $MODEL")

        assert "wake failed (undefined a8s var: $MODEL)" in _read_log("A")

    def test_repeated_failures_walk_the_schedule(self, agent):
        env = _in_trash("A", "flaky")
        delays = []
        for attempt in range(1, MAX_WAKE_ATTEMPTS):
            before = datetime.now(timezone.utc)
            _settle_wake(agent, [env], 3)
            record = read_wake_retry("A")
            assert record["attempts"] == attempt
            next_at = datetime.fromisoformat(record["next_at"].replace("Z", "+00:00"))
            delays.append(round((next_at - before).total_seconds()))
            # Next attempt consumes it again, as a wake would.
            env = inbox_dir("A") / env.name
            env.rename(trash_dir("A") / env.name)
            env = trash_dir("A") / env.name

        assert delays == WAKE_RETRY_SCHEDULE

    def test_cap_dead_letters_into_trash(self, agent):
        env = _in_trash("A", "poison")
        write_wake_retry("A", [env.name], MAX_WAKE_ATTEMPTS - 1, datetime.now(timezone.utc))

        _settle_wake(agent, [env], 3)

        assert env.is_file(), "dead letters stay in trash, inspectable"
        assert not list(inbox_dir("A").glob("*.json"))
        assert read_wake_retry("A") is None
        log = _read_log("A")
        assert f"dead letter after {MAX_WAKE_ATTEMPTS} failed wakes" in log

        import txlog

        events = txlog.read_events(env.stem)
        assert [e["event"] for e in events] == ["DROPPED"]
        assert "left in trash" in events[0]["detail"]

    def test_a_different_delivery_restarts_the_count(self, agent):
        first = _in_trash("A", "one")
        write_wake_retry("A", [first.name], MAX_WAKE_ATTEMPTS - 1, datetime.now(timezone.utc))
        second = _in_trash("A", "two")

        _settle_wake(agent, [second], 3)

        record = read_wake_retry("A")
        assert record["attempts"] == 1
        assert record["unit"] == [second.name]
        assert (inbox_dir("A") / second.name).is_file()

    def test_batch_failure_requeues_every_envelope(self, agent):
        envs = [_in_trash("A", f"batch-{i}") for i in range(3)]

        _settle_wake(agent, envs, 3)

        assert sorted(f.name for f in inbox_dir("A").glob("*.json")) == sorted(
            e.name for e in envs
        )
        assert read_wake_retry("A")["unit"] == sorted(e.name for e in envs)
        assert "requeued 3" in _read_log("A")

    def test_success_after_a_failure_clears_the_backoff(self, agent):
        env = _in_trash("A", "eventually fine")
        _settle_wake(agent, [env], 3)
        env = inbox_dir("A") / env.name
        env.rename(trash_dir("A") / env.name)

        _settle_wake(agent, [trash_dir("A") / env.name], 0)

        assert read_wake_retry("A") is None
        assert not list(inbox_dir("A").glob("*.json"))


class TestWakeRetryReady:
    def test_no_record_is_ready(self, agent):
        assert _wake_retry_ready("A")

    def test_future_next_at_is_not_ready(self, agent):
        write_wake_retry(
            "A", ["x.json"], 1, datetime.now(timezone.utc) + timedelta(seconds=30)
        )
        assert not _wake_retry_ready("A")

    def test_elapsed_next_at_is_ready(self, agent):
        write_wake_retry(
            "A", ["x.json"], 1, datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        assert _wake_retry_ready("A")

    def test_corrupt_record_is_ready(self, agent):
        wake_retry_path("A").parent.mkdir(parents=True, exist_ok=True)
        wake_retry_path("A").write_text("{not json")
        assert _wake_retry_ready("A")

    def test_unparseable_timestamp_is_ready(self, agent):
        wake_retry_path("A").parent.mkdir(parents=True, exist_ok=True)
        wake_retry_path("A").write_text(json.dumps({"attempts": 1, "next_at": "soon"}))
        assert _wake_retry_ready("A")


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
        "invoke": [str(fixtures_dir / "mock-flaky-cli"), "MSG:$MESSAGE"],
        **extra,
    }


class TestFailureModesKeepMailDeliverable:
    def test_nonzero_exit_requeues_the_message(self, fake_home, tmp_path, fixtures_dir):
        _register(tmp_path, _flaky_def(fixtures_dir))
        _queue_inbox("A", "survive the crash")

        attached_loop(["A"], 0.05, single_pass=True)

        bodies = [
            json.loads(f.read_text())["content"]
            for f in inbox_dir("A").glob("*.json")
        ]
        assert bodies == ["survive the crash"]
        log = _read_log("A")
        assert "wake failed (exit 3)" in log
        assert read_wake_retry("A")["attempts"] == 1

    def test_missing_binary_requeues_the_message(self, fake_home, tmp_path):
        _register(tmp_path, {"invoke": [str(tmp_path / "no-such-cli"), "$MESSAGE"]})
        _queue_inbox("A", "bad definition")

        attached_loop(["A"], 0.05, single_pass=True)

        assert len(list(inbox_dir("A").glob("*.json"))) == 1
        log = _read_log("A")
        assert "command not found" in log
        assert "wake failed (spawn failed)" in log

    def test_undefined_var_requeues_the_message(self, fake_home, tmp_path, fixtures_dir):
        _register(
            tmp_path,
            {"invoke": [str(fixtures_dir / "mock-cli"), "MODEL:$MODEL|$MESSAGE"]},
        )
        _queue_inbox("A", "unexpanded")

        attached_loop(["A"], 0.05, single_pass=True)

        assert len(list(inbox_dir("A").glob("*.json"))) == 1
        assert "wake failed (undefined a8s var: $MODEL)" in _read_log("A")

    def test_timeout_kill_requeues_the_message(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod

        monkeypatch.setenv("MOCK_SLEEP", "5")
        _register(
            tmp_path,
            {
                "invoke": [str(fixtures_dir / "mock-slow-cli"), "MSG:$MESSAGE"],
                "max_wake_seconds": 0.25,
            },
        )
        _queue_inbox("A", "hangs forever")

        waits = 0

        def stop_once_requeued(self, timeout=None):
            nonlocal waits
            waits += 1
            if "wake failed" in _read_log("A") or waits >= 60:
                if daemon_mod._STOP_EVENT is not None:
                    daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", stop_once_requeued)
        attached_loop(["A"], 0.05, single_pass=False)

        log = _read_log("A")
        assert "max wake time" in log
        assert "wake failed" in log
        bodies = [
            json.loads(f.read_text())["content"]
            for f in inbox_dir("A").glob("*.json")
        ]
        assert bodies == ["hangs forever"]

    def test_batch_failure_requeues_the_whole_batch(
        self, fake_home, tmp_path, fixtures_dir
    ):
        _register(
            tmp_path,
            {
                "invoke": [str(fixtures_dir / "mock-flaky-cli"), "SINGLE"],
                "batch": {
                    "invoke": [str(fixtures_dir / "mock-flaky-cli"), "BATCH"],
                    "limit": 5,
                },
            },
        )
        for i in range(3):
            _queue_inbox("A", f"batch-{i}")

        attached_loop(["A"], 0.05, single_pass=True)

        assert len(list(inbox_dir("A").glob("*.json"))) == 3
        assert "batch exec:" in _read_log("A")
        assert read_wake_retry("A")["attempts"] == 1


class TestBackoffPreventsHotSpin:
    def test_a_broken_cli_wakes_once_across_many_passes(
        self, fake_home, tmp_path, fixtures_dir
    ):
        _register(tmp_path, _flaky_def(fixtures_dir))
        _queue_inbox("A", "poison for now")

        for _ in range(5):
            attached_loop(["A"], 0.01, single_pass=True)

        log = _read_log("A")
        assert log.count("] exec: ") == 1, "backoff must gate the retry, not spin"
        assert len(list(inbox_dir("A").glob("*.json"))) == 1
        assert read_wake_retry("A")["attempts"] == 1

    def test_backoff_gates_the_inner_drain_loop(
        self, fake_home, tmp_path, fixtures_dir
    ):
        # A single sync pass with several queued messages drains the inbox in an
        # inner while-loop. Without the retry gate the first failure requeues the
        # message and the loop immediately sees it again — a tight spin inside
        # one iteration.
        _register(tmp_path, _flaky_def(fixtures_dir))
        for i in range(3):
            _queue_inbox("A", f"msg-{i}")

        attached_loop(["A"], 0.01, single_pass=True)

        log = _read_log("A")
        assert log.count("] exec: ") == 1
        assert len(list(inbox_dir("A").glob("*.json"))) == 3

    def test_the_record_survives_a_handler_restart(
        self, fake_home, tmp_path, fixtures_dir
    ):
        _register(tmp_path, _flaky_def(fixtures_dir))
        _queue_inbox("A", "still backing off")

        attached_loop(["A"], 0.01, single_pass=True)
        armed = read_wake_retry("A")
        attached_loop(["A"], 0.01, single_pass=True)

        assert read_wake_retry("A") == armed
        assert _read_log("A").count("] exec: ") == 1


class TestRecovery:
    def test_delivery_succeeds_once_the_cli_recovers(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        ok_file = tmp_path / "cli-fixed"
        monkeypatch.setenv("MOCK_OK_FILE", str(ok_file))
        _register(tmp_path, _flaky_def(fixtures_dir))
        _queue_inbox("A", "delivered on retry")

        attached_loop(["A"], 0.01, single_pass=True)
        assert len(list(inbox_dir("A").glob("*.json"))) == 1

        # Fix the CLI and let the backoff elapse.
        ok_file.write_text("ok")
        record = read_wake_retry("A")
        write_wake_retry(
            "A",
            record["unit"],
            record["attempts"],
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )

        attached_loop(["A"], 0.01, single_pass=True)

        assert not list(inbox_dir("A").glob("*.json")), "acked — no requeue"
        assert read_wake_retry("A") is None
        assert len(list(trash_dir("A").glob("*.json"))) == 1
        assert _read_log("A").count("] exec: ") == 2
        assert "MOCK-CLI: MSG:delivered on retry" in _read_log("A")

    def test_exhaustion_dead_letters_after_the_full_schedule(
        self, fake_home, tmp_path, fixtures_dir
    ):
        _register(tmp_path, _flaky_def(fixtures_dir))
        _queue_inbox("A", "never deliverable")

        for _ in range(MAX_WAKE_ATTEMPTS):
            record = read_wake_retry("A")
            if record is not None:
                write_wake_retry(
                    "A",
                    record["unit"],
                    record["attempts"],
                    datetime.now(timezone.utc) - timedelta(seconds=1),
                )
            attached_loop(["A"], 0.01, single_pass=True)

        log = _read_log("A")
        assert log.count("] exec: ") == MAX_WAKE_ATTEMPTS
        assert f"dead letter after {MAX_WAKE_ATTEMPTS} failed wakes" in log
        assert not list(inbox_dir("A").glob("*.json"))
        assert len(list(trash_dir("A").glob("*.json"))) == 1
        assert read_wake_retry("A") is None
