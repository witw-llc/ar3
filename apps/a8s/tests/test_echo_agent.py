"""Tests for the built-in echo definition and its wake handler.

`a8s add <name> <dir> echo` yields a working node with nothing pre-placed
in the agent dir: `definitions/echo.json` invokes the a8s-owned
`echo_agent.py` via `$A8S_DIR`, which stages a reply to the sender through
the same outbox path `tell` uses. One tell to an echo node proves the whole
path outbox -> router -> wake -> reply.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import echo_agent
from commands import cmd_add
from core import Participant, TELL_OUTBOX_DIR_ENV, outbox_dir
from daemon import attached_loop
from definitions import default_definition_path
from mailbox import _write_outbox, ensure_mailboxes
from registry import load_registry, save_registry


def _read_envelopes(directory: Path) -> list[dict]:
    return [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted(directory.glob("*.json"))
    ]


@pytest.fixture
def echo_pair(fake_home, tmp_path):
    """A filedrop sender seat `a` and an echo node `e`, both registered."""
    a_root = tmp_path / "a"
    e_root = tmp_path / "e"
    a_root.mkdir()
    e_root.mkdir()
    save_registry({
        "a": {
            "root": str(a_root),
            "definition": str(default_definition_path("filedrop")),
        },
        "e": {
            "root": str(e_root),
            "definition": str(default_definition_path("echo")),
        },
    })
    ensure_mailboxes(Participant("a", a_root))
    ensure_mailboxes(Participant("e", e_root))
    return a_root, e_root


class TestAddBuiltinEcho:
    def test_bare_echo_kind_resolves_bundled(self, fake_home, tmp_path):
        root = tmp_path / "probe"
        root.mkdir()
        assert cmd_add(["probe", str(root), "echo"]) == 0
        assert load_registry()["probe"]["definition"] == str(
            default_definition_path("echo")
        )

    def test_agent_dir_needs_no_handler_binary(self, fake_home, tmp_path):
        root = tmp_path / "probe"
        root.mkdir()
        assert cmd_add(["probe", str(root), "echo"]) == 0
        assert not (root / "echo-agent-cli").exists()
        assert not (root / "echo_agent.py").exists()


class TestEchoWake:
    def test_reply_stages_to_sender_with_same_content(self, echo_pair):
        a_root, e_root = echo_pair
        _write_outbox("a", a_root, "e", "ping 123\nline two", [])

        assert attached_loop(["a", "e"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(e_root))
        assert len(replies) == 1
        assert replies[0]["to"] == "a"
        assert replies[0]["content"] == "ping 123\nline two"
        assert replies[0]["files"] == []

        assert attached_loop(["a", "e"], 0.1, single_pass=True) == 0
        delivered = _read_envelopes(a_root / ".inbox")
        assert len(delivered) == 1
        assert delivered[0]["from"] == "e"
        assert delivered[0]["content"] == "ping 123\nline two"

    def test_batch_of_messages_replies_per_message(self, echo_pair):
        a_root, e_root = echo_pair
        bodies = ["first", "second", "third"]
        for body in bodies:
            _write_outbox("a", a_root, "e", body, [])

        assert attached_loop(["a", "e"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(e_root))
        assert sorted(r["content"] for r in replies) == sorted(bodies)
        assert all(r["to"] == "a" for r in replies)

    def test_attachment_acknowledged_by_name_not_echoed(self, echo_pair):
        a_root, e_root = echo_pair
        payload = a_root / "avatar.jpg"
        payload.write_text("bytes")
        _write_outbox(
            "a", a_root, "e", "see attached", [],
            attachment_sources=[payload],
        )

        assert attached_loop(["a", "e"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(e_root))
        assert len(replies) == 1
        assert replies[0]["content"] == (
            "see attached\nattachments received: avatar.jpg"
        )
        assert replies[0]["files"] == []


class TestEchoHandler:
    def test_split_message_without_attachments(self):
        body, names = echo_agent.split_message("plain\nbody")
        assert body == "plain\nbody"
        assert names == []

    def test_split_message_peels_attached_file_lines(self):
        message = (
            "see attached\n\n"
            "ATTACHED FILE: /tmp/x/01ABC/one.txt\n"
            "ATTACHED FILE: /tmp/x/01ABC/two.png"
        )
        body, names = echo_agent.split_message(message)
        assert body == "see attached"
        assert names == ["one.txt", "two.png"]

    def test_senderless_wake_stages_nothing(self, tmp_path, monkeypatch):
        outbox = tmp_path / ".outbox"
        outbox.mkdir()
        monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
        assert echo_agent.main(["", "hello"]) == 0
        assert _read_envelopes(outbox) == []

    def test_main_writes_reply_envelope(self, tmp_path, monkeypatch):
        outbox = tmp_path / ".outbox"
        outbox.mkdir()
        monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
        assert echo_agent.main(["alice", "hello"]) == 0
        replies = _read_envelopes(outbox)
        assert len(replies) == 1
        assert replies[0]["to"] == "alice"
        assert replies[0]["content"] == "hello"
