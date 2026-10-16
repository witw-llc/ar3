"""Tests for network.py — config IO, seen table, receive_envelope filter,
the one-try publish hook. The transport-side details live in
test_transport_paho.py; here we use a stub Transport so we can run without
a real broker."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

import pytest
import network

from core import (
    Participant,
    agent_log_path,
    inbox_dir,
    network_config_path,
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
from transports import PublishAmbiguous, Transport, TransportError
from ar3.ulid import new as new_ulid
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

        spec = {
            "transport": "mqtt",
            "broker": "mqtt://localhost:1883",
            "topic": "t",
            "user": "alice",
        }
        save_network_config({"remotes": {"hub": spec}})
        put_remote_secrets("hub", spec, {"pass": "s3cret"})
        merged = merge_remote_secrets("hub", load_network_config()["remotes"]["hub"])
        assert merged["user"] == "alice"
        assert merged["pass"] == "s3cret"
        # Legacy inline pass still works until rewritten.
        inline = {"transport": "mqtt", "broker": "mqtt://x", "topic": "t", "pass": "old"}
        assert merge_remote_secrets("missing", inline)["pass"] == "old"

    def test_a_secret_without_a_binding_is_missing(self, fake_home):
        from core import secrets_config_path
        from network import merge_remote_secrets

        spec = {"transport": "mqtt", "broker": "mqtt://h", "topic": "t"}
        save_network_config({"remotes": {"hub": spec}})
        secrets_config_path().write_text(
            json.dumps({"remotes": {"hub": {"pass": "old"}}, "services": {}})
        )
        assert "pass" not in merge_remote_secrets("hub", spec)

    @pytest.mark.parametrize("section", ["remotes", "services"])
    def test_a_reader_never_pairs_a_spec_with_another_writes_secret(
        self, fake_home, monkeypatch, section
    ):
        """The reader holds the spec it read from network.json while a writer
        replaces both files; the secret it then reads belongs to the new
        destination and must not be sent to the old one."""
        import network
        from commands import cmd_remote, cmd_storage

        def write(host, password):
            if section == "remotes":
                assert cmd_remote(["hub", f"mqtt://{host}", "t", "--pass", password]) == 0
            else:
                assert cmd_storage([
                    "hub", f"webdav://{host}/dav",
                    "--base-url", f"https://{host}/dav", "--password", password,
                ]) == 0

        write("old.example.com", "old-secret")
        built = []

        def capture(name, spec):
            built.append(spec)
            raise ValueError("captured")

        monkeypatch.setattr(network, "_build_transport", capture)
        monkeypatch.setattr(network, "_build_service", capture)
        monkeypatch.setattr(network, "_SERVICE_CACHE", None)
        real = network.load_secrets_config
        state = {"paused": False}

        def pausing_loader():
            if not state["paused"]:
                state["paused"] = True
                write("new.example.com", "new-secret")
            return real()

        monkeypatch.setattr(network, "load_secrets_config", pausing_loader)
        # Reader: loads the old spec, pauses, the overwrite completes, resumes.
        (network.load_remotes if section == "remotes" else network.load_services)()
        monkeypatch.setattr(network, "load_secrets_config", real)
        assert len(built) == 1
        assert "old.example.com" in json.dumps(built[0])
        assert "pass" not in built[0] and "password" not in built[0]

    @pytest.mark.parametrize("section", ["remotes", "services"])
    def test_an_unchanged_entry_still_yields_its_secret(
        self, fake_home, monkeypatch, section
    ):
        import network
        from commands import cmd_remote, cmd_storage

        if section == "remotes":
            assert cmd_remote(["hub", "mqtt://h.example.com", "t", "--pass", "pw"]) == 0
        else:
            assert cmd_storage([
                "hub", "webdav://h.example.com/dav",
                "--base-url", "https://h.example.com/dav", "--password", "pw",
            ]) == 0
        built = []

        def capture(name, spec):
            built.append(spec)
            raise ValueError("captured")

        monkeypatch.setattr(network, "_build_transport", capture)
        monkeypatch.setattr(network, "_build_service", capture)
        monkeypatch.setattr(network, "_SERVICE_CACHE", None)
        (network.load_remotes if section == "remotes" else network.load_services)()
        assert [s.get("pass") or s.get("password") for s in built] == ["pw"]


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

    def test_folder_remote_built(self, fake_home, tmp_path):
        shared = tmp_path / "Dropbox" / "A8S"
        shared.mkdir(parents=True)
        save_network_config({
            "remotes": {"box": {"transport": "folder", "path": str(shared)}}
        })
        remotes = load_remotes()
        assert [r.id for r in remotes] == ["box"]
        assert remotes[0]._base == shared

    def test_folder_missing_path_skipped(self, fake_home):
        save_network_config({"remotes": {"box": {"transport": "folder"}}})
        assert load_remotes() == []

    def test_folder_accepts_the_broker_option_bag(self, fake_home, tmp_path):
        """`load_remotes` hands every remote the node tag and, for a health
        run, the probe overrides. A folder has no broker session to name, so
        those keys have to land somewhere rather than skipping the remote."""
        shared = tmp_path / "A8S"
        shared.mkdir()
        save_network_config({
            "remotes": {"box": {"transport": "folder", "path": str(shared)}}
        })
        probe = load_remotes(
            node="node-a",
            overrides={"client_id": "a8s-health-0000", "clean_session": True, "probe": True},
        )
        assert [r.id for r in probe] == ["box"]
        assert probe[0]._probe is True


class TestRemoteIdentity:
    """`node` and `overrides` decide which broker session a process claims."""

    @pytest.fixture
    def mqtt_home(self, fake_home, monkeypatch):
        pytest.importorskip("paho.mqtt.client")
        monkeypatch.delenv("A8S_CLIENT_TAG", raising=False)
        save_network_config({
            "remotes": {
                "hub": {
                    "transport": "mqtt",
                    "broker": "mqtt://localhost:1883",
                    "topic": "t",
                }
            }
        })

    def test_node_tag_separates_nodes_on_one_host(self, mqtt_home):
        a = load_remotes(node="node-a")[0]
        b = load_remotes(node="node-b")[0]
        assert a._client_id != b._client_id
        assert load_remotes(node="node-a")[0]._client_id == a._client_id

    def test_probe_overrides_take_a_throwaway_identity(self, mqtt_home):
        node = load_remotes(node="node-a")[0]
        probe = load_remotes(
            overrides={"client_id": "a8s-health-0000", "clean_session": True}
        )[0]
        assert probe._client_id == "a8s-health-0000"
        assert probe._client_id != node._client_id
        assert probe._client._clean_session is True
        assert node._client._clean_session is False

    def test_spec_client_id_wins_over_node_tag(self, fake_home, monkeypatch):
        pytest.importorskip("paho.mqtt.client")
        monkeypatch.delenv("A8S_CLIENT_TAG", raising=False)
        save_network_config({
            "remotes": {
                "hub": {
                    "transport": "mqtt",
                    "broker": "mqtt://localhost:1883",
                    "topic": "t",
                    "client_id": "a8s-pinned",
                }
            }
        })
        assert load_remotes(node="node-a")[0]._client_id == "a8s-pinned"


# ---------- seen table ----------


def _ulid_minted_days_ago(days: float) -> str:
    from ar3.ulid import ALPHABET

    ts_ms = int((time.time() - days * 86400) * 1000)
    n = (ts_ms << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(ALPHABET[n & 0x1F])
        n >>= 5
    return "".join(reversed(chars))


def _seen_rows():
    import sqlite3
    from contextlib import closing
    from core import transactions_path

    with closing(sqlite3.connect(str(transactions_path()))) as conn:
        return dict(
            (row[0], (row[1], row[2]))
            for row in conn.execute("SELECT id, kind, seen_at FROM seen")
        )


@pytest.fixture
def fresh_expiry_clock(monkeypatch):
    monkeypatch.setattr(network, "_seen_expiry_last", None)


class TestSeenTable:
    def test_empty_initial_state(self, fake_home):
        assert seen_id_contains(new_ulid()) is False

    def test_append_then_contains(self, fake_home):
        u = new_ulid()
        seen_id_append(u)
        assert seen_id_contains(u) is True

    def test_distinct_ids_independent(self, fake_home):
        a, b = new_ulid(), new_ulid()
        seen_id_append(a)
        assert seen_id_contains(a) is True
        assert seen_id_contains(b) is False

    def test_append_is_idempotent(self, fake_home):
        u = new_ulid()
        seen_id_append(u)
        seen_id_append(u)
        seen_id_append(u, kind="receipt")
        assert list(_seen_rows()) == [u]
        assert _seen_rows()[u][0] == "message"

    def test_kinds_are_recorded(self, fake_home):
        m, r = new_ulid(), new_ulid()
        seen_id_append(m)
        seen_id_append(r, kind="receipt")
        rows = _seen_rows()
        assert rows[m][0] == "message"
        assert rows[r][0] == "receipt"

    def test_receipts_never_evict_messages(self, fake_home):
        import txlog

        m = new_ulid()
        seen_id_append(m)
        for _ in range(20_000):
            txlog.seen_insert(new_ulid(), "receipt")
        assert seen_id_contains(m) is True

    def test_a_broken_store_answers_false_and_swallows_appends(
        self, fake_home, monkeypatch
    ):
        import txlog

        def boom():
            raise OSError("disk I/O error")

        monkeypatch.setattr(txlog, "_connect", boom)
        u = new_ulid()
        seen_id_append(u)
        assert seen_id_contains(u) is False

    def test_expiry_is_by_age_and_by_kind(self, fake_home, fresh_expiry_clock):
        import txlog

        now = time.time()
        old_receipt, young_receipt = new_ulid(), new_ulid()
        young_message, old_message = new_ulid(), new_ulid()
        txlog.seen_insert(old_receipt, "receipt", now - 3 * 86400)
        txlog.seen_insert(young_receipt, "receipt", now - 2 * 86400 + 3600)
        txlog.seen_insert(young_message, "message", now - 8 * 86400 + 3600)
        txlog.seen_insert(old_message, "message", now - 9 * 86400)
        seen_id_append(new_ulid())
        rows = _seen_rows()
        assert old_receipt not in rows
        assert old_message not in rows
        assert young_receipt in rows
        assert young_message in rows

    def test_expiry_runs_at_most_once_per_ten_minutes(
        self, fake_home, fresh_expiry_clock, monkeypatch
    ):
        import txlog

        clock = [1000.0]
        monkeypatch.setattr(network.time, "monotonic", lambda: clock[0])
        seen_id_append(new_ulid())
        stale = new_ulid()
        txlog.seen_insert(stale, "message", time.time() - 9 * 86400)
        clock[0] += 599
        seen_id_append(new_ulid())
        assert stale in _seen_rows()
        clock[0] += 2
        seen_id_append(new_ulid())
        assert stale not in _seen_rows()


# ---------- publish hook ----------


def _msg(to: str = "X", content: str = "hi") -> dict:
    return {"id": new_ulid(), "from": "A", "to": to, "content": content, "files": []}


class TestPublishOnce:
    def test_publishes_to_all(self, fake_home, tmp_path):
        a_root = tmp_path / "A"; a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})
        r1 = StubTransport("r1")
        r2 = StubTransport("r2")
        publish = make_publish_remotes([r1, r2])
        outcomes = publish(_msg(), "A", set())
        assert outcomes == {"r1": (network.SENT, ""), "r2": (network.SENT, "")}
        assert len(r1.published) == 1
        assert len(r2.published) == 1
        assert json.loads(r1.published[0])["to"] == "X"
        log = agent_log_path("A").read_text()
        assert "remote r1: published -> X: hi" in log
        assert "remote r2: published -> X: hi" in log

    def test_failure_warns_and_reports_the_failed_remote(self, fake_home, tmp_path):
        a_root = tmp_path / "A"; a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})
        r1 = StubTransport("r1")
        r2 = StubTransport("r2", fail_publish=True)
        publish = make_publish_remotes([r1, r2])
        outcomes = publish(_msg(), "A", set())
        assert outcomes["r1"] == (network.SENT, "")
        assert outcomes["r2"][0] == network.FAILED
        assert "fail_publish" in outcomes["r2"][1]
        assert "WARN remote r2 publish failed" in agent_log_path("A").read_text()

    def test_ambiguous_counts_as_sent_and_says_so(self, fake_home, tmp_path):
        a_root = tmp_path / "A"; a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})

        class NoAck(StubTransport):
            def publish(self, envelope: bytes) -> None:
                self.published.append(envelope)
                raise PublishAmbiguous(f"{self._id}: no PUBACK within 30s")

        r1 = NoAck("r1")
        outcomes = make_publish_remotes([r1])(_msg(), "A", set())
        assert outcomes == {"r1": (network.AMBIGUOUS, "r1: no PUBACK within 30s")}
        assert "counted as sent" in agent_log_path("A").read_text()

    def test_a_remote_without_a_link_is_not_waited_on_again_in_the_pass(self, fake_home, tmp_path):
        a_root = tmp_path / "A"; a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})

        class Disconnected(StubTransport):
            calls = 0
            link = False

            def is_connected(self) -> bool:
                return self.link

            def publish(self, envelope: bytes) -> None:
                self.calls += 1
                if not self.link:
                    raise TransportError(f"{self._id}: broker not connected")
                self.published.append(envelope)

        r1 = Disconnected("r1")
        publish = make_publish_remotes([r1])
        down: set[str] = set()
        assert publish(_msg(), "A", down)["r1"][0] == network.FAILED
        assert down == {"r1"}
        assert publish(_msg(), "A", down)["r1"][0] == network.FAILED
        assert r1.calls == 1
        r1.link = True
        assert publish(_msg(), "A", down)["r1"][0] == network.SENT
        assert r1.calls == 2

    def test_a_failure_with_the_link_up_does_not_mark_the_remote_down(self, fake_home, tmp_path):
        a_root = tmp_path / "A"; a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})
        down: set[str] = set()
        make_publish_remotes([StubTransport("r1", fail_publish=True)])(_msg(), "A", down)
        assert down == set()

    def test_success_reaches_global_log(self, fake_home, tmp_path, monkeypatch):
        a_root = tmp_path / "A"; a_root.mkdir()
        save_registry({"A": {"root": str(a_root)}})
        diagnostics = []
        monkeypatch.setattr(network, "out", diagnostics.append)
        publish = make_publish_remotes([StubTransport("r1")])
        publish(_msg(), "A", set())
        assert any("published ->" in d and "r1" in d for d in diagnostics)


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
        assert diagnostics == [f"REMOTE_SKIP id={msg_id} to='GHOST' reason=not in local registry"]
        assert tx_events[0] == (
            "NOT_LOCAL",
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

    def test_unknown_recipient_is_not_local_never_dropped(
        self, two_local_agents, monkeypatch,
    ):
        # A shared topic delivers every envelope to every node; a node that
        # doesn't host the recipient must not log a terminal DROPPED for a
        # message some other node is about to deliver fine.
        tx_events = []
        network._REMOTE_DIAGNOSTIC_LAST.clear()
        monkeypatch.setattr(network.txlog, "log", lambda event, **fields: tx_events.append((event, fields)))
        receive_envelope(json.dumps({
            "id": new_ulid(), "from": "X", "to": "SOMEONE_ELSES_AGENT",
            "content": "not for this node", "files": [],
        }).encode(), two_local_agents)
        assert [event for event, _fields in tx_events] == ["NOT_LOCAL"]
        assert "DROPPED" not in [event for event, _fields in tx_events]

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
            envelope["a8s_control"]["version"] = 3
            receive_envelope(
                json.dumps(envelope).encode(),
                two_local_agents,
                remote_id=remote_id,
            )

        assert len(diagnostics) == 2
        assert all("unsupported or malformed a8s control envelope" in line for line in diagnostics)
        drops = [fields for event, fields in events if event == "DISCARDED"]
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


class TestOneReceiverPerMachine:
    """One envelope reaches every daemon on the machine, because each runs its
    own subscriber and resolves recipients from the shared registry. The
    seen-table insert names one of them: the insert that adds the row goes on
    to deliver, and every other sighting drops. The row goes in before
    delivery, so a slow attachment leaves no gap for a second receiver."""

    def _envelope(self, msg_id, to="B"):
        return json.dumps({
            "id": msg_id, "from": "REMOTE_X", "to": to,
            "content": "hello", "files": [],
        }).encode()

    @staticmethod
    def _receipts(published):
        return [
            parse_delivery_receipt(json.loads(b)) for b in published
        ]

    def test_the_seen_insert_has_one_winner(self, fake_home):
        u = new_ulid()
        assert seen_id_append(u) is True
        assert seen_id_append(u) is False
        assert seen_id_append(u, kind="receipt") is False

    def test_a_store_that_cannot_be_written_lets_delivery_through(
        self, fake_home, monkeypatch
    ):
        import txlog

        def broken():
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(txlog, "_connect", broken)
        assert txlog.seen_insert(new_ulid(), "message") is True

    def test_a_second_receiver_mid_delivery_is_turned_away(self, two_local_agents):
        # The second receiver arrives while the first is still downloading.
        msg_id = new_ulid()
        envelope = self._envelope(msg_id)
        seen: list[str] = []

        real_write = network._write_to_inbox

        def slow_write(msg_for_recipient, recipient, mid, *args, **kwargs):
            # Stand in for the download: a sibling receiver gets its whole run
            # in while this one is still working.
            network.receive_envelope(envelope, two_local_agents)
            seen.append(mid)
            return real_write(msg_for_recipient, recipient, mid, *args, **kwargs)

        network._write_to_inbox = slow_write
        try:
            network.receive_envelope(envelope, two_local_agents)
        finally:
            network._write_to_inbox = real_write

        assert seen == [msg_id]  # the sibling never got as far as writing
        assert len(list(inbox_dir("B").iterdir())) == 1

    def test_an_inbox_drained_between_writes_still_gets_one_copy(self, two_local_agents):
        # Why the inbox-already-contains guard is not enough: a filedrop proxy
        # empties the inbox within milliseconds, so a second writer would find
        # no envelope there and land a fresh copy.
        msg_id = new_ulid()
        envelope = self._envelope(msg_id)
        real_write = network._write_to_inbox
        reentered: list[str] = []

        def write_then_drain(msg_for_recipient, recipient, mid, *args, **kwargs):
            result = real_write(msg_for_recipient, recipient, mid, *args, **kwargs)
            if not reentered:
                reentered.append(mid)
                for landed in inbox_dir("B").iterdir():
                    landed.unlink()  # the proxy consumed it
                network.receive_envelope(envelope, two_local_agents)
            return result

        network._write_to_inbox = write_then_drain
        try:
            network.receive_envelope(envelope, two_local_agents)
        finally:
            network._write_to_inbox = real_write

        assert reentered == [msg_id]
        assert list(inbox_dir("B").iterdir()) == []

    def test_a_sibling_mid_delivery_sends_no_receipt(self, two_local_agents):
        from network import make_receive_callback

        msg_id = new_ulid()
        envelope = self._envelope(msg_id)
        first_sent: list[bytes] = []
        second_sent: list[bytes] = []
        second = make_receive_callback(
            lambda: two_local_agents, services=[],
            publish_control=second_sent.append,
        )

        def first_publish(receipt: bytes) -> None:
            # The first daemon is publishing its receipt; the second daemon's
            # subscriber is offered the same envelope now, and again later.
            second(envelope)
            first_sent.append(receipt)

        first = make_receive_callback(
            lambda: two_local_agents, services=[], publish_control=first_publish,
        )
        first(envelope)
        second(envelope)
        assert second_sent == []
        receipts = self._receipts(first_sent)
        assert [(r.for_id, r.stage) for r in receipts] == [(msg_id, "inbox_write")]
        assert len(list(inbox_dir("B").iterdir())) == 1

    def test_a_second_daemon_after_the_first_finished_sends_no_receipt(
        self, two_local_agents
    ):
        msg_id = new_ulid()
        first_sent: list[bytes] = []
        second_sent: list[bytes] = []
        receive_envelope(
            self._envelope(msg_id), two_local_agents,
            publish_control=first_sent.append,
        )
        receive_envelope(
            self._envelope(msg_id), two_local_agents,
            publish_control=second_sent.append,
        )
        assert len(first_sent) == 1
        assert second_sent == []
        assert len(list(inbox_dir("B").iterdir())) == 1

    def test_mail_for_nobody_here_is_recorded_and_logged_once(self, two_local_agents):
        from txlog import read_events

        msg_id = new_ulid()
        receive_envelope(self._envelope(msg_id, to="NOBODY"), two_local_agents)
        receive_envelope(self._envelope(msg_id, to="NOBODY"), two_local_agents)
        assert seen_id_contains(msg_id) is True
        assert [r["event"] for r in read_events(msg_id)] == ["NOT_LOCAL"]

    def test_an_unreadable_registry_drops_the_envelope_quietly(
        self, two_local_agents
    ):
        from network import make_receive_callback
        from registry import participants_from_registry, registry_path

        registry_path().write_text("{ not json")
        sent: list[bytes] = []
        callback = make_receive_callback(
            participants_from_registry, services=[], publish_control=sent.append,
        )
        msg_id = new_ulid()
        assert callback(self._envelope(msg_id, to="B")) is None
        assert sent == []


class TestFailedDeliveryDrops:
    """The seen row goes in before delivery, so the transport consumes the
    envelope whatever happens next. A delivery that fails is logged once, as
    one DROPPED row, and never tried again."""

    def _envelope(self, msg_id, to="B"):
        return json.dumps({
            "id": msg_id, "from": "REMOTE_X", "to": to,
            "content": "hello", "files": [],
        }).encode()

    def test_a_failed_inbox_write_is_logged_once_and_lost(
        self, two_local_agents, monkeypatch
    ):
        from txlog import read_events

        # A file where the inbox directory belongs: the write cannot land.
        blocked = inbox_dir("B")
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("not a directory")
        msg_id = new_ulid()
        receive_envelope(self._envelope(msg_id), two_local_agents, remote_id="hub")
        blocked.unlink()
        receive_envelope(self._envelope(msg_id), two_local_agents, remote_id="hub")

        assert seen_id_contains(msg_id) is True
        assert not (inbox_dir("B") / f"{msg_id}.json").exists()
        dropped = [r for r in read_events(msg_id) if r["event"] == "DROPPED"]
        assert len(dropped) == 1
        assert dropped[0]["to"] == "B"
        assert dropped[0]["remote"] == "hub"
        assert "inbox write failed" in dropped[0]["detail"]
        assert f"WARN dropped incoming envelope id={msg_id}" in (
            agent_log_path("B").read_text()
        )

    def test_one_lost_member_of_a_fan_out_does_not_hold_the_others(
        self, two_local_agents, monkeypatch
    ):
        save_aliases({"ops": ["A", "B"]})
        real = network._write_to_inbox

        def only_a(msg, recipient, *rest, **kw):
            if recipient.name == "B":
                return False
            return real(msg, recipient, *rest, **kw)

        sent: list[bytes] = []
        monkeypatch.setattr(network, "_write_to_inbox", only_a)
        msg_id = new_ulid()
        receive_envelope(
            self._envelope(msg_id, to="ops"), two_local_agents,
            publish_control=sent.append,
        )
        monkeypatch.setattr(network, "_write_to_inbox", real)
        receive_envelope(self._envelope(msg_id, to="ops"), two_local_agents)

        assert (inbox_dir("A") / f"{msg_id}.json").is_file()
        assert not inbox_dir("B").exists() or not list(inbox_dir("B").iterdir())
        receipts = [parse_delivery_receipt(json.loads(b)) for b in sent]
        assert [r.recipients for r in receipts] == [("A",)]

    def test_a_crash_mid_delivery_is_not_tried_again(
        self, two_local_agents, monkeypatch
    ):
        from network import make_receive_callback

        def explode(*args, **kwargs):
            raise RuntimeError("disk went away")

        callback = make_receive_callback(lambda: two_local_agents, services=[])
        msg_id = new_ulid()
        real = network._write_to_inbox
        monkeypatch.setattr(network, "_write_to_inbox", explode)
        assert callback(self._envelope(msg_id)) is None
        monkeypatch.setattr(network, "_write_to_inbox", real)
        callback(self._envelope(msg_id))
        assert seen_id_contains(msg_id) is True
        assert not inbox_dir("B").exists() or not list(inbox_dir("B").iterdir())


class TestRestartNeverRedelivers:
    def _envelope(self, msg_id, to="B"):
        return json.dumps({
            "id": msg_id, "from": "REMOTE_X", "to": to,
            "content": "hello", "files": [],
        }).encode()

    def test_receipt_traffic_cannot_rotate_a_message_out(self, two_local_agents):
        import txlog

        msg_id = new_ulid()
        first_sent: list[bytes] = []
        receive_envelope(
            self._envelope(msg_id), two_local_agents,
            publish_control=first_sent.append,
        )
        assert len(list(inbox_dir("B").iterdir())) == 1
        assert len(first_sent) == 1
        for _ in range(11_000):
            txlog.seen_insert(new_ulid(), "receipt")
        second_sent: list[bytes] = []
        receive_envelope(
            self._envelope(msg_id), two_local_agents,
            publish_control=second_sent.append,
        )
        assert len(list(inbox_dir("B").iterdir())) == 1
        assert second_sent == []

    def test_an_envelope_older_than_the_memory_is_refused(self, two_local_agents):
        from txlog import read_events

        msg_id = _ulid_minted_days_ago(8)
        sent: list[bytes] = []
        receive_envelope(
            self._envelope(msg_id), two_local_agents,
            publish_control=sent.append, remote_id="hub",
        )
        assert not inbox_dir("B").exists()
        assert sent == []
        assert seen_id_contains(msg_id) is False
        rows = read_events(msg_id)
        assert [r["event"] for r in rows] == ["EXPIRED"]
        assert rows[0]["detail"] == "older than dedup_days (7 days)"
        assert rows[0]["remote"] == "hub"
        assert rows[0]["from"] == "REMOTE_X"
        assert rows[0]["to"] == "B"

    def test_aged_mail_for_another_machine_leaves_no_row(self, two_local_agents):
        from txlog import read_events

        msg_id = _ulid_minted_days_ago(8)
        sent: list[bytes] = []
        receive_envelope(
            self._envelope(msg_id, to="elsewhere"), two_local_agents,
            publish_control=sent.append, remote_id="hub",
        )
        assert sent == []
        assert seen_id_contains(msg_id) is False
        assert read_events(msg_id) == []

    def test_an_old_receipt_is_dropped_silently(self, two_local_agents):
        from txlog import read_events

        msg_id = _ulid_minted_days_ago(2)
        envelope = json.dumps({
            "id": msg_id, "from": "REMOTE_X", "to": "B",
            "a8s_control": {"type": "delivery_receipt"},
        }).encode()
        called: list[dict] = []
        orig = network._receive_control_envelope
        network._receive_control_envelope = lambda *a, **k: called.append(a)
        try:
            receive_envelope(envelope, two_local_agents)
        finally:
            network._receive_control_envelope = orig
        assert called == []
        assert seen_id_contains(msg_id) is False
        assert read_events(msg_id) == []

    def test_a_six_day_old_message_is_delivered(self, two_local_agents):
        msg_id = _ulid_minted_days_ago(6)
        receive_envelope(self._envelope(msg_id), two_local_agents)
        assert [f.name for f in inbox_dir("B").iterdir()] == [f"{msg_id}.json"]
        assert seen_id_contains(msg_id) is True


class TestAttachmentWait:
    """The bounded fetch wait runs off the subscriber worker. The seen row is
    written and the envelope consumed when the wait starts; nothing holds the
    message for a later try."""

    def _envelope(self, msg_id, to="B"):
        return json.dumps({
            "id": msg_id, "from": "REMOTE_X", "to": to, "content": "hello",
            "files": [{"filename": "big.bin", "storage": "s3://bucket/big.bin"}],
        }).encode()

    @pytest.fixture
    def held_pool(self, monkeypatch):
        """Catch the waiting job instead of running it."""
        from concurrent.futures import Future

        class Pool:
            def __init__(self):
                self.jobs = []

            def submit(self, fn):
                self.jobs.append(fn)
                done = Future()
                done.set_result(None)
                return done

        pool = Pool()
        monkeypatch.setattr(network, "_get_attachment_pool", lambda: pool)
        monkeypatch.setattr(network, "_receive_wait_seconds", lambda: 900)
        monkeypatch.setattr(network, "_attachments_missing", lambda *a, **k: True)
        return pool

    def test_the_wait_starts_with_the_row_written_and_no_receipt(
        self, two_local_agents, held_pool, monkeypatch
    ):
        monkeypatch.setattr(
            "mailbox._download_files_to_recipient", lambda msg, *a, **k: msg
        )
        sent: list[bytes] = []
        msg_id = new_ulid()
        receive_envelope(
            self._envelope(msg_id), two_local_agents, publish_control=sent.append,
        )
        assert seen_id_contains(msg_id) is True
        assert len(held_pool.jobs) == 1
        assert sent == []
        assert not inbox_dir("B").exists() or not list(inbox_dir("B").iterdir())

        held_pool.jobs[0]()
        assert (inbox_dir("B") / f"{msg_id}.json").is_file()
        stages = [parse_delivery_receipt(json.loads(b)).stage for b in sent]
        assert stages == ["inbox_write"]

    def test_a_redelivery_during_the_wait_starts_no_second_download(
        self, two_local_agents, held_pool
    ):
        msg_id = new_ulid()
        receive_envelope(self._envelope(msg_id), two_local_agents)
        receive_envelope(self._envelope(msg_id), two_local_agents)
        assert len(held_pool.jobs) == 1

    def test_a_wait_that_raises_is_dropped_once(
        self, two_local_agents, held_pool, monkeypatch
    ):
        from txlog import read_events

        def boom(msg, *a, **k):
            if "wait_s" in k:
                return msg  # the first fetch, which only decides to wait
            raise RuntimeError("the network went away")

        monkeypatch.setattr("mailbox._download_files_to_recipient", boom)
        msg_id = new_ulid()
        receive_envelope(self._envelope(msg_id), two_local_agents)
        held_pool.jobs[0]()
        rows = [r for r in read_events(msg_id) if r["event"] == "DROPPED"]
        assert len(rows) == 1
        assert "attachment wait failed" in rows[0]["detail"]
        assert seen_id_contains(msg_id) is True

    def test_a_wait_that_cannot_start_is_dropped_once(
        self, two_local_agents, held_pool, monkeypatch
    ):
        from txlog import read_events

        class Refuses:
            def submit(self, fn):
                raise RuntimeError("pool is shut down")

        monkeypatch.setattr(network, "_get_attachment_pool", lambda: Refuses())
        msg_id = new_ulid()
        receive_envelope(self._envelope(msg_id), two_local_agents)
        rows = [r for r in read_events(msg_id) if r["event"] == "DROPPED"]
        assert len(rows) == 1
        assert "attachment wait not started" in rows[0]["detail"]
        assert seen_id_contains(msg_id) is True


def test_network_config_save_is_atomic(fake_home, monkeypatch):
    # A node reads network.json at start; a torn read there starts no remotes.
    from core import network_config_path

    calls = []
    real = network.atomic_write_text
    monkeypatch.setattr(
        network, "atomic_write_text",
        lambda path, text, **kw: calls.append(path) or real(path, text, **kw),
    )
    save_network_config({"remotes": {"r": {"transport": "folder"}}, "services": {}})
    assert calls == [network_config_path()]
    assert json.loads(network_config_path().read_text())["remotes"] == {"r": {"transport": "folder"}}


class TestReceiptLogging:
    """The agent log shows one line per delivery; every receipt lives in the
    transaction log."""

    @staticmethod
    def _receipt(msg_id, stage, node=""):
        original = {"id": msg_id, "from": "A", "to": "B"}
        return build_delivery_receipt(original, ["B"], stage, node=node)

    @staticmethod
    def _agent_log(name):
        from core import agent_log_path
        path = agent_log_path(name)
        return path.read_text() if path.exists() else ""

    def test_an_older_node_s_no_local_recipient_receipt_is_dropped_quietly(self, two_local_agents):
        from core import transactions_path

        msg_id = new_ulid()
        old = self._receipt(msg_id, "inbox_write", node="n-1")
        old["a8s_control"]["stage"] = "no_local_recipient"
        network._receive_control_envelope(old, two_local_agents, "hub")
        assert not transactions_path().exists()
        assert msg_id not in self._agent_log("A")

    def test_inbox_write_logs_a_delivered_line(self, two_local_agents):
        msg_id = new_ulid()
        network._receive_control_envelope(
            self._receipt(msg_id, "inbox_write"), two_local_agents, "hub",
        )
        log = self._agent_log("A")
        assert f"delivered id={msg_id} -> B" in log
        assert "delivery confirmed" not in log

    def test_node_is_optional_and_reaches_the_txlog(self, two_local_agents):
        from txlog import read_events

        bare = self._receipt(new_ulid(), "inbox_write")
        assert "node" not in bare["a8s_control"]
        assert parse_delivery_receipt(bare).node == ""
        msg_id = new_ulid()
        tagged = self._receipt(msg_id, "attachment_failed", node="n-abc")
        assert parse_delivery_receipt(tagged).node == "n-abc"
        network._receive_control_envelope(tagged, two_local_agents, "hub")
        details = [e["detail"] for e in read_events(msg_id)]
        assert any("attachment_failed; node=n-abc; receipt_id=" in d for d in details)
