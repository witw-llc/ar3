"""Transport plugin interface for a8s remote routing (issue #63).

a8s never asks what kind of transport a remote is. It calls `publish(envelope)`
on every configured remote and runs a subscriber thread per remote that hands
incoming envelopes to one common receive function. The transport ABC below
codifies that contract: implementations live in sibling modules
(`mqtt_paho.py`, future `mqtt_mini.py`, `https.py`, `peer.py`).

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
OnMessage = Callable[[bytes], None]


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
        caller may invoke `start` once per process lifetime."""

    @abstractmethod
    def stop(self) -> None:
        """Tear down the subscriber and block until the network thread ends."""

    @abstractmethod
    def publish(self, envelope: bytes) -> None:
        """Send one envelope. Raises `TransportError` on failure."""
