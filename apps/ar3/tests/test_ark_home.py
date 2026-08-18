"""Tests for the shared config-home resolver — ar3's foundation layer.

One `app_home` resolves state roots for every product (A8S_HOME / R4T_HOME /
K7E_HOME all call through it). a8s alone carries a `legacy` fallback for its
pre-XDG `~/.a8s` layout; no other app passes one.
"""
from __future__ import annotations

from pathlib import Path

from ark.home import app_home


class TestOverride:
    def test_env_override_wins_outright(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        override = tmp_path / "explicit"
        assert app_home("a8s", str(override)) == override

    def test_override_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert app_home("a8s", "~/explicit") == tmp_path / "explicit"

    def test_blank_override_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert app_home("a8s", "   ") == tmp_path / ".config" / "a8s"

    def test_none_override_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert app_home("a8s", None) == tmp_path / ".config" / "a8s"


class TestXdg:
    def test_xdg_config_home_wins_over_default(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg-base"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        monkeypatch.setenv("HOME", str(tmp_path / "home-unused"))
        assert app_home("r4t", None) == xdg / "r4t"

    def test_defaults_to_home_dot_config_when_xdg_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert app_home("k7e", None) == tmp_path / ".config" / "k7e"


class TestLegacyFallback:
    def test_prefers_existing_xdg_dir_over_legacy(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        config = tmp_path / ".config" / "a8s"
        config.mkdir(parents=True)
        legacy = tmp_path / ".a8s"
        legacy.mkdir()
        assert app_home("a8s", None, legacy=legacy) == config

    def test_falls_back_to_legacy_when_xdg_dir_absent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        legacy = tmp_path / ".a8s"
        legacy.mkdir()
        assert app_home("a8s", None, legacy=legacy) == legacy

    def test_defaults_to_xdg_dir_when_neither_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        legacy = tmp_path / ".a8s"
        assert app_home("a8s", None, legacy=legacy) == tmp_path / ".config" / "a8s"

    def test_no_legacy_param_never_consults_a_dot_app_dir(self, tmp_path, monkeypatch):
        # r4t and k7e pass no `legacy` — a stray `~/.r4t` must never be found.
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".r4t").mkdir()
        assert app_home("r4t", None) == tmp_path / ".config" / "r4t"

    def test_override_still_wins_over_legacy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        override = tmp_path / "explicit"
        legacy = tmp_path / ".a8s"
        legacy.mkdir()
        assert app_home("a8s", str(override), legacy=legacy) == override


class TestDoesNotCreate:
    def test_returned_path_need_not_exist(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        home = app_home("a8s", None)
        assert isinstance(home, Path)
        assert not home.exists()
