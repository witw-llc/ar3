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
from typing import Callable


# `on_message` callback signature. Receives raw envelope bytes (the JSON the
# sender published). Implementations must be safe to call from a network thread.
# A transport offers each envelope once and consumes it after the callback
# returns or raises: `folder` and `s3` record it in the consumed ledger, and
# `mqtt` acknowledges it.
OnMessage = Callable[[bytes], None]


class TransportError(Exception):
    """Raised by `publish` when the transport did not take the message
    (broker unreachable, auth fail, in-flight queue full, etc). The send
    fails; a8s does not try it again."""


class PublishAmbiguous(TransportError):
    """Raised by `publish` when the envelope may have gone out and the
    transport cannot say: no acknowledgement in time, or a link that dropped
    after the send. The router counts it as sent and never retries it."""


class Transport(ABC):
    """A single configured remote."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Stable identifier matching the user's `network.json` entry name.
        Names the remote in log lines and transaction rows."""

    @abstractmethod
    def start(self, on_message: OnMessage) -> None:
        """Begin the subscriber loop. `on_message(envelope_bytes)` fires for
        each incoming envelope. Implementations should be re-entrant; the
        caller may invoke `start` once per process lifetime.

        Each envelope is offered once and consumed after the callback, whether
        it returns or raises."""

    @abstractmethod
    def stop(self) -> None:
        """Tear down the subscriber and block until the network thread ends."""

    @abstractmethod
    def publish(self, envelope: bytes) -> None:
        """Send one envelope. Raises `TransportError` on a definite failure
        and `PublishAmbiguous` when the outcome is unknown."""

    def is_connected(self) -> bool:
        """Whether the link is up now. A transport with no link to lose is
        always connected."""
        return True

    def publish_control(self, envelope: bytes) -> None:
        """Send one control envelope (a delivery receipt) once. Raises
        `TransportError` when it did not go out; the receipt is dropped.

        A transport with no link to lose sends it the same way as `publish`."""
        self.publish(envelope)
