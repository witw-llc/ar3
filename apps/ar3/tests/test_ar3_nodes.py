"""Tests for `ar3.nodes`, the read-only view of the a8s registry that tools and the front door share.

The registry is read by node name, from the home a8s resolves, and a read fails closed: a registry that is
missing, unreadable or malformed holds no node. A tool decides which directory is a node's from here and
nowhere else.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ar3 import nodes
from ar3.home import app_home


def _registry(home, agents=None, aliases=None, raw=None):
    home.mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else json.dumps({"agents": agents or {}, "aliases": aliases or {}, "namespaces": {}})
    (home / "a8s.json").write_text(text, encoding="utf-8")


def _node(root, definition="/repo/apps/a8s/definitions/tool-browser.json"):
    return {"root": str(root), "definition": definition, "vars": {}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("A8S_HOME", str(tmp_path / "a8s"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path / "a8s"


class TestHome:
    def test_a8s_home_names_the_registry_home_for_the_override(self, home):
        assert nodes.a8s_home() == home

    def test_it_resolves_as_the_shared_resolver_does_without_an_override(self, tmp_path, monkeypatch):
        monkeypatch.delenv("A8S_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert nodes.a8s_home() == app_home("a8s", None, legacy=tmp_path / ".a8s") == tmp_path / "xdg" / "a8s"

    def test_it_falls_back_to_the_legacy_home_that_a8s_falls_back_to(self, tmp_path, monkeypatch):
        monkeypatch.delenv("A8S_HOME", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".a8s").mkdir()
        assert nodes.a8s_home() == tmp_path / ".a8s"

    def test_a_node_is_read_from_the_home_the_override_names(self, home, tmp_path):
        _registry(home, {"garmin": _node(tmp_path / "garmin")})
        assert nodes.node_root("garmin", "tool-browser") == tmp_path / "garmin"

    def test_a_node_in_another_home_is_not_found(self, home, tmp_path, monkeypatch):
        _registry(home, {"garmin": _node(tmp_path / "garmin")})
        monkeypatch.setenv("A8S_HOME", str(tmp_path / "elsewhere"))
        assert nodes.node_root("garmin", "tool-browser") is None


class TestRead:
    def test_it_returns_the_agents_and_the_aliases(self, home, tmp_path):
        _registry(home, {"a": _node(tmp_path)}, {"team": ["a"]})
        agents, aliases = nodes.read()
        assert list(agents) == ["a"] and aliases == {"team": ["a"]}

    @pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '"text"', "{"])
    def test_a_registry_that_is_not_an_object_holds_nothing(self, home, raw):
        _registry(home, raw=raw)
        assert nodes.read() == ({}, {})

    def test_a_missing_registry_holds_nothing(self, home):
        assert nodes.read() == ({}, {})

    def test_a_registry_that_cannot_be_read_holds_nothing(self, home):
        home.mkdir()
        (home / "a8s.json").mkdir()
        assert nodes.read() == ({}, {})

    def test_sections_of_the_wrong_shape_read_as_empty(self, home):
        _registry(home, raw=json.dumps({"agents": ["x"], "aliases": "y"}))
        assert nodes.read() == ({}, {})

    def test_a_registry_nested_too_deep_for_the_parser_holds_nothing(self, home):
        _registry(home, raw="[" * 100000)
        assert nodes.read() == ({}, {})

    def test_a_machine_with_no_home_directory_holds_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("A8S_HOME", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        def no_home():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "home", staticmethod(no_home))
        assert nodes.read() == ({}, {})
        assert nodes.node_root("garmin", "tool-browser") is None

    def test_an_agent_entry_that_is_not_an_object_is_left_out(self, home, tmp_path):
        _registry(home, {"good": _node(tmp_path), "bad": "x", "worse": None})
        assert list(nodes.read()[0]) == ["good"]


class TestDefinitionStem:
    @pytest.mark.parametrize("raw, stem", [
        ("/repo/apps/a8s/definitions/tool-browser.json", "tool-browser"),
        ("tool-browser", "tool-browser"),
        ("tool-browser.json", "tool-browser"),
        ("~/.config/a8s/definitions/mine.json", "mine"),
        (None, "default"),
        ("", "default"),
    ])
    def test_it_is_the_last_part_without_json(self, raw, stem):
        assert nodes.definition_stem(raw) == stem


class TestNodeRoot:
    def test_it_is_the_registered_root_of_a_node_that_runs_the_definition(self, home, tmp_path):
        _registry(home, {"garmin": _node(tmp_path / "garmin")})
        assert nodes.node_root("garmin", "tool-browser") == tmp_path / "garmin"

    def test_a_bare_definition_name_counts(self, home, tmp_path):
        _registry(home, {"garmin": _node(tmp_path / "garmin", definition="tool-browser")})
        assert nodes.node_root("garmin", "tool-browser") == tmp_path / "garmin"

    def test_a_node_of_another_kind_has_no_root_for_this_definition(self, home, tmp_path):
        _registry(home, {"shell": _node(tmp_path / "shell", definition="/repo/definitions/tool-command-shell.json")})
        assert nodes.node_root("shell", "tool-browser") is None
        assert nodes.node_root("shell", "tool-command-shell") == tmp_path / "shell"

    def test_a_node_with_no_definition_runs_default(self, home, tmp_path):
        _registry(home, {"plain": {"root": str(tmp_path)}})
        assert nodes.node_root("plain", "tool-browser") is None
        assert nodes.node_root("plain", "default") == tmp_path

    def test_an_unregistered_name_has_no_root(self, home, tmp_path):
        _registry(home, {"garmin": _node(tmp_path)})
        assert nodes.node_root("nobody", "tool-browser") is None

    def test_the_name_is_matched_exactly(self, home, tmp_path):
        _registry(home, {"garmin": _node(tmp_path)})
        assert nodes.node_root("Garmin", "tool-browser") is None
        assert nodes.node_root("garmin ", "tool-browser") is None
        assert nodes.node_root("", "tool-browser") is None

    def test_an_alias_is_not_a_node(self, home, tmp_path):
        _registry(home, {"garmin": _node(tmp_path)}, {"team": ["garmin"]})
        assert nodes.node_root("team", "tool-browser") is None

    @pytest.mark.parametrize("root", ["", "relative/dir", ".", "~/node", None, 7, ["/abs"]])
    def test_a_root_that_is_not_an_absolute_path_is_none(self, home, root):
        _registry(home, {"garmin": {"root": root, "definition": "tool-browser"}})
        assert nodes.node_root("garmin", "tool-browser") is None

    def test_a_missing_root_is_none(self, home):
        _registry(home, {"garmin": {"definition": "tool-browser"}})
        assert nodes.node_root("garmin", "tool-browser") is None

    def test_it_does_not_look_at_the_working_directory_or_the_environment(self, home, tmp_path, monkeypatch):
        (tmp_path / "recipes").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("A8S_TURN_RECIPIENT", "garmin")
        assert nodes.node_root("garmin", "tool-browser") is None
        _registry(home, {"garmin": _node(tmp_path / "elsewhere")})
        assert nodes.node_root("garmin", "tool-browser") == tmp_path / "elsewhere"

    def test_it_writes_nothing(self, home, tmp_path):
        _registry(home, {"garmin": _node(tmp_path)})
        before = sorted(os.listdir(home)), (home / "a8s.json").stat().st_mtime_ns
        nodes.node_root("garmin", "tool-browser")
        nodes.read()
        assert (sorted(os.listdir(home)), (home / "a8s.json").stat().st_mtime_ns) == before
        assert isinstance(nodes.a8s_home(), Path)
