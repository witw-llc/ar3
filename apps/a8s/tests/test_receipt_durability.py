"""A delivery receipt survives the receiver's stop and a broker blip.

Both tests run the real receive wiring (`start_remotes`) against a local
mosquitto and watch the topic from a second client, so a receipt counts only
once it is on the wire.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

pytest.importorskip("paho.mqtt.client")

import network
from ar3.ulid import new as new_ulid
from core import Participant, inbox_dir
from delivery_receipt import parse_delivery_receipt
from mailbox import ensure_mailboxes
from mqtt_cluster import wait_connected
from registry import participants_from_registry, registry_path, save_registry
from txlog import read_events
from transports.mqtt import MqttTransport


class _Observer:
    """A second client on the topic that collects receipts by `for_id`."""

    def __init__(self, port: int, topic: str) -> None:
        self.receipts: list = []
        self.arrived = threading.Event()
        self.transport = MqttTransport(
            remote_id="hub",
            broker=f"mqtt://127.0.0.1:{port}",
            topic=topic,
            client_id=f"a8s-obs-{new_ulid()[-8:]}",
            clean_session=True,
        )

    def _on(self, payload: bytes) -> None:
        receipt = parse_delivery_receipt(json.loads(payload))
        if receipt is not None:
            self.receipts.append(receipt)
            self.arrived.set()

    def __enter__(self) -> "_Observer":
        self.transport.start(self._on)
        wait_connected([self.transport])
        return self

    def __exit__(self, *_exc) -> None:
        self.transport.stop()

    def wait_for(self, msg_id: str, timeout: float) -> list:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [r for r in self.receipts if r.for_id == msg_id]
            if found:
                return found
            self.arrived.wait(0.05)
            self.arrived.clear()
        return [r for r in self.receipts if r.for_id == msg_id]


@pytest.fixture
def target(fake_home, tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    save_registry({"TARGET": {"root": str(root)}})
    participant = Participant("TARGET", root)
    ensure_mailboxes(participant)
    return participant


def _receiver(port: int, topic: str, **opts) -> MqttTransport:
    return MqttTransport(
        remote_id="hub",
        broker=f"mqtt://127.0.0.1:{port}",
        topic=topic,
        client_id=f"a8s-rx-{new_ulid()[-8:]}",
        **opts,
    )


def _send(port: int, topic: str, msg_id: str) -> None:
    sender = MqttTransport(
        remote_id="hub",
        broker=f"mqtt://127.0.0.1:{port}",
        topic=topic,
        client_id=f"a8s-tx-{new_ulid()[-8:]}",
        clean_session=True,
    )
    sender.start(lambda _b: None)
    try:
        sender.publish(json.dumps({
            "id": msg_id, "from": "REMOTE_X", "to": "TARGET",
            "content": "ping", "files": [],
        }).encode())
    finally:
        sender.stop()


def _slow_receipts(monkeypatch, before) -> None:
    """Run `before()` on the worker between the inbox write and the receipt
    publish — the window the release run lost its receipt in."""
    real = network._publish_delivery_receipt

    def wrapped(*args, **kwargs):
        before()
        return real(*args, **kwargs)

    monkeypatch.setattr(network, "_publish_delivery_receipt", wrapped)


def test_receipt_is_published_when_stop_follows_the_inbox_write(
    mqtt_broker, target, monkeypatch,
):
    topic = f"a8s/test-receipt-stop-{new_ulid()}"
    _slow_receipts(monkeypatch, lambda: time.sleep(0.3))
    msg_id = new_ulid()
    with _Observer(mqtt_broker, topic) as observer:
        rx = network.start_remotes(
            [_receiver(mqtt_broker, topic)], lambda: [target], services=[],
        )
        try:
            wait_connected(rx)
            _send(mqtt_broker, topic, msg_id)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if (inbox_dir("TARGET") / f"{msg_id}.json").exists():
                    break
                time.sleep(0.001)
            else:
                pytest.fail("envelope never reached the inbox")
        finally:
            network.stop_remotes(rx)
        receipts = observer.wait_for(msg_id, timeout=3.0)
    assert [r.stage for r in receipts] == ["inbox_write"]


def test_receipt_held_during_a_broker_blip_goes_out_after_reconnect(
    mqtt_broker, target, monkeypatch,
):
    topic = f"a8s/test-receipt-blip-{new_ulid()}"
    receiver = _receiver(mqtt_broker, topic, connect_timeout_s=0.3)

    def sever_link() -> None:
        sock = receiver._client.socket()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline = time.monotonic() + 3.0
        while receiver._client.is_connected() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not receiver._client.is_connected(), "link never dropped"

    _slow_receipts(monkeypatch, sever_link)
    msg_id = new_ulid()
    with _Observer(mqtt_broker, topic) as observer:
        rx = network.start_remotes([receiver], lambda: [target], services=[])
        try:
            wait_connected(rx)
            _send(mqtt_broker, topic, msg_id)
            receipts = observer.wait_for(msg_id, timeout=8.0)
        finally:
            network.stop_remotes(rx)
    assert [r.stage for r in receipts] == ["inbox_write"]
    assert (inbox_dir("TARGET") / f"{msg_id}.json").exists()
    published = [e for e in read_events(msg_id) if e["event"] == "RECEIPT_PUBLISHED"]
    assert len(published) == 1
    assert "held until the link is back" in published[0]["detail"]


def test_an_envelope_refused_while_the_registry_is_unreadable_lands_once_it_reads(
    mqtt_broker, target, monkeypatch,
):
    """The receive path answers False while the registry will not parse, so
    the transport keeps the message unacknowledged and offers it again; once
    the registry reads, the next offer delivers it."""
    import transports.mqtt as mqtt_mod

    monkeypatch.setattr(mqtt_mod, "_RETRY_FIRST_S", 0.2)
    refused = threading.Event()
    real_out = network.out

    def watch(line: str) -> None:
        if "registry unreadable" in line:
            refused.set()
        real_out(line)

    monkeypatch.setattr(network, "out", watch)
    good = registry_path().read_text()
    registry_path().write_text("{ not json")
    topic = f"a8s/test-unreadable-registry-{new_ulid()}"
    msg_id = new_ulid()
    rx = network.start_remotes(
        [_receiver(mqtt_broker, topic)], participants_from_registry, services=[],
    )
    try:
        wait_connected(rx)
        _send(mqtt_broker, topic, msg_id)
        assert refused.wait(timeout=5.0), "the receive path never refused it"
        assert not (inbox_dir("TARGET") / f"{msg_id}.json").exists()
        registry_path().write_text(good)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (inbox_dir("TARGET") / f"{msg_id}.json").exists():
                break
            time.sleep(0.02)
        else:
            pytest.fail("envelope never reached the inbox after the registry read")
    finally:
        network.stop_remotes(rx)
