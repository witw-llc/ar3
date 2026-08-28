"""Tests for mailbox.py — routing fan-out, queue helpers, content/file split."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core import (
    BACKOFF_SCHEDULE,
    MAX_ATTEMPTS,
    MAX_FILE_BYTES,
    Participant,
    files_dir,
    inbound_bundle_dir,
    inbox_dir,
    inbox_tmp_dir,
    outbox_dir,
    pending_bundle_dir,
    pending_dir,
    retry_sidecar_path,
    trash_dir,
)
from mailbox import (
    _pending_attachment_status,
    _split_content_and_files,
    _upload_files_for_remote,
    _write_outbox,
    ensure_mailboxes,
    next_inbox_message,
    route_outboxes,
)
from registry import participants_from_registry, save_aliases, save_namespaces, save_registry, save_namespace_options


class TestPendingAttachmentStatus:
    def test_missing_bundle_file(self, fake_home):
        path, reason = _pending_attachment_status(
            "A",
            "01TEST",
            {"filename": "missing.tif"},
        )
        assert path is None
        assert reason.startswith("not found:")

    def test_oversize_file(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.setenv("A8S_MAX_FILE_BYTES", "10")
        bundle = pending_dir("A") / "01TEST"
        bundle.mkdir(parents=True)
        big = bundle / "huge.tif"
        big.write_bytes(b"x" * 20)
        path, reason = _pending_attachment_status(
            "A",
            "01TEST",
            {"filename": "huge.tif"},
        )
        assert path is None
        assert "exceeds max_file_bytes" in reason

    def test_path_field_rejected(self, fake_home):
        path, reason = _pending_attachment_status(
            "A",
            "01TEST",
            {"filename": "x.tif", "path": "/tmp/x.tif"},
        )
        assert path is None
        assert "path field" in reason

    def test_upload_logs_specific_reason(self, fake_home, tmp_path):
        from txlog import read_events

        a_root = tmp_path / "a"
        a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})
        a = Participant("A", a_root)
        ensure_mailboxes(a)
        pending = pending_dir("A") / "01UPLOAD"
        pending.mkdir(parents=True)
        (pending / "01UPLOAD.json").write_text(
            json.dumps(
                {
                    "id": "01UPLOAD",
                    "from": "A",
                    "to": "REMOTE",
                    "content": "see file",
                    "files": [{"filename": "Scan.TIF"}],
                }
            )
        )
        sidecar = {"attempts": 0, "uploaded": {}}
        msg = json.loads((pending / "01UPLOAD.json").read_text())
        ok = _upload_files_for_remote(msg, a, [_StubStorage("svc")], sidecar)
        assert ok is False
        failed = [
            event
            for event in read_events("01UPLOAD")
            if event["event"] == "FILE_UPLOAD_FAILED"
        ]
        assert len(failed) == 1
        assert "not found:" in failed[0]["detail"]


def _write_staged(sender_name: str, sender_root: Path, to: str, content: str, *sources: Path) -> Path:
    return _write_outbox(
        sender_name,
        sender_root,
        to,
        content,
        [],
        attachment_sources=list(sources),
    )


# ---------- _split_content_and_files ----------

class TestSplitContentAndFiles:
    def test_no_files(self):
        body, files = _split_content_and_files("hello world")
        assert body == "hello world"
        assert files == []

    def test_single_file(self):
        raw = "see attached\nFILE: /tmp/build.log"
        body, files = _split_content_and_files(raw)
        assert body == "see attached"
        assert files == [{"filename": "build.log", "path": "/tmp/build.log"}]

    def test_multiple_files_preserve_order(self):
        raw = "two attachments\nFILE: /a/b.log\nFILE: /c/d.log"
        body, files = _split_content_and_files(raw)
        assert body == "two attachments"
        assert files == [
            {"filename": "b.log", "path": "/a/b.log"},
            {"filename": "d.log", "path": "/c/d.log"},
        ]

    def test_files_must_be_at_end(self):
        # FILE: lines in the middle are NOT extracted (only trailing ones).
        raw = "FILE: /not-extracted\nbody"
        body, files = _split_content_and_files(raw)
        assert body == "FILE: /not-extracted\nbody"
        assert files == []

    def test_empty_input(self):
        body, files = _split_content_and_files("")
        assert body == ""
        assert files == []


# ---------- ensure_mailboxes ----------

class TestEnsureMailboxes:
    def test_creates_inbox_trash_outbox(self, fake_home, tmp_path):
        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        p = Participant("X", agent_root)
        ensure_mailboxes(p)
        assert inbox_dir("X").is_dir()
        assert trash_dir("X").is_dir()
        assert outbox_dir(agent_root).is_dir()


# ---------- _write_outbox ----------

class TestWriteOutbox:
    def test_writes_message_json(self, fake_home, tmp_path):
        path = _write_outbox(
            sender_name="A",
            sender_root=tmp_path,
            to="B",
            content="hi",
            files=[],
        )
        assert path.is_file()
        msg = json.loads(path.read_text())
        assert msg["from"] == "A"
        assert msg["to"] == "B"
        assert msg["content"] == "hi"
        assert msg["files"] == []
        assert "date" in msg

    def test_filename_is_ulid_and_matches_id(self, fake_home, tmp_path):
        from ark.ulid import is_ulid
        path = _write_outbox("A", tmp_path, "B", "hi", [])
        # Filename = "<ulid>.json" — sortable, opaque, no sender leak in name.
        stem = path.stem
        assert is_ulid(stem)
        # The message's `id` field equals the filename stem so receivers can
        # dedupe by ID without re-parsing the filename.
        msg = json.loads(path.read_text())
        assert msg["id"] == stem


# ---------- route_outboxes ----------

@pytest.fixture
def two_agents(fake_home, tmp_path):
    """Set up two agents, return their Participants."""
    a_root = tmp_path / "a"; a_root.mkdir()
    b_root = tmp_path / "b"; b_root.mkdir()
    save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
    a = Participant("A", a_root)
    b = Participant("B", b_root)
    ensure_mailboxes(a)
    ensure_mailboxes(b)
    return a, b


@pytest.fixture
def three_agents(fake_home, tmp_path):
    """Three agents A, B, C with an alias `devs` -> [A, B, C]."""
    parts = []
    for n in ("A", "B", "C"):
        root = tmp_path / n
        root.mkdir()
        parts.append(Participant(n, root))
    save_registry({p.name: {"root": str(p.root)} for p in parts})
    save_aliases({"devs": ["A", "B", "C"]})
    for p in parts:
        ensure_mailboxes(p)
    return parts


class TestRouteOutboxes:
    def test_single_agent_delivery(self, two_agents):
        a, b = two_agents
        # A writes to B
        _write_outbox("A", a.root, "B", "hi", [])
        n = route_outboxes([a, b], all_agents=[a, b])
        assert n == 1
        # B's inbox has one message
        files = list(inbox_dir("B").iterdir())
        assert len(files) == 1
        msg = json.loads(files[0].read_text())
        assert msg["from"] == "A"
        assert msg["to"] == "B"
        assert msg["content"] == "hi"
        # A's outbox is empty
        assert list(outbox_dir(a.root).iterdir()) == []

    def test_alias_fanout_excludes_sender(self, three_agents):
        a, b, c = three_agents
        # A writes to alias devs (which contains [A, B, C]).
        _write_outbox("A", a.root, "devs", "roster meeting", [])
        n = route_outboxes([a, b, c], all_agents=[a, b, c])
        # Sender excluded → 2 recipients (B, C).
        assert n == 2
        assert list(inbox_dir("A").iterdir()) == []
        b_msg = json.loads(next(inbox_dir("B").iterdir()).read_text())
        # Strict opacity (#69, #70): no `alias` / `others_count` fields, and
        # `to` preserves the alias name (mailing-list semantics).
        assert "alias" not in b_msg
        assert "others_count" not in b_msg
        assert b_msg["to"] == "devs"
        assert b_msg["from"] == "A"

    def test_alias_fanout_preserves_to_for_all_recipients(self, three_agents):
        # Both fanout recipients see `to: devs`; the message shape is
        # identical for them (no individual "you got this" leak).
        a, b, c = three_agents
        save_aliases({"devs": ["B", "C"]})
        _write_outbox("A", a.root, "devs", "msg", [])
        route_outboxes([a, b, c], all_agents=[a, b, c])
        for n in ("B", "C"):
            m = json.loads(next(inbox_dir(n).iterdir()).read_text())
            assert m["to"] == "devs"
            assert "alias" not in m
            assert "others_count" not in m

    def test_empty_to_is_rejected_to_trash(self, two_agents):
        a, b = two_agents
        # Write a malformed outbox file with empty `to`.
        outbox = outbox_dir(a.root)
        bad = outbox / "20260101T000000_A.json"
        bad.write_text(json.dumps({
            "from": "A", "to": "", "content": "rogue", "files": [],
        }))
        n = route_outboxes([a, b], all_agents=[a, b])
        assert n == 0
        # Outbox file moved to A's trash.
        assert not bad.is_file()
        assert any("rogue" in f.read_text() for f in trash_dir("A").iterdir())

    def test_unknown_recipient_with_no_remotes_is_trashed(self, two_agents):
        # With the two-phase ingest + process design, an unknown recipient
        # has no path forward when no remotes are configured: local has no
        # match, and there's nothing to publish to. Trash immediately so the
        # outbox dir doesn't accumulate undeliverable messages.
        a, b = two_agents
        outbox = outbox_dir(a.root)
        bad = outbox / "20260101T000000_A.json"
        bad.write_text(json.dumps({
            "from": "A", "to": "BOGUS", "content": "x", "files": [],
        }))
        n = route_outboxes([a, b], all_agents=[a, b])
        assert n == 0
        # Original outbox file is gone (ingest moved it out).
        assert not bad.is_file()
        # The message landed in A's trash — terminal failure.
        trashed = list(trash_dir("A").iterdir())
        assert any("BOGUS" in f.read_text() for f in trashed)

    def test_from_is_force_overwritten(self, two_agents):
        a, b = two_agents
        # Hand-write an outbox JSON with a SPOOFED from.
        outbox = outbox_dir(a.root)
        f = outbox / "20260101T000000_A.json"
        f.write_text(json.dumps({
            "from": "VICTIM",  # spoofed; should be overwritten with sender
            "to": "B",
            "content": "spoof attempt",
            "files": [],
        }))
        route_outboxes([a, b], all_agents=[a, b])
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        # Routing forces from = sender's actual name, regardless of the JSON.
        assert delivered["from"] == "A"

    def test_from_in_foreign_namespace_is_overwritten(self, two_agents):
        a, b = two_agents
        save_namespaces({"acme": "B"})  # bound to someone else
        outbox = outbox_dir(a.root)
        f = outbox / "20260101T000000_A.json"
        f.write_text(json.dumps({
            "from": "acme:gerry",
            "to": "B",
            "content": "spoof attempt",
            "files": [],
        }))
        route_outboxes([a, b], all_agents=[a, b])
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["from"] == "A"

    def test_local_routing_appends_seen_ids(self, two_agents):
        """When local routing commits, the message ULID enters the seen-ids
        ring. Without this a remote round-trip — we publish to MQTT, the
        broker pushes back to our own subscriber — would deliver the same
        envelope a second time, and the handler would wake on it twice
        (the bug seen in PR #85's live test where the connector emailed
        every routed message twice)."""
        from network import seen_id_contains
        a, b = two_agents
        path = _write_outbox("A", a.root, "B", "dedup-test", [])
        msg_id = json.loads(path.read_text())["id"]
        assert not seen_id_contains(msg_id)
        route_outboxes([a, b], all_agents=[a, b])
        assert seen_id_contains(msg_id), (
            "Local routing must claim the ULID so an MQTT round-trip is deduped"
        )

    def test_local_route_then_receive_envelope_is_no_op(self, two_agents):
        """End-to-end repro of the round-trip duplicate: local routing
        delivers, then the same envelope arrives via the remote subscriber
        (`receive_envelope`). The receive must dedupe — no second inbox
        file."""
        from network import receive_envelope
        a, b = two_agents
        path = _write_outbox("A", a.root, "B", "loopback", [])
        envelope_bytes = path.read_text().encode("utf-8")
        route_outboxes([a, b], all_agents=[a, b])
        # Simulate MQTT round-trip: drain the inbox first, mimicking the
        # local handler's wake (so the inbox-file-already-exists short-circuit
        # in receive_envelope can't be the one catching the dup).
        inbox_b = inbox_dir("B")
        for f in list(inbox_b.iterdir()):
            f.unlink()
        receive_envelope(envelope_bytes, [a, b])
        assert list(inbox_b.iterdir()) == [], (
            "Round-trip must be deduped via seen-ids, not delivered again"
        )


class TestSharedOutboxAttribution:
    """Issue #150 — several filedrop names on one mount share `<root>/.outbox/`.
    A claimed `from` naming a co-registered peer must attribute pending + wire
    `from` to that peer; unbacked claims keep force-stamp on the first owner."""

    @pytest.fixture
    def shared_filedrop(self, fake_home, tmp_path):
        mount = tmp_path / "gdrive" / "a8s"
        mount.mkdir(parents=True)
        recipient_root = tmp_path / "recipient"
        recipient_root.mkdir()
        save_registry({
            "my-google": {"root": str(mount)},
            "neil-email": {"root": str(mount)},
            "B": {"root": str(recipient_root)},
        })
        command = Participant("my-google", mount)
        email = Participant("neil-email", mount)
        recipient = Participant("B", recipient_root)
        for p in (command, email, recipient):
            ensure_mailboxes(p)
        return command, email, recipient

    def _stage(self, mount: Path, claimed_from: str, to: str, content: str, name: str) -> Path:
        outbox = outbox_dir(mount)
        path = outbox / name
        path.write_text(json.dumps({
            "from": claimed_from,
            "to": to,
            "content": content,
            "files": [],
        }))
        return path

    def test_co_registered_from_is_honored(self, shared_filedrop):
        command, email, recipient = shared_filedrop
        # First owner in the senders list would have stolen this before #150.
        self._stage(command.root, "neil-email", "B", "from email principal", "01EMAIL.json")
        route_outboxes(
            [command, email, recipient],
            all_agents=[command, email, recipient],
        )
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["from"] == "neil-email"
        assert delivered["content"] == "from email principal"

    def test_single_handler_honors_co_registered_peer(self, shared_filedrop):
        # Daemon path: `a8s start my-google` handles one name; peers live in
        # all_agents. Co-owners must come from the registry, not senders.
        command, email, recipient = shared_filedrop
        self._stage(command.root, "neil-email", "B", "from email principal", "01EMAIL.json")
        n = route_outboxes(
            [command],
            all_agents=[command, email, recipient],
        )
        assert n == 1
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["from"] == "neil-email"

    def test_single_handler_spoof_stamps_the_scanning_handler(self, shared_filedrop):
        command, email, recipient = shared_filedrop
        self._stage(command.root, "VICTIM", "B", "spoof", "01SPOOF.json")
        route_outboxes(
            [command],
            all_agents=[command, email, recipient],
        )
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["from"] == "my-google"

    def test_either_peer_claim_stands_regardless_of_sender_order(self, shared_filedrop):
        command, email, recipient = shared_filedrop
        self._stage(command.root, "my-google", "B", "cmd", "01CMD.json")
        self._stage(command.root, "neil-email", "B", "mail", "02MAIL.json")
        # Email listed first — must not stamp the command message as neil-email.
        route_outboxes(
            [email, command, recipient],
            all_agents=[command, email, recipient],
        )
        by_content = {
            json.loads(p.read_text())["content"]: json.loads(p.read_text())["from"]
            for p in inbox_dir("B").iterdir()
        }
        assert by_content == {"cmd": "my-google", "mail": "neil-email"}

    def test_spoofed_claim_still_force_stamps_first_owner(self, shared_filedrop):
        command, email, recipient = shared_filedrop
        self._stage(command.root, "VICTIM", "B", "spoof", "01SPOOF.json")
        route_outboxes(
            [command, email, recipient],
            all_agents=[command, email, recipient],
        )
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["from"] == "my-google"

    def test_foreign_agent_claim_is_not_honored(self, shared_filedrop):
        # B is registered but does not share the mount — claiming B is a spoof.
        command, email, recipient = shared_filedrop
        self._stage(command.root, "B", "B", "loop", "01LOOP.json")
        route_outboxes(
            [command, email, recipient],
            all_agents=[command, email, recipient],
        )
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["from"] == "my-google"

    def test_case_insensitive_peer_match(self, shared_filedrop):
        command, email, recipient = shared_filedrop
        self._stage(command.root, "Neil-Email", "B", "cased", "01CASE.json")
        route_outboxes(
            [command, email, recipient],
            all_agents=[command, email, recipient],
        )
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["from"] == "neil-email"

    def test_attachment_bundle_follows_attributed_owner(self, shared_filedrop):
        command, email, recipient = shared_filedrop
        outbox = outbox_dir(command.root)
        msg_id = "01ATTACH"
        bundle = outbox / msg_id
        bundle.mkdir()
        (bundle / "note.txt").write_text("payload")
        (outbox / f"{msg_id}.json").write_text(json.dumps({
            "id": msg_id,
            "from": "neil-email",
            "to": "B",
            "content": "with file",
            "files": [{"filename": "note.txt"}],
        }))
        route_outboxes(
            [command, email, recipient],
            all_agents=[command, email, recipient],
        )
        delivered = json.loads((inbox_dir("B") / f"{msg_id}.json").read_text())
        assert delivered["from"] == "neil-email"
        assert (recipient.files_bundle_dir(msg_id) / "note.txt").read_text() == "payload"
        assert not pending_bundle_dir("my-google", msg_id).exists()
        assert not pending_bundle_dir("neil-email", msg_id).exists()


class TestTwoNodesOneRepo:
    """The owner's case: a Codex seat and a Claude seat rooted at one repo.

    Before path-field interpolation both resolved `<repo>/.outbox`, one handler
    won the scan race and renamed the other's mail into its own pending, and
    `_stamp_from` put the winner's name on the loser's message. `$NODE` gives
    each node a directory of its own, and every stage downstream already keys
    on the resolved path.
    """

    @pytest.fixture
    def two_seats(self, fake_home, tmp_path):
        repo = tmp_path / "ar3-private"
        repo.mkdir()
        reg = {}
        for name in ("codex-ares", "claude-ares"):
            defn = tmp_path / f"two-seat-{name}.json"
            defn.write_text(json.dumps({
                "invoke": ["harness", "-p", "$MESSAGE"],
                "outbox_dir": ".outbox-$NODE",
                "inbox_dir": ".inbox-$NODE",
                "files_dir": ".files-$NODE",
                # The env lie: routing still wins, and what it now injects is
                # the interpolated path.
                "env": {"TELL_OUTBOX_DIR": str(tmp_path / "bogus")},
            }))
            reg[name] = {"root": str(repo), "definition": str(defn)}
        save_registry(reg)
        parts = {p.name: p for p in participants_from_registry()}
        return repo, parts["codex-ares"], parts["claude-ares"]

    def test_resolution_is_distinct_and_absolute(self, two_seats):
        repo, codex, claude = two_seats
        assert codex.outbox_path() == (repo / ".outbox-codex-ares").resolve()
        assert claude.outbox_path() == (repo / ".outbox-claude-ares").resolve()
        assert codex.inbox_path() != claude.inbox_path()
        assert codex.files_path() != claude.files_path()

    def test_wake_env_carries_the_nodes_own_outbox(self, two_seats):
        import daemon
        from definitions import load_definition

        _repo, codex, _claude = two_seats
        env = daemon._wake_env(codex, load_definition("codex-ares"))
        assert env["TELL_OUTBOX_DIR"] == str(codex.outbox_path())

    def test_each_seat_is_stamped_by_the_owner_of_its_outbox(self, two_seats):
        # The load-bearing assertion. On a shared `.outbox` one of these two
        # envelopes comes out with the other seat's name on it.
        from tell import write_outbox_envelope

        _repo, codex, claude = two_seats
        for p in (codex, claude):
            ensure_mailboxes(p)
        write_outbox_envelope(codex.outbox_path(), "claude-ares", "from codex", [])
        write_outbox_envelope(claude.outbox_path(), "codex-ares", "from claude", [])

        route_outboxes([codex, claude], all_agents=[codex, claude])

        to_claude = json.loads(next(inbox_dir("claude-ares").iterdir()).read_text())
        to_codex = json.loads(next(inbox_dir("codex-ares").iterdir()).read_text())
        assert to_claude["from"] == "codex-ares"
        assert to_claude["content"] == "from codex"
        assert to_codex["from"] == "claude-ares"
        assert to_codex["content"] == "from claude"

    def test_neither_outbox_is_emptied_by_the_other_handler(self, two_seats):
        # One handler holding only `codex-ares` must not ingest the Claude
        # seat's outbox: the two are separate keys in `owners_by_outbox`.
        from tell import write_outbox_envelope

        _repo, codex, claude = two_seats
        for p in (codex, claude):
            ensure_mailboxes(p)
        write_outbox_envelope(claude.outbox_path(), "codex-ares", "mine", [])
        route_outboxes([codex], all_agents=[codex, claude])
        assert [f.name for f in claude.outbox_path().iterdir()]
        assert list(inbox_dir("codex-ares").iterdir()) == []

    def test_a_tell_typed_in_the_repo_still_refuses_to_guess(self, two_seats, monkeypatch):
        # `TELL_OUTBOX_DIR` unset and CWD inside the shared root matches both
        # seats. Ambiguity is reported, never resolved by dict order.
        from tell import _outboxes_matching_cwd

        repo, _codex, _claude = two_seats
        monkeypatch.chdir(repo)
        assert len(_outboxes_matching_cwd(repo.resolve())) == 2


class TestNamespaceRouting:
    """Issue #148 — a `<prefix>:<sub-address>` recipient delivers to the
    single agent bound to the prefix, with the full address preserved in
    `to` so the node can self-route internally via $RECIPIENT."""

    @pytest.fixture
    def namespace_agents(self, fake_home, tmp_path):
        a_root = tmp_path / "a"; a_root.mkdir()
        node_root = tmp_path / "node"; node_root.mkdir()
        save_registry({"A": {"root": str(a_root)}, "NODE": {"root": str(node_root)}})
        save_namespaces({"acme": "NODE"})
        a = Participant("A", a_root)
        node = Participant("NODE", node_root)
        ensure_mailboxes(a)
        ensure_mailboxes(node)
        return a, node

    def test_delivers_one_message_with_to_preserved(self, namespace_agents):
        a, node = namespace_agents
        _write_outbox("A", a.root, "acme:phil", "hi phil", [])
        n = route_outboxes([a, node], all_agents=[a, node])
        assert n == 1
        files = list(inbox_dir("NODE").iterdir())
        assert len(files) == 1
        msg = json.loads(files[0].read_text())
        assert msg["to"] == "acme:phil"
        assert msg["from"] == "A"

    def test_prefix_case_insensitive_sub_address_verbatim(self, namespace_agents):
        a, node = namespace_agents
        _write_outbox("A", a.root, "ACME:Ops:Phil", "hi", [])
        route_outboxes([a, node], all_agents=[a, node])
        msg = json.loads(next(inbox_dir("NODE").iterdir()).read_text())
        assert msg["to"] == "ACME:Ops:Phil"

    def test_empty_sub_address_is_trashed(self, namespace_agents):
        # Malformed address — same handling as any malformed recipient.
        a, node = namespace_agents
        _write_outbox("A", a.root, "acme:", "malformed", [])
        n = route_outboxes([a, node], all_agents=[a, node])
        assert n == 0
        assert list(inbox_dir("NODE").iterdir()) == []
        assert any("malformed" in f.read_text() for f in trash_dir("A").iterdir())

    def test_unknown_prefix_with_no_remotes_is_trashed(self, namespace_agents):
        a, node = namespace_agents
        _write_outbox("A", a.root, "ghost:phil", "nowhere to go", [])
        n = route_outboxes([a, node], all_agents=[a, node])
        assert n == 0
        assert any("ghost:phil" in f.read_text() for f in trash_dir("A").iterdir())

    def test_unknown_prefix_with_remotes_publishes(self, namespace_agents):
        # Same fallback as an unknown agent name: another cluster may hold
        # the binding, so the envelope goes out with `to` untouched.
        a, node = namespace_agents
        published: list[dict] = []

        def publish(msg, sender_name, succeeded_so_far, attempt_count):
            published.append(msg)
            return ["hub"]

        _write_outbox("A", a.root, "ghost:phil", "cross-cluster", [])
        n = route_outboxes(
            [a, node], all_agents=[a, node],
            publish_remotes=publish, configured_remote_ids=["hub"],
        )
        assert n == 0
        assert len(published) == 1
        assert published[0]["to"] == "ghost:phil"
        assert list(pending_dir("A").iterdir()) == []

    def test_dangling_bound_agent_treated_as_unknown(self, fake_home, tmp_path):
        a_root = tmp_path / "a"; a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})
        save_namespaces({"acme": "GONE"})
        a = Participant("A", a_root)
        ensure_mailboxes(a)
        _write_outbox("A", a.root, "acme:phil", "orphaned", [])
        n = route_outboxes([a], all_agents=[a])
        assert n == 0
        assert any("orphaned" in f.read_text() for f in trash_dir("A").iterdir())

    def test_log_keeps_full_to_visible(self, namespace_agents):
        from core import agent_log_path
        a, node = namespace_agents
        _write_outbox("A", a.root, "acme:phil", "hi", [])
        route_outboxes([a, node], all_agents=[a, node])
        log = agent_log_path("A").read_text()
        assert "acme:phil" in log
        assert "namespace via NODE" in log


class TestNamespaceEgressIdentity:
    """A namespace binding presents member attribution outward by default —
    a sub-sender claim under the node's own prefix stands to any recipient.
    Binding with `--opaque` conceals instead: mail leaving the prefix presents
    as the bare prefix, one address outside whatever it fronts. Either way the
    enclosing outbox settles whose message it is."""

    @pytest.fixture
    def node_and_outsider(self, fake_home, tmp_path):
        node_root = tmp_path / "node"; node_root.mkdir()
        outsider_root = tmp_path / "outsider"; outsider_root.mkdir()
        save_registry({
            "acme-node": {"root": str(node_root)},
            "B": {"root": str(outsider_root)},
        })
        save_namespaces({"acme": "acme-node"})
        node = Participant("acme-node", node_root)
        outsider = Participant("B", outsider_root)
        ensure_mailboxes(node)
        ensure_mailboxes(outsider)
        return node, outsider

    def _conceal(self, *prefixes: str) -> None:
        save_namespace_options({p: {"opaque": True} for p in prefixes})

    def _stage(self, node: Participant, claimed: str, to: str) -> None:
        f = outbox_dir(node.root) / "20260101T000000_NODE.json"
        f.write_text(json.dumps({
            "from": claimed, "to": to, "content": "status green", "files": [],
        }))

    def _delivered_from(self, node, outsider) -> str:
        route_outboxes([node, outsider], all_agents=[node, outsider])
        return json.loads(next(inbox_dir("B").iterdir()).read_text())["from"]

    # --- default: attribution outward ---

    def test_member_claim_stands_to_an_outsider_by_default(self, node_and_outsider):
        node, outsider = node_and_outsider
        self._stage(node, "acme:lead", "B")
        assert self._delivered_from(node, outsider) == "acme:lead"

    def test_node_name_egresses_as_itself_by_default(self, node_and_outsider):
        node, outsider = node_and_outsider
        self._stage(node, "acme-node", "B")
        assert self._delivered_from(node, outsider) == "acme-node"

    def test_spoofed_claim_is_discarded_by_default(self, node_and_outsider):
        node, outsider = node_and_outsider
        self._stage(node, "VICTIM", "B")
        assert self._delivered_from(node, outsider) == "acme-node"

    # --- opaque: the binding conceals ---

    def test_member_egress_presents_the_bare_namespace(self, node_and_outsider):
        node, outsider = node_and_outsider
        self._conceal("acme")
        self._stage(node, "acme:lead", "B")
        assert self._delivered_from(node, outsider) == "acme"

    def test_node_name_egresses_as_its_sole_opaque_namespace(self, node_and_outsider):
        node, outsider = node_and_outsider
        self._conceal("acme")
        self._stage(node, "acme-node", "B")
        assert self._delivered_from(node, outsider) == "acme"

    def test_presented_prefix_uses_the_registry_spelling(self, node_and_outsider):
        node, outsider = node_and_outsider
        save_namespaces({"Acme": "acme-node"})
        self._conceal("Acme")
        self._stage(node, "ACME:lead", "B")
        assert self._delivered_from(node, outsider) == "Acme"

    def test_spoofed_claim_presents_as_the_opaque_prefix(self, node_and_outsider):
        # The claim buys nothing — the node presents as its own namespace, and
        # `VICTIM` is gone.
        node, outsider = node_and_outsider
        self._conceal("acme")
        self._stage(node, "VICTIM", "B")
        assert self._delivered_from(node, outsider) == "acme"

    def test_several_opaque_prefixes_leave_the_agent_name_standing(self, node_and_outsider):
        node, outsider = node_and_outsider
        save_namespaces({"acme": "acme-node", "ops": "acme-node"})
        self._conceal("acme", "ops")
        self._stage(node, "acme-node", "B")
        assert self._delivered_from(node, outsider) == "acme-node"

    def test_several_opaque_prefixes_still_honor_a_claim_under_one(self, node_and_outsider):
        node, outsider = node_and_outsider
        save_namespaces({"acme": "acme-node", "ops": "acme-node"})
        self._conceal("acme", "ops")
        self._stage(node, "ops:lead", "B")
        assert self._delivered_from(node, outsider) == "ops"

    def test_opacity_is_per_prefix(self, node_and_outsider):
        # A claim under the node's non-opaque prefix keeps attribution even
        # though a sibling prefix conceals.
        node, outsider = node_and_outsider
        save_namespaces({"acme": "acme-node", "ops": "acme-node"})
        self._conceal("acme")
        self._stage(node, "ops:lead", "B")
        assert self._delivered_from(node, outsider) == "ops:lead"

    def test_traffic_inside_the_opaque_prefix_keeps_the_sub_sender(self, node_and_outsider):
        # The recipient is the node itself, so there is no local delivery to
        # read — the remote publish carries the envelope that would go out.
        node, outsider = node_and_outsider
        self._conceal("acme")
        published: list[dict] = []

        def publish(msg, sender_name, succeeded_so_far, attempt_count):
            published.append(msg)
            return ["hub"]

        self._stage(node, "acme:phil", "acme:jane")
        route_outboxes(
            [node, outsider], all_agents=[node, outsider],
            publish_remotes=publish, configured_remote_ids=["hub"],
        )
        assert [m["from"] for m in published] == ["acme:phil"]

    def test_reply_to_the_bare_namespace_routes_in_through_the_binding(
        self, node_and_outsider
    ):
        # The round trip opacity depends on: the outsider answers the name it
        # saw, and the message enters at the bound node with `to` intact for
        # the node to self-route. Routing is opacity-independent.
        node, outsider = node_and_outsider
        self._conceal("acme")
        _write_outbox("B", outsider.root, "acme", "thanks", [])
        n = route_outboxes([node, outsider], all_agents=[node, outsider])
        assert n == 1
        delivered = json.loads(next(inbox_dir("acme-node").iterdir()).read_text())
        assert delivered["to"] == "acme"
        assert delivered["from"] == "B"

    def test_unnamespace_clears_the_opacity_option(self, node_and_outsider, capsys):
        from commands import cmd_namespace, cmd_unnamespace
        assert cmd_namespace(["acme", "acme-node", "--opaque"]) == 0
        assert "opaque" in capsys.readouterr().out
        assert cmd_unnamespace(["acme"]) == 0
        from registry import load_namespace_options
        assert load_namespace_options() == {}

    def test_rebind_without_the_flag_clears_opacity(self, node_and_outsider, capsys):
        from commands import cmd_namespace
        from registry import opaque_prefixes
        assert cmd_namespace(["acme", "acme-node", "--opaque"]) == 0
        assert opaque_prefixes() == {"acme"}
        assert cmd_namespace(["acme", "acme-node"]) == 0
        assert opaque_prefixes() == set()


class TestEnvelopeMeta:
    """Issue #167 — the envelope carries an opaque `meta` object so one node can
    hand another its protocol metadata (r4t's message class). a8s is the
    courier: it copies the object across every hop it owns and never reads a
    key of it."""

    def _stage(self, sender: Participant, name: str, to: str, meta: dict) -> None:
        f = outbox_dir(sender.root) / "20260101T000000_A.json"
        f.write_text(json.dumps({
            "from": name, "to": to, "content": "status green",
            "files": [], "meta": meta,
        }))

    def test_local_routing_carries_meta(self, two_agents):
        a, b = two_agents
        self._stage(a, "A", "B", {"class": "auto"})
        route_outboxes([a, b], all_agents=[a, b])
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["meta"] == {"class": "auto"}

    def test_alias_fanout_carries_meta_to_every_recipient(self, three_agents):
        a, b, c = three_agents
        save_aliases({"devs": ["B", "C"]})
        self._stage(a, "A", "devs", {"class": "auto"})
        route_outboxes([a, b, c], all_agents=[a, b, c])
        for n in ("B", "C"):
            m = json.loads(next(inbox_dir(n).iterdir()).read_text())
            assert m["meta"] == {"class": "auto"}

    def test_remote_publish_carries_meta(self, two_agents):
        a, b = two_agents
        published: list[dict] = []

        def publish(msg, sender_name, succeeded_so_far, attempt_count):
            published.append(msg)
            return ["hub"]

        self._stage(a, "A", "B", {"class": "auto"})
        route_outboxes(
            [a, b], all_agents=[a, b],
            publish_remotes=publish, configured_remote_ids=["hub"],
        )
        assert [m["meta"] for m in published] == [{"class": "auto"}]

    def test_namespace_egress_stamping_leaves_meta_alone(self, fake_home, tmp_path):
        # The stamp rewrites how the sender presents (#315). Metadata rides
        # through untouched — the two decisions are independent.
        node_root = tmp_path / "node"; node_root.mkdir()
        outsider_root = tmp_path / "outsider"; outsider_root.mkdir()
        save_registry({
            "acme-node": {"root": str(node_root)},
            "B": {"root": str(outsider_root)},
        })
        save_namespaces({"acme": "acme-node"})
        save_namespace_options({"acme": {"opaque": True}})
        node = Participant("acme-node", node_root)
        outsider = Participant("B", outsider_root)
        ensure_mailboxes(node)
        ensure_mailboxes(outsider)
        self._stage(node, "acme:lead", "B", {"class": "auto"})
        route_outboxes([node, outsider], all_agents=[node, outsider])
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["from"] == "acme"
        assert delivered["meta"] == {"class": "auto"}


class TestAtomicFanout:
    """Issue #67 — `route_outboxes` stages routed copies under each recipient's
    `inbox.tmp/<source-name>` and only renames them into `inbox/` after every
    recipient has staged. A crash mid-fan-out should not produce duplicates
    on retry: recipients whose final `inbox/<source-name>` already exists are
    skipped."""

    def test_uses_source_filename_in_inbox(self, three_agents):
        a, b, c = three_agents
        out_path = _write_outbox("A", a.root, "devs", "roster msg", [])
        save_aliases({"devs": ["A", "B", "C"]})
        route_outboxes([a, b, c], all_agents=[a, b, c])
        # Recipients receive a file named exactly like the source outbox file.
        b_files = list(inbox_dir("B").iterdir())
        assert len(b_files) == 1
        assert b_files[0].name == out_path.name
        c_files = list(inbox_dir("C").iterdir())
        assert c_files[0].name == out_path.name

    def test_inbox_tmp_is_empty_after_clean_run(self, three_agents):
        a, b, c = three_agents
        save_aliases({"devs": ["A", "B", "C"]})
        _write_outbox("A", a.root, "devs", "roster msg", [])
        route_outboxes([a, b, c], all_agents=[a, b, c])
        for p in (a, b, c):
            assert list(inbox_tmp_dir(p.name).iterdir()) == []

    def test_retry_skips_already_delivered_recipient(self, three_agents):
        # Simulate "process died after delivering to B but before unlinking
        # A's outbox." Pre-populate B's inbox with the source filename and
        # leave A's outbox file in place. Re-routing should NOT re-deliver
        # to B, only fill in C; the outbox file is then unlinked.
        a, b, c = three_agents
        save_aliases({"devs": ["A", "B", "C"]})
        out_path = _write_outbox("A", a.root, "devs", "roster msg", [])
        # Pre-populate B's inbox with a copy of the message under the same
        # filename — represents a successful prior staging that promoted to
        # inbox/ before the process died.
        with out_path.open("r", encoding="utf-8") as f:
            base_msg = json.load(f)
        base_msg["from"] = "A"  # routing force-overwrites this anyway
        # `to` stays at "devs" — strict opacity preserves the original target.
        b_pre = inbox_dir("B") / out_path.name
        with b_pre.open("w", encoding="utf-8") as f:
            json.dump(base_msg, f)

        route_outboxes([a, b, c], all_agents=[a, b, c])

        # B still has exactly one copy (no duplicate via .1 suffix).
        b_files = list(inbox_dir("B").iterdir())
        assert len(b_files) == 1
        assert b_files[0].name == out_path.name
        # C now has the message.
        c_files = list(inbox_dir("C").iterdir())
        assert len(c_files) == 1
        # Source outbox unlinked after the routing pass committed.
        assert not out_path.is_file()


class TestFileTransfer:
    """Issue #62 — outbox bundles copy into each recipient's `.files/<id>/`."""

    @pytest.fixture
    def file_agents(self, fake_home, tmp_path):
        a_root = tmp_path / "a"; a_root.mkdir()
        b_root = tmp_path / "b"; b_root.mkdir()
        save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
        a = Participant("A", a_root)
        b = Participant("B", b_root)
        ensure_mailboxes(a)
        ensure_mailboxes(b)
        return a, b

    def test_copies_file_to_recipient_files_dir(self, file_agents):
        a, b = file_agents
        payload = a.root / "report.txt"
        payload.write_text("hello payload")
        out_path = _write_staged("A", a.root, "B", "see attached", payload)
        msg_id = out_path.stem
        route_outboxes([a, b], all_agents=[a, b])
        bundle = b.files_bundle_dir(msg_id)
        assert (bundle / "report.txt").read_text() == "hello payload"
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["files"] == [{"filename": "report.txt"}]
        assert delivered["id"] == msg_id

    def test_alias_fanout_copies_to_each_recipient(self, fake_home, tmp_path):
        agents = {}
        for n in ("A", "B", "C"):
            d = tmp_path / n; d.mkdir()
            agents[n] = Participant(n, d)
        save_registry({n: {"root": str(p.root)} for n, p in agents.items()})
        save_aliases({"devs": ["B", "C"]})
        for p in agents.values():
            ensure_mailboxes(p)
        a = agents["A"]
        payload = a.root / "data.csv"
        payload.write_text("col1,col2\n1,2\n")
        out_path = _write_staged("A", a.root, "devs", "roster data", payload)
        msg_id = out_path.stem
        route_outboxes(list(agents.values()), all_agents=list(agents.values()))
        for n in ("B", "C"):
            assert (agents[n].files_bundle_dir(msg_id) / "data.csv").read_text() == "col1,col2\n1,2\n"

    def test_envelope_path_field_is_rejected(self, fake_home, tmp_path, file_agents):
        a, b = file_agents
        _write_outbox("A", a.root, "B", "leaking", [
            {"filename": "secrets.txt", "path": "/outside/secrets.txt"},
        ])
        route_outboxes([a, b], all_agents=[a, b])
        assert not files_dir(b.root).exists()
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["files"] == []

    def test_staged_attachment_outside_sender_root_delivers(self, fake_home, tmp_path):
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        drop = tmp_path / "mailbox"
        a_root.mkdir()
        b_root.mkdir()
        drop.mkdir()
        payload = drop / "note.txt"
        payload.write_text("from drop folder")
        save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
        a = Participant("A", a_root)
        b = Participant("B", b_root)
        ensure_mailboxes(a)
        ensure_mailboxes(b)
        out_path = _write_staged("A", a.root, "B", "drop attach", payload)
        msg_id = out_path.stem
        route_outboxes([a, b], all_agents=[a, b])
        assert (b.files_bundle_dir(msg_id) / "note.txt").read_text() == "from drop folder"

    def test_missing_staged_file_is_dropped(self, file_agents):
        a, b = file_agents
        _write_outbox("A", a.root, "B", "ghost", [{"filename": "ghost.txt"}])
        route_outboxes([a, b], all_agents=[a, b])
        assert not files_dir(b.root).exists()
        delivered = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert delivered["files"] == []

    def test_oversized_source_is_dropped(self, file_agents):
        a, b = file_agents
        big = a.root / "big.bin"
        big.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        _write_staged("A", a.root, "B", "huge", big)
        route_outboxes([a, b], all_agents=[a, b])
        assert not files_dir(b.root).exists()

    def test_same_basename_different_messages_both_deliver(self, file_agents):
        a, b = file_agents
        msg_ids: list[str] = []
        for i in range(2):
            entry_path = a.root / "doc.txt"
            entry_path.write_text(f"v{i}")
            out_path = _write_staged("A", a.root, "B", f"msg {i}", entry_path)
            msg_ids.append(out_path.stem)
            route_outboxes([a, b], all_agents=[a, b])
        assert len(msg_ids) == 2
        for mid, expected in zip(msg_ids, ("v0", "v1"), strict=True):
            assert (b.files_bundle_dir(mid) / "doc.txt").read_text() == expected


class TestFilesDirContract:
    """PR #137 checklist — inbound attachment routing via files_dir."""

    @pytest.fixture
    def file_agents(self, fake_home, tmp_path):
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        a_root.mkdir()
        b_root.mkdir()
        save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
        a = Participant("A", a_root)
        b = Participant("B", b_root)
        ensure_mailboxes(a)
        ensure_mailboxes(b)
        return a, b

    def test_default_delivers_under_dot_files_msg_id(self, file_agents):
        a, b = file_agents
        payload = a.root / "avatar.jpg"
        payload.write_text("image bytes")
        out_path = _write_staged("A", a.root, "B", "here", payload)
        msg_id = out_path.stem
        route_outboxes([a, b], all_agents=[a, b])
        bundle = b.files_bundle_dir(msg_id)
        assert bundle == (b.root / ".files" / msg_id).resolve()
        assert (bundle / "avatar.jpg").read_text() == "image bytes"

    def test_definition_files_dir_routes_via_registry(self, fake_home, tmp_path):
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        external = tmp_path / "var" / "attachments" / "bob"
        a_root.mkdir()
        b_root.mkdir()
        defn = tmp_path / "b-def.json"
        defn.write_text(
            json.dumps({"invoke": ["echo", "x"], "files_dir": str(external)})
        )
        save_registry({
            "A": {"root": str(a_root)},
            "B": {"root": str(b_root), "definition": str(defn)},
        })
        agents = participants_from_registry()
        by_name = {p.name: p for p in agents}
        for p in agents:
            ensure_mailboxes(p)
        payload = a_root / "avatar.jpg"
        payload.write_text("bob avatar")
        out_path = _write_staged("A", a_root, "B", "see attached", payload)
        msg_id = out_path.stem
        route_outboxes(agents, all_agents=agents)
        assert by_name["B"].files_path() == external.resolve()
        assert (external / msg_id / "avatar.jpg").read_text() == "bob avatar"
        assert not files_dir(b_root).exists()


class TestNextInboxMessage:
    def test_returns_oldest(self, fake_home, tmp_path):
        agent_root = tmp_path / "x"
        agent_root.mkdir()
        p = Participant("X", agent_root)
        ensure_mailboxes(p)
        # Drop two ULID-named JSON files directly into the inbox; ULID
        # lex-order matches creation order, so first should sort first.
        from ark.ulid import new as new_ulid
        first_id = new_ulid()
        first = inbox_dir("X") / f"{first_id}.json"
        first.write_text(json.dumps({"id": first_id, "to": "X", "content": "first"}))
        second_id = new_ulid()
        second = inbox_dir("X") / f"{second_id}.json"
        second.write_text(json.dumps({"id": second_id, "to": "X", "content": "second"}))

        result = next_inbox_message(p)
        # next_inbox_message returns the SORTED-FIRST file.
        assert result.name == sorted([first.name, second.name])[0]

    def test_returns_none_when_empty(self, fake_home, tmp_path):
        agent_root = tmp_path / "x"
        agent_root.mkdir()
        p = Participant("X", agent_root)
        ensure_mailboxes(p)
        assert next_inbox_message(p) is None


class TestNewestInboxMtime:
    def test_none_when_empty(self, fake_home, tmp_path):
        from mailbox import newest_inbox_mtime
        agent_root = tmp_path / "x"
        agent_root.mkdir()
        p = Participant("X", agent_root)
        ensure_mailboxes(p)
        assert newest_inbox_mtime(p) is None

    def test_returns_newest_mtime_as_utc(self, fake_home, tmp_path):
        import os
        from datetime import datetime, timezone
        from mailbox import newest_inbox_mtime

        agent_root = tmp_path / "x"
        agent_root.mkdir()
        p = Participant("X", agent_root)
        ensure_mailboxes(p)
        older = inbox_dir("X") / "a.json"
        newer = inbox_dir("X") / "b.json"
        older.write_text("{}")
        newer.write_text("{}")
        t0 = datetime(2026, 4, 28, 14, 30, 0, tzinfo=timezone.utc).timestamp()
        t1 = datetime(2026, 4, 28, 14, 30, 5, tzinfo=timezone.utc).timestamp()
        os.utime(older, (t0, t0))
        os.utime(newer, (t1, t1))
        got = newest_inbox_mtime(p)
        assert got is not None
        assert got.tzinfo is not None
        assert abs(got.timestamp() - t1) < 0.01


class TestIngestPhase:
    """Phase 1 of `route_outboxes` (issue #63): a8s never reads a file in
    `<root>/.outbox/`; on every pass it atomically moves new outbox files
    out to `~/.a8s/agents/<sender>/pending/` before any further processing.
    Retry sidecars and trash all live under ~/.a8s/."""

    def test_outbox_emptied_after_pass(self, two_agents):
        a, b = two_agents
        out_path = _write_outbox("A", a.root, "B", "hi", [])
        # Pre-pass: file is in A's outbox.
        assert out_path.is_file()
        route_outboxes([a, b], all_agents=[a, b])
        # Post-pass: outbox dir is empty for both senders.
        assert list(outbox_dir(a.root).iterdir()) == []
        assert list(outbox_dir(b.root).iterdir()) == []

    def test_pending_dir_holds_messages_during_routing(self, fake_home, tmp_path):
        # Solo sender with no recipients in the registry — ingest still happens
        # but processing trashes the message (no path). Verifying the ingest
        # rename in isolation requires watching the filesystem, but we can
        # observe via the trash that the file flowed through pending/.
        a_root = tmp_path / "solo"; a_root.mkdir()
        save_registry({"SOLO": {"root": str(a_root)}})
        a = Participant("SOLO", a_root)
        ensure_mailboxes(a)
        _write_outbox("SOLO", a.root, "GHOST", "lost", [])
        route_outboxes([a], all_agents=[a])
        # Outbox empty.
        assert list(outbox_dir(a.root).iterdir()) == []
        # Pending also empty (no path forward → trashed in phase 2).
        assert list(pending_dir("SOLO").iterdir()) == []
        # Trashed.
        assert any("lost" in f.read_text() for f in trash_dir("SOLO").iterdir())

    def test_ingest_from_custom_outbox_dir(self, fake_home, tmp_path):
        a_root = tmp_path / "agent"
        a_root.mkdir()
        b_root = tmp_path / "b"
        b_root.mkdir()
        external = tmp_path / "external-outbox"
        external.mkdir()
        save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
        a = Participant("A", a_root, outbox=external)
        b = Participant("B", b_root)
        ensure_mailboxes(a)
        ensure_mailboxes(b)
        msg_path = external / "01TEST.json"
        msg_path.write_text(
            json.dumps(
                {
                    "id": "01TEST",
                    "date": "2026-01-01T00:00:00Z",
                    "from": "A",
                    "to": "B",
                    "content": "from external",
                    "files": [],
                }
            )
        )
        route_outboxes([a, b], all_agents=[a, b])
        assert list(external.iterdir()) == []
        assert (inbox_dir("B") / "01TEST.json").is_file()


class TestRetrySidecar:
    """Per-message retry sidecar. With no remotes configured, the happy path
    never creates a sidecar — local delivery succeeds in one pass. The
    sidecar machinery only kicks in when something can't be delivered."""

    def test_no_sidecar_left_after_happy_path(self, two_agents):
        a, b = two_agents
        _write_outbox("A", a.root, "B", "hi", [])
        route_outboxes([a, b], all_agents=[a, b])
        # No sidecars left over for either sender.
        for s in (a, b):
            for f in pending_dir(s.name).iterdir():
                assert not f.name.endswith(".retry")
        # Pending dirs empty for both senders.
        assert list(pending_dir(a.name).iterdir()) == []

    def test_unknown_recipient_with_no_remotes_trashes_immediately(self, two_agents):
        # Defensive: with no remotes there's no point retrying — terminal
        # failure happens on the first pass. No sidecar is left behind.
        a, b = two_agents
        _write_outbox("A", a.root, "BOGUS", "lost", [])
        route_outboxes([a, b], all_agents=[a, b])
        assert list(pending_dir("A").iterdir()) == []
        trashed = list(trash_dir("A").iterdir())
        assert any("lost" in f.read_text() for f in trashed)


class TestRemotePublishHook:
    """Chunk 4 leaves the publish_remotes hook unwired. Stub it here to
    confirm the contract: when at least one configured remote hasn't yet
    accepted, the sidecar persists with bumped attempts; once every remote
    is in `succeeded_remotes`, the message finalizes (unlinks)."""

    def test_remote_failure_creates_sidecar_with_attempts(self, two_agents):
        a, b = two_agents

        def stub_publish(msg, sender_name, succeeded_so_far, attempt_count):
            # Always fail — return the input unchanged (no remote IDs added).
            return list(succeeded_so_far)

        _write_outbox("A", a.root, "B", "hi", [])
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=stub_publish,
            configured_remote_ids=["hub"],
        )
        # Local delivery succeeded — B has the message.
        assert len(list(inbox_dir("B").iterdir())) == 1
        # But the sidecar persists in A's pending/, with attempts=1 and a
        # next_attempt scheduled per BACKOFF_SCHEDULE[0].
        pending_files = [f for f in pending_dir("A").iterdir()
                         if f.name.endswith(".json") and not f.name.endswith(".retry")]
        assert len(pending_files) == 1
        sidecar_path = retry_sidecar_path(pending_files[0])
        assert sidecar_path.is_file()
        side = json.loads(sidecar_path.read_text())
        assert side["attempts"] == 1
        assert side["local_delivered"] is True
        assert side["succeeded_remotes"] == []
        assert side["next_attempt"]  # ISO timestamp set

    def test_remote_success_finalizes(self, two_agents):
        a, b = two_agents

        def stub_publish(msg, sender_name, succeeded_so_far, attempt_count):
            # Mark hub as succeeded.
            return list(succeeded_so_far) + ["hub"]

        _write_outbox("A", a.root, "B", "hi", [])
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=stub_publish,
            configured_remote_ids=["hub"],
        )
        # Local delivery + remote publish both succeeded → no sidecar, no
        # pending file.
        remaining = list(pending_dir("A").iterdir())
        assert remaining == []

    def test_remote_only_delivery_unknown_local(self, two_agents):
        # `to: GHOST` is unknown locally but remotes are configured. The
        # message should publish and finalize even without a local match.
        a, b = two_agents

        published = []

        def stub_publish(msg, sender_name, succeeded_so_far, attempt_count):
            published.append(msg.get("to"))
            return list(succeeded_so_far) + ["hub"]

        _write_outbox("A", a.root, "GHOST", "remote-only", [])
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=stub_publish,
            configured_remote_ids=["hub"],
        )
        # The publish hook saw the envelope.
        assert published == ["GHOST"]
        # Pending is clean — remote-only delivery counts as success.
        assert list(pending_dir("A").iterdir()) == []
        # Nothing in trash either.
        assert list(trash_dir("A").iterdir()) == []

    def test_remote_only_records_convo(self, two_agents, fake_home):
        from convo import load_entries

        a, b = two_agents

        def stub_publish(msg, sender_name, succeeded_so_far, attempt_count):
            return list(succeeded_so_far) + ["hub"]

        _write_outbox("A", a.root, "GHOST", "remote-only", [])
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=stub_publish,
            configured_remote_ids=["hub"],
        )
        rows = load_entries()
        assert len(rows) == 1
        assert rows[0]["from"] == "A"
        assert rows[0]["to"] == "GHOST"
        assert rows[0]["recipients"] == ["GHOST"]
        assert rows[0]["content"] == "remote-only"

    def test_backoff_exhaustion_trashes(self, two_agents):
        a, b = two_agents

        def always_fails(msg, sender_name, succeeded_so_far, attempt_count):
            return list(succeeded_so_far)

        _write_outbox("A", a.root, "B", "stubborn", [])
        # Run MAX_ATTEMPTS + 1 passes, manually overriding the sidecar's
        # next_attempt each time so the backoff gate doesn't skip us.
        for _ in range(MAX_ATTEMPTS + 1):
            route_outboxes(
                [a, b],
                all_agents=[a, b],
                publish_remotes=always_fails,
                configured_remote_ids=["hub"],
            )
            # Force the sidecar to allow another pass right away.
            for f in pending_dir("A").iterdir():
                if f.name.endswith(".retry"):
                    side = json.loads(f.read_text())
                    side["next_attempt"] = ""
                    f.write_text(json.dumps(side))
        # After exhaustion the message is in trash, sidecar is gone.
        assert list(pending_dir("A").iterdir()) == []
        trashed = list(trash_dir("A").iterdir())
        assert any("stubborn" in f.read_text() for f in trashed)

    def test_backoff_exhaustion_records_discard_and_keeps_attachment(self, two_agents):
        # A discard is the end of a message's life: it must leave a DISCARDED
        # breadcrumb naming the last failure, and it must park the attachment
        # bytes in trash rather than delete them (#93).
        from txlog import read_events

        a, b = two_agents
        payload = a.root / "doc.txt"
        payload.write_text("payload bytes")

        def always_fails(msg, sender_name, succeeded_so_far, attempt_count):
            return list(succeeded_so_far)

        svc = _StubStorage("svc")
        out_path = _write_staged("A", a.root, "GHOST", "see attached", payload)
        msg_id = out_path.stem
        for _ in range(MAX_ATTEMPTS + 1):
            route_outboxes(
                [a, b],
                all_agents=[a, b],
                publish_remotes=always_fails,
                configured_remote_ids=["hub"],
                services=[svc],
            )
            for f in pending_dir("A").iterdir():
                if f.name.endswith(".retry"):
                    side = json.loads(f.read_text())
                    side["next_attempt"] = ""
                    f.write_text(json.dumps(side))
        assert list(pending_dir("A").iterdir()) == []
        discarded = [e for e in read_events(msg_id) if e["event"] == "DISCARDED"]
        assert len(discarded) == 1
        assert discarded[0]["from"] == "A"
        assert discarded[0]["to"] == "GHOST"
        assert discarded[0]["files"] == "doc.txt"
        assert "backoff exhausted" in discarded[0]["detail"]
        assert "hub" in discarded[0]["detail"]
        # Envelope and attachment bytes both survive in trash.
        assert json.loads((trash_dir("A") / f"{msg_id}.json").read_text())["content"] == "see attached"
        assert (trash_dir("A") / msg_id / "doc.txt").read_text() == "payload bytes"

    def test_malformed_next_attempt_does_not_poison_the_pass(self, two_agents):
        # An operator-edited sidecar carrying a non-string `next_attempt` used
        # to raise out of the backoff gate and abort the whole pass (#93).
        a, b = two_agents

        def always_fails(msg, sender_name, succeeded_so_far, attempt_count):
            return list(succeeded_so_far)

        first = _write_outbox("A", a.root, "B", "first", [])
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=always_fails,
            configured_remote_ids=["hub"],
        )
        sidecar_path = retry_sidecar_path(pending_dir("A") / first.name)
        side = json.loads(sidecar_path.read_text())
        assert side["attempts"] == 1
        side["next_attempt"] = 1774000000.5
        sidecar_path.write_text(json.dumps(side))

        _write_outbox("A", a.root, "B", "second", [])
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=always_fails,
            configured_remote_ids=["hub"],
        )
        # Unparseable means due now — the message was attempted, not skipped.
        assert json.loads(sidecar_path.read_text())["attempts"] == 2
        # And the pass carried on: the message behind it still delivered.
        bodies = {json.loads(f.read_text())["content"] for f in inbox_dir("B").iterdir()}
        assert bodies == {"first", "second"}

    def test_file_payloads_skip_remote_publish(self, two_agents):
        # v1 limitation: messages with FILE: payloads stay local-only.
        # The publish hook must not be called; the sidecar should treat all
        # configured remotes as already-succeeded so the message finalizes
        # on local delivery alone.
        a, b = two_agents
        payload = a.root / "doc.txt"
        payload.write_text("payload")
        called = []

        def stub_publish(msg, sender_name, succeeded_so_far, attempt_count):
            called.append(msg)
            return list(succeeded_so_far) + ["hub"]

        _write_staged("A", a.root, "B", "see attached", payload)
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=stub_publish,
            configured_remote_ids=["hub"],
        )
        assert called == []  # publish hook not invoked
        assert list(pending_dir("A").iterdir()) == []  # finalized
        assert len(list(inbox_dir("B").iterdir())) == 1  # local delivery happened


# ---------- storage services (#90) ----------


class _StubStorage:
    """Test double for `StorageService`. Records uploads, returns deterministic
    URLs; can be configured to fail a fixed number of times before succeeding,
    or to refuse downloads of foreign URLs to mirror the real dispatch logic."""

    def __init__(self, name: str, *, fail_n: int = 0):
        self._id = name
        self._counter = 0
        self._fail_n = fail_n
        self.uploads: list[Path] = []
        self.downloads: list[str] = []
        self.bytes_for: dict[str, bytes] = {}

    @property
    def id(self) -> str:
        return self._id

    @classmethod
    def supports_config_url(cls, url: str) -> bool:
        return True

    def store(self, src: Path, *, msg_id: str = "") -> str:
        if self._fail_n > 0:
            self._fail_n -= 1
            from services import StorageError

            raise StorageError(f"{self._id}: simulated failure")
        self._counter += 1
        url = f"stub://{self._id}/{self._counter}"
        self.uploads.append(src)
        self.bytes_for[url] = src.read_bytes()
        return url

    def retrieve(self, url: str, dest: Path) -> bool:
        if not url.startswith(f"stub://{self._id}/"):
            return False
        self.downloads.append(url)
        if url not in self.bytes_for:
            from services import StorageError

            raise StorageError(f"{self._id}: missing {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.bytes_for[url])
        return True


class TestStorageUpload:
    """`_upload_files_for_remote` and the rerouted `_process_pending` branch."""

    def test_upload_to_single_service_publishes_with_storage_urls(self, two_agents):
        a, b = two_agents
        payload = a.root / "doc.txt"
        payload.write_text("payload bytes")
        published: list[dict] = []

        def stub_publish(msg, sender_name, succeeded_so_far, attempt_count):
            published.append(msg)
            return list(succeeded_so_far) + ["hub"]

        s = _StubStorage("svc")
        out_path = _write_staged("A", a.root, "GHOST", "see attached", payload)
        entry_id = out_path.stem
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=stub_publish,
            configured_remote_ids=["hub"],
            services=[s],
        )
        assert len(s.uploads) == 1
        assert len(published) == 1
        files = published[0]["files"]
        assert len(files) == 1
        assert files[0]["filename"] == "doc.txt"
        assert files[0]["storage"] == [f"stub://svc/1"]
        # `path` is dropped on the wire.
        assert "path" not in files[0]
        # Message finalized — no pending leftovers.
        assert list(pending_dir("A").iterdir()) == []

    def test_one_dead_service_does_not_block_the_send(self, two_agents):
        # Redundancy is any-of. A second storage service exists so that the
        # first one going down cannot stop the mail; requiring all-of inverted
        # that, and one unreachable remote pushed the whole message through
        # the backoff schedule and into the trash.
        a, b = two_agents
        payload = a.root / "doc.txt"
        payload.write_text("payload bytes")
        published: list[dict] = []

        def stub_publish(msg, sender_name, succeeded_so_far, attempt_count):
            published.append(msg)
            return list(succeeded_so_far) + ["hub"]

        good = _StubStorage("good")
        dead = _StubStorage("dead", fail_n=99)
        _write_staged("A", a.root, "GHOST", "see attached", payload)
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=stub_publish,
            configured_remote_ids=["hub"],
            services=[good, dead],
        )
        # Both were attempted; one copy is enough to go.
        assert len(good.uploads) == 1
        assert len(dead.uploads) == 0
        assert len(published) == 1
        # Only the service that accepted the file contributes a URL, so a
        # dead service publishes nothing rather than a link that 404s.
        urls = published[0]["files"][0]["storage"]
        assert urls == [u for u in urls if u.startswith("stub://good/")]
        # Nothing is left waiting on the dead service.
        assert list(pending_dir("A").iterdir()) == []

    def test_a_file_no_service_accepted_still_blocks(self, two_agents):
        # Any-of means at least one, not zero. An envelope whose attachment
        # nobody can fetch is the silent loss #93 closed.
        a, b = two_agents
        payload = a.root / "doc.txt"
        payload.write_text("payload bytes")
        published: list[dict] = []

        def stub_publish(msg, sender_name, succeeded_so_far, attempt_count):
            published.append(msg)
            return list(succeeded_so_far) + ["hub"]

        _write_staged("A", a.root, "GHOST", "see attached", payload)
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=stub_publish,
            configured_remote_ids=["hub"],
            services=[_StubStorage("dead", fail_n=99),
                      _StubStorage("also-dead", fail_n=99)],
        )
        assert published == []
        pending_files = [f for f in pending_dir("A").iterdir()
                         if f.name.endswith(".json") and not f.name.endswith(".retry")]
        assert len(pending_files) == 1
        sidecar = json.loads(retry_sidecar_path(pending_files[0]).read_text())
        # A blocking failure is what an eventual discard should name.
        assert "last_error" in sidecar

    def test_a_survivable_failure_is_not_the_discard_diagnostic(self, two_agents):
        # The operator still hears that a service is down, but it is not what
        # the send is waiting on, so it must not become the discard reason.
        a, b = two_agents
        payload = a.root / "doc.txt"
        payload.write_text("payload bytes")
        sidecar: dict = {}
        msg = {"id": "01KZAAAAAAAAAAAAAAAAAAAAAA", "to": "GHOST",
               "files": [{"filename": "doc.txt"}]}
        bundle = pending_bundle_dir("A", msg["id"])
        bundle.mkdir(parents=True, exist_ok=True)
        shutil.copy2(payload, bundle / "doc.txt")

        assert _upload_files_for_remote(
            msg, a, [_StubStorage("good"), _StubStorage("dead", fail_n=99)], sidecar
        ) is True
        assert "last_error" not in sidecar

    def test_sidecar_cache_survives_a_block_by_another_file(self, two_agents):
        # One file covered, one file not, so the send blocks. The retry must
        # not re-upload the file that already landed.
        a, b = two_agents
        good = _StubStorage("good")
        sidecar: dict = {}
        msg = {"id": "01KZBBBBBBBBBBBBBBBBBBBBBB", "to": "GHOST",
               "files": [{"filename": "here.txt"}, {"filename": "gone.txt"}]}
        bundle = pending_bundle_dir("A", msg["id"])
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "here.txt").write_text("payload bytes")
        # `gone.txt` is named in the envelope but its bytes never staged.

        assert _upload_files_for_remote(msg, a, [good], sidecar) is False
        assert len(good.uploads) == 1
        assert sidecar["uploaded"]["here.txt"]["good"].startswith("stub://good/")

        assert _upload_files_for_remote(msg, a, [good], sidecar) is False
        assert len(good.uploads) == 1  # unchanged — served from the sidecar

    def test_store_failure_reaches_txlog_and_sidecar(self, two_agents):
        # A failing store used to be a per-agent WARN and nothing else, so a
        # blocked upload host left zero trace for `a8s trace` (#93).
        from txlog import read_events

        a, b = two_agents
        payload = a.root / "doc.txt"
        payload.write_text("payload bytes")

        def stub_publish(msg, sender_name, succeeded_so_far, attempt_count):
            return list(succeeded_so_far) + ["hub"]

        flaky = _StubStorage("flaky", fail_n=1)
        out_path = _write_staged("A", a.root, "GHOST", "see attached", payload)
        msg_id = out_path.stem
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=stub_publish,
            configured_remote_ids=["hub"],
            services=[flaky],
        )
        failed = [e for e in read_events(msg_id) if e["event"] == "FILE_UPLOAD_FAILED"]
        assert len(failed) == 1
        assert failed[0]["from"] == "A"
        assert failed[0]["to"] == "GHOST"
        assert failed[0]["files"] == "doc.txt"
        assert "flaky" in failed[0]["detail"]
        assert "attempt 1" in failed[0]["detail"]
        # The reason also lands on the retry record so a later discard can
        # name what killed the send.
        pending_files = [f for f in pending_dir("A").iterdir() if f.name.endswith(".json")]
        side = json.loads(retry_sidecar_path(pending_files[0]).read_text())
        assert "flaky" in side["last_error"]

    def test_no_services_keeps_v1_skip(self, two_agents):
        # Already covered by `test_file_payloads_skip_remote_publish`, but
        # this version asserts the v1 fallback log explicitly hits when
        # services list is empty (vs unset).
        a, b = two_agents
        payload = a.root / "doc.txt"
        payload.write_text("x")
        published = []

        def stub_publish(msg, sender_name, succeeded_so_far, attempt_count):
            published.append(msg)
            return list(succeeded_so_far) + ["hub"]

        _write_staged("A", a.root, "B", "see attached", payload)
        route_outboxes(
            [a, b],
            all_agents=[a, b],
            publish_remotes=stub_publish,
            configured_remote_ids=["hub"],
            services=[],
        )
        assert published == []  # remote skipped — no storage configured
        # Local delivery still works.
        assert len(list(inbox_dir("B").iterdir())) == 1


class TestStorageDownload:
    """`_download_files_to_recipient` — exercised via `network.receive_envelope`
    so the test covers the whole receive-side path that an MQTT subscriber
    would drive."""

    def test_falls_through_to_second_url(self, fake_home, tmp_path):
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        b_root = tmp_path / "B"; b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        # Two services: one only handles its own URLs, one handles the second.
        s_first = _StubStorage("first")
        s_second = _StubStorage("second")
        # Pre-populate `second`'s store with a synthetic URL+bytes (as if
        # the sender had uploaded there).
        s_second.bytes_for["stub://second/42"] = b"the-payload"

        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id,
            "from": "REMOTE_X",
            "to": "B",
            "content": "see attached",
            "files": [{
                "filename": "doc.txt",
                # First URL belongs to NO configured service (foreign host).
                # Second URL belongs to `s_second`.
                "storage": ["stub://other/99", "stub://second/42"],
            }],
        }).encode()
        receive_envelope(envelope, [b], services=[s_first, s_second])
        assert (b.files_bundle_dir(msg_id) / "doc.txt").read_bytes() == b"the-payload"
        inbox_msg = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert inbox_msg["files"] == [{"filename": "doc.txt"}]

    def test_all_urls_unsupported_drops_file_keeps_message(self, fake_home, tmp_path):
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        b_root = tmp_path / "B"; b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        s = _StubStorage("only-one")  # doesn't recognize stub://other/ URLs
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "X", "to": "B",
            "content": "see attached",
            "files": [{"filename": "doc.txt", "storage": ["stub://other/99"]}],
        }).encode()
        receive_envelope(envelope, [b], services=[s])
        # Message delivered; attachment marked unavailable for the agent.
        body = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert body["files"] == [{
            "filename": "doc.txt",
            "error": "ATTACHMENT_UNAVAILABLE",
            "detail": "could not download; contact an administrator",
        }]
        assert body["content"] == "see attached"

    def test_archive_records_the_failure_the_recipient_actually_got(
        self, fake_home, tmp_path
    ):
        """The receive path must hand the archive the resolved envelope.

        `_download_files_to_recipient` returns a NEW envelope carrying
        `error`/`detail`; the original still carries `storage`. Recording the
        original taught the renderer nothing — it saw a clean entry and printed
        the same line a delivered file produces, which is the whole of #222.
        """
        import convo
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        b_root = tmp_path / "B"; b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        s = _StubStorage("only-one")  # cannot serve stub://other/ URLs
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "X", "to": "B",
            "content": "see attached",
            "files": [{"filename": "doc.txt", "storage": ["stub://other/99"]}],
        }).encode()
        receive_envelope(envelope, [b], services=[s])

        entries = convo.load_entries()
        assert len(entries) == 1
        lost = entries[0].get("files_unavailable")
        assert lost, "archive recorded a lost attachment as a delivered one"
        assert lost[0]["filename"] == "doc.txt"

    def test_alias_fanout_leaves_no_recipient_out_of_the_archive(
        self, fake_home, tmp_path, monkeypatch
    ):
        """Every recipient of one message must reach the archive row.

        Recording one row per download outcome read correctly and was silently
        destructive: `messages.message_id` is UNIQUE and `_insert_entry` used
        `INSERT OR IGNORE`, so every group after the first was dropped — the
        row *and* its `message_agents` recipients. The second recipient then
        had no conversation at all, which is worse than the bug being fixed.
        """
        import convo
        import network
        from network import receive_envelope
        from registry import save_aliases, save_registry
        from ark.ulid import new as new_ulid

        parts = []
        for name in ("B", "C"):
            r = tmp_path / name; r.mkdir()
            parts.append(Participant(name, r))
        save_registry({p.name: {"root": str(p.root)} for p in parts})
        save_aliases({"devs": ["B", "C"]})
        # wait=0 keeps a failed download on the immediate path instead of
        # deferring it, so both outcomes land in one batch.
        monkeypatch.setattr(network, "_receive_wait_seconds", lambda: 0)

        s = _StubStorage("svc")
        s.bytes_for["stub://svc/7"] = b"the-payload"
        real_retrieve = s.retrieve
        seen = {"n": 0}

        def retrieve(url, dest):
            seen["n"] += 1
            if seen["n"] > 1:  # the second recipient's copy fails
                from services import StorageError

                raise StorageError("transient")
            return real_retrieve(url, dest)

        s.retrieve = retrieve
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "X", "to": "devs",
            "content": "see attached",
            "files": [{"filename": "doc.txt", "storage": ["stub://svc/7"]}],
        }).encode()
        receive_envelope(envelope, parts, services=[s])

        assert len(convo.load_entries()) == 1
        for name in ("B", "C"):
            got = convo.load_agent_entries(name, limit=10)
            assert got, f"{name} has no conversation entry"
        # One row cannot hold both truths, and it resolves toward the loss.
        assert convo.load_entries()[0].get("files_unavailable")

    def test_a_deferred_failure_reaches_a_row_an_earlier_success_wrote(
        self, fake_home, tmp_path
    ):
        """A deferred recipient records the same message id long after the
        immediate batch. `INSERT OR IGNORE` dropped that write whole, so the
        deferred recipient never appeared in `a8s convo` and its lost file was
        never reported against a row already written clean."""
        import convo
        from ark.ulid import new as new_ulid

        msg_id = new_ulid()
        base = {"id": msg_id, "from": "X", "to": "devs", "content": "see attached"}
        convo.record(
            {**base, "files": [{"filename": "doc.txt", "path": "/x/doc.txt"}]},
            recipients=["B"],
        )
        convo.record(
            {**base, "files": [{
                "filename": "doc.txt",
                "error": "ATTACHMENT_UNAVAILABLE",
                "detail": "could not download; contact an administrator",
            }]},
            recipients=["C"],
        )

        assert len(convo.load_entries()) == 1
        assert convo.load_agent_entries("C", limit=10), "deferred recipient lost"
        entry = convo.load_entries()[0]
        lost = entry.get("files_unavailable")
        assert lost and lost[0]["filename"] == "doc.txt"
        # The index and the entry must agree. Attaching the name to
        # message_agents alone lets the lookup find a row that does not list
        # the name, so involves_agent denies what the query just asserted and
        # the rendered row credits only whoever was written first.
        assert convo.involves_agent(entry, "C")
        assert sorted(entry.get("recipients") or []) == ["B", "C"]

    def test_a_sender_declared_loss_survives_the_legacy_strip(
        self, fake_home, tmp_path
    ):
        """#212. A sender that could not upload publishes
        `{filename, error, detail}` with no `storage`, precisely so the
        recipient learns the file existed and was lost. The legacy-strip rule
        emptied the whole array whenever nothing carried `storage`, so that
        entry died on arrival and the agent saw `files: []` — the same silence,
        one hop later."""
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        b_root = tmp_path / "B"; b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "phone", "to": "B",
            "content": "see attached",
            "files": [{
                "filename": "photo.jpg",
                "error": "ATTACHMENT_UNAVAILABLE",
                "detail": "no storage configured on the sender",
            }],
        }).encode()
        receive_envelope(envelope, [b], services=[_StubStorage("svc")])

        body = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert body["files"] == [{
            "filename": "photo.jpg",
            "error": "ATTACHMENT_UNAVAILABLE",
            "detail": "no storage configured on the sender",
        }]
        assert body["content"] == "see attached"

    def test_a_mixed_envelope_downloads_the_good_and_carries_the_lost(
        self, fake_home, tmp_path
    ):
        """Surviving the strip is not enough: the download path skipped any
        entry without URLs, so in a mixed envelope the error entry was dropped
        one step later, and the sender's own reason with it."""
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        b_root = tmp_path / "B"; b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        s = _StubStorage("svc")
        s.bytes_for["stub://svc/1"] = b"the-payload"
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "phone", "to": "B",
            "content": "one made it",
            "files": [
                {"filename": "good.txt", "storage": ["stub://svc/1"]},
                {"filename": "lost.jpg", "error": "ATTACHMENT_UNAVAILABLE",
                 "detail": "upload produced no usable URL"},
            ],
        }).encode()
        receive_envelope(envelope, [b], services=[s])

        assert (b.files_bundle_dir(msg_id) / "good.txt").read_bytes() == b"the-payload"
        body = json.loads(next(inbox_dir("B").iterdir()).read_text())
        by_name = {e["filename"]: e for e in body["files"]}
        assert sorted(by_name) == ["good.txt", "lost.jpg"]
        assert not by_name["good.txt"].get("error")
        # The sender's reason, not one we invented on its behalf.
        assert by_name["lost.jpg"]["detail"] == "upload produced no usable URL"

    def test_a_sender_declared_loss_does_not_defer_delivery(
        self, fake_home, tmp_path, monkeypatch
    ):
        """There is no URL to retry, so waiting cannot change the outcome.
        Deferring would hold the message for the whole retry window and then
        deliver exactly what was available at the start."""
        import network
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        b_root = tmp_path / "B"; b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        monkeypatch.setattr(network, "_receive_wait_seconds", lambda: 900)
        submitted = []
        monkeypatch.setattr(
            network, "_submit_deferred_delivery",
            lambda *a, **k: submitted.append(a),
        )

        s = _StubStorage("svc")
        s.bytes_for["stub://svc/1"] = b"ok"
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "phone", "to": "B", "content": "hi",
            "files": [
                {"filename": "good.txt", "storage": ["stub://svc/1"]},
                {"filename": "lost.jpg", "error": "ATTACHMENT_UNAVAILABLE"},
            ],
        }).encode()
        receive_envelope(envelope, [b], services=[s])

        assert submitted == [], "deferred on a loss that can never be retried"
        assert list(inbox_dir("B").iterdir()), "message not delivered"

    def test_a_retryable_failure_still_defers_when_a_loss_shares_the_envelope(
        self, fake_home, tmp_path, monkeypatch
    ):
        """The other direction of the same discrimination, and the only shape
        in which the `source` argument can change anything.

        A sender-declared loss must not defer, but it must not suppress a
        deferral either. Sync-backed bytes are often still in flight on first
        touch; treating "some loss is present" as "nothing is worth waiting
        for" would deliver immediately and stamp a file that was seconds away
        as permanently unavailable.
        """
        import network
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        b_root = tmp_path / "B"; b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        monkeypatch.setattr(network, "_receive_wait_seconds", lambda: 900)
        submitted = []
        monkeypatch.setattr(
            network, "_submit_deferred_delivery",
            lambda *a, **k: submitted.append(a),
        )

        s = _StubStorage("svc")  # URL is claimed but the bytes are not there yet
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "phone", "to": "B", "content": "hi",
            "files": [
                {"filename": "inflight.txt", "storage": ["stub://svc/1"]},
                {"filename": "lost.jpg", "error": "ATTACHMENT_UNAVAILABLE"},
            ],
        }).encode()
        receive_envelope(envelope, [b], services=[s])

        assert submitted, "a file still worth waiting for was not deferred"

    def test_true_legacy_entries_are_still_stripped(self, fake_home, tmp_path):
        """The contract that rule exists for stands: no storage, no error, no
        entry."""
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        b_root = tmp_path / "B"; b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "old", "to": "B", "content": "hi",
            "files": [{"filename": "legacy.txt"}],
        }).encode()
        receive_envelope(envelope, [b], services=[_StubStorage("svc")])
        body = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert body["files"] == []

    def test_no_services_strips_files(self, fake_home, tmp_path):
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        b_root = tmp_path / "B"; b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "X", "to": "B",
            "content": "see attached",
            "files": [{"filename": "doc.txt", "storage": ["stub://x/1"]}],
        }).encode()
        receive_envelope(envelope, [b])
        body = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert body["files"][0]["error"] == "ATTACHMENT_UNAVAILABLE"
        assert body["content"] == "see attached"

    def test_https_presigned_without_storage_services(self, fake_home, tmp_path):
        from _fake_storage import start_fake_tempfile_server
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        server, base = start_fake_tempfile_server()
        try:
            server.files["f0001"] = b"remote-payload"
            url = f"{base}/f0001/download"
            b_root = tmp_path / "B"
            b_root.mkdir()
            save_registry({"B": {"root": str(b_root)}})
            b = Participant("B", b_root)
            msg_id = new_ulid()
            envelope = json.dumps({
                "id": msg_id,
                "from": "X",
                "to": "B",
                "content": "see attached",
                "files": [{"filename": "doc.txt", "storage": [url]}],
            }).encode()
            receive_envelope(envelope, [b], services=[])
            assert (b.files_bundle_dir(msg_id) / "doc.txt").read_bytes() == b"remote-payload"
            body = json.loads(next(inbox_dir("B").iterdir()).read_text())
            assert body["files"] == [{"filename": "doc.txt"}]
        finally:
            server.shutdown()
            server.server_close()

    def test_rejects_path_traversal_filename(self, fake_home, tmp_path):
        from network import receive_envelope
        from registry import save_registry
        from ark.ulid import new as new_ulid

        b_root = tmp_path / "B"
        b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)
        msg_id = new_ulid()
        escaped = tmp_path / "ESCAPED"
        envelope = json.dumps({
            "id": msg_id,
            "from": "X",
            "to": "B",
            "content": "evil",
            "files": [{
                "filename": "../../../ESCAPED/pwned",
                "storage": ["http://127.0.0.1:9/nope"],
            }],
        }).encode()
        receive_envelope(envelope, [b], services=[])
        assert not (escaped / "pwned").exists()
        body = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert body["files"][0]["error"] == "ATTACHMENT_UNAVAILABLE"
        assert "not a basename" in body["files"][0]["detail"]


class TestWorstAttachmentOutcome:
    """`_worst_attachment_outcome` — the archive keeps one row per message id,
    so a fan-out whose recipients disagree about a file must resolve to a
    single view. It resolves toward the loss: a lost file described as
    delivered sends a reader after something that was never written, while the
    reverse only sends them to check a file they already hold."""

    def test_no_attachments_passes_the_envelope_through(self):
        from network import _worst_attachment_outcome

        assert _worst_attachment_outcome([{"content": "hi"}])["content"] == "hi"

    def test_a_loss_outranks_a_success_in_either_order(self):
        from network import _worst_attachment_outcome

        ok = {"files": [{"filename": "doc.txt", "path": "/x/doc.txt"}]}
        lost = {"files": [{"filename": "doc.txt", "error": "ATTACHMENT_UNAVAILABLE"}]}
        assert _worst_attachment_outcome([ok, lost])["files"][0].get("error")
        assert _worst_attachment_outcome([lost, ok])["files"][0].get("error")

    def test_copies_pair_by_filename_not_position(self):
        """`_download_files_to_recipient` appends each success as it lands and
        every failure afterwards, so the orders diverge exactly when the
        outcomes do — the only case this function is ever called for. Pairing
        by index overwrote a delivered file with another recipient's lost one,
        erasing a name from the archive and duplicating another."""
        from network import _worst_attachment_outcome

        # B got both. C's a.txt failed, so its b.txt success sorts ahead of it.
        b_got = {"files": [{"filename": "a.txt"}, {"filename": "b.txt"}]}
        c_got = {"files": [
            {"filename": "b.txt"},
            {"filename": "a.txt", "error": "ATTACHMENT_UNAVAILABLE"},
        ]}
        files = _worst_attachment_outcome([b_got, c_got])["files"]
        by_name = {e["filename"]: e for e in files}
        assert sorted(by_name) == ["a.txt", "b.txt"], "a filename was lost"
        assert len(files) == 2, "a filename was duplicated"
        assert by_name["a.txt"].get("error")
        assert not by_name["b.txt"].get("error"), "a delivered file reported lost"

    def test_a_loss_only_one_recipient_saw_still_lands(self):
        from network import _worst_attachment_outcome

        files = _worst_attachment_outcome([
            {"files": [{"filename": "a.txt"}]},
            {"files": [{"filename": "b.txt", "error": "E"}]},
        ])["files"]
        by_name = {e["filename"]: e for e in files}
        assert by_name["b.txt"].get("error")
        assert not by_name["a.txt"].get("error")

    def test_the_caller_envelopes_are_left_alone(self):
        # These are the envelopes already written to each recipient's inbox.
        from network import _worst_attachment_outcome

        ok = {"files": [{"filename": "doc.txt"}]}
        _worst_attachment_outcome([ok, {"files": [{"filename": "doc.txt", "error": "E"}]}])
        assert "error" not in ok["files"][0]
