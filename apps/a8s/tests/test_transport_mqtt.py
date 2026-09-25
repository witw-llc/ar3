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

import paho.mqtt.client as mqtt

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


def test_publish_not_acknowledged_raises_after_ack_timeout(mqtt_broker):
    """rc reports success but is_published() never flips True — the PUBACK
    itself never lands (broker died mid-ack, a residential latency spike,
    etc). This is the production failure mode: the envelope usually already
    reached the broker, so publish() must raise and let the routing layer's
    retry (absorbed by the receiver's ULID dedup) run rather than silently
    calling it delivered."""
    t = MqttTransport(
        remote_id="hub",
        broker=mqtt_broker,
        topic="a8s/test-never-acked",
        client_id="a8s-test-never-acked",
        ack_timeout_s=0.3,
    )
    t.start(lambda _b: None)
    try:
        class _NeverAcked:
            rc = mqtt.MQTT_ERR_SUCCESS

            def wait_for_publish(self, timeout=None):
                time.sleep(timeout)

            def is_published(self):
                return False

        t._client.publish = lambda *a, **kw: _NeverAcked()  # type: ignore[method-assign]
        start_time = time.monotonic()
        with pytest.raises(TransportError, match="publish not acknowledged"):
            t.publish(b"x")
        elapsed = time.monotonic() - start_time
        assert elapsed >= 0.25, "should have waited ~ack_timeout_s before giving up"
    finally:
        t.stop()


def test_publish_ack_wait_uses_ack_timeout_not_connect_timeout(mqtt_broker):
    """The PUBACK wait must key off ack_timeout_s, not connect_timeout_s —
    those two waits mean different things and residential latency tails
    made a shared 5s timeout too short for the ack side."""
    t = MqttTransport(
        remote_id="hub",
        broker=mqtt_broker,
        topic="a8s/test-ack-timeout-used",
        client_id="a8s-test-ack-timeout-used",
        connect_timeout_s=5.0,
        ack_timeout_s=0.2,
    )
    t.start(lambda _b: None)
    try:
        seen = {}

        class _Recorder:
            rc = mqtt.MQTT_ERR_SUCCESS

            def wait_for_publish(self, timeout=None):
                seen["timeout"] = timeout

            def is_published(self):
                return True

        t._client.publish = lambda *a, **kw: _Recorder()  # type: ignore[method-assign]
        t.publish(b"x")
        assert seen["timeout"] == 0.2
    finally:
        t.stop()


# ---------- held control publishes ----------


def _sever(t: MqttTransport) -> None:
    sock = t._client.socket()
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    deadline = time.monotonic() + 3.0
    while t._client.is_connected() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not t._client.is_connected()


def test_is_connected_follows_the_link(mqtt_broker):
    t = MqttTransport(
        remote_id="hub", broker=mqtt_broker, topic="a8s/test-is-connected",
        client_id="a8s-test-is-connected",
    )
    assert t.is_connected() is False
    t.start(lambda _b: None)
    try:
        assert t.is_connected() is True
        _sever(t)
        assert t.is_connected() is False
        deadline = time.monotonic() + 8.0
        while not t.is_connected() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert t.is_connected() is True
    finally:
        t.stop()
    assert t.is_connected() is False


def test_is_connected_is_false_when_no_broker_answers():
    t = MqttTransport(
        remote_id="dead", broker=f"mqtt://127.0.0.1:{_free_port()}",
        topic="a8s/test-dead-link", client_id="a8s-test-dead-link",
        connect_timeout_s=0.2,
    )
    t.start(lambda _b: None)
    try:
        assert t.is_connected() is False
    finally:
        t.stop()


def test_held_control_publishes_go_out_in_order_after_reconnect(mqtt_broker):
    got: list[bytes] = []
    three = threading.Event()

    def on_msg(payload: bytes) -> None:
        got.append(payload)
        if len(got) == 3:
            three.set()

    observer = MqttTransport(
        remote_id="hub", broker=mqtt_broker, topic="a8s/test-held-order",
        client_id="a8s-test-held-order-obs",
    )
    t = MqttTransport(
        remote_id="hub", broker=mqtt_broker, topic="a8s/test-held-order",
        client_id="a8s-test-held-order",
    )
    observer.start(on_msg)
    t.start(lambda _b: None)
    try:
        _sever(t)
        assert [t.publish_control(p) for p in (b"one", b"two", b"three")] == [False] * 3
        assert three.wait(timeout=8.0), f"held publishes never arrived: {got}"
        assert got == [b"one", b"two", b"three"]
        deadline = time.monotonic() + 3.0
        while t._pending_control and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not t._pending_control
    finally:
        t.stop()
        observer.stop()


def test_control_publish_sends_at_once_when_connected(mqtt_broker):
    t = MqttTransport(
        remote_id="hub", broker=mqtt_broker, topic="a8s/test-control-now",
        client_id="a8s-test-control-now",
    )
    t.start(lambda _b: None)
    try:
        assert t.publish_control(b"now") is True
        assert not t._pending_control
    finally:
        t.stop()


def test_stop_with_link_down_says_how_many_receipts_it_dropped(monkeypatch):
    import transports.mqtt as mqtt_mod

    lines: list[str] = []
    monkeypatch.setattr(mqtt_mod, "out", lines.append)
    t = MqttTransport(
        remote_id="dead", broker=f"mqtt://127.0.0.1:{_free_port()}",
        topic="a8s/test-dead-held", client_id="a8s-test-dead-held",
        connect_timeout_s=0.2,
    )
    t.start(lambda _b: None)
    assert t.publish_control(b"a") is False
    assert t.publish_control(b"b") is False
    assert lines == []
    t.stop()
    assert len(lines) == 1
    assert "dropped 2 held delivery receipt(s)" in lines[0]


def test_held_list_overflow_drops_the_oldest_and_says_so(monkeypatch):
    import transports.mqtt as mqtt_mod

    lines: list[str] = []
    monkeypatch.setattr(mqtt_mod, "out", lines.append)
    monkeypatch.setattr(mqtt_mod, "_PENDING_CONTROL_MAX", 2)
    t = MqttTransport(
        remote_id="dead", broker=f"mqtt://127.0.0.1:{_free_port()}",
        topic="a8s/test-dead-full", client_id="a8s-test-dead-full",
        connect_timeout_s=0.2,
    )
    t.start(lambda _b: None)
    try:
        for payload in (b"1", b"2", b"3"):
            t.publish_control(payload)
        assert list(t._pending_control) == [b"2", b"3"]
        assert len(lines) == 1
        assert "dropped 1 delivery receipt" in lines[0]
    finally:
        t.stop()


def test_stop_leaves_late_arrivals_unacknowledged_for_redelivery(mqtt_broker):
    """A message that lands while `stop()` drains is neither handled nor
    acknowledged, so the broker offers it again to the same session."""
    handled: list[bytes] = []
    t = MqttTransport(
        remote_id="hub", broker=mqtt_broker, topic="a8s/test-late-arrival",
        client_id="a8s-test-late-arrival",
    )
    t.start(handled.append)
    t._stopping = True
    pub = MqttTransport(
        remote_id="hub", broker=mqtt_broker, topic="a8s/test-late-arrival",
        client_id="a8s-test-late-arrival-pub",
    )
    pub.start(lambda _b: None)
    try:
        pub.publish(b"late")
        time.sleep(0.3)
    finally:
        pub.stop()
        t.stop()
    assert handled == []

    again: list[bytes] = []
    arrived = threading.Event()
    t2 = MqttTransport(
        remote_id="hub", broker=mqtt_broker, topic="a8s/test-late-arrival",
        client_id="a8s-test-late-arrival",
    )
    t2.start(lambda b: (again.append(b), arrived.set()))
    try:
        assert arrived.wait(timeout=3.0)
        assert again == [b"late"]
    finally:
        t2.stop()


# ---------- acknowledge only what the receive path consumed ----------


def _spy_acks(t: MqttTransport, events: list) -> None:
    real = t._client.ack

    def ack(mid, qos):
        events.append(("ack", mid))
        return real(mid, qos)

    t._client.ack = ack  # type: ignore[method-assign]


def _publish_one(broker: str, topic: str, payload: bytes) -> None:
    pub = MqttTransport(
        remote_id="hub", broker=broker, topic=topic,
        client_id=f"{topic.replace('/', '-')}-pub", clean_session=True,
    )
    pub.start(lambda _b: None)
    try:
        pub.publish(payload)
    finally:
        pub.stop()


def _redelivered(broker: str, topic: str, client_id: str, wait_s: float) -> list[bytes]:
    """Reconnect under `client_id` with a consuming callback and return what
    the broker hands the session within `wait_s`."""
    got: list[bytes] = []
    arrived = threading.Event()

    def consume(payload: bytes) -> bool:
        got.append(payload)
        arrived.set()
        return True

    again = MqttTransport(
        remote_id="hub", broker=broker, topic=topic, client_id=client_id,
    )
    again.start(consume)
    try:
        arrived.wait(timeout=wait_s)
        time.sleep(0.2)
    finally:
        again.stop()
    return got


def test_a_refused_message_is_offered_again_and_acked_once_consumed(
    mqtt_broker, monkeypatch,
):
    import transports.mqtt as mqtt_mod

    monkeypatch.setattr(mqtt_mod, "_RETRY_FIRST_S", 0.1)
    topic, cid = "a8s/test-retry-then-consume", "a8s-test-retry-then-consume"
    events: list = []
    answers = iter([False, True])
    done = threading.Event()

    def on_msg(payload: bytes) -> bool:
        answer = next(answers)
        events.append(("cb", payload, answer))
        if answer:
            done.set()
        return answer

    t = MqttTransport(remote_id="hub", broker=mqtt_broker, topic=topic, client_id=cid)
    _spy_acks(t, events)
    t.start(on_msg)
    try:
        _publish_one(mqtt_broker, topic, b"once")
        assert done.wait(timeout=5.0), f"never re-offered: {events}"
        deadline = time.monotonic() + 2.0
        while events[-1][0] != "ack" and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        t.stop()
    assert [e[:3] if e[0] == "cb" else e[0] for e in events] == [
        ("cb", b"once", False), ("cb", b"once", True), "ack",
    ]
    assert _redelivered(mqtt_broker, topic, cid, wait_s=1.0) == []


def test_a_message_never_consumed_is_redelivered_to_the_next_session(mqtt_broker):
    topic, cid = "a8s/test-never-consumed", "a8s-test-never-consumed"
    offered = threading.Event()
    events: list = []

    def refuse(payload: bytes) -> bool:
        offered.set()
        return False

    t = MqttTransport(remote_id="hub", broker=mqtt_broker, topic=topic, client_id=cid)
    _spy_acks(t, events)
    t.start(refuse)
    try:
        _publish_one(mqtt_broker, topic, b"owed")
        assert offered.wait(timeout=3.0)
    finally:
        t.stop()
    assert events == []
    assert _redelivered(mqtt_broker, topic, cid, wait_s=3.0) == [b"owed"]


def test_a_consumed_message_is_acked_at_once_and_not_redelivered(mqtt_broker):
    topic, cid = "a8s/test-consumed-at-once", "a8s-test-consumed-at-once"
    events: list = []
    acked = threading.Event()

    t = MqttTransport(remote_id="hub", broker=mqtt_broker, topic=topic, client_id=cid)
    _spy_acks(t, events)
    t.start(lambda _b: True)
    try:
        _publish_one(mqtt_broker, topic, b"done")
        deadline = time.monotonic() + 3.0
        while not events and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        t.stop()
    assert [e[0] for e in events] == ["ack"]
    assert _redelivered(mqtt_broker, topic, cid, wait_s=1.0) == []


def test_a_link_drop_hands_unconsumed_messages_back_to_the_broker(
    mqtt_broker, monkeypatch,
):
    """A retry from a connection that ended is never acked on the next one:
    the transport forgets it and the broker offers it again."""
    import transports.mqtt as mqtt_mod

    monkeypatch.setattr(mqtt_mod, "_RETRY_FIRST_S", 30.0)
    topic, cid = "a8s/test-retry-link-drop", "a8s-test-retry-link-drop"
    accept = threading.Event()
    offers: list[bytes] = []
    consumed = threading.Event()

    def on_msg(payload: bytes) -> bool:
        offers.append(payload)
        if accept.is_set():
            consumed.set()
            return True
        return False

    t = MqttTransport(remote_id="hub", broker=mqtt_broker, topic=topic, client_id=cid)
    t.start(on_msg)
    try:
        _publish_one(mqtt_broker, topic, b"handed-back")
        deadline = time.monotonic() + 3.0
        while not offers and time.monotonic() < deadline:
            time.sleep(0.01)
        assert offers == [b"handed-back"]
        accept.set()
        _sever(t)
        assert consumed.wait(timeout=8.0), "the broker never offered it again"
        time.sleep(0.1)
        assert not t._retry
    finally:
        t.stop()
    assert offers == [b"handed-back", b"handed-back"]
    assert _redelivered(mqtt_broker, topic, cid, wait_s=1.0) == []


def test_retry_list_overflow_drops_the_oldest_and_says_how_many(
    mqtt_broker, monkeypatch,
):
    import transports.mqtt as mqtt_mod

    lines: list[str] = []
    monkeypatch.setattr(mqtt_mod, "out", lines.append)
    monkeypatch.setattr(mqtt_mod, "_RETRY_MAX", 2)
    topic, cid = "a8s/test-retry-overflow", "a8s-test-retry-overflow"
    offered: list[bytes] = []
    t = MqttTransport(remote_id="hub", broker=mqtt_broker, topic=topic, client_id=cid)
    t.start(lambda b: offered.append(b) or False)
    try:
        for payload in (b"1", b"2", b"3"):
            _publish_one(mqtt_broker, topic, payload)
        deadline = time.monotonic() + 3.0
        while len(offered) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        assert [p for p, *_ in t._retry] == [b"2", b"3"]
    finally:
        t.stop()
    warns = [ln for ln in lines if "retry list" in ln]
    assert len(warns) == 1
    assert "dropped 1" in warns[0]


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

    def test_ack_timeout_defaults_to_30s(self):
        t = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t")
        assert t._ack_timeout_s == 30.0

    def test_ack_timeout_coerced_from_string(self):
        t = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", ack_timeout_s="1.5")
        assert t._ack_timeout_s == 1.5

    def test_publish_qos_must_be_0_or_1(self):
        with pytest.raises(ValueError, match="publish_qos"):
            MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", publish_qos=2)
        t = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", publish_qos="0")
        assert t._publish_qos == 0


# ---------- default client identity ----------


class TestDefaultClientId:
    @pytest.fixture(autouse=True)
    def _no_tag_env(self, monkeypatch):
        monkeypatch.delenv("A8S_CLIENT_TAG", raising=False)

    def test_distinct_node_tags_never_collide(self):
        a = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", node_tag="node-a")
        b = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", node_tag="node-b")
        assert a._client_id != b._client_id

    def test_same_node_tag_is_stable_across_instantiations(self):
        first = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", node_tag="node-a")
        second = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", node_tag="node-a")
        assert first._client_id == second._client_id

    def test_distinct_remotes_never_collide(self):
        a = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", node_tag="node-a")
        b = MqttTransport(remote_id="spare", broker="mqtt://x", topic="t", node_tag="node-a")
        assert a._client_id != b._client_id

    def test_env_tag_replaces_node_tag(self, monkeypatch):
        default = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", node_tag="node-a")
        monkeypatch.setenv("A8S_CLIENT_TAG", "override-tag")
        overridden = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", node_tag="node-a")
        assert overridden._client_id != default._client_id

    def test_explicit_client_id_wins(self, monkeypatch):
        monkeypatch.setenv("A8S_CLIENT_TAG", "override-tag")
        t = MqttTransport(
            remote_id="hub",
            broker="mqtt://x",
            topic="t",
            node_tag="node-a",
            client_id="a8s-explicit",
        )
        assert t._client_id == "a8s-explicit"

    def test_clean_session_defaults_false(self):
        t = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t")
        assert t._client._clean_session is False

    def test_clean_session_opt_in(self):
        t = MqttTransport(remote_id="hub", broker="mqtt://x", topic="t", clean_session=True)
        assert t._client._clean_session is True
