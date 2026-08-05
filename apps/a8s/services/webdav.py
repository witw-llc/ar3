"""WebDAV upload storage with a separate public `base_url` for downloads.

The configured `url` uses the `webdav://` scheme (mapped to HTTPS for PUT).
Use when the WebDAV path and the public URL differ (e.g. FastMail WebDAV vs a
custom domain).

  a8s storage fm webdav://webdav.fastmail.com/dav/fs/user@domain/a8s \\
      --base-url https://files.example.com/a8s --user me@domain --password ...

`store` PUTs bytes and returns a URL under `base_url`. `retrieve` reads from
the local WebDAV tree only when URL matches `base_url` and the file is present;
cross-cluster receivers use plain HTTP GET on the public URL.
"""
from __future__ import annotations

import base64
import secrets
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from services import StorageError, StorageService
from services.attachment_path import bundle_file_path
from services.public_url import (
    join_public_url,
    public_scheme_ok,
    relative_key_under_base,
)

_KNOWN_OPTS: set[str] = {
    "base_url",
    "prefix",
    "user",
    "password",
    "timeout_s",
}

DEFAULT_PREFIX = "a8s"
DEFAULT_TIMEOUT_S = 60


def _webdav_url_to_https(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    if scheme == "webdav":
        return urllib.parse.urlunsplit(
            ("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
        )
    if scheme in ("http", "https") and public_scheme_ok(scheme):
        return url.strip()
    raise ValueError(f"unsupported scheme {parsed.scheme!r}")


class WebdavService(StorageService):
    def __init__(self, name: str, *, url: str, **opts: Any) -> None:
        unknown = set(opts) - _KNOWN_OPTS
        if unknown:
            raise ValueError(
                f"storage {name!r}: unknown option(s) {', '.join(sorted(unknown))}"
            )
        base_url = (opts.get("base_url") or "").strip()
        if not base_url:
            raise ValueError(f"storage {name!r}: base_url is required")
        parsed = urllib.parse.urlsplit(base_url)
        if not public_scheme_ok(parsed.scheme) or not parsed.netloc:
            raise ValueError(
                f"storage {name!r}: base_url must be https with a host"
            )
        self._name = name
        self._dav_base = _webdav_url_to_https(url).rstrip("/")
        self._base_url = base_url.rstrip("/")
        self._prefix = str(opts.get("prefix") or DEFAULT_PREFIX).strip("/")
        self._user = (opts.get("user") or "").strip()
        self._password = (opts.get("password") or "").strip()
        raw_timeout = opts.get("timeout_s")
        self._timeout_s = int(DEFAULT_TIMEOUT_S if raw_timeout is None else raw_timeout)
        if self._timeout_s < 1:
            raise ValueError(f"storage {name!r}: timeout_s must be positive")

    @property
    def id(self) -> str:
        return self._name

    @classmethod
    def supports_config_url(cls, url: str) -> bool:
        try:
            return urllib.parse.urlsplit(url.strip()).scheme.lower() == "webdav"
        except ValueError:
            return False

    def _auth_header(self) -> str | None:
        if not self._user:
            return None
        token = base64.b64encode(
            f"{self._user}:{self._password}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {token}"

    def _object_key(self, filename: str) -> str:
        token = secrets.token_hex(8)
        safe = Path(filename).name
        return f"{self._prefix}/{token}/{safe}" if self._prefix else f"{token}/{safe}"

    def _put_url_for_key(self, key: str) -> str:
        return f"{self._dav_base}/{key}"

    def store(self, src: Path) -> str:
        key = self._object_key(src.name)
        put_url = self._put_url_for_key(key)
        try:
            body = src.read_bytes()
        except OSError as e:
            raise StorageError(f"webdav cannot read {src.name}: {e}") from e
        req = urllib.request.Request(
            put_url,
            data=body,
            method="PUT",
            headers={
                "User-Agent": "a8s/1",
                "Content-Type": "application/octet-stream",
            },
        )
        auth = self._auth_header()
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                if resp.status and resp.status >= 400:
                    raise StorageError(f"webdav PUT HTTP {resp.status} for {put_url}")
        except urllib.error.HTTPError as e:
            raise StorageError(
                f"webdav PUT HTTP {e.code} for {put_url}: {e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise StorageError(
                f"webdav PUT network error for {put_url}: {e.reason}"
            ) from e
        return join_public_url(self._base_url, key)

    def retrieve(self, url: str, dest: Path) -> bool:
        key = relative_key_under_base(self._base_url, url)
        if key is None:
            return False
        get_url = self._put_url_for_key(key)
        req = urllib.request.Request(get_url, method="GET", headers={"User-Agent": "a8s/1"})
        auth = self._auth_header()
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                safe_dest, err = bundle_file_path(dest.parent, dest.name)
                if safe_dest is None:
                    raise StorageError(f"webdav: {err}")
                safe_dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = safe_dest.with_name(safe_dest.name + ".part")
                with tmp.open("wb") as out:
                    shutil.copyfileobj(resp, out)
                tmp.replace(safe_dest)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise StorageError(
                f"webdav GET HTTP {e.code} for {get_url}: {e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise StorageError(
                f"webdav GET network error for {get_url}: {e.reason}"
            ) from e
        except OSError as e:
            raise StorageError(f"webdav write failed for {dest}: {e}") from e
        return True
