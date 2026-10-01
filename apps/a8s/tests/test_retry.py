"""Tests for `a8s retry <name>` — end a failed wake's backoff and return its dead letters."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cli import dispatch
from core import agent_log_path, inbox_dir, read_wake_retry, write_wake_retry
from daemon import _wake_retry_ready
from registry import save_registry


@pytest.fixture
def backed_off(fake_home, tmp_path):
    root = tmp_path / "agent-root"
    root.mkdir()
    save_registry({"alpha": {"root": str(root)}})
    write_wake_retry(
        "alpha", ["a.json", "b.json"], 3,
        datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    return "alpha"


class TestRetry:
    def test_a8s_retry_ends_the_backoff_and_the_attempt_count(self, backed_off, capsys):
        assert not _wake_retry_ready(backed_off)
        assert dispatch("retry", [backed_off], 1.0) == 0
        assert read_wake_retry(backed_off) is None
        assert _wake_retry_ready(backed_off)
        assert "2 message(s) wait for the node to start" in capsys.readouterr().out

    def test_the_agent_log_records_who_ended_it(self, backed_off):
        dispatch("retry", [backed_off], 1.0)
        assert "backoff ended by `a8s retry`" in agent_log_path(backed_off).read_text()

    def test_no_backoff_is_not_a_failure(self, fake_home, tmp_path, capsys):
        save_registry({"alpha": {"root": str(tmp_path)}})
        assert dispatch("retry", ["alpha"], 1.0) == 0
        assert "no wake is waiting on a backoff" in capsys.readouterr().out

    def test_an_unknown_name_exits_one(self, fake_home, capsys):
        assert dispatch("retry", ["nobody"], 1.0) == 1
        assert "a8s: no agent named 'nobody'" in capsys.readouterr().err

    def test_a_missing_name_is_a_usage_error(self, fake_home, capsys):
        assert dispatch("retry", [], 1.0) == 2
        assert "usage: a8s retry <name>" in capsys.readouterr().err


class TestDeadLetterRetry:
    """The real path: a wake that fails to the cap, then the operator's command."""

    @pytest.fixture
    def agent(self, fake_home, tmp_path):
        from core import Participant
        from mailbox import ensure_mailboxes

        root = tmp_path / "agent-root"
        root.mkdir()
        save_registry({"alpha": {"root": str(root)}})
        p = Participant("alpha", root)
        ensure_mailboxes(p)
        return p

    @staticmethod
    def _consume(name, content="poison"):
        import json

        from ar3.ulid import new as new_ulid
        from core import trash_dir

        msg_id = new_ulid()
        path = trash_dir(name) / f"{msg_id}.json"
        path.write_text(json.dumps({
            "id": msg_id, "date": "2026-04-29T12:00:00Z",
            "from": "other", "to": name, "content": content, "files": [],
        }))
        return path

    @staticmethod
    def _fail_to_the_cap(agent, env):
        from core import MAX_WAKE_ATTEMPTS, trash_dir
        from daemon import _settle_wake

        for _ in range(MAX_WAKE_ATTEMPTS):
            _settle_wake(agent, [env], 3)
            inbox_file = inbox_dir(agent.name) / env.name
            if inbox_file.is_file():
                inbox_file.rename(trash_dir(agent.name) / env.name)

    def test_retry_returns_a_dead_letter_to_the_inbox(self, agent, capsys):
        import txlog
        from core import read_dead_letters

        env = self._consume("alpha")
        self._fail_to_the_cap(agent, env)
        assert env.is_file()
        assert read_dead_letters("alpha") == [env.name]
        assert "a8s retry alpha" in agent_log_path("alpha").read_text()
        assert not (inbox_dir("alpha") / env.name).exists()

        assert dispatch("retry", ["alpha"], 1.0) == 0

        assert (inbox_dir("alpha") / env.name).is_file()
        assert not env.exists()
        assert read_dead_letters("alpha") == []
        assert "1 dead letter(s) returned to the inbox" in capsys.readouterr().out
        events = [e["event"] for e in txlog.read_events(env.stem)]
        assert events[-1] == "REQUEUED"

    def test_it_gets_a_full_set_of_attempts(self, agent):
        from core import MAX_WAKE_ATTEMPTS, trash_dir
        from daemon import _settle_wake

        env = self._consume("alpha")
        self._fail_to_the_cap(agent, env)
        dispatch("retry", ["alpha"], 1.0)
        back = inbox_dir("alpha") / env.name
        back.rename(trash_dir("alpha") / env.name)

        _settle_wake(agent, [env], 3)

        assert read_wake_retry("alpha")["attempts"] == 1
        assert MAX_WAKE_ATTEMPTS > 1

    def test_a_second_dead_lettering_is_recorded_again(self, agent):
        from core import read_dead_letters

        env = self._consume("alpha")
        self._fail_to_the_cap(agent, env)
        dispatch("retry", ["alpha"], 1.0)
        (inbox_dir("alpha") / env.name).rename(env)
        self._fail_to_the_cap(agent, env)

        assert read_dead_letters("alpha") == [env.name]
        assert dispatch("retry", ["alpha"], 1.0) == 0
        assert (inbox_dir("alpha") / env.name).is_file()

    def test_a_later_ack_leaves_it_in_trash_unrecorded(self, agent):
        from core import read_dead_letters
        from daemon import _settle_wake

        env = self._consume("alpha")
        self._fail_to_the_cap(agent, env)
        dispatch("retry", ["alpha"], 1.0)
        (inbox_dir("alpha") / env.name).rename(env)

        _settle_wake(agent, [env], 0)

        assert env.is_file()
        assert read_dead_letters("alpha") == []
        assert not list(inbox_dir("alpha").glob("*.json"))

    def test_a_swept_file_is_reported_and_is_not_an_error(self, agent, capsys):
        from core import read_dead_letters

        env = self._consume("alpha")
        self._fail_to_the_cap(agent, env)
        env.unlink()

        assert dispatch("retry", ["alpha"], 1.0) == 0

        assert "1 dead letter(s) gone from trash" in capsys.readouterr().out
        assert read_dead_letters("alpha") == []
        assert not list(inbox_dir("alpha").glob("*.json"))

    def test_each_dead_letter_is_its_own_marker_named_as_its_trash_file(self, agent):
        from core import dead_letters_dir

        env = self._consume("alpha")
        self._fail_to_the_cap(agent, env)

        assert [p.name for p in dead_letters_dir("alpha").iterdir()] == [env.name]
        assert (dead_letters_dir("alpha") / env.name).read_bytes() == b""

    def test_a_dead_letter_recorded_while_retry_runs_is_not_lost(self, agent, monkeypatch):
        import commands
        from core import read_dead_letters

        first = self._consume("alpha", "first")
        second = self._consume("alpha", "second")
        self._fail_to_the_cap(agent, first)
        real_list = commands.read_dead_letters

        def list_then_let_another_wake_cap(name):
            view = real_list(name)
            monkeypatch.setattr(commands, "read_dead_letters", real_list)
            self._fail_to_the_cap(agent, second)
            return view

        monkeypatch.setattr(commands, "read_dead_letters", list_then_let_another_wake_cap)
        assert dispatch("retry", ["alpha"], 1.0) == 0

        assert (inbox_dir("alpha") / first.name).is_file()
        assert read_dead_letters("alpha") == [second.name]
        assert second.is_file()

        assert dispatch("retry", ["alpha"], 1.0) == 0
        assert (inbox_dir("alpha") / second.name).is_file()
        assert read_dead_letters("alpha") == []

    def test_a_swept_file_loses_its_marker_on_retry(self, agent):
        from core import read_dead_letters

        env = self._consume("alpha")
        self._fail_to_the_cap(agent, env)
        env.unlink()
        dispatch("retry", ["alpha"], 1.0)

        assert read_dead_letters("alpha") == []

    def test_a_second_retry_finds_nothing(self, agent, capsys):
        env = self._consume("alpha")
        self._fail_to_the_cap(agent, env)
        dispatch("retry", ["alpha"], 1.0)
        capsys.readouterr()

        assert dispatch("retry", ["alpha"], 1.0) == 0

        assert "no dead letters" in capsys.readouterr().out
        assert len(list(inbox_dir("alpha").glob("*.json"))) == 1

    def test_a_node_with_a_backoff_and_a_dead_letter_gets_both_repaired(
        self, agent, capsys
    ):
        from daemon import _settle_wake

        dead = self._consume("alpha", "dead")
        self._fail_to_the_cap(agent, dead)
        live = self._consume("alpha", "live")
        _settle_wake(agent, [live], 3)
        assert read_wake_retry("alpha") is not None
        assert not _wake_retry_ready("alpha")

        assert dispatch("retry", ["alpha"], 1.0) == 0

        out = capsys.readouterr().out
        assert "backoff ended by `a8s retry`" in out
        assert "1 dead letter(s) returned to the inbox" in out
        assert read_wake_retry("alpha") is None
        assert sorted(f.name for f in inbox_dir("alpha").glob("*.json")) == sorted(
            [dead.name, live.name]
        )
