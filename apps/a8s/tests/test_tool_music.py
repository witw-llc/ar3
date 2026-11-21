"""The bundled `tool-music` definition, driven through real routing (no model)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commands import cmd_add
from core import Participant
from daemon import attached_loop
from definitions import default_definition_path, resolve_definition_arg
from mailbox import _write_outbox, ensure_mailboxes
from registry import load_registry, save_registry


def _read_envelopes(directory: Path) -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(directory.glob("*.json"))]


@pytest.fixture
def music_pair(fake_home, tmp_path, monkeypatch):
    """Filedrop `a` and `b` plus `tool-music` node `mu` allowing `a`."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("AR3_MUSIC_CACHE", str(tmp_path / "music-cache"))
    roots = {name: tmp_path / name for name in ("a", "b", "mu")}
    for root in roots.values():
        root.mkdir()
    save_registry({
        "a": {"root": str(roots["a"]), "definition": str(default_definition_path("filedrop"))},
        "b": {"root": str(roots["b"]), "definition": str(default_definition_path("filedrop"))},
        "mu": {
            "root": str(roots["mu"]),
            "definition": str(default_definition_path("tool-music")),
            "vars": {"ALLOW": "a", "DURATION": "45"},
        },
    })
    for name, root in roots.items():
        ensure_mailboxes(Participant(name, root))
    return roots


def _route() -> None:
    attached_loop(["a", "b", "mu"], 0.05, single_pass=True)
    attached_loop(["a", "b", "mu"], 0.05, single_pass=True)


class TestAddBuiltin:
    def test_bare_name_resolves_bundled(self):
        assert resolve_definition_arg("tool-music") == default_definition_path("tool-music")

    def test_allow_var_is_mandatory_and_wake_is_long(self):
        defn = json.loads(default_definition_path("tool-music").read_text(encoding="utf-8"))
        assert "$ALLOW" in defn["invoke"]
        assert "$ALLOW?" not in defn["invoke"]
        assert defn["max_wake_seconds"] >= 3000

    def test_cmd_add_registers_tool_music(self, fake_home, tmp_path):
        root = tmp_path / "mu-node"
        root.mkdir()
        assert cmd_add(["mu-node", str(root), "tool-music", "--ALLOW=operator"]) == 0
        entry = load_registry()["mu-node"]
        assert entry["definition"] == str(default_definition_path("tool-music"))
        assert entry["vars"] == {"ALLOW": "operator"}


class TestRoutedMusic:
    def test_unallowed_sender_gets_no_reply(self, music_pair):
        _write_outbox("b", music_pair["b"], "mu", "help", [])
        _route()
        assert _read_envelopes(music_pair["b"] / ".inbox") == []

    def test_help_and_option_errors_reply_without_loading_a_model(self, music_pair, tmp_path):
        _write_outbox("a", music_pair["a"], "mu", "help", [])
        _route()
        replies = _read_envelopes(music_pair["a"] / ".inbox")
        assert len(replies) == 1
        assert "mu: tool-music" in replies[0]["content"]
        assert "--duration" in replies[0]["content"]

        _write_outbox("a", music_pair["a"], "mu", "--duration 700 epic orchestral", [])
        _route()
        replies = _read_envelopes(music_pair["a"] / ".inbox")
        assert len(replies) == 2
        assert "duration must be between 10 and 600 seconds" in replies[1]["content"]
        assert not (tmp_path / "music-cache").exists()
