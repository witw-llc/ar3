"""The bundled `tool-command-shell` definition, driven through real routing.

`a8s add <name> <dir> tool-command-shell --ALLOW=...` yields a working node:
the definition invokes the a8s-sibling handler at `$A8S_DIR/../tools/`, which
runs the command and stages a reply through the same outbox path `tell` uses.
One tell to the node proves outbox -> router -> wake -> command -> reply.

The ACL is the required `$ALLOW` var: a node added without it is unresolved and
never wakes. A refused sender gets no reply.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from commands import cmd_add
from core import Participant, outbox_dir
from daemon import attached_loop
from definitions import default_definition_path, resolve_definition_arg
from mailbox import _write_outbox, ensure_mailboxes
from registry import load_registry, save_registry

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the handler shells out to a POSIX sh in these tests"
)


def _read_envelopes(directory: Path) -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(directory.glob("*.json"))]


@pytest.fixture
def shell_pair(fake_home, tmp_path):
    """A filedrop sender `a` and a shell tool node `sh`, ALLOW listing `a`."""
    a_root = tmp_path / "a"
    sh_root = tmp_path / "sh"
    a_root.mkdir()
    sh_root.mkdir()
    save_registry({
        "a": {"root": str(a_root), "definition": str(default_definition_path("filedrop"))},
        "sh": {
            "root": str(sh_root),
            "definition": str(default_definition_path("tool-command-shell")),
            "vars": {"ALLOW": "a"},
        },
    })
    ensure_mailboxes(Participant("a", a_root))
    ensure_mailboxes(Participant("sh", sh_root))
    return a_root, sh_root


class TestAddBuiltin:
    def test_bare_name_resolves_bundled(self, fake_home, tmp_path):
        root = tmp_path / "probe"
        root.mkdir()
        assert cmd_add(["probe", str(root), "tool-command-shell", "--ALLOW=a"]) == 0
        assert load_registry()["probe"]["definition"] == str(
            default_definition_path("tool-command-shell")
        )

    def test_definition_requires_allow_var(self):
        # `$ALLOW` has no `?`, so an unset ALLOW makes the node unresolved and
        # it never wakes — the ACL is mandatory by construction.
        path = resolve_definition_arg("tool-command-shell")
        argv = json.loads(path.read_text())["invoke"]
        assert "$ALLOW" in argv
        assert "$ALLOW?" not in argv


class TestWake:
    def test_allowed_sender_runs_command_and_replies(self, shell_pair):
        a_root, sh_root = shell_pair
        _write_outbox("a", a_root, "sh", "echo round-trip-ok", [])

        assert attached_loop(["a", "sh"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(sh_root))
        assert len(replies) == 1
        assert replies[0]["to"] == "a"
        assert "round-trip-ok" in replies[0]["content"]
        assert replies[0]["content"].startswith("$ echo round-trip-ok")

        assert attached_loop(["a", "sh"], 0.1, single_pass=True) == 0
        delivered = _read_envelopes(a_root / ".inbox")
        assert len(delivered) == 1
        assert delivered[0]["from"] == "sh"
        assert "round-trip-ok" in delivered[0]["content"]

    def test_refused_sender_gets_no_reply(self, fake_home, tmp_path):
        a_root = tmp_path / "a"
        sh_root = tmp_path / "sh"
        a_root.mkdir()
        sh_root.mkdir()
        save_registry({
            "a": {"root": str(a_root), "definition": str(default_definition_path("filedrop"))},
            "sh": {
                "root": str(sh_root),
                "definition": str(default_definition_path("tool-command-shell")),
                "vars": {"ALLOW": "someone-else"},
            },
        })
        ensure_mailboxes(Participant("a", a_root))
        ensure_mailboxes(Participant("sh", sh_root))
        _write_outbox("a", a_root, "sh", "echo should-not-run", [])

        assert attached_loop(["a", "sh"], 0.1, single_pass=True) == 0
        assert _read_envelopes(outbox_dir(sh_root)) == []


class TestHelpWake:
    @pytest.mark.parametrize("spelling", ["help", "/help", "--help", "/?"])
    def test_help_answers_the_usage_and_runs_nothing(self, fake_home, shell_pair, spelling):
        a_root, sh_root = shell_pair
        _write_outbox("a", a_root, "sh", spelling, [])

        assert attached_loop(["a", "sh"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(sh_root))
        assert len(replies) == 1
        assert replies[0]["to"] == "a"
        assert replies[0]["content"].startswith("sh: tool-command-shell \u2014 usage")
        assert "README: apps/tools/command_shell/README.md" in replies[0]["content"]

    def test_help_me_is_a_command_not_help(self, fake_home, shell_pair):
        a_root, sh_root = shell_pair
        _write_outbox("a", a_root, "sh", "echo help", [])

        assert attached_loop(["a", "sh"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(sh_root))
        assert replies[0]["content"].startswith("$ echo help")
