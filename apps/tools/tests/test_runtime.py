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
        assert out.startswith("hello\n")
        assert "to alice" not in out
        assert f"artifact: {reply_dir / 'report.txt'}" in out
        assert (reply_dir / "report.txt").read_text() == "big output"

    def test_does_not_run_a8s_tell(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path))

        def boom(*a, **k):
            raise AssertionError("print mode must not spawn a8s tell")

        monkeypatch.setattr(rt.subprocess, "run", boom)
        assert rt.send_reply("alice", "hello") == 0


class TestHelp:
    @pytest.mark.parametrize("body", [
        "help", "/help", "--help", "/?", "HELP", "/Help", "  help  ", "help\n", "\n/?\n",
        "help\n\nATTACHED FILE: /tmp/x",
    ])
    def test_the_four_spellings_are_help(self, body):
        assert rt.is_help(body)

    @pytest.mark.parametrize("body", [
        "", "   ", "help me open a page", "/help recipe", "helpful", "go\nhelp", "help\nhelp",
        "?", "-h", "ATTACHED FILE: /tmp/x",
    ])
    def test_anything_else_is_not_help(self, body):
        assert not rt.is_help(body)

    def test_help_reply_names_the_node_the_tool_and_the_readme(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("# x\n\n## Usage\n\nDo a thing.\n\n## State\n\nnone\n")
        text = rt.help_reply("tool-x", "mynode", readme, "apps/tools/x/README.md")
        lines = text.splitlines()
        assert lines[0] == "mynode: tool-x \u2014 usage"
        assert lines[1] == ""
        assert lines[2] == "Do a thing."
        assert "none" not in text
        assert lines[-1] == "README: apps/tools/x/README.md"

    def test_help_reply_puts_extra_before_the_readme_line(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("## Usage\n\nDo a thing.\n")
        text = rt.help_reply("tool-x", "n", readme, "p/README.md", extra="Extra line.")
        assert text == "n: tool-x \u2014 usage\n\nDo a thing.\n\nExtra line.\n\nREADME: p/README.md"

    def test_help_reply_says_when_the_section_is_missing(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("# x\n\n## State\n\nnone\n")
        text = rt.help_reply("tool-x", "n", readme, "p/README.md")
        assert text == 'n: tool-x \u2014 usage\n\nno "## Usage" section in p/README.md\n\nREADME: p/README.md'
        gone = rt.help_reply("tool-x", "n", tmp_path / "nope.md", "p/README.md")
        assert 'no "## Usage" section in p/README.md' in gone


class TestUsageFromReadme:
    def _write(self, tmp_path, text):
        path = tmp_path / "README.md"
        path.write_text(text)
        return path

    def test_the_section_is_found_and_trimmed(self, tmp_path):
        path = self._write(tmp_path, "# T\n\n## Needs\n\nx\n\n## Usage\n\nLine one.\nLine two.\n\n## State\n\ny\n")
        assert rt.usage_from_readme(path) == "Line one.\nLine two."

    def test_the_section_at_the_end_of_the_file(self, tmp_path):
        path = self._write(tmp_path, "# T\n\n## Usage\n\nLast section.\n\n\n")
        assert rt.usage_from_readme(path) == "Last section."

    def test_a_missing_section_is_none(self, tmp_path):
        path = self._write(tmp_path, "# T\n\n## Usage text\n\nnot it\n")
        assert rt.usage_from_readme(path) is None

    def test_a_missing_file_is_none(self, tmp_path):
        assert rt.usage_from_readme(tmp_path / "nope.md") is None

    def test_subsections_and_tables_are_kept_verbatim(self, tmp_path):
        text = "## Usage\n\nIntro.\n\n### Commands\n\n| A | B |\n|---|---|\n| `x` | y |\n\n### Options\n\nz\n\n## Trust\n\nq\n"
        path = self._write(tmp_path, text)
        assert rt.usage_from_readme(path) == (
            "Intro.\n\n### Commands\n\n| A | B |\n|---|---|\n| `x` | y |\n\n### Options\n\nz"
        )

    def test_a_later_h2_heading_ends_it(self, tmp_path):
        path = self._write(tmp_path, "## Usage\n\nA.\n## Next\n\nB.\n")
        assert rt.usage_from_readme(path) == "A."

    def test_a_hash_line_inside_a_code_fence_does_not_end_it(self, tmp_path):
        path = self._write(tmp_path, "## Usage\n\n```\n## not a heading\n```\n\nTail.\n\n## End\n")
        assert rt.usage_from_readme(path) == "```\n## not a heading\n```\n\nTail."

    def test_the_first_usage_section_wins(self, tmp_path):
        path = self._write(tmp_path, "## Usage\n\nOne.\n\n## Usage\n\nTwo.\n")
        assert rt.usage_from_readme(path) == "One."
