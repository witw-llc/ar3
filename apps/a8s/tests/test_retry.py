"""Tests for `a8s retry <name>` — end the backoff a failed wake armed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cli import dispatch
from core import agent_log_path, read_wake_retry, write_wake_retry
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
