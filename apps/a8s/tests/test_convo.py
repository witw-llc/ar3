"""Tests for convo.py — conversation archive and `a8s convo` formatting."""
from __future__ import annotations

import json
import time

import pytest

from convo import (
    ConversationArchiveError,
    decode_template,
    extract_heading_templates,
    follow_conversation,
    format_conversation,
    format_entry,
    involves_agent,
    load_agent_records,
    load_entries,
    open_glow_stdout,
    print_entries,
    prune_conversations,
    record,
    write_block,
)
from core import conversations_path
from commands import cmd_convo
from settings import DEFAULTS

needs_tzset = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="the process timezone is not settable here"
)


@pytest.fixture
def machine_timezone(monkeypatch):
    """Run a test in a named zone and put the real one back afterwards.

    `TZ` reaches the C library only through `tzset`, and `monkeypatch`'s undo
    restores the variable without calling it, so teardown calls it itself.
    """

    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def in_los_angeles(machine_timezone):
    machine_timezone("America/Los_Angeles")


class TestInvolvesAgent:
    def test_from(self):
        entry = {"from": "Bob", "to": "Alice", "recipients": ["Alice"]}
        assert involves_agent(entry, "bob")

    def test_to(self):
        entry = {"from": "Alice", "to": "Bob", "recipients": ["Bob"]}
        assert involves_agent(entry, "bob")

    def test_alias_recipient(self):
        entry = {"from": "Alice", "to": "devs", "recipients": ["Bob", "Carol"]}
        assert involves_agent(entry, "bob")
        assert involves_agent(entry, "carol")
        assert not involves_agent(entry, "dave")


class TestRecord:
    def test_appends_entry(self, fake_home):
        record(
            {
                "id": "01JTEST000000000000000000",
                "date": "2026-06-18T12:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "hello",
                "files": [{"filename": "x.txt"}],
            },
            recipients=["Bob"],
        )
        rows = load_entries()
        assert len(rows) == 1
        assert rows[0]["from"] == "Alice"
        assert rows[0]["to"] == "Bob"
        assert rows[0]["content"] == "hello"
        assert rows[0]["files"] == ["x.txt"]
        assert rows[0]["recipients"] == ["Bob"]

    def test_skips_empty_recipients(self, fake_home):
        """Nothing recorded, and no store created to say so in — a reader now
        distinguishes an archive with no rows from one that is not there, and
        a write that stored nothing must leave the second."""
        record({"id": "01JTEST000000000000000001", "from": "A", "to": "B", "content": "x"}, recipients=[])
        assert not conversations_path().exists()

    def test_dedupes_by_msg_id(self, fake_home):
        msg = {
            "id": "01JTEST000000000000000002",
            "from": "A",
            "to": "B",
            "content": "once",
        }
        record(msg, recipients=["B"])
        record(msg, recipients=["B"])
        assert len(load_entries()) == 1

    def test_concurrent_writers_do_not_lose_rows(self, fake_home):
        from concurrent.futures import ThreadPoolExecutor

        def write(i: int) -> None:
            record(
                {
                    "id": f"01JCONCURRENT{i:012d}",
                    "from": "A",
                    "to": "B",
                    "content": str(i),
                },
                recipients=["B"],
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(40)))
        assert {row["content"] for row in load_entries()} == {
            str(i) for i in range(40)
        }

    def test_housekeeping_prunes_to_max_rows(self, fake_home):
        for i in range(5):
            record(
                {
                    "id": f"01JTEST00000000000000000{i}",
                    "date": f"2026-06-18T12:00:0{i}.000000Z",
                    "from": "A",
                    "to": "B",
                    "content": f"m{i}",
                },
                recipients=["B"],
            )
        assert len(load_entries()) == 5
        assert prune_conversations(3) == 2
        rows = load_entries()
        assert len(rows) == 3
        assert rows[0]["content"] == "m2"
        assert rows[-1]["content"] == "m4"


@pytest.fixture(autouse=True)
def _utc_zone(zone):
    """`a8s convo` shows local time, so every heading assertion below would
    otherwise read differently in Kenmore and in Berlin."""
    zone("UTC")


class TestFormatConversation:
    def test_outbound_uses_heading_out(self, fake_home):
        record(
            {
                "id": "01JOUT0000000000000000000",
                "date": "2026-06-18T14:00:00.000000Z",
                "from": "Bob",
                "to": "Alice",
                "content": "ping",
            },
            recipients=["Alice"],
        )
        text = format_conversation("Bob", limit=10)
        assert "## from Bob to Alice at 2026-06-18 14:00:00 UTC" in text
        assert "ping" in text
        assert "###" not in text

    def test_inbound_uses_heading_in(self, fake_home):
        record(
            {
                "id": "01JIN00000000000000000000",
                "date": "2026-06-18T15:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "pong",
            },
            recipients=["Bob"],
        )
        text = format_conversation("Bob", limit=10)
        assert "### from Alice to Bob at 2026-06-18 15:00:00 UTC" in text
        assert "pong" in text

    def test_alias_inbound_for_member(self, fake_home):
        record(
            {
                "id": "01JALIAS00000000000000000",
                "date": "2026-06-18T16:00:00.000000Z",
                "from": "Alice",
                "to": "devs",
                "content": "standup",
            },
            recipients=["Bob", "Carol"],
        )
        text = format_conversation("Bob", limit=10)
        assert "### from Alice to devs at 2026-06-18 16:00:00 UTC" in text
        assert "standup" in text

    def test_limit_returns_last_n_chronologically(self, fake_home):
        for i in range(3):
            record(
                {
                    "id": f"01JSEQ00000000000000000{i}",
                    "date": f"2026-06-18T10:00:0{i}.000000Z",
                    "from": "Alice",
                    "to": "Bob",
                    "content": f"msg{i}",
                },
                recipients=["Bob"],
            )
        text = format_conversation("Bob", limit=2)
        assert "msg1" in text
        assert "msg2" in text
        assert "msg0" not in text

    def test_custom_headings(self, fake_home):
        record(
            {
                "id": "01JCUST00000000000000000",
                "date": "2026-06-18T17:00:00.000000Z",
                "from": "Bob",
                "to": "Alice",
                "content": "hi",
            },
            recipients=["Alice"],
        )
        text = format_conversation(
            "Bob",
            limit=10,
            heading_out="OUT {from}->{to} @ {timestamp}",
            heading_in="IN",
        )
        assert "OUT Bob->Alice @ 2026-06-18 17:00:00 UTC" in text

    def test_attachment_shows_full_path_when_on_disk(self, fake_home, tmp_path):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root.resolve())}})
        msg_id = "01JATT000000000000000000"
        attachment = root / ".files" / msg_id / "note.md"
        attachment.parent.mkdir(parents=True)
        attachment.write_text("payload", encoding="utf-8")
        record(
            {
                "id": msg_id,
                "date": "2026-06-18T18:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "see attached",
                "files": [{"filename": "note.md"}],
            },
            recipients=["Bob"],
        )
        text = format_conversation("Bob", limit=1)
        assert f"attachment: {attachment.resolve()}" in text

    def test_attachment_falls_back_to_basename_when_missing(self, fake_home, tmp_path):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root.resolve())}})
        record(
            {
                "id": "01JMISSING000000000000000",
                "date": "2026-06-18T18:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "gone",
                "files": [{"filename": "missing.pdf"}],
            },
            recipients=["Bob"],
        )
        text = format_conversation("Bob", limit=1)
        assert "attachment: missing.pdf" in text
        assert "missing.pdf" == text.split("attachment: ")[-1].strip()

    def test_lost_attachment_says_so_and_says_why(self, fake_home, tmp_path):
        """A file the transfer could not deliver must not render in the
        vocabulary of one that arrived. The owner read `- attachment: x.md`
        for a file that did not exist and went looking for it."""
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root.resolve())}})
        record(
            {
                "id": "01JLOST00000000000000000",
                "date": "2026-06-18T18:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "see attached",
                "files": [
                    {
                        "filename": "notes.md",
                        "error": "ATTACHMENT_UNAVAILABLE",
                        "detail": "could not download after 900s",
                    }
                ],
            },
            recipients=["Bob"],
        )
        text = format_conversation("Bob", limit=1)
        assert "ATTACHMENT UNAVAILABLE: notes.md" in text
        assert "could not download after 900s" in text
        # The success vocabulary must not appear for it.
        assert "- attachment: notes.md" not in text

    def test_lost_and_delivered_in_one_message_read_differently(self, fake_home, tmp_path):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root.resolve())}})
        msg_id = "01JMIXED00000000000000000"
        arrived = root / ".files" / msg_id / "arrived.md"
        arrived.parent.mkdir(parents=True)
        arrived.write_text("payload", encoding="utf-8")
        record(
            {
                "id": msg_id,
                "date": "2026-06-18T18:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "two files",
                "files": [
                    {"filename": "arrived.md"},
                    {"filename": "lost.md", "error": "ATTACHMENT_UNAVAILABLE",
                     "detail": "upload produced no url"},
                ],
            },
            recipients=["Bob"],
        )
        text = format_conversation("Bob", limit=1)
        assert f"attachment: {arrived.resolve()}" in text
        assert "ATTACHMENT UNAVAILABLE: lost.md" in text

    def test_error_without_detail_still_reports_the_loss(self, fake_home, tmp_path):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root.resolve())}})
        record(
            {
                "id": "01JBARE000000000000000000",
                "date": "2026-06-18T18:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "gone",
                "files": [{"filename": "x.md", "error": "ATTACHMENT_UNAVAILABLE"}],
            },
            recipients=["Bob"],
        )
        text = format_conversation("Bob", limit=1)
        assert "ATTACHMENT UNAVAILABLE: x.md" in text

    def test_clean_envelope_gains_no_unavailable_key(self):
        from convo import entry_from_message

        entry = entry_from_message(
            {"id": "X", "from": "a", "to": "b", "content": "hi",
             "files": [{"filename": "report.md", "storage": "https://example.com/x"}]}
        )
        assert entry["files"] == ["report.md"]
        # The archive shape only grows when there is something to record.
        assert "files_unavailable" not in entry

    def test_a_lost_file_keeps_its_name_in_files(self):
        from convo import entry_from_message

        entry = entry_from_message(
            {"id": "X", "from": "a", "to": "b", "content": "hi",
             "files": [{"filename": "lost.md", "error": "ATTACHMENT_UNAVAILABLE",
                        "detail": "no url"}]}
        )
        # The name is still information — that a file was meant to be here.
        assert entry["files"] == ["lost.md"]
        assert entry["files_unavailable"] == [
            {"filename": "lost.md", "detail": "no url"}
        ]


class TestGlowOutput:
    def test_print_entries_writes_through_glow_stream(self, capsys):
        writes: list[str] = []

        class FakeGlow:
            def write(self, text: str) -> int:
                writes.append(text)
                return len(text)

            def finalize(self) -> None:
                pass

            def close(self) -> None:
                writes.append("__close__")

        print_entries(
            "Bob",
            [
                {
                    "id": "01JGLOW00000000000000000",
                    "date": "2026-06-18T12:00:00.000000Z",
                    "from": "Alice",
                    "to": "Bob",
                    "content": "hello",
                }
            ],
            glow_stream=FakeGlow(),
        )
        assert len(writes) == 1
        assert "hello" in writes[0]
        assert capsys.readouterr().out == ""

    def test_open_glow_stdout_uses_l9m_stream(self, monkeypatch):
        opened: list[str] = []

        class FakeGlow:
            def close(self) -> None:
                pass

        def fake_open(theme: str = "auto"):
            opened.append(theme)
            return FakeGlow()

        import glow_util

        monkeypatch.setattr(glow_util, "open_glow_stdout", fake_open)
        stream = open_glow_stdout("dracula")
        assert opened == ["dracula"]
        stream.close()

    def test_cmd_convo_glow_theme_flag(self, fake_home, tmp_path, monkeypatch):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root)}})
        opened: list[str] = []

        class FakeGlow:
            def write(self, text: str) -> int:
                return len(text)

            def finalize(self) -> None:
                pass

            def close(self) -> None:
                pass

        record(
            {"id": "01JGLOW000000000000000001", "from": "Bob", "to": "Alice", "content": "hi"},
            recipients=["Alice"],
        )
        monkeypatch.setattr("convo.open_glow_stdout", lambda theme: (opened.append(theme) or FakeGlow()))
        assert cmd_convo(["bob", "--limit", "1", "--glow", "dracula"]) == 0
        assert opened == ["dracula"]

    def test_cmd_convo_glow_env(self, fake_home, tmp_path, monkeypatch):
        from registry import save_registry

        monkeypatch.setenv("A8S_GLOW", "tokyo-night")
        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root)}})
        opened: list[str] = []

        class FakeGlow:
            def write(self, text: str) -> int:
                return len(text)

            def finalize(self) -> None:
                pass

            def close(self) -> None:
                pass

        record(
            {"id": "01JGLOW000000000000000002", "from": "Bob", "to": "Alice", "content": "hi"},
            recipients=["Alice"],
        )
        monkeypatch.setattr("convo.open_glow_stdout", lambda theme: (opened.append(theme) or FakeGlow()))
        assert cmd_convo(["bob", "--limit", "1"]) == 0
        assert opened == ["tokyo-night"]

    def test_write_block_finalizes_glow_for_fenced_markdown(self):
        """Agent replies often include ``` fences; without finalize, GlowStream
        holds the entry until the stream closes (looks like silent inbound)."""
        class CapturingGlow:
            def __init__(self):
                self.writes: list[str] = []
                self.finalized = 0

            def write(self, text: str) -> int:
                self.writes.append(text)
                return len(text)

            def finalize(self) -> None:
                self.finalized += 1

            def close(self) -> None:
                pass

        glow = CapturingGlow()
        body = "### from remote to bob\n\nHere:\n\n```\ncode\n"
        write_block(body, glow)
        assert glow.finalized == 1
        assert any("```" in w for w in glow.writes)


class TestHeadingTemplates:
    def test_decode_template_escapes(self):
        assert decode_template("a\\nb") == "a\nb"
        assert decode_template("a\\tc") == "a\tc"

    def test_extract_multiline_tokens(self):
        argv, out, inn = extract_heading_templates(
            ["bob", "--heading-out", "line1", "line2", "--limit", "3"]
        )
        assert argv == ["bob", "--limit", "3"]
        assert out == "line1\nline2"
        assert inn is None

    def test_format_entry_multiline_heading(self, fake_home):
        record(
            {
                "id": "01JML000000000000000000",
                "date": "2026-06-18T14:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "body",
            },
            recipients=["Bob"],
        )
        text = format_conversation(
            "Bob",
            limit=1,
            heading_in="from {from}\n_{timestamp}_",
        )
        assert "from Alice\n_2026-06-18 14:00:00 UTC_" in text
        assert "body" in text

    def test_timestamp_reads_in_the_machines_zone(self, fake_home, zone):
        """What the operator reads is their own wall clock — the archive keeps
        the UTC it was handed."""
        zone("America/Los_Angeles")
        record(
            {
                "id": "01JTZ000000000000000000",
                "date": "2026-06-18T14:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "body",
            },
            recipients=["Bob"],
        )
        text = format_conversation("Bob", limit=1)
        assert "### from Alice to Bob at 2026-06-18 07:00:00 PDT" in text
        assert "2026-06-18T14:00:00.000000Z" not in text

    def test_utc_placeholder_exposes_the_stored_value(self, fake_home, zone):
        """One heading can carry both: the local reading a human wants and the
        stored instant a script wants."""
        zone("America/Los_Angeles")
        record(
            {
                "id": "01JUTC000000000000000000",
                "date": "2026-06-18T14:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "body",
            },
            recipients=["Bob"],
        )
        text = format_conversation(
            "Bob", limit=1, heading_in="{timestamp} == {utc}"
        )
        assert "2026-06-18 07:00:00 PDT == 2026-06-18T14:00:00.000000Z" in text

    def test_ulid_placeholder_renders_the_message_id(self, fake_home):
        """The row's own `message_id`, stable and time-ordered, is the cursor
        a no-monitor heartbeat records (#265)."""
        record(
            {
                "id": "01JSNC00000000000000000042",
                "date": "2026-06-18T14:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "body",
            },
            recipients=["Bob"],
        )
        text = format_conversation("Bob", limit=1, heading_in="id={ulid}")
        assert "id=01JSNC00000000000000000042" in text

    def test_ulid_placeholder_is_empty_when_the_row_has_none(self, fake_home):
        record(
            {"date": "2026-06-18T14:00:00.000000Z", "from": "Alice", "to": "Bob", "content": "x"},
            recipients=["Bob"],
        )
        text = format_conversation("Bob", limit=1, heading_in="id=[{ulid}]")
        assert "id=[]" in text


class TestCmdConvo:
    def test_help(self, capsys):
        assert cmd_convo(["--help"]) == 0
        out = capsys.readouterr().out
        assert "a8s convo" in out
        assert "{from}" in out
        assert "{timestamp}" in out
        assert "Multiline" in out

    def test_help_with_agent_name(self, capsys):
        assert cmd_convo(["bob", "--help"]) == 0
        assert "heading templates" in capsys.readouterr().out

    def test_multiline_heading_flag(self, fake_home, tmp_path, capsys):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root)}})
        record(
            {
                "id": "01JMLCMD0000000000000000",
                "date": "2026-06-18T14:00:00.000000Z",
                "from": "Bob",
                "to": "Alice",
                "content": "sent",
            },
            recipients=["Alice"],
        )
        assert (
            cmd_convo(
                [
                    "bob",
                    "--heading-out",
                    "**{from}**",
                    "→ {to}",
                    "--limit",
                    "1",
                ]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "**Bob**\n→ Alice" in out
        assert "sent" in out

    def test_unknown_agent(self, fake_home, capsys):
        assert cmd_convo(["nope"]) == 1
        assert "no agent named" in capsys.readouterr().err

    def test_rejects_non_positive_limit(self, fake_home, capsys):
        assert cmd_convo(["bob", "--limit", "0"]) == 2
        assert "--limit must be a positive integer" in capsys.readouterr().err

    def test_follow_flag_parses(self, fake_home, tmp_path, monkeypatch):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root)}})

        def fake_follow(agent, **kwargs):
            fake_follow.agent = agent
            fake_follow.kwargs = kwargs
            raise KeyboardInterrupt

        import convo as convo_mod

        monkeypatch.setattr(convo_mod, "follow_conversation", fake_follow)
        assert cmd_convo(["bob", "-f", "--limit", "3"]) == 0
        assert fake_follow.agent == "Bob"
        assert fake_follow.kwargs["limit"] == 3

    def test_prints_formatted_history(self, fake_home, tmp_path, capsys):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root)}})
        record(
            {
                "id": "01JCMD000000000000000000",
                "date": "2026-06-18T18:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "for harness",
            },
            recipients=["Bob"],
        )
        assert cmd_convo(["bob", "--limit", "5"]) == 0
        out = capsys.readouterr().out
        assert "for harness" in out
        assert "Alice" in out


class TestCmdConvoOnAnUnreadableArchive:
    """Exit 0 with nothing printed has to keep meaning "no messages" (#276).

    A desktop seat whose harness sandbox could read the registry but not the
    archive ran `a8s convo` on its heartbeat, got an empty result and exit 0,
    and read it as no mail. The same command with wider filesystem access
    showed two delivered messages. The registry being readable is what made
    the failure partial and therefore silent: an unreadable config home as a
    whole already failed correctly with `no agent named ...`.
    """

    @staticmethod
    def _register(tmp_path):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root)}})

    def test_a_missing_archive_names_the_file_and_exits_one(
        self, fake_home, tmp_path, capsys
    ):
        self._register(tmp_path)
        assert cmd_convo(["bob", "--limit", "3"]) == 1
        err = capsys.readouterr().err
        assert f"a8s: no conversation store at {conversations_path()}" in err

    def test_reading_does_not_create_the_archive(self, fake_home, tmp_path):
        """A read that creates the store makes the next read a truthful "no
        rows" about a history it just replaced, so the evidence disappears on
        the second look."""
        self._register(tmp_path)
        cmd_convo(["bob", "--limit", "3"])
        assert not conversations_path().exists()

    def test_an_unreadable_archive_names_the_file_and_exits_one(
        self, fake_home, tmp_path, capsys, unreadable_file
    ):
        self._register(tmp_path)
        record(
            {"id": "01JLOCKED0000000000000001", "from": "Alice", "to": "Bob", "content": "x"},
            recipients=["Bob"],
        )
        unreadable_file(conversations_path())
        assert cmd_convo(["bob", "--limit", "3"]) == 1
        err = capsys.readouterr().err
        assert f"a8s: cannot read {conversations_path()}" in err

    @staticmethod
    def _not_the_archive(path, shape):
        """A file `is_file()` accepts and `record` never wrote.

        Zero bytes is what a truncated write or a half-finished copy leaves,
        and SQLite opens it as an empty database. The unrelated table is the
        same class one step on: a real database belonging to something else.
        """
        import sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        if shape == "unrelated-schema":
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE somebody_elses (id INTEGER)")

    @pytest.mark.parametrize("shape", ["zero-byte", "unrelated-schema"])
    def test_a_file_that_is_not_the_archive_names_it_and_changes_nothing(
        self, fake_home, tmp_path, capsys, shape
    ):
        """The file being there is not evidence the history is empty.

        Reading through the writable connect initialized whatever it opened,
        so a zero-byte store came back a 24 KiB archive with the schema in it
        and every read after that was a truthful "no messages" about a file
        the seat had just overwritten. Asserted on the bytes, because an
        initialized store and an untouched one both exit 1 once the schema is
        checked, and only the bytes say which happened.
        """
        self._register(tmp_path)
        path = conversations_path()
        self._not_the_archive(path, shape)
        before = path.read_bytes()
        assert cmd_convo(["bob", "--limit", "3"]) == 1
        assert f"a8s: cannot read {path}" in capsys.readouterr().err
        assert path.read_bytes() == before

    @pytest.mark.parametrize("shape", ["zero-byte", "unrelated-schema"])
    def test_follow_leaves_it_alone_too(self, fake_home, tmp_path, capsys, shape):
        """`-f` opens the store on its own path, and its poll reopens it once
        a second — an initializing read there rewrites the file repeatedly."""
        self._register(tmp_path)
        path = conversations_path()
        self._not_the_archive(path, shape)
        before = path.read_bytes()
        assert cmd_convo(["bob", "-f"]) == 1
        assert f"a8s: cannot read {path}" in capsys.readouterr().err
        assert path.read_bytes() == before

    def test_a_readable_archive_with_no_rows_prints_nothing_and_exits_zero(
        self, fake_home, tmp_path, capsys
    ):
        """The positive control, and the reason the two cases above cannot be
        satisfied by refusing everything."""
        self._register(tmp_path)
        record(
            {"id": "01JEMPTY00000000000000001", "from": "Alice", "to": "Bob", "content": "x"},
            recipients=["Bob"],
        )
        import sqlite3

        with sqlite3.connect(conversations_path()) as conn:
            conn.execute("DELETE FROM messages")
        assert cmd_convo(["bob", "--limit", "3"]) == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_follow_reports_the_same_way(self, fake_home, tmp_path, capsys):
        """`-f` opens the store on its own path and would otherwise traceback
        — and, going through `_connect`, create the file it could not find."""
        self._register(tmp_path)
        assert cmd_convo(["bob", "-f"]) == 1
        assert "no conversation store at" in capsys.readouterr().err
        assert not conversations_path().exists()


class TestConversationsPath:
    def test_default_under_a8s_home(self, fake_home):
        assert conversations_path() == fake_home / ".a8s" / "conversations.sqlite3"

    def test_respects_a8s_home(self, fake_home, monkeypatch, tmp_path):
        custom = tmp_path / "custom"
        monkeypatch.setenv("A8S_HOME", str(custom))
        custom.mkdir()
        assert conversations_path() == custom / "conversations.sqlite3"


class TestRoutingIntegration:
    """Archive hooks on local route — one logical row per alias fan-out."""

    def test_alias_fanout_records_once(self, fake_home, tmp_path):
        from core import Participant
        from mailbox import _write_outbox, ensure_mailboxes, route_outboxes
        from registry import save_aliases, save_registry

        agents = {}
        for n in ("A", "B", "C"):
            d = tmp_path / n.lower()
            d.mkdir()
            agents[n] = Participant(n, d)
        save_registry({n: {"root": str(p.root)} for n, p in agents.items()})
        save_aliases({"devs": ["B", "C"]})
        for p in agents.values():
            ensure_mailboxes(p)
        payload = agents["A"].root / "x.txt"
        payload.write_text("x")
        _write_outbox("A", agents["A"].root, "devs", "roster note", [], attachment_sources=[payload])
        route_outboxes(list(agents.values()), all_agents=list(agents.values()))

        rows = load_entries()
        assert len(rows) == 1
        assert rows[0]["to"] == "devs"
        assert sorted(rows[0]["recipients"]) == ["B", "C"]
        assert rows[0]["content"] == "roster note"

    def test_bob_convo_after_routed_thread(self, fake_home, tmp_path):
        from core import Participant
        from mailbox import _write_outbox, ensure_mailboxes, route_outboxes
        from registry import save_registry

        a_root = tmp_path / "alice"
        b_root = tmp_path / "bob"
        a_root.mkdir()
        b_root.mkdir()
        save_registry({"Alice": {"root": str(a_root)}, "Bob": {"root": str(b_root)}})
        alice = Participant("Alice", a_root)
        bob = Participant("Bob", b_root)
        ensure_mailboxes(alice)
        ensure_mailboxes(bob)

        _write_outbox("Alice", a_root, "Bob", "question", [])
        route_outboxes([alice, bob], all_agents=[alice, bob])
        _write_outbox("Bob", b_root, "Alice", "answer", [])
        route_outboxes([alice, bob], all_agents=[alice, bob])

        text = format_conversation("Bob", limit=10)
        assert "### from Alice to Bob" in text
        assert "question" in text
        assert "## from Bob to Alice" in text
        assert "answer" in text
        assert text.index("question") < text.index("answer")


def test_default_max_rows_is_50000():
    assert DEFAULTS["convo_max_rows"] == 50_000


class TestFollowConversation:
    def test_follow_prints_new_entry(self, fake_home, tmp_path, capsys, monkeypatch):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root)}})
        record(
            {
                "id": "01JOLD000000000000000000",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "old",
            },
            recipients=["Bob"],
        )

        sleeps = {"n": 0}

        def fake_sleep(_interval: float) -> None:
            sleeps["n"] += 1
            if sleeps["n"] == 1:
                record(
                    {
                        "id": "01JNEW000000000000000000",
                        "date": "2026-06-18T11:00:00.000000Z",
                        "from": "Alice",
                        "to": "Bob",
                        "content": "fresh",
                    },
                    recipients=["Bob"],
                )
                return
            raise KeyboardInterrupt

        monkeypatch.setattr("convo.time.sleep", fake_sleep)
        with pytest.raises(KeyboardInterrupt):
            follow_conversation("Bob", limit=1, poll_interval=0.01)
        out = capsys.readouterr().out
        assert "old" in out
        assert "fresh" in out

    def test_archive_is_sqlite(self, fake_home):
        import sqlite3

        record(
            {"id": "01A", "from": "A", "to": "B", "content": "one"},
            recipients=["B"],
        )
        with sqlite3.connect(conversations_path()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
            assert (
                conn.execute(
                    "SELECT agent_key FROM message_agents ORDER BY agent_key"
                ).fetchall()
                == [("a",), ("b",)]
            )

    def test_follow_surfaces_every_message_between_polls(
        self, fake_home, capsys, monkeypatch
    ):
        record(
            {"id": "01OLD", "from": "Alice", "to": "Bob", "content": "old"},
            recipients=["Bob"],
        )
        sleeps = {"n": 0}

        def fake_sleep(_interval: float) -> None:
            sleeps["n"] += 1
            if sleeps["n"] == 1:
                for i in range(4):
                    record(
                        {
                            "id": f"01NEW{i}",
                            "from": "Alice",
                            "to": "Bob",
                            "content": f"new-{i}",
                        },
                        recipients=["Bob"],
                    )
                return
            raise KeyboardInterrupt

        monkeypatch.setattr("convo.time.sleep", fake_sleep)
        with pytest.raises(KeyboardInterrupt):
            follow_conversation("Bob", limit=1, poll_interval=0.01)
        out = capsys.readouterr().out
        assert out.count("old") == 1
        for i in range(4):
            assert out.count(f"new-{i}") == 1

    def test_housekeeping_does_not_change_follow_display_limit(
        self, fake_home, capsys, monkeypatch
    ):
        for i in range(5):
            record(
                {"id": f"01{i}", "from": "Alice", "to": "Bob", "content": f"m{i}"},
                recipients=["Bob"],
            )
        assert prune_conversations(3) == 2
        monkeypatch.setattr(
            "convo.time.sleep",
            lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            follow_conversation("Bob", limit=1, poll_interval=0.01)
        out = capsys.readouterr().out
        assert "m4" in out
        assert "m2" not in out
        assert "m3" not in out

    def test_follow_warns_when_housekeeping_advances_past_cursor(
        self, fake_home, capsys, monkeypatch
    ):
        record(
            {"id": "01OLD", "from": "Alice", "to": "Bob", "content": "old"},
            recipients=["Bob"],
        )
        sleeps = {"n": 0}

        def fake_sleep(_interval: float) -> None:
            sleeps["n"] += 1
            if sleeps["n"] == 1:
                for i in range(3):
                    record(
                        {
                            "id": f"01GAP{i}",
                            "from": "Alice",
                            "to": "Bob",
                            "content": f"gap-{i}",
                        },
                        recipients=["Bob"],
                    )
                prune_conversations(1)
                return
            raise KeyboardInterrupt

        monkeypatch.setattr("convo.time.sleep", fake_sleep)
        with pytest.raises(KeyboardInterrupt):
            follow_conversation("Bob", limit=1, poll_interval=0.01)
        captured = capsys.readouterr()
        assert "messages may have been missed" in captured.err
        assert "gap-2" in captured.out
        assert "gap-0" not in captured.out

    def test_follow_recovers_when_archive_sequence_resets(
        self, fake_home, capsys, monkeypatch
    ):
        for i in range(3):
            record(
                {"id": f"01OLD{i}", "from": "Alice", "to": "Bob", "content": f"old-{i}"},
                recipients=["Bob"],
            )
        sleeps = {"n": 0}

        def fake_sleep(_interval: float) -> None:
            sleeps["n"] += 1
            if sleeps["n"] == 1:
                conversations_path().unlink()
                record(
                    {
                        "id": "01REPLACEMENT",
                        "from": "Alice",
                        "to": "Bob",
                        "content": "replacement-row",
                    },
                    recipients=["Bob"],
                )
                return
            raise KeyboardInterrupt

        monkeypatch.setattr("convo.time.sleep", fake_sleep)
        with pytest.raises(KeyboardInterrupt):
            follow_conversation("Bob", limit=1, poll_interval=0.01)
        captured = capsys.readouterr()
        assert "conversation archive sequence reset from 3 to 1" in captured.err
        assert captured.out.count("old-2") == 1
        assert captured.out.count("replacement-row") == 1


class TestSenderFilter:
    def _thread(self) -> None:
        for i, sender in enumerate(["Alice", "Carol", "Alice", "Dave"]):
            record(
                {
                    "id": f"01JSENDER0000000000000{i:03d}",
                    "date": f"2026-06-18T12:00:0{i}.000000Z",
                    "from": sender,
                    "to": "Bob",
                    "content": f"{sender.lower()}-{i}",
                },
                recipients=["Bob"],
            )

    def test_keeps_only_named_sender(self, fake_home):
        self._thread()
        text = format_conversation("Bob", limit=10, senders=["Alice"])
        assert "alice-0" in text
        assert "alice-2" in text
        assert "carol-1" not in text
        assert "dave-3" not in text

    def test_match_is_case_insensitive(self, fake_home):
        self._thread()
        assert "carol-1" in format_conversation("Bob", limit=10, senders=["CAROL"])

    def test_several_senders(self, fake_home):
        self._thread()
        text = format_conversation("Bob", limit=10, senders=["carol", "dave"])
        assert "carol-1" in text
        assert "dave-3" in text
        assert "alice-0" not in text

    def test_limit_counts_matches_not_rows_scanned(self, fake_home):
        self._thread()
        text = format_conversation("Bob", limit=2, senders=["Alice"])
        assert "alice-0" in text
        assert "alice-2" in text

    def test_own_sends_are_reachable(self, fake_home):
        record(
            {
                "id": "01JSENDEROWN000000000000",
                "date": "2026-06-18T13:00:00.000000Z",
                "from": "Bob",
                "to": "Alice",
                "content": "mine",
            },
            recipients=["Alice"],
        )
        assert "mine" in format_conversation("Bob", limit=10, senders=["bob"])
        assert format_conversation("Bob", limit=10, senders=["alice"]) == ""

    def test_cmd_convo_from_flag(self, fake_home, tmp_path, capsys):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root)}})
        self._thread()
        assert cmd_convo(["bob", "--from", "alice"]) == 0
        out = capsys.readouterr().out
        assert "alice-2" in out
        assert "carol-1" not in out

    def test_follow_filters_new_rows(self, fake_home, monkeypatch, capsys):
        record(
            {
                "id": "01JFOLLOWOLD000000000000",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Carol",
                "to": "Bob",
                "content": "backlog-noise",
            },
            recipients=["Bob"],
        )
        sleeps = {"n": 0}

        def fake_sleep(_interval: float) -> None:
            sleeps["n"] += 1
            if sleeps["n"] == 1:
                for i, sender in enumerate(["Carol", "Alice"]):
                    record(
                        {
                            "id": f"01JFOLLOWNEW00000000000{i}",
                            "date": "2026-06-18T11:00:00.000000Z",
                            "from": sender,
                            "to": "Bob",
                            "content": f"live-{sender.lower()}",
                        },
                        recipients=["Bob"],
                    )
                return
            raise KeyboardInterrupt

        monkeypatch.setattr("convo.time.sleep", fake_sleep)
        with pytest.raises(KeyboardInterrupt):
            follow_conversation("Bob", limit=5, poll_interval=0.01, senders=["Alice"])
        out = capsys.readouterr().out
        assert "live-alice" in out
        assert "live-carol" not in out
        assert "backlog-noise" not in out


class TestSinceCursor:
    """`load_agent_records(..., since=...)` — the three cursor forms #265 adds."""

    CURSOR_ID = "01JSNC00000000000000000002"

    def _seed(self):
        record(
            {
                "id": "01JSNC00000000000000000001",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "before-cursor",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": self.CURSOR_ID,
                "date": "2026-06-18T11:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "at-cursor",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000003",
                "date": "2026-06-18T12:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "after-1",
            },
            recipients=["Bob"],
        )
        # Inserted last (highest seq) but dated BEFORE the cursor row — the
        # late-delivery case a ulid cursor must not drop.
        record(
            {
                "id": "01JSNC00000000000000000004",
                "date": "2026-06-18T09:30:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "late-delivery",
            },
            recipients=["Bob"],
        )

    @staticmethod
    def _seed_burst(count: int) -> str:
        """Record a cursor row and `count` messages after it; return the
        cursor's ulid."""
        cursor = "01JSNC00000000000000000100"
        record(
            {
                "id": cursor,
                "date": "2026-09-10T14:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "cursor-row",
            },
            recipients=["Bob"],
        )
        for i in range(1, count + 1):
            record(
                {
                    "id": f"01JSNC000000000000000001{i:02d}",
                    "date": "2026-09-10T14:00:00.000000Z",
                    "from": "Alice",
                    "to": "Bob",
                    "content": f"burst-{i}",
                },
                recipients=["Bob"],
            )
        return cursor

    def test_ulid_cursor_returns_rows_after_it_in_insertion_order(self, fake_home):
        self._seed()
        records = load_agent_records("Bob", limit=10, since=self.CURSOR_ID)
        contents = [entry["content"] for _, entry in records]
        assert contents == ["after-1", "late-delivery"]

    def test_ulid_cursor_includes_a_row_dated_before_the_cursor(self, fake_home):
        """The acceptance case: `late-delivery` is dated earlier than the
        cursor row but was inserted after it, so its seq is greater. A
        timestamp cursor would drop it; a ulid cursor cannot."""
        self._seed()
        records = load_agent_records("Bob", limit=10, since=self.CURSOR_ID)
        assert "late-delivery" in [entry["content"] for _, entry in records]

    def test_unknown_ulid_raises_naming_it(self, fake_home):
        self._seed()
        unknown = "01JSNC00000000000000099999"
        with pytest.raises(ConversationArchiveError, match=unknown):
            load_agent_records("Bob", limit=10, since=unknown)

    def test_timestamp_cursor_returns_rows_after_the_last_one_at_or_before_it(
        self, fake_home
    ):
        record(
            {
                "id": "01JSNC00000000000000000021",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "before",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000022",
                "date": "2026-06-18T11:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "at-cursor",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000023",
                "date": "2026-06-18T12:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "after",
            },
            recipients=["Bob"],
        )
        records = load_agent_records(
            "Bob", limit=10, since="2026-06-18T11:30:00.000000Z"
        )
        contents = [entry["content"] for _, entry in records]
        assert contents == ["after"]

    def test_timestamp_cursor_can_miss_a_late_delivery_dated_at_or_before_it(
        self, fake_home
    ):
        """The documented gap `_since_floor` explains: `late-delivery` (dated
        9:30, before the 11:30 cursor) arrives after `after-1` and becomes the
        newest row with `date <= cursor`, so it is the floor rather than
        something found past it — and `after-1`, inserted before it, is
        swallowed along with it. A `--since <ulid>` cursor has no such gap
        (see `test_ulid_cursor_includes_a_row_dated_before_the_cursor`)."""
        self._seed()
        records = load_agent_records(
            "Bob", limit=10, since="2026-06-18T11:30:00.000000Z"
        )
        assert records == []

    @needs_tzset
    def test_bare_date_cursor_starts_at_local_midnight(self, fake_home, in_los_angeles):
        """A bare date names a calendar day, and a delegator asking for today
        means their own today. `2026-09-10` in America/Los_Angeles starts at
        07:00Z; the 01:00Z row is still September 9 there."""
        record(
            {
                "id": "01JSNC00000000000000000051",
                "date": "2026-09-10T01:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "late-on-september-9-pdt",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000052",
                "date": "2026-09-10T15:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "september-10-pdt",
            },
            recipients=["Bob"],
        )
        records = load_agent_records("Bob", limit=10, since="2026-09-10")
        contents = [entry["content"] for _, entry in records]
        assert contents == ["september-10-pdt"]

    @needs_tzset
    def test_bare_date_cursor_includes_the_row_on_its_own_midnight(
        self, fake_home, in_los_angeles
    ):
        """A bare date asks for the whole day. The row stamped exactly on its
        local midnight (07:00:00Z in America/Los_Angeles) belongs to that day
        and is returned; the row one second before it is not."""
        for n, stamp, content in (
            (61, "2026-09-10T06:59:59.000000Z", "before-midnight"),
            (62, "2026-09-10T07:00:00.000000Z", "on-midnight"),
            (63, "2026-09-10T07:00:01.000000Z", "after-midnight"),
        ):
            record(
                {
                    "id": f"01JSNC000000000000000000{n}",
                    "date": stamp,
                    "from": "Alice",
                    "to": "Bob",
                    "content": content,
                },
                recipients=["Bob"],
            )
        records = load_agent_records("Bob", limit=10, since="2026-09-10")
        assert [e["content"] for _, e in records] == ["on-midnight", "after-midnight"]

    def test_timestamp_cursor_excludes_the_row_on_its_own_instant(self, fake_home):
        """A timestamp is a point already seen through: the row stamped exactly
        on it is the floor, not a result."""
        for n, stamp, content in (
            (64, "2026-09-10T14:00:00.000000Z", "on-the-instant"),
            (65, "2026-09-10T14:00:01.000000Z", "after-the-instant"),
        ):
            record(
                {
                    "id": f"01JSNC000000000000000000{n}",
                    "date": stamp,
                    "from": "Alice",
                    "to": "Bob",
                    "content": content,
                },
                recipients=["Bob"],
            )
        records = load_agent_records("Bob", limit=10, since="2026-09-10T14:00:00Z")
        assert [e["content"] for _, e in records] == ["after-the-instant"]

    @needs_tzset
    def test_cursor_with_an_explicit_offset_is_an_absolute_instant(
        self, fake_home, machine_timezone
    ):
        """An offset in the cursor settles the instant on its own, so the
        machine's own zone (UTC here) must not move it. Read as naive-local
        this cursor would be 00:00Z and select both rows."""
        machine_timezone("UTC")
        record(
            {
                "id": "01JSNC00000000000000000053",
                "date": "2026-09-10T01:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "before-the-offset-cursor",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000054",
                "date": "2026-09-10T15:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "after-the-offset-cursor",
            },
            recipients=["Bob"],
        )
        records = load_agent_records(
            "Bob", limit=10, since="2026-09-10T00:00:00-07:00"
        )
        contents = [entry["content"] for _, entry in records]
        assert contents == ["after-the-offset-cursor"]

    def test_timestamp_cursor_compares_instants_not_stored_spellings(self, fake_home):
        """`14:00:00Z` and `14:00:00.000000Z` are the same instant and sort
        the other way round as text. The cursor row establishes the floor, so
        only the row a second later comes back."""
        record(
            {
                "id": "01JSNC00000000000000000055",
                "date": "2026-09-10T14:00:00Z",
                "from": "Alice",
                "to": "Bob",
                "content": "at-the-cursor-instant",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000056",
                "date": "2026-09-10T14:00:01Z",
                "from": "Alice",
                "to": "Bob",
                "content": "one-second-later",
            },
            recipients=["Bob"],
        )
        records = load_agent_records(
            "Bob", limit=10, since="2026-09-10T14:00:00Z"
        )
        contents = [entry["content"] for _, entry in records]
        assert contents == ["one-second-later"]

    def test_duration_cursor_reads_a_date_without_fractional_seconds(self, fake_home):
        """The window's own floor is formatted with six fractional digits. A
        stored date spelled without any is inside or outside that window by
        the instant it names, not by how it sorts against that spelling."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)

        def whole_second(seconds_ago: float) -> str:
            return (now - timedelta(seconds=seconds_ago)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        record(
            {
                "id": "01JSNC00000000000000000057",
                "date": whole_second(3 * 3600),
                "from": "Alice",
                "to": "Bob",
                "content": "three-hours-ago",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000058",
                "date": whole_second(10 * 60),
                "from": "Alice",
                "to": "Bob",
                "content": "ten-minutes-ago",
            },
            recipients=["Bob"],
        )
        records = load_agent_records("Bob", limit=10, since="1h")
        contents = [entry["content"] for _, entry in records]
        assert contents == ["ten-minutes-ago"]

    def test_an_unparsable_date_is_neither_a_floor_nor_a_duration_match(
        self, fake_home
    ):
        """A date nothing can parse cannot be compared, so it never becomes
        the timestamp floor and no date window selects it. It is still a row,
        and a seq floor found past it still returns it."""
        from datetime import datetime, timedelta, timezone

        record(
            {
                "id": "01JSNC00000000000000000059",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "parsable-old",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000060",
                "date": "whenever",
                "from": "Alice",
                "to": "Bob",
                "content": "unparsable",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000061",
                "date": (
                    datetime.now(timezone.utc) - timedelta(minutes=10)
                ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "from": "Alice",
                "to": "Bob",
                "content": "recent",
            },
            recipients=["Bob"],
        )
        by_stamp = load_agent_records(
            "Bob", limit=10, since="2026-06-18T11:00:00.000000Z"
        )
        assert [entry["content"] for _, entry in by_stamp] == ["unparsable", "recent"]
        by_window = load_agent_records("Bob", limit=10, since="1h")
        assert [entry["content"] for _, entry in by_window] == ["recent"]

    def test_since_without_a_limit_drains_every_row_after_the_cursor(self, fake_home):
        """The burst case: a poller that woke late must not have to guess how
        big the backlog was. No `--limit` means every row past the cursor."""
        cursor = self._seed_burst(12)
        records = load_agent_records("Bob", limit=None, since=cursor)
        contents = [entry["content"] for _, entry in records]
        assert contents == [f"burst-{i}" for i in range(1, 13)]

    def test_since_with_a_limit_pages_the_oldest_rows_first(self, fake_home):
        """Twelve unseen rows read five at a time, each page continuing from
        the newest ulid the last one returned, arrive in order with none
        skipped — the guarantee the documented polling procedure rests on."""
        cursor = self._seed_burst(12)
        pages = []
        while True:
            page = load_agent_records("Bob", limit=5, since=cursor)
            if not page:
                break
            pages.append([entry["content"] for _, entry in page])
            cursor = page[-1][1]["id"]
        assert pages == [
            [f"burst-{i}" for i in range(1, 6)],
            [f"burst-{i}" for i in range(6, 11)],
            ["burst-11", "burst-12"],
        ]

    def test_duration_cursor_selects_rows_dated_within_the_window(self, fake_home):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)

        def stamp(seconds_ago: float) -> str:
            return (now - timedelta(seconds=seconds_ago)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )

        record(
            {
                "id": "01JSNC00000000000000000005",
                "date": stamp(3 * 3600),
                "from": "Alice",
                "to": "Bob",
                "content": "three-hours-ago",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000006",
                "date": stamp(10 * 60),
                "from": "Alice",
                "to": "Bob",
                "content": "ten-minutes-ago",
            },
            recipients=["Bob"],
        )
        records = load_agent_records("Bob", limit=10, since="1h")
        contents = [entry["content"] for _, entry in records]
        assert contents == ["ten-minutes-ago"]

    def test_malformed_cursor_raises_value_error(self, fake_home):
        self._seed()
        with pytest.raises(ValueError, match="banana"):
            load_agent_records("Bob", limit=10, since="banana")

    def test_since_composes_with_from_and_limit(self, fake_home):
        self._seed()
        record(
            {
                "id": "01JSNC00000000000000000007",
                "date": "2026-06-18T13:00:00.000000Z",
                "from": "Carol",
                "to": "Bob",
                "content": "carol-after",
            },
            recipients=["Bob"],
        )
        cursor = "01JSNC00000000000000000001"
        page = load_agent_records("Bob", limit=1, senders=["alice"], since=cursor)
        assert [entry["content"] for _, entry in page] == ["at-cursor"]
        # The page is the OLDEST match past the cursor, so continuing from
        # the ulid it returned reaches every later one in turn. Carol's row
        # sits between two of them and is never counted against the limit.
        seen = []
        while page:
            seen.extend(entry["content"] for _, entry in page)
            cursor = page[-1][1]["id"]
            page = load_agent_records("Bob", limit=1, senders=["alice"], since=cursor)
        assert seen == ["at-cursor", "after-1", "late-delivery"]

    def test_since_with_nothing_new_returns_empty(self, fake_home):
        self._seed()
        newest = "01JSNC00000000000000000004"
        assert load_agent_records("Bob", limit=10, since=newest) == []


class TestCmdConvoSince:
    @staticmethod
    def _register(tmp_path):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root)}})

    def test_since_ulid_shows_rows_after_it(self, fake_home, tmp_path, capsys):
        self._register(tmp_path)
        record(
            {
                "id": "01JSNC00000000000000000011",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "cursor-row",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000012",
                "date": "2026-06-18T11:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "new-row",
            },
            recipients=["Bob"],
        )
        assert cmd_convo(["bob", "--since", "01JSNC00000000000000000011"]) == 0
        out = capsys.readouterr().out
        assert "new-row" in out
        assert "cursor-row" not in out

    def test_since_unknown_ulid_exits_one_naming_it(self, fake_home, tmp_path, capsys):
        self._register(tmp_path)
        record(
            {"id": "01JSNC00000000000000000013", "from": "Alice", "to": "Bob", "content": "x"},
            recipients=["Bob"],
        )
        unknown = "01JSNC00000000000000099998"
        assert cmd_convo(["bob", "--since", unknown]) == 1
        err = capsys.readouterr().err
        assert unknown in err
        assert err.startswith("a8s:")

    def test_since_malformed_value_exits_two(self, fake_home, tmp_path, capsys):
        self._register(tmp_path)
        record(
            {"id": "01JSNC00000000000000000041", "from": "Alice", "to": "Bob", "content": "x"},
            recipients=["Bob"],
        )
        assert cmd_convo(["bob", "--since", "banana"]) == 2
        err = capsys.readouterr().err
        assert "--since" in err
        assert "banana" in err

    def test_since_timestamp(self, fake_home, tmp_path, capsys):
        self._register(tmp_path)
        record(
            {
                "id": "01JSNC00000000000000000014",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "old",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000015",
                "date": "2026-06-18T12:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "new",
            },
            recipients=["Bob"],
        )
        assert cmd_convo(["bob", "--since", "2026-06-18T11:00:00Z"]) == 0
        out = capsys.readouterr().out
        assert "new" in out
        assert "old" not in out

    def test_since_duration(self, fake_home, tmp_path, capsys):
        from datetime import datetime, timedelta, timezone

        self._register(tmp_path)
        now = datetime.now(timezone.utc)
        record(
            {
                "id": "01JSNC00000000000000000016",
                "date": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "from": "Alice",
                "to": "Bob",
                "content": "three-hours-ago",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000017",
                "date": (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "from": "Alice",
                "to": "Bob",
                "content": "ten-minutes-ago",
            },
            recipients=["Bob"],
        )
        assert cmd_convo(["bob", "--since", "1h"]) == 0
        out = capsys.readouterr().out
        assert "ten-minutes-ago" in out
        assert "three-hours-ago" not in out

    def test_since_composes_with_from_and_limit(self, fake_home, tmp_path, capsys):
        self._register(tmp_path)
        record(
            {
                "id": "01JSNC00000000000000000018",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "cursor-row",
            },
            recipients=["Bob"],
        )
        for i, (sender, minute) in enumerate(
            [("Carol", 11), ("Alice", 12), ("Alice", 13)]
        ):
            record(
                {
                    "id": f"01JSNC0000000000000000002{i}",
                    "date": f"2026-06-18T{minute}:00:00.000000Z",
                    "from": sender,
                    "to": "Bob",
                    "content": f"{sender.lower()}-{minute}",
                },
                recipients=["Bob"],
            )
        assert (
            cmd_convo(
                [
                    "bob",
                    "--since",
                    "01JSNC00000000000000000018",
                    "--from",
                    "alice",
                    "--limit",
                    "1",
                ]
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "alice-12" in out
        assert "alice-13" not in out
        assert "carol-11" not in out

    def test_since_with_nothing_new_prints_nothing_and_exits_zero(
        self, fake_home, tmp_path, capsys
    ):
        """A readable store with no rows past the cursor is not an error (#276)."""
        self._register(tmp_path)
        newest = "01JSNC00000000000000000019"
        record(
            {"id": newest, "from": "Alice", "to": "Bob", "content": "x"},
            recipients=["Bob"],
        )
        assert cmd_convo(["bob", "--since", newest]) == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_without_since_the_default_window_is_still_the_newest_ten(
        self, fake_home, tmp_path, capsys
    ):
        """`--limit` defaults to nothing so a cursor can drain; a tail view
        with no cursor still stops at ten, and still at the newest ten."""
        self._register(tmp_path)
        TestSinceCursor._seed_burst(12)
        assert cmd_convo(["bob"]) == 0
        out = capsys.readouterr().out
        assert out.count("### ") == 10
        assert "burst-12" in out
        assert "burst-3" in out
        assert "burst-2" not in out
        assert "cursor-row" not in out

    def test_since_rejects_follow(self, fake_home, tmp_path, capsys):
        self._register(tmp_path)
        assert cmd_convo(["bob", "-f", "--since", "1h"]) == 2
        err = capsys.readouterr().err
        assert "--follow" in err
        assert "--since" in err


class TestJsonOutput:
    @staticmethod
    def _register(tmp_path):
        from registry import save_registry

        root = tmp_path / "bob"
        root.mkdir()
        save_registry({"Bob": {"root": str(root)}})

    def test_json_carries_ulid_and_seq(self, fake_home, tmp_path, capsys):
        self._register(tmp_path)
        record(
            {
                "id": "01JSNC00000000000000000031",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "hello",
                "files": [{"filename": "x.txt"}],
            },
            recipients=["Bob"],
        )
        assert cmd_convo(["bob", "--json"]) == 0
        lines = [
            line for line in capsys.readouterr().out.splitlines() if line.strip()
        ]
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["ulid"] == "01JSNC00000000000000000031"
        assert row["seq"] == 1
        assert row["from"] == "Alice"
        assert row["to"] == "Bob"
        assert row["utc"] == "2026-06-18T10:00:00.000000Z"
        assert row["content"] == "hello"
        assert row["files"] == ["x.txt"]
        assert row["files_unavailable"] == []

    def test_json_composes_with_since(self, fake_home, tmp_path, capsys):
        self._register(tmp_path)
        record(
            {
                "id": "01JSNC00000000000000000032",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "cursor-row",
            },
            recipients=["Bob"],
        )
        record(
            {
                "id": "01JSNC00000000000000000033",
                "date": "2026-06-18T11:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "new-row",
            },
            recipients=["Bob"],
        )
        assert (
            cmd_convo(["bob", "--json", "--since", "01JSNC00000000000000000032"])
            == 0
        )
        lines = [
            line for line in capsys.readouterr().out.splitlines() if line.strip()
        ]
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["ulid"] == "01JSNC00000000000000000033"
        assert row["seq"] == 2

    def test_json_carries_attachment_failures(self, fake_home, tmp_path, capsys):
        """The markdown view says an attachment never arrived; JSON has to
        say it too, or a heartbeat reading JSON acts on evidence it never
        received."""
        self._register(tmp_path)
        record(
            {
                "id": "01JSNC00000000000000000035",
                "date": "2026-06-18T10:00:00.000000Z",
                "from": "Alice",
                "to": "Bob",
                "content": "review this",
                "files": [
                    {
                        "filename": "proof.txt",
                        "error": "download failed",
                        "detail": "HTTP 404",
                    }
                ],
            },
            recipients=["Bob"],
        )
        assert cmd_convo(["bob", "--json"]) == 0
        row = json.loads(capsys.readouterr().out.strip())
        assert row["files"] == ["proof.txt"]
        assert row["files_unavailable"] == [
            {"filename": "proof.txt", "error": "", "detail": "HTTP 404"}
        ]

        assert cmd_convo(["bob"]) == 0
        markdown = capsys.readouterr().out
        assert "ATTACHMENT UNAVAILABLE: proof.txt: HTTP 404" in markdown

    def test_json_drains_a_burst_and_pages_it_without_skipping(
        self, fake_home, tmp_path, capsys
    ):
        """The same burst through the CLI. Twelve messages after the cursor:
        no `--limit` hands back all twelve oldest-first, and `--limit 5`
        walks the same twelve in three pages, each continuing from the
        newest ulid the last one printed."""
        self._register(tmp_path)
        cursor = TestSinceCursor._seed_burst(12)

        def poll(*extra):
            assert cmd_convo(["bob", "--json", "--since", cursor, *extra]) == 0
            return [
                json.loads(line)
                for line in capsys.readouterr().out.splitlines()
                if line.strip()
            ]

        drained = poll()
        assert [row["content"] for row in drained] == [
            f"burst-{i}" for i in range(1, 13)
        ]

        pages = []
        while True:
            page = poll("--limit", "5")
            if not page:
                break
            pages.append([row["content"] for row in page])
            cursor = page[-1]["ulid"]
        assert pages == [
            [f"burst-{i}" for i in range(1, 6)],
            [f"burst-{i}" for i in range(6, 11)],
            ["burst-11", "burst-12"],
        ]

    def test_json_with_no_rows_prints_nothing(self, fake_home, tmp_path, capsys):
        self._register(tmp_path)
        record(
            {"id": "01JSNC00000000000000000034", "from": "Alice", "to": "Carol", "content": "x"},
            recipients=["Carol"],
        )
        assert cmd_convo(["bob", "--json"]) == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
