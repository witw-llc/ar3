"""Tests for `a8s retry <name>` — end a failed wake's backoff, make parked sends due, and return dead letters."""
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


class TestParkedSendRetry:
    """An outbound send that failed to publish waits in pending/ behind a sidecar."""

    @pytest.fixture
    def node(self, fake_home, tmp_path):
        from core import Participant
        from mailbox import ensure_mailboxes

        root = tmp_path / "agent-root"
        root.mkdir()
        save_registry({"alpha": {"root": str(root)}})
        ensure_mailboxes(Participant("alpha", root))
        return "alpha"

    @staticmethod
    def _park(name, *, next_attempt, attempts=2, remotes=("mqtt-1",), sidecar=True):
        import json

        from ar3.ulid import new as new_ulid
        from core import pending_dir, retry_sidecar_path

        msg_id = new_ulid()
        path = pending_dir(name) / f"{msg_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": msg_id, "to": "beta", "content": "hi", "files": []}))
        if sidecar:
            retry_sidecar_path(path).write_text(json.dumps({
                "attempts": attempts,
                "next_attempt": next_attempt,
                "succeeded_remotes": list(remotes),
                "local_delivered": True,
                "uploaded": {"a.txt": {"svc": "https://example.test/a"}},
            }))
        return path

    @staticmethod
    def _read(path):
        import json

        from core import retry_sidecar_path

        return json.loads(retry_sidecar_path(path).read_text())

    @staticmethod
    def _in_an_hour():
        return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    @staticmethod
    def _marker(name):
        from mailbox import retry_request_path

        return retry_request_path(name)

    @staticmethod
    def _pass(name, tmp_path, *, accept=("ok",), remotes=("ok", "bad"), calls=None):
        """One router pass over `name`'s pending sends with a fake remote that
        accepts only the ids in `accept`. `calls` collects (message id, ids
        already succeeded) per publish."""
        from core import Participant
        from mailbox import route_outboxes

        sender = Participant(name, tmp_path / "agent-root")

        def publish(msg, sender_name, prev, attempts):
            if calls is not None:
                calls.append((msg["id"], list(prev)))
            return list(prev) + [r for r in remotes if r in accept and r not in prev]

        return route_outboxes(
            [sender], all_agents=[sender],
            publish_remotes=publish, configured_remote_ids=list(remotes),
        )

    def test_retry_drops_a_marker_and_never_writes_a_sidecar(self, node):
        from core import retry_sidecar_path

        path = self._park(node, next_attempt=self._in_an_hour())
        sidecar = retry_sidecar_path(path)
        before = (sidecar.read_bytes(), sidecar.stat().st_mtime_ns)

        assert dispatch("retry", [node], 1.0) == 0

        assert self._marker(node).is_file()
        assert (sidecar.read_bytes(), sidecar.stat().st_mtime_ns) == before

    def test_a_pass_with_the_marker_runs_a_backed_off_send_and_the_router_writes_the_sidecar(
        self, node, tmp_path
    ):
        path = self._park(node, next_attempt=self._in_an_hour(), attempts=2, remotes=())
        dispatch("retry", [node], 1.0)
        calls = []

        self._pass(node, tmp_path, calls=calls)

        assert len(calls) == 1
        assert not self._marker(node).exists()
        after = self._read(path)
        assert after["attempts"] == 3
        assert after["succeeded_remotes"] == ["ok"]
        assert after["next_attempt"] != ""
        assert after["uploaded"] == {"a.txt": {"svc": "https://example.test/a"}}

    def test_a_pass_without_the_marker_still_honors_next_attempt(self, node, tmp_path):
        path = self._park(node, next_attempt=self._in_an_hour(), remotes=())
        before = self._read(path)
        calls = []

        self._pass(node, tmp_path, calls=calls)

        assert calls == []
        assert self._read(path) == before

    def test_a_marker_left_by_a_stopped_node_is_honored_on_the_first_pass(self, node, tmp_path):
        path = self._park(node, next_attempt=self._in_an_hour(), remotes=())
        self._marker(node).touch()
        calls = []

        self._pass(node, tmp_path, calls=calls)

        assert len(calls) == 1
        assert not self._marker(node).exists()
        self._pass(node, tmp_path, calls=calls)
        assert len(calls) == 1  # one request forces one pass, not every pass
        assert self._read(path)["next_attempt"] != ""

    def test_the_marker_is_not_a_pending_message(self, node, tmp_path):
        self._marker(node).touch()

        assert self._pass(node, tmp_path) == 0

        assert not self._marker(node).exists()

    def test_no_parked_send_means_no_marker(self, node):
        self._park(node, next_attempt="")
        self._park(node, next_attempt=self._in_an_hour(), sidecar=False)

        assert dispatch("retry", [node], 1.0) == 0

        assert not self._marker(node).exists()

    def test_a_router_save_between_the_count_and_the_pass_is_not_lost(self, node, tmp_path):
        """The lost-update race: the router saves progress after retry counted
        the send. Retry writes no sidecar, so the saved progress stands and the
        healthy remote and the local inbox are not served twice."""
        import json

        from core import Participant, inbox_dir, retry_sidecar_path
        from mailbox import ensure_mailboxes

        beta_root = tmp_path / "beta-root"
        beta_root.mkdir()
        save_registry({"alpha": {"root": str(tmp_path / "agent-root")}, "beta": {"root": str(beta_root)}})
        ensure_mailboxes(Participant("beta", beta_root))
        path = self._park(node, next_attempt=self._in_an_hour(), attempts=2, remotes=())
        assert dispatch("retry", [node], 1.0) == 0

        sidecar = json.loads(retry_sidecar_path(path).read_text())
        sidecar.update(
            attempts=3, succeeded_remotes=["ok"], local_delivered=True,
            next_attempt=self._in_an_hour(),
        )
        retry_sidecar_path(path).write_text(json.dumps(sidecar))
        calls = []

        self._pass(node, tmp_path, calls=calls)

        assert [prev for _, prev in calls] == [["ok"]]
        after = self._read(path)
        assert after["succeeded_remotes"] == ["ok"]
        assert after["local_delivered"] is True
        assert after["attempts"] == 4
        assert list(inbox_dir("beta").glob("*.json")) == []

    def test_a_send_already_due_is_not_rewritten_or_counted(self, node, capsys):
        from core import retry_sidecar_path

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        due = self._park(node, next_attempt=past)
        empty = self._park(node, next_attempt="")
        marks = {p: retry_sidecar_path(p).stat().st_mtime_ns for p in (due, empty)}

        assert dispatch("retry", [node], 1.0) == 0

        assert {p: retry_sidecar_path(p).stat().st_mtime_ns for p in (due, empty)} == marks
        out = capsys.readouterr().out
        assert "pending send(s)" not in out
        assert "no pending send is parked" in out

    def test_a_pending_file_without_a_sidecar_is_untouched_and_not_counted(self, node, capsys):
        from core import retry_sidecar_path

        path = self._park(node, next_attempt="", sidecar=False)
        text = path.read_text()

        assert dispatch("retry", [node], 1.0) == 0

        assert path.read_text() == text
        assert not retry_sidecar_path(path).exists()
        assert "pending send(s)" not in capsys.readouterr().out

    def test_the_line_counts_the_sends_and_says_when(self, node, capsys):
        for _ in range(2):
            self._park(node, next_attempt=self._in_an_hour())

        dispatch("retry", [node], 1.0)

        assert capsys.readouterr().out.strip().splitlines() == [
            "[alpha] 2 pending send(s) due now; wait for the node to start"
        ]

    def test_a_running_node_wakes_on_the_next_pass(self, node, capsys, monkeypatch):
        import commands

        monkeypatch.setattr(commands, "_read_handler_pid", lambda name: 4242)
        self._park(node, next_attempt=self._in_an_hour())

        dispatch("retry", [node], 1.0)

        assert "[alpha] 1 pending send(s) due now; wake on the next pass" in capsys.readouterr().out

    def test_the_all_clear_names_all_three_things(self, node, capsys):
        assert dispatch("retry", [node], 1.0) == 0
        assert capsys.readouterr().out.strip() == (
            "alpha: no wake is waiting on a backoff, no pending send is parked, and no dead letters"
        )

    def test_the_lines_come_in_backoff_then_sends_then_dead_letters_order(
        self, backed_off, capsys
    ):
        import json

        from core import mark_dead_letter, pending_dir, retry_sidecar_path, trash_dir

        name = backed_off
        pending_dir(name).mkdir(parents=True, exist_ok=True)
        trash_dir(name).mkdir(parents=True, exist_ok=True)
        path = self._park(name, next_attempt=self._in_an_hour())
        assert retry_sidecar_path(path).is_file()
        letter = trash_dir(name) / "01ARZ3NDEKTSV4RRFFQ69G5FAV.json"
        letter.write_text(json.dumps({"id": letter.stem, "to": name, "content": "x", "files": []}))
        mark_dead_letter(name, letter.name)

        dispatch("retry", [name], 1.0)

        lines = capsys.readouterr().out.strip().splitlines()
        assert [("backoff ended" in l, "pending send(s)" in l, "dead letter(s)" in l) for l in lines] == [
            (True, False, False), (False, True, False), (False, False, True),
        ]
