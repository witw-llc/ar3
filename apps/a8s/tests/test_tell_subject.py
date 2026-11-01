"""A sender-written subject: `tell --subject`, the envelope's top-level
`subject`, routing and the transports carrying it, `a8s convo --json`, and
the MCP `a8s_tell` tool."""
from __future__ import annotations

import json
import os
import threading

import pytest

from ar3.ulid import new as new_ulid
from core import Participant, inbox_dir, outbox_dir
from mailbox import ensure_mailboxes, route_outboxes
from registry import save_registry
from tell import SUBJECT_MAX_CHARS, write_outbox_envelope
from test_tell import _run

SUBJECT = "Re: the 0.1.125 review — two findings"


def _staged(outbox):
    (path,) = list(outbox.glob("*.json"))
    return json.loads(path.read_text(encoding="utf-8"))


class TestTellFlag:
    @pytest.mark.parametrize("form", [["--subject", SUBJECT], [f"--subject={SUBJECT}"]])
    def test_the_subject_is_a_top_level_envelope_field(self, tmp_path, form):
        outbox = tmp_path / "staging"
        outbox.mkdir()
        res = _run(tmp_path, *form, "bob", "the body", outbox=outbox)
        assert res.returncode == 0, res.stderr
        msg = _staged(outbox)
        assert msg["subject"] == SUBJECT
        assert msg["content"] == "the body"

    def test_no_subject_writes_no_field(self, tmp_path):
        outbox = tmp_path / "staging"
        outbox.mkdir()
        assert _run(tmp_path, "bob", "the body", outbox=outbox).returncode == 0
        assert "subject" not in _staged(outbox)

    def test_after_the_option_end_it_is_message_text(self, tmp_path):
        outbox = tmp_path / "staging"
        outbox.mkdir()
        res = _run(tmp_path, "--", "bob", "--subject", "x", outbox=outbox)
        assert res.returncode == 0, res.stderr
        msg = _staged(outbox)
        assert "subject" not in msg
        assert msg["content"] == "--subject x"

    def test_a_subject_at_the_limit_is_kept(self, tmp_path):
        outbox = tmp_path / "staging"
        outbox.mkdir()
        subject = "s" * SUBJECT_MAX_CHARS
        assert _run(tmp_path, "--subject", subject, "bob", "b", outbox=outbox).returncode == 0
        assert _staged(outbox)["subject"] == subject

    @pytest.mark.parametrize("subject,why", [
        ("two\nlines", "one line"),
        pytest.param("carriage\rreturn", "one line", marks=pytest.mark.skipif(
            os.name == "nt",
            reason="cmd.exe drops a CR from tell.cmd's argv, so the value that "
            "arrives has none and nothing invalid is sent",
        )),
        ("s" * (SUBJECT_MAX_CHARS + 1), "longer than 200"),
        ("   ", "requires text"),
    ])
    def test_a_bad_subject_is_refused_and_nothing_is_sent(self, tmp_path, subject, why):
        outbox = tmp_path / "staging"
        outbox.mkdir()
        res = _run(tmp_path, "--subject", subject, "bob", "b", outbox=outbox)
        assert res.returncode == 2
        assert why in res.stderr
        assert list(outbox.glob("*.json")) == []

    def test_two_subjects_are_refused(self, tmp_path):
        outbox = tmp_path / "staging"
        outbox.mkdir()
        res = _run(tmp_path, "--subject", "a", "--subject=b", "bob", "b", outbox=outbox)
        assert res.returncode == 2 and "twice" in res.stderr

    def test_a_missing_subject_value_is_refused(self, tmp_path):
        res = _run(tmp_path, "bob", "b", "--subject", outbox=tmp_path)
        assert res.returncode == 2 and "requires text" in res.stderr


@pytest.fixture
def pair(fake_home, tmp_path):
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    a_root.mkdir()
    b_root.mkdir()
    save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
    a, b = Participant("A", a_root), Participant("B", b_root)
    ensure_mailboxes(a)
    ensure_mailboxes(b)
    return a, b


def _inbox(name):
    return [json.loads(p.read_text()) for p in inbox_dir(name).glob("*.json")]


class TestRouting:
    def test_local_routing_carries_the_subject_unchanged(self, pair):
        a, b = pair
        write_outbox_envelope(outbox_dir(a.root), "B", "hi", [], extra={"subject": SUBJECT})
        route_outboxes([a, b], all_agents=[a, b])
        (msg,) = _inbox("B")
        assert msg["subject"] == SUBJECT

    def test_a_subject_that_is_not_a_string_is_not_routed(self, pair):
        a, b = pair
        write_outbox_envelope(outbox_dir(a.root), "B", "hi", [], extra={"subject": ["not", "text"]})
        route_outboxes([a, b], all_agents=[a, b])
        assert _inbox("B") == []

    def test_a_remote_envelope_lands_with_its_subject(self, pair):
        from network import receive_envelope

        a, b = pair
        envelope = {
            "id": new_ulid(), "date": "2026-10-30T12:00:00Z", "from": "far",
            "to": "B", "content": "hi", "files": [], "subject": SUBJECT,
        }
        receive_envelope(json.dumps(envelope).encode(), [a, b], remote_id="hub")
        (msg,) = _inbox("B")
        assert msg["subject"] == SUBJECT


def _wire(to="target"):
    return json.dumps({
        "id": new_ulid(), "from": "example-sender", "to": to,
        "content": "hi", "subject": SUBJECT,
    }).encode("utf-8")


class TestTransports:
    def test_the_folder_transport_carries_the_subject(self, fake_home, tmp_path):
        from transports.folder import FolderTransport

        folder = tmp_path / "box"
        folder.mkdir()
        t = FolderTransport(remote_id="box", path=str(folder))
        t.publish(_wire())
        (written,) = list(folder.glob("*.json"))
        assert json.loads(written.read_bytes())["subject"] == SUBJECT
        # Its own publish is not offered back on this machine; another
        # machine's envelope is.
        theirs = _wire()
        (folder / f"{json.loads(theirs)['id']}.json").write_bytes(theirs)
        seen = []
        t._on_message = seen.append
        t._poll_once()
        assert [json.loads(raw)["subject"] for raw in seen] == [SUBJECT]

    def test_the_s3_transport_carries_the_subject(self, fake_home):
        from test_transport_s3 import _register, _transport

        _register("target")
        sender, bucket = _transport()
        sender.publish(_wire())
        receiver, _ = _transport()
        receiver._client_cache = bucket
        seen = []
        receiver._on_message = seen.append
        receiver._poll_once()
        assert [json.loads(raw)["subject"] for raw in seen] == [SUBJECT]

    def test_the_mqtt_transport_carries_the_subject(self, mqtt_broker):
        pytest.importorskip("paho.mqtt.client")
        from transports.mqtt import MqttTransport

        url = f"mqtt://127.0.0.1:{mqtt_broker}"
        got = threading.Event()
        seen = []
        sub = MqttTransport(remote_id="hub", broker=url, topic="a8s/subject", client_id="a8s-subject-sub")
        pub = MqttTransport(remote_id="hub", broker=url, topic="a8s/subject", client_id="a8s-subject-pub")
        sub.start(lambda raw: (seen.append(raw), got.set()))
        pub.start(lambda raw: None)
        try:
            pub.publish(_wire())
            assert got.wait(timeout=3.0)
            assert json.loads(seen[0])["subject"] == SUBJECT
        finally:
            pub.stop()
            sub.stop()


class TestConvoJson:
    def _rows(self, capsys):
        from commands import cmd_convo

        assert cmd_convo(["bob", "--json"]) == 0
        return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    def test_rows_carry_the_subject_only_when_present(self, fake_home, tmp_path, capsys):
        from convo import record

        (tmp_path / "bob").mkdir()
        save_registry({"Bob": {"root": str(tmp_path / "bob")}})
        base = {"date": "2026-10-30T12:00:00.000000Z", "from": "Alice", "to": "Bob", "files": []}
        record({**base, "id": new_ulid(), "content": "with", "subject": SUBJECT}, recipients=["Bob"])
        record({**base, "id": new_ulid(), "content": "without"}, recipients=["Bob"])
        rows = {row["content"]: row for row in self._rows(capsys)}
        assert rows["with"]["subject"] == SUBJECT
        assert "subject" not in rows["without"]


class TestMcp:
    def _call(self, arguments):
        import io

        import mcp_server

        request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "tell", "arguments": arguments}}
        out = io.StringIO()
        mcp_server.serve(io.StringIO(json.dumps(request) + "\n"), out)
        return json.loads(out.getvalue())["result"]

    def test_the_schema_offers_an_optional_subject(self):
        import mcp_server

        schema = mcp_server.TOOL["inputSchema"]
        assert schema["properties"]["subject"]["type"] == "string"
        assert "subject" not in schema["required"]

    def test_a_subject_reaches_the_envelope(self, fake_home, tmp_path, monkeypatch):
        outbox = tmp_path / "staging"
        outbox.mkdir()
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        monkeypatch.chdir(tmp_path)
        result = self._call({"recipient": "BOB", "body": "hi", "subject": SUBJECT})
        assert result["isError"] is False, result
        assert _staged(outbox)["subject"] == SUBJECT

    @pytest.mark.parametrize("subject,why", [
        ("two\nlines", "one line"), (7, "must be a string"),
        ("s" * (SUBJECT_MAX_CHARS + 1), "longer than 200"),
    ])
    def test_a_bad_subject_is_an_error_and_sends_nothing(self, fake_home, tmp_path, monkeypatch, subject, why):
        outbox = tmp_path / "staging"
        outbox.mkdir()
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        result = self._call({"recipient": "BOB", "body": "hi", "subject": subject})
        assert result["isError"] is True
        assert why in result["content"][0]["text"]
        assert list(outbox.glob("*.json")) == []
