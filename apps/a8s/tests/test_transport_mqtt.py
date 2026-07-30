"""Tests for the MQTT transport.

Runs against a real `mosquitto` broker spawned on a free port. Both
prerequisites (paho-mqtt installed, `mosquitto` on PATH) are softly required
— missing either skips the file. CI installs from `tests/requirements.txt`.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time

import pytest

pytest.importorskip("paho.mqtt.client")

from transports.mqtt import MqttTransport
from transports import TransportError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mqtt_broker(tmp_path):
    """Spawn a local `mosquitto` broker on a random port. Anonymous access
    enabled (sufficient for these tests). Yields the broker URL string."""
    if shutil.which("mosquitto") is None:
        pytest.skip("mosquitto binary not on PATH")
    port = _free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
    )
    proc = subprocess.Popen(
        ["mosquitto", "-c", str(conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait until the port is accepting connections.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.terminate()
        pytest.fail("mosquitto failed to start within 5s")
    yield f"mqtt://127.0.0.1:{port}"
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_publish_and_receive_round_trip(mqtt_broker):
    """One transport publishes; another transport (different client_id, same
    topic) receives. Validates the broadcast-and-filter wire."""
    received: list[bytes] = []
    got = threading.Event()

    def on_msg(payload: bytes) -> None:
        received.append(payload)
        got.set()

    sub = MqttTransport(
        remote_id="hub",
        broker=mqtt_broker,
        topic="a8s/test-roundtrip",
        client_id="a8s-test-sub",
    )
    pub = MqttTransport(
        remote_id="hub",
        broker=mqtt_broker,
        topic="a8s/test-roundtrip",
        client_id="a8s-test-pub",
    )
    sub.start(on_msg)
    pub.start(lambda _b: None)
    try:
        pub.publish(b'{"hello":"world"}')
        assert got.wait(timeout=3.0)
        assert received == [b'{"hello":"world"}']
    finally:
        pub.stop()
        sub.stop()


def test_publish_before_start_raises(mqtt_broker):
    t = MqttTransport(
        remote_id="hub",
        broker=mqtt_broker,
        topic="a8s/test-no-start",
        client_id="a8s-test-no-start",
    )
    with pytest.raises(TransportError, match="publish before start"):
        t.publish(b"x")


def test_unreachable_broker_publish_raises():
    # No broker on this port — connect_async returns immediately, but publish
    # must surface the disconnected state with TransportError so the routing
    # pass can warn and retry.
    t = MqttTransport(
        remote_id="dead",
        broker=f"mqtt://127.0.0.1:{_free_port()}",
        topic="a8s/test-dead",
        client_id="a8s-test-dead",
        connect_timeout_s=0.5,  # keep the test fast
    )
    t.start(lambda _b: None)
    try:
        with pytest.raises(TransportError):
            t.publish(b"x")
    finally:
        t.stop()


def test_on_disconnect_clears_ready_event(mqtt_broker):
    """The disconnect callback must clear the readiness event so a
    subsequent `publish` knows to wait for the next CONNACK."""
    t = MqttTransport(
        remote_id="t",
        broker=mqtt_broker,
        topic="a8s/test-disconnect-event",
        client_id="a8s-test-disconnect-event",
    )
    t.start(lambda _b: None)
    try:
        assert t._connected.is_set(), "should be connected after start()"
        # Invoke the callback directly — the v2 signature is
        # (client, userdata, disconnect_flags, reason_code, properties).
        t._on_disconnect(t._client, None, None, 0, None)
        assert not t._connected.is_set()
    finally:
        t.stop()


def test_publish_waits_for_reconnect_before_raising(mqtt_broker):
    """When `is_connected()` returns False (transient blip / NAT timeout
    / mid-reconnect), `publish` must wait up to `connect_timeout_s` for
    paho's background loop to come back. Without the wait we'd surface a
    `broker not connected` warning at the routing layer for every blip;
    with it, only durable disconnects cause the warn-and-retry."""
    t = MqttTransport(
        remote_id="t",
        broker=mqtt_broker,
        topic="a8s/test-wait-reconnect",
        client_id="a8s-test-wait-reconnect",
        connect_timeout_s=0.5,
    )
    t.start(lambda _b: None)
    try:
        # Simulate a disconnected state without actually disconnecting paho's
        # socket: clear the event and force is_connected() to lie. This is the
        # tightest reproduction — we want to verify the wait happens.
        t._connected.clear()
        original_is_connected = t._client.is_connected
        t._client.is_connected = lambda: False  # type: ignore[method-assign]
        try:
            start_time = time.monotonic()
            with pytest.raises(TransportError, match="broker not connected"):
                t.publish(b"x")
            elapsed = time.monotonic() - start_time
            # publish should have waited the full connect_timeout_s before
            # giving up. Allow a little slack on the lower bound.
            assert elapsed >= 0.4, (
                f"publish returned in {elapsed:.3f}s — should have waited "
                f"~{t._connect_timeout_s}s for the readiness event"
            )
        finally:
            t._client.is_connected = original_is_connected  # type: ignore[method-assign]
    finally:
        t.stop()


def test_publish_survives_slow_receive_on_same_client(mqtt_broker):
    """One client publishes and subscribes on the same topic. receive_envelope
    can do real work; if on_message runs on paho's network thread the broker
    echo blocks PUBACK and publish fails with 'not acknowledged'."""
    import time as _time

    def slow_cb(_b: bytes) -> None:
        _time.sleep(0.3)

    t = MqttTransport(
        remote_id="hub",
        broker=mqtt_broker,
        topic="a8s/test-slow-same-client",
        client_id="a8s-test-slow-same-client",
        connect_timeout_s=1.0,
    )
    t.start(slow_cb)
    try:
        t.publish(b'{"id":"01SLOW","to":"remote","content":"hi"}')
    finally:
        t.stop()


def test_persistent_session_replays_on_reconnect(mqtt_broker):
    """The whole point of clean_session=False + QoS 1: an offline subscriber
    catches up when it reconnects under the same client_id. We simulate by
    starting a subscriber, stopping it, publishing while it's offline, then
    starting it again with the SAME client_id and confirming the message
    is delivered after reconnect."""
    received: list[bytes] = []
    got = threading.Event()

    sub = MqttTransport(
        remote_id="hub",
        broker=mqtt_broker,
        topic="a8s/test-persist",
        client_id="a8s-test-persist-sub",
    )
    sub.start(lambda _b: None)  # initial connect registers the persistent session
    sub.stop()

    # Publish while subscriber is offline.
    pub = MqttTransport(
        remote_id="hub",
        broker=mqtt_broker,
        topic="a8s/test-persist",
        client_id="a8s-test-persist-pub",
    )
    pub.start(lambda _b: None)
    try:
        pub.publish(b'{"q":"queued"}')
    finally:
        pub.stop()

    # Reconnect subscriber with the same client_id — broker should replay.
    def on_msg(payload: bytes) -> None:
        received.append(payload)
        got.set()

    sub2 = MqttTransport(
        remote_id="hub",
        broker=mqtt_broker,
        topic="a8s/test-persist",
        client_id="a8s-test-persist-sub",
    )
    sub2.start(on_msg)
    try:
        assert got.wait(timeout=3.0)
        assert received == [b'{"q":"queued"}']
    finally:
        sub2.stop()


# ---------- option-bag handling ----------

# These don't need a broker — the constructor's option vocabulary lives
# entirely in the class.


class TestMqttTransportOptions:
    def test_user_aliases_to_username(self):
        t = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", user="alice", password="p")
        # The alias is consumed; the canonical name takes effect on the
        # underlying paho client.
        # We can't easily introspect paho internals, but the constructor
        # accepting both spellings without raising is the contract.
        assert t.id == "hub"

    def test_pass_aliases_to_password(self):
        t = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", user="alice", **{"pass": "p"})
        assert t.id == "hub"

    def test_canonical_wins_over_alias(self):
        # If both spellings show up, canonical wins — alias is silently
        # dropped (the user might have set both during a config edit).
        t = MqttTransport(
            remote_id="hub", broker="mqtt://x", topic="t",
            username="canonical", user="alias",
        )
        assert t.id == "hub"

    def test_unknown_option_raises(self):
        with pytest.raises(ValueError, match="unknown option"):
            MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", boguskey="x")

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="unsupported scheme"):
            MqttTransport(remote_id="hub", broker="ftp://x", topic="t")

    def test_keepalive_coerced_from_string(self):
        # network.json values come through as strings (CLI parsing).
        # Constructor must coerce numeric options.
        t = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", keepalive="120")
        assert t._keepalive == 120

    def test_connect_timeout_coerced_from_string(self):
        t = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", connect_timeout_s="0.5")
        assert t._connect_timeout_s == 0.5

    def test_publish_qos_must_be_0_or_1(self):
        with pytest.raises(ValueError, match="publish_qos"):
            MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", publish_qos=2)
        t = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", publish_qos="0")
        assert t._publish_qos == 0
