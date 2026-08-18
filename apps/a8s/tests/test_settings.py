"""Tests for settings.py and `a8s config`."""
from __future__ import annotations

import json

import pytest

import settings as sm
from commands import cmd_config


class TestSettingsResolution:
    def test_default_when_file_and_env_missing(self, fake_home):
        assert sm.get_int("convo_max_rows") == 50_000
        assert sm.get_float("loop_interval") == 1.0
        assert sm.get_int("max_file_bytes") == 50 * 1024 * 1024
        assert sm.get_int("max_seen_ids") == 10000

    def test_settings_file_takes_precedence_over_env(self, fake_home, monkeypatch):
        monkeypatch.setenv("A8S_CONVO_MAX_ROWS", "50")
        sm.set_setting("convo_max_rows", 200)
        assert sm.get_int("convo_max_rows") == 200

    def test_env_used_when_key_absent_from_file(self, fake_home, monkeypatch):
        monkeypatch.setenv("A8S_LOOP_INTERVAL", "2.5")
        assert sm.get_float("loop_interval") == 2.5

    def test_unset_falls_back_to_env_then_default(self, fake_home, monkeypatch):
        sm.set_setting("convo_max_rows", 200)
        sm.unset_setting("convo_max_rows")
        monkeypatch.setenv("A8S_CONVO_MAX_ROWS", "75")
        assert sm.get_int("convo_max_rows") == 75
        monkeypatch.delenv("A8S_CONVO_MAX_ROWS", raising=False)
        assert sm.get_int("convo_max_rows") == 50_000

    def test_set_rejects_non_positive(self, fake_home):
        with pytest.raises(ValueError, match="positive"):
            sm.set_setting("convo_max_rows", 0)
        with pytest.raises(ValueError, match="positive"):
            sm.set_setting("loop_interval", 0)

    def test_persists_to_settings_json(self, fake_home):
        sm.set_setting("convo_max_rows", 1500)
        sm.set_setting("loop_interval", 0.5)
        raw = json.loads(sm.settings_path().read_text())
        assert raw["convo_max_rows"] == 1500
        assert raw["loop_interval"] == 0.5

    def test_cannot_set_read_only_knob(self, fake_home):
        with pytest.raises(KeyError):
            sm.set_setting("definition.invoke", ["echo"])

    def test_get_read_only_returns_catalog_default(self, fake_home):
        assert sm.get_setting("definition.files_ttl_hours") == 48


class TestRunnerLifecycleKnobs:
    """`wake_drain_grace_seconds`, `txlog_heartbeat_seconds`, and
    `watchdog_wedge_seconds` — the alive-but-deaf incident's three new knobs.
    The latter two allow 0 to mean "disabled", so they must be readable
    without `get_int`/`get_float`'s positive floor."""

    def test_defaults(self, fake_home):
        assert sm.get_setting("wake_drain_grace_seconds") == 5.0
        assert sm.get_setting("txlog_heartbeat_seconds") == 300.0
        assert sm.get_setting("watchdog_wedge_seconds") == 120.0

    def test_wake_drain_grace_seconds_rejects_non_positive(self, fake_home):
        with pytest.raises(ValueError, match="positive"):
            sm.set_setting("wake_drain_grace_seconds", 0)

    def test_heartbeat_and_watchdog_accept_zero_to_disable(self, fake_home):
        sm.set_setting("txlog_heartbeat_seconds", 0)
        sm.set_setting("watchdog_wedge_seconds", 0)
        assert sm.get_setting("txlog_heartbeat_seconds") == 0
        assert sm.get_setting("watchdog_wedge_seconds") == 0

    def test_heartbeat_and_watchdog_reject_negative(self, fake_home):
        with pytest.raises(ValueError, match="zero or positive"):
            sm.set_setting("txlog_heartbeat_seconds", -1)
        with pytest.raises(ValueError, match="zero or positive"):
            sm.set_setting("watchdog_wedge_seconds", -1)

    def test_env_vars_apply(self, fake_home, monkeypatch):
        monkeypatch.setenv("A8S_WAKE_DRAIN_GRACE_SECONDS", "0.5")
        monkeypatch.setenv("A8S_TXLOG_HEARTBEAT_SECONDS", "0")
        monkeypatch.setenv("A8S_WATCHDOG_WEDGE_SECONDS", "0.2")
        assert sm.get_setting("wake_drain_grace_seconds") == 0.5
        assert sm.get_setting("txlog_heartbeat_seconds") == 0.0
        assert sm.get_setting("watchdog_wedge_seconds") == 0.2


class TestCatalog:
    def test_lists_all_groups(self):
        groups = [label for label, _ in sm.list_catalog()]
        assert any("Machine-wide" in g for g in groups)
        assert any("definition" in g for g in groups)
        assert any("Registry" in g for g in groups)
        assert any("Network" in g for g in groups)

    def test_machine_knobs_are_writable(self):
        for knob in sm.KNOBS:
            if knob.group == "machine":
                assert knob.writable


class TestCmdConfig:
    def test_list_shows_catalog(self, fake_home, capsys):
        assert cmd_config([]) == 0
        out = capsys.readouterr().out
        assert "convo_max_rows: 50000" in out
        assert "definition.invoke" in out
        assert "registry.agents" in out
        assert "network.remotes" in out
        assert "TELL_OUTBOX_DIR" in out
        assert "remote.backoff_schedule" in out

    def test_get_set_unset_writable(self, fake_home, capsys):
        assert cmd_config(["set", "convo_max_rows", "2500"]) == 0
        assert "convo_max_rows=2500" in capsys.readouterr().out
        capsys.readouterr()
        assert cmd_config(["get", "convo_max_rows"]) == 0
        assert capsys.readouterr().out.strip() == "2500"
        assert cmd_config(["unset", "convo_max_rows"]) == 0
        assert "effective 50000" in capsys.readouterr().out

    def test_get_read_only_knob(self, fake_home, capsys):
        assert cmd_config(["get", "definition.batch.limit"]) == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "5"
        assert "batch" in captured.err.lower()

    def test_unknown_setting(self, fake_home, capsys):
        assert cmd_config(["get", "bogus"]) == 1
        assert "unknown setting" in capsys.readouterr().err
