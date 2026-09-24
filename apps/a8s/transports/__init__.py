"""Transport plugin interface for a8s remote routing.

a8s never asks what kind of transport a remote is. It calls `publish(envelope)`
on every configured remote and runs a subscriber thread per remote that hands
incoming envelopes to one common receive function. The transport ABC below
codifies that contract: implementations live in sibling modules — `mqtt.py`
(a broker), `folder.py` (a directory somebody else syncs), `s3.py` (a bucket,
for a machine that only has HTTPS on 443), and future ones such as
`mqtt_mini.py` and `peer.py`.

Lifecycle:
  remote = SomeTransport(remote_id, ...config...)
  remote.start(on_message)   # spins up a subscriber thread / network loop
  ...
  remote.publish(envelope)   # called from the routing pass; raises on failure
  ...
  remote.stop()              # blocks until subscriber thread ends
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


# `on_message` callback signature. Receives raw envelope bytes (the JSON the
# sender published). Implementations must be safe to call from a network thread.
#
# The callback may answer whether this node finished with the envelope. `False`
# says it did not — a sibling receiver holds it, or delivery failed and the
# message was deliberately left deliverable — so a transport whose
# acknowledgement destroys its only copy (`s3`) must leave that envelope on the
# wire. `None` is no answer at all, which is what a transport gets from any
# receive path it cannot interrogate, and it reads as a plain delivery. A
# transport that never deletes on receive (`mqtt`, `folder`) ignores the answer
# entirely.
OnMessage = Callable[[bytes], Optional[bool]]


class TransportError(Exception):
    """Raised by `publish` when the message couldn't be delivered to the
    transport (broker unreachable, auth fail, in-flight queue full, etc).
    Callers warn-and-continue and rely on the per-message retry sidecar."""


class Transport(ABC):
    """A single configured remote."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Stable identifier matching the user's `network.json` entry name.
        Used as the dedup key in the per-message retry sidecar so multiple
        runs against the same broker don't double-publish."""

    @abstractmethod
    def start(self, on_message: OnMessage) -> None:
        """Begin the subscriber loop. `on_message(envelope_bytes)` fires for
        each incoming envelope. Implementations should be re-entrant; the
        caller may invoke `start` once per process lifetime.

        `False` means the receive path did not finish with the envelope: a
        sibling holds the claim, an inbox would not take it, or a download is
        still running. A transport that holds the only copy — an object in a
        bucket, a file in a folder — must keep it and offer it again. `None`
        and `True` are both consent to drop it. A transport whose wire has
        already acknowledged the message by the time it reaches here, as MQTT
        has, can only ignore the answer."""

    @abstractmethod
    def stop(self) -> None:
        """Tear down the subscriber and block until the network thread ends."""

    @abstractmethod
    def publish(self, envelope: bytes) -> None:
        """Send one envelope. Raises `TransportError` on failure."""
