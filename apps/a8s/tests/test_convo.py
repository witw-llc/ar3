"""Tests for convo.py — conversation archive and `a8s convo` formatting."""
from __future__ import annotations

import pytest

from convo import (
    decode_template,
    extract_heading_templates,
    follow_conversation,
    format_conversation,
    format_entry,
    involves_agent,
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
        record({"id": "01JTEST000000000000000001", "from": "A", "to": "B", "content": "x"}, recipients=[])
        assert load_entries() == []

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


@pytest.fixture
def zone(monkeypatch):
    """Force the process's local zone so a rendered heading is the same string
    on every machine that runs this suite."""
    import time as _time

    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        _time.tzset()

    yield use
    monkeypatch.undo()
    _time.tzset()


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
