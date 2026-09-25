"""MQTT Transport implementation.

Uses MQTT 3.1.1 with `clean_session=False` and QoS 1 so the broker holds
messages for an offline subscriber until reconnect — this is the persistent-
session shape remote routing needs. The client_id needs to be stable
across runs (same machine, same a8s install) for the broker to recognize the
session and replay; we default to a hash of (host, node tag, remote id) so
each node on a host keeps its own session.

Today the implementation is paho-mqtt. A pure-stdlib mini-MQTT fallback is
deferred to a follow-up PR; when it lands, this module will auto-select
(paho if importable, mini otherwise) so the user-facing config kind stays
`mqtt` either way.

Construction is option-bag-shaped: anything past `remote_id`, `broker`,
`topic` arrives as `**opts` — the constructor aliases common shorthand
(`user`/`pass` → `username`/`password`), pulls known keys, and rejects
anything left over so an obvious typo in `network.json` fails loud.
"""
from __future__ import annotations

import collections
import hashlib
import os
import queue
import socket
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

# `ar3` sits in `<repo>/lib` (core.py already put it on sys.path) and vendors
# paho-mqtt so an a8s install with no pip still gets the transport. A copy of
# this tree relocated away from that root, or a run under `AR3_NO_VENDOR`,
# falls back to whatever `paho` a system install or venv provides — the import
# below either resolves it or raises, same as before the vendor hook existed.
try:
    from ar3.vendor import ensure_vendor

    ensure_vendor()
except ImportError:
    pass

import paho.mqtt.client as mqtt

from core import out
from transports import OnMessage, Transport, TransportError


# Aliases the option bag accepts in addition to the canonical names.
_OPT_ALIASES: dict[str, str] = {
    "user": "username",
    "pass": "password",
}

# Recognized option keys (post-aliasing). Anything else raises.
_KNOWN_OPTS: set[str] = {
    "username",
    "password",
    "client_id",
    "keepalive",
    "connect_timeout_s",
    "ack_timeout_s",
    "publish_qos",
    "clean_session",
    # `a8s health` sets this on every remote it builds. A throwaway client id
    # and a clean session already make this client a probe, so there is
    # nothing further to do with it here.
    "probe",
}

# Replaces the node tag in the default client id when set.
_CLIENT_TAG_ENV = "A8S_CLIENT_TAG"

# Receipts held while the link is down. A receipt is a few hundred bytes, and
# a node that stays unreachable long enough to fill this has a bigger problem
# than the oldest confirmation.
_PENDING_CONTROL_MAX = 256

# Received messages the receive path has not consumed yet. Each is re-offered
# on its own backoff and acknowledged only once consumed. The broker's
# in-flight window caps what it hands one session unacknowledged (mosquitto:
# 20), so this bound matters only for a broker configured far wider.
_RETRY_MAX = 256
_RETRY_FIRST_S = 1.0
_RETRY_CAP_S = 30.0

# Worker-queue markers. A received message is a
# `(payload, mid, qos, connection)` tuple.
_FLUSH = object()
_STOP = object()


def _default_client_id(remote_id: str, node_tag: str = "") -> str:
    """Stable per-(host, node, remote) id. The hash is deterministic so the
    broker re-attaches us to the same persistent session on every restart."""
    # MQTT 3.1.1 §3.1.4-2 has the broker disconnect whoever already holds a
    # client id, handing the newcomer the persistent session and its queued
    # QoS-1 messages — so the node tag has to be in the hash, or two a8s
    # processes on one host silently steal each other's mail.
    tag = os.environ.get(_CLIENT_TAG_ENV) or node_tag
    h = hashlib.sha256(
        f"{socket.gethostname()}::{tag}::{remote_id}".encode()
    ).hexdigest()[:16]
    return f"a8s-{h}"


class MqttTransport(Transport):
    """One configured MQTT remote.

    Args:
        remote_id: stable name from `network.json` (used for dedup keying).
        broker: URL — `mqtt://host[:1883]` or `mqtts://host[:8883]`.
        topic: the broadcast topic both publish and subscribe target.
        **opts: per-remote options forwarded from `network.json`. Recognized:
            username / password (aliased from `user` / `pass`), client_id,
            keepalive (seconds, default 60), connect_timeout_s (default 5.0),
            ack_timeout_s (default 30.0, PUBACK wait — separate from
            connect_timeout_s because a residential network's latency tail
            can outlast a healthy connection), publish_qos (0 or 1,
            default 1), clean_session (default False — short-lived probe
            clients set True so they neither inherit nor orphan a durable
            session). Subscribe stays at QoS 1.
            a8s-android (Java Paho) publishes asynchronously and never waits
            for PUBACK; this client waits, and the broker echo runs through
            `on_message` on the same connection — see `_worker_loop`.
            Unknown keys raise ValueError so a typo in the config doesn't
            silently produce a broken remote.
    """

    def __init__(
        self,
        remote_id: str,
        *,
        broker: str,
        topic: str,
        **opts: Any,
    ) -> None:
        node_tag: str = str(opts.pop("node_tag", "") or "")
        # Normalize alias keys (user → username, etc.). If both alias and
        # canonical are present, canonical wins and the alias is dropped
        # silently — the user might have set both during a config edit; we
        # don't surprise them with an error there.
        for short, full in _OPT_ALIASES.items():
            if short in opts and full not in opts:
                opts[full] = opts.pop(short)
            elif short in opts:
                opts.pop(short)
        unknown = set(opts) - _KNOWN_OPTS
        if unknown:
            raise ValueError(
                f"remote {remote_id!r}: unknown option(s) {sorted(unknown)} "
                f"(known: {sorted(_KNOWN_OPTS)} + aliases {sorted(_OPT_ALIASES)})"
            )
        username: Optional[str] = opts.get("username")
        password: Optional[str] = opts.get("password")
        client_id: Optional[str] = opts.get("client_id")
        keepalive: int = int(opts.get("keepalive", 60))
        connect_timeout_s: float = float(opts.get("connect_timeout_s", 5.0))
        ack_timeout_s: float = float(opts.get("ack_timeout_s", 30.0))
        publish_qos: int = int(opts.get("publish_qos", 1))
        clean_session: bool = bool(opts.get("clean_session", False))
        if publish_qos not in (0, 1):
            raise ValueError(
                f"remote {remote_id!r}: publish_qos must be 0 or 1, got {publish_qos!r}"
            )

        self._remote_id = remote_id
        self._topic = topic
        self._keepalive = keepalive
        self._connect_timeout_s = connect_timeout_s
        self._ack_timeout_s = ack_timeout_s
        self._publish_qos = publish_qos
        parsed = urlparse(broker)
        if parsed.scheme not in ("mqtt", "mqtts"):
            raise ValueError(
                f"remote {remote_id!r}: unsupported scheme {parsed.scheme!r} "
                f"(expected mqtt or mqtts)"
            )
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or (8883 if parsed.scheme == "mqtts" else 1883)
        self._tls = parsed.scheme == "mqtts"
        self._client_id = client_id or _default_client_id(remote_id, node_tag)
        # Manual ack: a message is acknowledged only once the receive path
        # reports it consumed. One it has not consumed, or one that arrives
        # while `stop()` drains, stays unacknowledged, and the broker offers
        # it again next session rather than it being acknowledged and dropped.
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            clean_session=clean_session,
            manual_ack=True,
        )
        if username is not None:
            self._client.username_pw_set(username, password)
        if self._tls:
            self._client.tls_set()
        self._connected = threading.Event()
        self._on_message: Optional[OnMessage] = None
        self._msg_queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._worker_thread: threading.Thread | None = None
        self._started = False
        self._stopping = False
        self._pending_lock = threading.Lock()
        self._pending_control: collections.deque[bytes] = collections.deque()
        # Bumped on every CONNACK. A packet id belongs to the connection that
        # carried it, so a message from an earlier connection is never acked
        # on this one; the broker offers it again instead.
        self._connection = 0
        # `[payload, mid, qos, connection, due, delay]`, touched only by the
        # worker.
        self._retry: collections.deque[list[Any]] = collections.deque()

    @property
    def id(self) -> str:
        return self._remote_id

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0 or (hasattr(reason_code, "is_failure") and not reason_code.is_failure):
            self._connection += 1
            client.subscribe(self._topic, qos=1)
            self._connected.set()
            # The worker sends what the outage held, in order, and forgets
            # retries the broker now owns again. Never here: a QoS-1 publish
            # waits for a PUBACK this network thread reads.
            self._msg_queue.put(_FLUSH)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        # Clear the readiness event so `publish()` can wait for the next
        # CONNACK before declaring failure. paho's loop auto-reconnects in
        # the background while `loop_start()` is running.
        self._connected.clear()

    def _on_message_cb(self, client, userdata, msg):
        # Never run the user callback on paho's network thread. a8s uses one
        # client for both publish and subscribe on the same topic; the broker
        # echoes our publishes back through on_message, and receive_envelope
        # can do real work (registry I/O, file downloads, convo archive).
        # Blocking here delays PUBACK for the in-flight outbound publish and
        # surfaces as "publish not acknowledged" at the routing layer.
        if self._stopping:
            return
        self._msg_queue.put((msg.payload, msg.mid, msg.qos, self._connection))

    def _worker_loop(self) -> None:
        while True:
            try:
                item = self._msg_queue.get(timeout=self._next_retry_in())
            except queue.Empty:
                item = None
            self._forget_earlier_connections()
            if item is _STOP:
                self._flush_pending_control()
                return
            if item is _FLUSH:
                self._flush_pending_control()
            elif item is not None:
                self._offer([*item, 0.0, 0.0])
            self._retry_due()

    def _offer(self, entry: list[Any]) -> None:
        """Hand one message to the receive path. Ack it when consumed;
        otherwise hold it for another offer after a backoff."""
        payload, mid, qos, connection, _due, delay = entry
        cb = self._on_message
        try:
            consumed = cb is None or cb(payload) is not False
        except Exception:
            consumed = False
        if consumed:
            if connection == self._connection:
                self._client.ack(mid, qos)
            return
        delay = min(delay * 2, _RETRY_CAP_S) if delay else _RETRY_FIRST_S
        entry[4] = time.monotonic() + delay
        entry[5] = delay
        if len(self._retry) >= _RETRY_MAX:
            self._retry.popleft()
            out(
                f"WARN remote {self._remote_id} retry list full ({_RETRY_MAX}); "
                f"dropped 1 unconsumed message, the oldest, unacknowledged "
                f"for the broker to offer again next session"
            )
        self._retry.append(entry)

    def _next_retry_in(self) -> Optional[float]:
        if not self._retry:
            return None
        return max(0.0, min(e[4] for e in self._retry) - time.monotonic())

    def _retry_due(self) -> None:
        now = time.monotonic()
        due = [e for e in self._retry if e[4] <= now]
        if not due:
            return
        self._retry = collections.deque(e for e in self._retry if e[4] > now)
        for entry in due:
            self._offer(entry)

    def _forget_earlier_connections(self) -> None:
        """Drop QoS-1 retries from a connection that has ended. The persistent
        session still holds them unacknowledged, and the broker offers them
        again on this connection."""
        if any(e[2] and e[3] != self._connection for e in self._retry):
            self._retry = collections.deque(
                e for e in self._retry if not e[2] or e[3] == self._connection
            )

    def is_connected(self) -> bool:
        """True while the broker has answered CONNACK and the link is up."""
        return (
            self._started
            and self._connected.is_set()
            and self._client.is_connected()
        )

    def start(self, on_message: OnMessage) -> None:
        if self._started:
            raise TransportError(f"{self._remote_id}: already started")
        self._on_message = on_message
        self._stopping = False
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name=f"a8s-mqtt-{self._remote_id}",
            daemon=True,
        )
        self._worker_thread.start()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message_cb
        try:
            self._client.connect_async(self._host, self._port, keepalive=self._keepalive)
        except OSError as e:
            raise TransportError(f"{self._remote_id}: connect_async failed: {e}") from e
        self._client.loop_start()
        self._started = True
        # Wait for initial CONNACK so a fast-failing broker surfaces here
        # rather than silently buffering publishes. After this, the loop
        # auto-reconnects on disconnect.
        if not self._connected.wait(timeout=self._connect_timeout_s):
            # Don't raise — let the background loop keep retrying. publish()
            # below will fail until the connection comes up, which is the
            # right signal for the routing pass to log a warning and retry
            # via the per-message backoff.
            pass

    def stop(self) -> None:
        """Drain, then disconnect. New arrivals, and taken messages the
        receive path has not consumed, are left unacknowledged for the broker
        to offer again next session; everything already taken is offered, its
        receipts sent while the link is still up, and only then does the
        client disconnect."""
        if not self._started:
            return
        self._stopping = True
        self._msg_queue.put(_STOP)
        worker = self._worker_thread
        if worker is not None:
            worker.join(timeout=self._connect_timeout_s + self._ack_timeout_s)
            self._worker_thread = None
        owed = len(self._retry)
        self._retry.clear()
        if owed:
            out(
                f"remote {self._remote_id} stopped with {owed} unconsumed "
                f"message(s) unacknowledged; the broker offers them next session"
            )
        with self._pending_lock:
            dropped = len(self._pending_control)
            self._pending_control.clear()
        if dropped:
            out(
                f"WARN remote {self._remote_id} stopped with the link down; "
                f"dropped {dropped} held delivery receipt(s)"
            )
        try:
            self._client.disconnect()
        except OSError:
            pass
        self._client.loop_stop()
        self._started = False

    def publish_control(self, envelope: bytes) -> bool:
        """Send a receipt now, or hold it for the next CONNACK when the link
        is down. Held receipts go out in the order they were made."""
        if not self._started:
            raise TransportError(f"{self._remote_id}: publish before start")
        with self._pending_lock:
            if self._pending_control or not self._client.is_connected():
                self._hold_control(envelope)
                return False
        try:
            self.publish(envelope)
            return True
        except TransportError:
            if self._client.is_connected():
                raise
        with self._pending_lock:
            self._hold_control(envelope)
        return False

    def _hold_control(self, envelope: bytes) -> None:
        if len(self._pending_control) >= _PENDING_CONTROL_MAX:
            self._pending_control.popleft()
            out(
                f"WARN remote {self._remote_id} held-receipt list full "
                f"({_PENDING_CONTROL_MAX}); dropped 1 delivery receipt, the oldest"
            )
        self._pending_control.append(envelope)
        # The link may have come back between the check and the append, with
        # its CONNACK finding nothing to flush.
        if self._client.is_connected():
            self._msg_queue.put(_FLUSH)

    def _flush_pending_control(self) -> None:
        while True:
            with self._pending_lock:
                if not self._pending_control or not self._client.is_connected():
                    return
                envelope = self._pending_control[0]
            try:
                self.publish(envelope)
            except TransportError:
                if self._client.is_connected():
                    out(
                        f"WARN remote {self._remote_id} held delivery receipt "
                        f"not acknowledged; kept for the next connection"
                    )
                return
            with self._pending_lock:
                if self._pending_control and self._pending_control[0] is envelope:
                    self._pending_control.popleft()

    def publish(self, envelope: bytes) -> None:
        if not self._started:
            raise TransportError(f"{self._remote_id}: publish before start")
        if not self._client.is_connected():
            # Transient blip — paho's background loop auto-reconnects.
            # Wait briefly for the next CONNACK before declaring failure
            # so a normal NAT-timeout / broker-flap doesn't trigger a
            # warn-and-backoff at the routing layer.
            self._connected.wait(timeout=self._connect_timeout_s)
        if not self._client.is_connected():
            raise TransportError(f"{self._remote_id}: broker not connected")
        info = self._client.publish(self._topic, payload=envelope, qos=self._publish_qos)
        rc = info.rc
        if rc != mqtt.MQTT_ERR_SUCCESS:
            raise TransportError(f"{self._remote_id}: publish rc={rc}")
        if self._publish_qos == 0:
            return
        try:
            info.wait_for_publish(timeout=self._ack_timeout_s)
        except (RuntimeError, ValueError) as e:
            raise TransportError(f"{self._remote_id}: wait_for_publish: {e}") from e
        if not info.is_published():
            raise TransportError(f"{self._remote_id}: publish not acknowledged")
