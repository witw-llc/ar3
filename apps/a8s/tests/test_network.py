"""Tests for network.py — config IO, seen-ids ring, receive_envelope filter,
publish_with_backoff hook. The transport-side details live in
test_transport_paho.py; here we use a stub Transport so we can run without
a real broker."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest
import network

from core import (
    MAX_SEEN_IDS,
    Participant,
    agent_log_path,
    inbox_dir,
    network_config_path,
    seen_ids_path,
)
from network import (
    configured_remote_ids,
    load_network_config,
    load_remotes,
    make_publish_remotes,
    receive_envelope,
    save_network_config,
    seen_id_append,
    seen_id_contains,
    start_remotes,
    stop_remotes,
)
from registry import save_aliases, save_namespaces, save_registry, save_namespace_options
from transports import Transport, TransportError
from ulid import new as new_ulid
from delivery_receipt import build_delivery_receipt, parse_delivery_receipt


# ---------- StubTransport for tests ----------


class StubTransport(Transport):
    """Captures publishes and forwards `simulate_recv(bytes)` to its callback."""

    def __init__(self, remote_id: str, *, fail_publish: bool = False):
        self._id = remote_id
        self.fail_publish = fail_publish
        self.published: list[bytes] = []
        self._on_message: Callable[[bytes], None] | None = None
        self.started = False

    @property
    def id(self) -> str:
        return self._id

    def start(self, on_message):
        self._on_message = on_message
        self.started = True

    def stop(self):
        self.started = False
        self._on_message = None

    def publish(self, envelope: bytes) -> None:
        if self.fail_publish:
            raise TransportError(f"{self._id}: fail_publish")
        self.published.append(envelope)

    def simulate_recv(self, payload: bytes) -> None:
        if self._on_message is None:
            raise RuntimeError("simulate_recv before start")
        self._on_message(payload)


# ---------- network.json IO ----------


class TestNetworkConfig:
    def test_absent_file_returns_empty(self, fake_home):
        cfg = load_network_config()
        assert cfg == {"remotes": {}, "services": {}}

    def test_round_trip(self, fake_home):
        save_network_config({"remotes": {"hub": {"transport": "mqtt", "broker": "mqtt://x", "topic": "t"}}})
        cfg = load_network_config()
        assert cfg["remotes"]["hub"]["broker"] == "mqtt://x"

    def test_malformed_treated_as_empty(self, fake_home):
        p = network_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json {")
        cfg = load_network_config()
        assert cfg == {"remotes": {}, "services": {}}

    def test_configured_remote_ids_order_preserved(self, fake_home):
        save_network_config({"remotes": {"a": {}, "z": {}, "m": {}}})
        ids = configured_remote_ids()
        assert ids == ["a", "z", "m"]

    def test_secrets_overlay_merged_at_load(self, fake_home, monkeypatch):
        from network import put_remote_secrets, merge_remote_secrets

        save_network_config({
            "remotes": {
                "hub": {
                    "transport": "mqtt",
                    "broker": "mqtt://localhost:1883",
                    "topic": "t",
                    "user": "alice",
                }
            }
        })
        put_remote_secrets("hub", {"pass": "s3cret"})
        merged = merge_remote_secrets("hub", load_network_config()["remotes"]["hub"])
        assert merged["user"] == "alice"
        assert merged["pass"] == "s3cret"
        # Legacy inline pass still works until rewritten.
        inline = {"transport": "mqtt", "broker": "mqtt://x", "topic": "t", "pass": "old"}
        assert merge_remote_secrets("missing", inline)["pass"] == "old"


class TestLoadRemotes:
    def test_unknown_transport_skipped(self, fake_home):
        save_network_config({"remotes": {"weird": {"transport": "telepathy", "broker": "x", "topic": "t"}}})
        # Should not raise; just skip the bad entry.
        remotes = load_remotes()
        assert remotes == []

    def test_mqtt_missing_fields_skipped(self, fake_home):
        save_network_config({"remotes": {"hub": {"transport": "mqtt"}}})
        remotes = load_remotes()
        assert remotes == []

    def test_unknown_option_in_config_skips_remote(self, fake_home):
        # `_build_transport` forwards unknown keys to the transport, which
        # raises ValueError — load_remotes catches and skips. This is the
        # backstop that makes `network.json` typo-tolerant at the system
        # level (the bad remote is dropped, others survive).
        save_network_config({
            "remotes": {
                "hub": {
                    "transport": "mqtt",
                    "broker": "mqtt://localhost:1883",
                    "topic": "t",
                    "boguskey": "x",
                }
            }
        })
        # Importing transports.mqtt requires paho.
        import importlib
        try:
            importlib.import_module("paho.mqtt.client")
        except ImportError:
            import pytest
            pytest.skip("paho-mqtt not installed")
        remotes = load_remotes()
        assert remotes == []


# ---------- seen-ids ring ----------


class TestSeenIdsRing:
    def test_empty_initial_state(self, fake_home):
        u = new_ulid()
        assert seen_id_contains(u) is False

    def test_append_then_contains(self, fake_home):
        u = new_ulid()
        seen_id_append(u)
        assert seen_id_contains(u) is True

    def test_distinct_ids_independent(self, fake_home):
        a, b = new_ulid(), new_ulid()
        seen_id_append(a)
        assert seen_id_contains(a) is True
        assert seen_id_contains(b) is False

    def test_rotation_at_cap(self, fake_home, monkeypatch):
        # Lower the cap so we don't have to write 10k lines.
        monkeypatch.setenv("A8S_MAX_SEEN_IDS", "5")
        ids = [new_ulid() for _ in range(8)]
        for u in ids:
            seen_id_append(u)
        # First 3 were rotated out.
        for u in ids[:3]:
            assert seen_id_contains(u) is False
        # Last 5 retained.
        for u in ids[3:]:
            assert seen_id_contains(u) is True


# ---------- publish_with_backoff hook ----------


class TestPublishWithBackoff:
    def test_publishes_to_all(self, fake_home, tmp_path):
        a_root = tmp_path / "A"; a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})
        r1 = StubTransport("r1")
        r2 = StubTransport("r2")
        publish = make_publish_remotes([r1, r2])
        msg = {"id": new_ulid(), "from": "A", "to": "X", "content": "hi", "files": []}
        succeeded = publish(msg, "A", [], 0)
        assert set(succeeded) == {"r1", "r2"}
        assert len(r1.published) == 1
        assert len(r2.published) == 1
        # Envelope is JSON-serialized msg.
        assert json.loads(r1.published[0])["to"] == "X"
        log = agent_log_path("A").read_text()
        assert "remote r1: published -> X: hi" in log
        assert "remote r2: published -> X: hi" in log

    def test_failure_warns_and_returns_partial(self, fake_home, tmp_path):
        a_root = tmp_path / "A"; a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})
        r1 = StubTransport("r1")
        r2 = StubTransport("r2", fail_publish=True)
        publish = make_publish_remotes([r1, r2])
        msg = {"id": new_ulid(), "from": "A", "to": "X", "content": "hi", "files": []}
        succeeded = publish(msg, "A", [], 0)
        assert succeeded == ["r1"]  # r2 failed → not added

    def test_skip_already_succeeded(self, fake_home, tmp_path):
        a_root = tmp_path / "A"; a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})
        r1 = StubTransport("r1")
        publish = make_publish_remotes([r1])
        msg = {"id": new_ulid(), "from": "A", "to": "X", "content": "hi", "files": []}
        # Pretend r1 already accepted.
        succeeded = publish(msg, "A", ["r1"], 0)
        assert succeeded == ["r1"]
        assert r1.published == []  # not re-published


# ---------- receive_envelope ----------


@pytest.fixture
def two_local_agents(fake_home, tmp_path):
    a_root = tmp_path / "A"; a_root.mkdir()
    b_root = tmp_path / "B"; b_root.mkdir()
    save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
    return [Participant("A", a_root), Participant("B", b_root)]


class TestReceiveEnvelope:
    def test_local_recipient_is_delivered(self, two_local_agents):
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "REMOTE_X", "to": "B",
            "content": "hello via remote", "files": [],
        }).encode()
        receive_envelope(envelope, two_local_agents)
        files = list(inbox_dir("B").iterdir())
        assert len(files) == 1
        assert files[0].name == f"{msg_id}.json"
        body = json.loads(files[0].read_text())
        assert body["from"] == "REMOTE_X"
        assert body["content"] == "hello via remote"

    def test_meta_survives_the_wire(self, two_local_agents):
        # #167: the receiving cluster writes the sender's `meta` into the inbox
        # untouched, so the wake can hand it to the node that speaks it.
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "acme", "to": "B",
            "content": "status green", "files": [], "meta": {"class": "auto"},
        }).encode()
        receive_envelope(envelope, two_local_agents)
        body = json.loads((inbox_dir("B") / f"{msg_id}.json").read_text())
        assert body["meta"] == {"class": "auto"}

    def test_unknown_recipient_records_rate_limited_diagnostic(
        self, two_local_agents, monkeypatch,
    ):
        diagnostics = []
        tx_events = []
        network._REMOTE_DIAGNOSTIC_LAST.clear()
        monkeypatch.setattr(network, "out", diagnostics.append)
        monkeypatch.setattr(network.txlog, "log", lambda event, **fields: tx_events.append((event, fields)))
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "X", "to": "GHOST",
            "content": "ignored", "files": [],
        }).encode()
        receive_envelope(envelope, two_local_agents)
        receive_envelope(json.dumps({
            "id": new_ulid(), "from": "X", "to": "GHOST",
            "content": "different secret", "files": [],
        }).encode(), two_local_agents)
        assert diagnostics == [f"REMOTE_DROP id={msg_id} to='GHOST' reason=not in local registry"]
        assert tx_events[0] == (
            "DROPPED",
            {
                "msg_id": msg_id,
                "recipient": "GHOST",
                "remote": "remote",
                "detail": "not in local registry",
            },
        )
        assert "ignored" not in diagnostics[0]
        # No inbox writes anywhere — the dirs may not even exist.
        for n in ("A", "B"):
            d = inbox_dir(n)
            assert not d.exists() or list(d.iterdir()) == []

    def test_alias_with_no_local_participants_records_diagnostic(
        self, fake_home, tmp_path, monkeypatch,
    ):
        root = tmp_path / "A"
        root.mkdir()
        save_registry({"A": {"root": str(root)}, "B": {"root": str(tmp_path / "B")}})
        save_aliases({"ops": ["B"]})
        diagnostics = []
        network._REMOTE_DIAGNOSTIC_LAST.clear()
        monkeypatch.setattr(network, "out", diagnostics.append)
        receive_envelope(json.dumps({
            "id": new_ulid(), "from": "X", "to": "ops", "content": "secret", "files": [],
        }).encode(), [Participant("A", root)])
        assert len(diagnostics) == 1
        assert "alias resolved to zero local recipients" in diagnostics[0]
        assert "secret" not in diagnostics[0]

    def test_valid_delivery_txlog_marks_inbox_write_without_content(
        self, two_local_agents, monkeypatch,
    ):
        events = []
        monkeypatch.setattr(network.txlog, "log", lambda event, **fields: events.append((event, fields)))
        msg_id = new_ulid()
        receive_envelope(json.dumps({
            "id": msg_id, "from": "X", "to": "B", "content": "private text", "files": [],
        }).encode(), two_local_agents)
        received = [fields for event, fields in events if event == "RECEIVED_REMOTE"]
        assert received == [{
            "msg_id": msg_id,
            "sender": "X",
            "recipient": "B",
            "files": None,
            "remote": "remote",
            "detail": "inbox write complete",
        }]
        assert "private text" not in repr(received)

    def test_valid_delivery_publishes_correlated_receipt(self, two_local_agents):
        published = []
        msg_id = new_ulid()
        receive_envelope(json.dumps({
            "id": msg_id, "from": "A", "to": "B", "content": "private", "files": [],
        }).encode(), two_local_agents, publish_control=published.append, remote_id="mqtt-one")

        assert len(published) == 1
        receipt = parse_delivery_receipt(json.loads(published[0]))
        assert receipt is not None
        assert receipt.for_id == msg_id
        assert receipt.sender == "A"
        assert receipt.recipients == ("B",)
        assert "private" not in published[0].decode()

    def test_receipt_is_internal_idempotent_and_never_receipted(
        self, two_local_agents, monkeypatch,
    ):
        events = []
        republished = []
        monkeypatch.setattr(network.txlog, "log", lambda event, **fields: events.append((event, fields)))
        original_id = new_ulid()
        envelope = build_delivery_receipt(
            {"id": original_id, "from": "A"},
            ["B"],
        )
        payload = json.dumps(envelope).encode()

        receive_envelope(
            payload,
            two_local_agents,
            publish_control=republished.append,
            remote_id="mqtt-one",
        )
        receive_envelope(
            payload,
            two_local_agents,
            publish_control=republished.append,
            remote_id="mqtt-one",
        )

        receipts = [fields for event, fields in events if event == "DELIVERY_RECEIPT"]
        assert len(receipts) == 1
        assert receipts[0]["msg_id"] == original_id
        assert receipts[0]["recipient"] == "B"
        assert republished == []
        assert all(not inbox_dir(name).exists() for name in ("A", "B"))

    def test_receipt_for_a_bare_namespace_lands_on_the_bound_node(
        self, two_local_agents, monkeypatch,
    ):
        # A node that owns a namespace sends under the bare prefix (#315), so
        # the name on a returning receipt is an address — resolve it through the
        # binding or the confirmation never reaches the node that sent.
        events = []
        monkeypatch.setattr(network.txlog, "log", lambda event, **fields: events.append((event, fields)))
        save_namespaces({"acme": "A"})
        save_namespace_options({"acme": {"opaque": True}})
        envelope = build_delivery_receipt({"id": new_ulid(), "from": "acme"}, ["B"])
        receive_envelope(json.dumps(envelope).encode(), two_local_agents)
        receipts = [fields for event, fields in events if event == "DELIVERY_RECEIPT"]
        assert [r["sender"] for r in receipts] == ["A"]

    def test_receipt_for_nonlocal_sender_is_ignored_without_loop(self, two_local_agents):
        envelope = build_delivery_receipt(
            {"id": new_ulid(), "from": "REMOTE_SENDER"},
            ["B"],
        )
        republished = []
        receive_envelope(
            json.dumps(envelope).encode(),
            two_local_agents,
            publish_control=republished.append,
        )
        assert republished == []

    def test_malformed_control_warning_is_rate_limited_per_remote(
        self, two_local_agents, monkeypatch,
    ):
        diagnostics = []
        events = []
        network._REMOTE_DIAGNOSTIC_LAST.clear()
        monkeypatch.setattr(network, "out", diagnostics.append)
        monkeypatch.setattr(network.txlog, "log", lambda event, **fields: events.append((event, fields)))

        for remote_id in ("one", "one", "two"):
            envelope = build_delivery_receipt(
                {"id": new_ulid(), "from": "A"},
                ["B"],
            )
            envelope["a8s_control"]["version"] = 2
            receive_envelope(
                json.dumps(envelope).encode(),
                two_local_agents,
                remote_id=remote_id,
            )

        assert len(diagnostics) == 2
        assert all("unsupported or malformed a8s control envelope" in line for line in diagnostics)
        drops = [fields for event, fields in events if event == "DROPPED"]
        assert [fields["remote"] for fields in drops] == ["one", "two"]

    def test_receipt_publish_failure_does_not_undo_inbox_write(
        self, two_local_agents, monkeypatch,
    ):
        diagnostics = []
        monkeypatch.setattr(network, "out", diagnostics.append)
        msg_id = new_ulid()

        def fail_publish(_payload):
            raise RuntimeError("broker unavailable")

        receive_envelope(json.dumps({
            "id": msg_id, "from": "A", "to": "B", "content": "private", "files": [],
        }).encode(), two_local_agents, publish_control=fail_publish, remote_id="mqtt-one")

        assert (inbox_dir("B") / f"{msg_id}.json").is_file()
        assert len(diagnostics) == 1
        assert "delivery receipt publish failed" in diagnostics[0]
        assert "private" not in diagnostics[0]

    def test_dedup_by_ulid(self, two_local_agents):
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "X", "to": "B",
            "content": "once", "files": [],
        }).encode()
        receive_envelope(envelope, two_local_agents)
        receive_envelope(envelope, two_local_agents)
        # Only one inbox write despite two arrivals.
        assert len(list(inbox_dir("B").iterdir())) == 1

    def test_alias_fanout(self, fake_home, tmp_path):
        a_root = tmp_path / "A"; a_root.mkdir()
        b_root = tmp_path / "B"; b_root.mkdir()
        save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
        save_aliases({"ops": ["A", "B"]})
        agents = [Participant("A", a_root), Participant("B", b_root)]
        envelope = json.dumps({
            "id": new_ulid(), "from": "REMOTE_X", "to": "ops",
            "content": "roster msg", "files": [],
        }).encode()
        receive_envelope(envelope, agents)
        # Both A and B got it (no sender-exclusion on inbound — sender lives
        # remotely so isn't in our local registry anyway).
        assert len(list(inbox_dir("A").iterdir())) == 1
        assert len(list(inbox_dir("B").iterdir())) == 1

    def test_namespace_recipient_delivered_to_bound_node(self, two_local_agents):
        # Issue #148: the receive-side filter resolves colon addresses via
        # the local namespaces map — this is how a cross-cluster tell to
        # `acme:phil` lands on the cluster that owns the `acme` prefix.
        save_namespaces({"acme": "B"})
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "REMOTE_X", "to": "acme:phil",
            "content": "cross-cluster prefix", "files": [],
        }).encode()
        receive_envelope(envelope, two_local_agents)
        files = list(inbox_dir("B").iterdir())
        assert len(files) == 1
        body = json.loads(files[0].read_text())
        assert body["to"] == "acme:phil"
        d = inbox_dir("A")
        assert not d.exists() or list(d.iterdir()) == []

    def test_unbound_prefix_dropped_silently(self, two_local_agents):
        envelope = json.dumps({
            "id": new_ulid(), "from": "X", "to": "ghost:phil",
            "content": "not ours", "files": [],
        }).encode()
        receive_envelope(envelope, two_local_agents)
        for n in ("A", "B"):
            d = inbox_dir(n)
            assert not d.exists() or list(d.iterdir()) == []

    def test_malformed_namespace_address_dropped_silently(self, two_local_agents):
        save_namespaces({"acme": "B"})
        envelope = json.dumps({
            "id": new_ulid(), "from": "X", "to": "acme:",
            "content": "malformed", "files": [],
        }).encode()
        receive_envelope(envelope, two_local_agents)
        d = inbox_dir("B")
        assert not d.exists() or list(d.iterdir()) == []

    def test_files_stripped(self, two_local_agents):
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id, "from": "X", "to": "B",
            "content": "see attached", "files": [{"filename": "x.txt", "path": "/sender/x.txt"}],
        }).encode()
        receive_envelope(envelope, two_local_agents)
        body = json.loads(next(inbox_dir("B").iterdir()).read_text())
        # files stripped — sender's path doesn't exist on receiver.
        assert body["files"] == []

    def test_malformed_json_dropped(self, two_local_agents):
        receive_envelope(b"not json {", two_local_agents)
        for p in two_local_agents:
            d = inbox_dir(p.name)
            assert not d.exists() or list(d.iterdir()) == []

    def test_missing_id_dropped(self, two_local_agents):
        envelope = json.dumps({"from": "X", "to": "B", "content": "no id"}).encode()
        receive_envelope(envelope, two_local_agents)
        d = inbox_dir("B")
        assert not d.exists() or list(d.iterdir()) == []

    def test_empty_to_dropped(self, two_local_agents):
        envelope = json.dumps({
            "id": new_ulid(), "from": "X", "to": "", "content": "x", "files": [],
        }).encode()
        receive_envelope(envelope, two_local_agents)
        d = inbox_dir("B")
        assert not d.exists() or list(d.iterdir()) == []


# ---------- start_remotes / stop_remotes ----------


class TestStartStop:
    def test_starts_each_remote(self, fake_home, tmp_path):
        r1 = StubTransport("r1")
        r2 = StubTransport("r2")
        started = start_remotes([r1, r2], lambda: [])
        assert r1.started and r2.started
        assert {r.id for r in started} == {"r1", "r2"}
        stop_remotes(started)
        assert not r1.started and not r2.started

    def test_failed_start_skipped(self, fake_home, tmp_path):
        class BadTransport(Transport):
            @property
            def id(self):
                return "bad"
            def start(self, on_message):
                raise RuntimeError("nope")
            def stop(self):
                pass
            def publish(self, envelope):
                pass
        good = StubTransport("good")
        started = start_remotes([BadTransport(), good], lambda: [])
        assert {r.id for r in started} == {"good"}
