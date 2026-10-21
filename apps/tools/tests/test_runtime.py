"""The shared tool-node runtime: ACL, wake parsing, and reply via `a8s tell`."""
from __future__ import annotations

import json

import pytest

import _runtime as rt


class TestAllowed:
    @pytest.mark.parametrize("sender", ["alice", "ALICE", "Alice"])
    def test_case_insensitive_match(self, sender):
        assert rt.allowed(sender, "alice,bob")

    def test_whitespace_and_comma_separated(self):
        assert rt.allowed("bob", "alice, bob  carol")
        assert rt.allowed("carol", "alice, bob  carol")

    def test_not_listed_is_refused(self):
        assert not rt.allowed("mallory", "alice,bob")

    def test_empty_allowlist_refuses_everyone(self):
        assert not rt.allowed("alice", "")
        assert not rt.allowed("alice", "  , ")

    def test_senderless_is_refused(self):
        assert not rt.allowed("", "alice,bob")


class TestSplitAttachments:
    def test_plain_message_has_no_attachments(self):
        assert rt.split_attachments("ls -la") == ("ls -la", [])

    def test_peels_attached_file_lines_and_blank(self):
        msg = "run this\n\nATTACHED FILE: /tmp/a.txt\nATTACHED FILE: /tmp/b.txt"
        body, paths = rt.split_attachments(msg)
        assert body == "run this"
        assert paths == ["/tmp/a.txt", "/tmp/b.txt"]

    def test_multiline_body_preserved(self):
        assert rt.split_attachments("line1\nline2") == ("line1\nline2", [])


class TestSendReply:
    def _reply(self, outbox):
        return json.loads(next(outbox.glob("*.json")).read_text())

    def test_reply_writes_envelope_to_outbox(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        assert rt.send_reply("alice", "line one\nline two") == 0
        env = self._reply(outbox)
        assert env["to"] == "alice"
        assert env["content"] == "line one\nline two"

    def test_attachment_rides_along(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        payload = tmp_path / "report.txt"
        payload.write_text("big output")
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        assert rt.send_reply("alice", "see attached", (str(payload),)) == 0
        env = self._reply(outbox)
        assert [f["filename"] for f in env["files"]] == ["report.txt"]

    def test_no_recipient_sends_nothing(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        assert rt.send_reply("", "body") == 0
        assert list(outbox.glob("*.json")) == []


class TestPrintMode:
    def test_prints_reply_and_copies_attachments(self, tmp_path, monkeypatch, capsys):
        reply_dir = tmp_path / "replies"
        payload = tmp_path / "report.txt"
        payload.write_text("big output")
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(reply_dir))
        monkeypatch.delenv("TELL_OUTBOX_DIR", raising=False)
        assert rt.send_reply("alice", "hello", (str(payload),)) == 0
        out = capsys.readouterr().out
        assert out.startswith("to alice:\nhello\n")
        assert f"attached: {reply_dir / 'report.txt'}" in out
        assert (reply_dir / "report.txt").read_text() == "big output"

    def test_does_not_run_a8s_tell(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path))

        def boom(*a, **k):
            raise AssertionError("print mode must not spawn a8s tell")

        monkeypatch.setattr(rt.subprocess, "run", boom)
        assert rt.send_reply("alice", "hello") == 0
