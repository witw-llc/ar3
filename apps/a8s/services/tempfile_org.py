"""TempFile.org storage service.

Wire format (https://tempfile.org/api):

  POST <base>/api/upload/local     multipart/form-data
    fields: files=<binary>, expiryHours=<1|6|24|48>
    response: {"files": [{"id", "name", "size", "url", "expiryTime"}, ...]}
              where "url" is e.g. "https://tempfile.org/<id>/"

  GET <returned-url>download       streams the bytes

The service is stateless — instances hold the operator-configured base URL
plus an `expiry_hours` knob, and that's it. One instance per `a8s storage`
entry; both `store` and `retrieve` are called on the same instance.

Dispatch: `supports_config_url` matches by host (default `tempfile.org`).
Operators can point this implementation at a self-hosted clone by giving
a different URL — `retrieve` accepts URLs whose host matches the
configured base, so the same code path serves both.

Construction is option-bag-shaped, mirroring `MqttTransport`: anything past
`name` and `url` arrives as `**opts` and unknown keys raise so a typo in
`network.json` fails loud at load time.
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import secrets
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from services import StorageError, StorageService


# Recognized opts (post-aliasing). Anything else raises.
_KNOWN_OPTS: set[str] = {
    "expiry_hours",
    "timeout_s",
}

_VALID_EXPIRY_HOURS = {1, 6, 24, 48}

_DEFAULT_HOSTS = ("tempfile.org", "www.tempfile.org")


def _build_multipart(filename: str, content_type: str, body: bytes, expiry_hours: int) -> tuple[bytes, str]:
    """Hand-rolled multipart/form-data — stdlib has no helper that handles
    binary file fields cleanly. Returns (encoded_body, content_type_header)."""
    boundary = "----a8s" + secrets.token_hex(16)
    parts: list[bytes] = []
    # files field
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        (
            f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    parts.append(body)
    parts.append(b"\r\n")
    # expiryHours field
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="expiryHours"\r\n\r\n')
    parts.append(str(expiry_hours).encode())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class TempFileOrgService(StorageService):
    """One configured TempFile.org service.

    Args:
        name: stable name from `network.json` (used as the cache key in
            the per-message `uploaded` sidecar).
        url: the service base URL — e.g. `https://tempfile.org`.
        **opts: per-service options forwarded from `network.json`. Recognized:
            expiry_hours (1, 6, 24, or 48; default 24), timeout_s (default 30).
            Unknown keys raise ValueError so a typo in the config doesn't
            silently produce a broken service.
    """

    def __init__(self, name: str, *, url: str, **opts: Any) -> None:
        unknown = set(opts) - _KNOWN_OPTS
        if unknown:
            raise ValueError(
                f"storage {name!r}: unknown option(s) {sorted(unknown)} "
                f"(known: {sorted(_KNOWN_OPTS)})"
            )
        expiry_hours = int(opts.get("expiry_hours", 24))
        if expiry_hours not in _VALID_EXPIRY_HOURS:
            raise ValueError(
                f"storage {name!r}: expiry_hours must be one of {sorted(_VALID_EXPIRY_HOURS)}, "
                f"got {expiry_hours}"
            )
        timeout_s = float(opts.get("timeout_s", 30))
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"storage {name!r}: unsupported scheme {parsed.scheme!r} (expected http or https)"
            )
        if not parsed.hostname:
            raise ValueError(f"storage {name!r}: URL missing host: {url!r}")
        self._name = name
        self._base = url.rstrip("/")
        self._host = parsed.hostname
        self._expiry_hours = expiry_hours
        self._timeout_s = timeout_s

    @property
    def id(self) -> str:
        return self._name

    @classmethod
    def supports_config_url(cls, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        return host in _DEFAULT_HOSTS

    def store(self, src: Path) -> str:
        try:
            size = src.stat().st_size
            with src.open("rb") as f:
                body = f.read()
        except OSError as e:
            raise StorageError(f"{self._name}: cannot read {src}: {e}") from e
        content_type = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
        encoded, ct_header = _build_multipart(src.name, content_type, body, self._expiry_hours)
        req = urllib.request.Request(
            f"{self._base}/api/upload/local",
            data=encoded,
            method="POST",
            headers={
                "Content-Type": ct_header,
                "Content-Length": str(len(encoded)),
                "User-Agent": "a8s/1",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            raise StorageError(
                f"{self._name}: upload HTTP {e.code} ({size} bytes): {e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise StorageError(f"{self._name}: upload network error: {e.reason}") from e
        try:
            data = json.loads(payload)
            files = data.get("files") or []
            if not files or "url" not in files[0]:
                raise ValueError(f"unexpected response shape: {data!r}")
            return str(files[0]["url"])
        except (ValueError, json.JSONDecodeError) as e:
            raise StorageError(f"{self._name}: malformed upload response: {e}") from e

    def retrieve(self, url: str, dest: Path) -> bool:
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        if host != self._host.lower():
            return False
        # The upload response's `url` ends with a trailing slash; the GET
        # endpoint is `<url>download`. Tolerate either form gracefully.
        download_url = url if url.endswith("/download") else url.rstrip("/") + "/download"
        req = urllib.request.Request(
            download_url,
            method="GET",
            headers={"User-Agent": "a8s/1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                tmp = dest.with_name(dest.name + ".part")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tmp.open("wb") as out:
                    shutil.copyfileobj(resp, out)
                os.replace(str(tmp), str(dest))
        except urllib.error.HTTPError as e:
            raise StorageError(
                f"{self._name}: download HTTP {e.code} for {url}: {e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise StorageError(
                f"{self._name}: download network error for {url}: {e.reason}"
            ) from e
        except OSError as e:
            raise StorageError(f"{self._name}: write failed for {dest}: {e}") from e
        return True
