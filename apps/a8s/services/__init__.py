"""Storage service plugin interface for a8s cross-cluster file transfer (#90).

A "service" is the file-side analogue of a "transport". Where the messaging
layer publishes/subscribes JSON envelopes, the file layer uploads bytes and
hands back URLs that travel inside those envelopes. Different words on
purpose: `a8s remote` configures messaging transports, `a8s storage`
configures file services, and the two are independently composable.

Lifecycle: services are stateless HTTP clients, so there's no start/stop.
The dispatcher in `network._build_service` instantiates one per
configured `~/.a8s/network.json` `services` entry and hands the same
instance to both the upload path (sender's `_process_pending`) and the
download path (receiver's `_download_files_to_recipient`).

Contract:
- `supports_config_url(url)` is a class method that the dispatcher uses
  to pick the right `StorageService` subclass for an operator-typed URL.
- `store(src)` uploads bytes and returns a download URL string. Raises
  `StorageError` on failure (network down, auth, oversized payload).
- `retrieve(url, dest)` returns False if the URL doesn't belong to this
  service (so the caller falls through to the next configured service).
  Returns True after writing the bytes to `dest`. Raises `StorageError`
  on a real download failure (the URL was ours but the server refused).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class StorageError(Exception):
    """Raised by `store` / `retrieve` when an upload or download genuinely
    failed (network, auth, oversize, server 5xx). Callers warn-and-continue
    and rely on the per-message retry sidecar to drive backoff."""


class StorageService(ABC):
    """A single configured storage backend."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Stable identifier matching the user's `network.json` entry name.
        Used as the cache key in the per-message `uploaded` sidecar so two
        passes against the same service don't double-upload."""

    @classmethod
    @abstractmethod
    def supports_config_url(cls, url: str) -> bool:
        """Return True if this subclass can handle the operator-typed URL.
        Used by `network._build_service` at config-load time to dispatch
        to the right implementation. Should not raise — anything funny
        about the URL is just "not for me"."""

    @abstractmethod
    def store(self, src: Path) -> str:
        """Upload the bytes at `src` and return a download URL. Raises
        `StorageError` on failure. The returned URL is opaque to the
        caller; the receiver round-trips it through `retrieve`."""

    @abstractmethod
    def retrieve(self, url: str, dest: Path) -> bool:
        """Download `url` into `dest`. Returns False if the URL doesn't
        belong to this service (caller should try the next configured
        service). Returns True after a successful write. Raises
        `StorageError` if the URL was ours but the download itself failed."""
