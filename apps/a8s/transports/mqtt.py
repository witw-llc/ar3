"""MQTT Transport implementation.

Uses MQTT 3.1.1 with `clean_session=False` and QoS 1 so the broker holds
messages for an offline subscriber until reconnect — this is the persistent-
session shape decided in the #63 plan. The client_id needs to be stable
across runs (same machine, same a8s install) for the broker to recognize the
session and replay; we default to `a8s-<machine-hash>-<remote_id>`.

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

import hashlib
import queue
import socket
import threading
from typing import Any, Optional
from urllib.parse import urlparse

import paho.mqtt.client as mqtt

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
    "publish_qos",
}


def _default_client_id(remote_id: str) -> str:
    """Stable per-(host, remote) id. The hash is deterministic so the broker
    re-attaches us to the same persistent session on every restart."""
    h = hashlib.sha256(f"{socket.gethostname()}::{remote_id}".encode()).hexdigest()[:16]
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
            publish_qos (0 or 1, default 1). Subscribe stays at QoS 1.
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
        publish_qos: int = int(opts.get("publish_qos", 1))
        if publish_qos not in (0, 1):
            raise ValueError(
                f"remote {remote_id!r}: publish_qos must be 0 or 1, got {publish_qos!r}"
            )

        self._remote_id = remote_id
        self._topic = topic
        self._keepalive = keepalive
        self._connect_timeout_s = connect_timeout_s
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
        self._client_id = client_id or _default_client_id(remote_id)
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            clean_session=False,
        )
        if username is not None:
            self._client.username_pw_set(username, password)
        if self._tls:
            self._client.tls_set()
        self._connected = threading.Event()
        self._on_message: Optional[OnMessage] = None
        self._msg_queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._worker_stop = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._started = False

    @property
    def id(self) -> str:
        return self._remote_id

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0 or (hasattr(reason_code, "is_failure") and not reason_code.is_failure):
            client.subscribe(self._topic, qos=1)
            self._connected.set()

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
        self._msg_queue.put(msg.payload)

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                payload = self._msg_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if payload is None:
                break
            cb = self._on_message
            if cb is None:
                continue
            try:
                cb(payload)
            except Exception:
                pass

    def start(self, on_message: OnMessage) -> None:
        if self._started:
            raise TransportError(f"{self._remote_id}: already started")
        self._on_message = on_message
        self._worker_stop.clear()
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
        if not self._started:
            return
        try:
            self._client.disconnect()
        except OSError:
            pass
        self._client.loop_stop()
        self._worker_stop.set()
        self._msg_queue.put(None)
        worker = self._worker_thread
        if worker is not None:
            worker.join(timeout=5.0)
            self._worker_thread = None
        self._started = False

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
            info.wait_for_publish(timeout=self._connect_timeout_s)
        except (RuntimeError, ValueError) as e:
            raise TransportError(f"{self._remote_id}: wait_for_publish: {e}") from e
        if not info.is_published():
            raise TransportError(f"{self._remote_id}: publish not acknowledged")
