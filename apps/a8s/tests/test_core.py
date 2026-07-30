"""Tests for core helpers added for the remote-routing PR (issue #63)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core import (
    BACKOFF_SCHEDULE,
    MAX_ATTEMPTS,
    MAX_SEEN_IDS,
    _a8s_dir,
    agent_dir,
    last_active_path,
    network_config_path,
    pending_dir,
    read_last_active,
    registry_path,
    retry_sidecar_path,
    seen_ids_path,
    touch_last_active,
)


class TestPendingDir:
    def test_under_agent_dir(self, fake_home):
        p = pending_dir("claude")
        assert p == agent_dir("claude") / "pending"
        # Sibling of inbox / inbox.tmp / trash — same parent.
        assert p.parent == agent_dir("claude")


class TestRetrySidecarPath:
    def test_appends_retry_suffix(self):
        f = Path("/tmp/01HX.json")
        assert retry_sidecar_path(f) == Path("/tmp/01HX.json.retry")


class TestSeenIdsPath:
    def test_cluster_wide(self, fake_home):
        # Single file under ~/.a8s/, not per-agent.
        p = seen_ids_path()
        assert p.parent == fake_home / ".a8s"
        assert p.name == "seen-ids"


class TestNetworkConfigPath:
    def test_under_a8s(self, fake_home):
        p = network_config_path()
        assert p == fake_home / ".a8s" / "network.json"


class TestBackoffConstants:
    def test_schedule_is_strictly_increasing(self):
        for a, b in zip(BACKOFF_SCHEDULE, BACKOFF_SCHEDULE[1:]):
            assert a < b

    def test_first_step_is_30s(self):
        assert BACKOFF_SCHEDULE[0] == 30

    def test_last_step_is_24h(self):
        assert BACKOFF_SCHEDULE[-1] == 86400

    def test_max_attempts_matches_schedule_length(self):
        assert MAX_ATTEMPTS == len(BACKOFF_SCHEDULE)


class TestSeenIdsCap:
    def test_cap_is_reasonable(self):
        # 26-char ULID + newline = 27 bytes per row; 10k rows ≈ 270 KiB.
        # Sanity: not zero, not absurd.
        assert 1000 <= MAX_SEEN_IDS <= 1_000_000


class TestA8sHomeOverride:
    def test_default_under_home(self, fake_home):
        assert _a8s_dir() == fake_home / ".a8s"

    def test_prefers_config_a8s_when_present(self, fake_home, monkeypatch):
        from core import resolve_a8s_home

        config = fake_home / ".config" / "a8s"
        config.mkdir(parents=True)
        assert resolve_a8s_home() == config

    def test_uses_legacy_when_only_dot_a8s(self, fake_home):
        from core import resolve_a8s_home

        assert (fake_home / ".a8s").is_dir()
        assert not (fake_home / ".config" / "a8s").exists()
        assert resolve_a8s_home() == fake_home / ".a8s"

    def test_new_install_defaults_to_config_a8s(self, tmp_path, monkeypatch):
        from core import resolve_a8s_home

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("A8S_HOME", raising=False)
        monkeypatch.delenv("USERPROFILE", raising=False)
        assert resolve_a8s_home() == tmp_path / ".config" / "a8s"
        assert _a8s_dir() == tmp_path / ".config" / "a8s"
        assert (tmp_path / ".config" / "a8s").is_dir()

    def test_env_var_overrides(self, fake_home, tmp_path, monkeypatch):
        sandbox = tmp_path / "sandbox-a8s"
        monkeypatch.setenv("A8S_HOME", str(sandbox))
        assert _a8s_dir() == sandbox
        assert sandbox.is_dir()

    def test_registry_path_honors_override(self, fake_home, tmp_path, monkeypatch):
        sandbox = tmp_path / "sandbox-a8s"
        monkeypatch.setenv("A8S_HOME", str(sandbox))
        assert registry_path() == sandbox / "a8s.json"

    def test_agent_dir_honors_override(self, fake_home, tmp_path, monkeypatch):
        sandbox = tmp_path / "sandbox-a8s"
        monkeypatch.setenv("A8S_HOME", str(sandbox))
        assert agent_dir("claude") == sandbox / "agents" / "claude"


class TestLastActive:
    def test_path_under_agent_dir(self, fake_home):
        assert last_active_path("claude") == agent_dir("claude") / "last-active"

    def test_read_returns_none_when_missing(self, fake_home):
        assert read_last_active("claude") is None

    def test_touch_then_read_round_trip(self, fake_home):
        agent_dir("claude").mkdir(parents=True, exist_ok=True)
        ts = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
        touch_last_active("claude", ts)
        assert read_last_active("claude") == ts

    def test_touch_writes_now_by_default(self, fake_home):
        agent_dir("claude").mkdir(parents=True, exist_ok=True)
        before = datetime.now(timezone.utc)
        touch_last_active("claude")
        after = datetime.now(timezone.utc)
        got = read_last_active("claude")
        assert got is not None
        assert before <= got <= after

    def test_read_handles_unparseable_content(self, fake_home):
        d = agent_dir("claude")
        d.mkdir(parents=True, exist_ok=True)
        last_active_path("claude").write_text("not-a-date")
        assert read_last_active("claude") is None
