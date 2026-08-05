"""Local directory storage for cloud-sync folders (Drive, OneDrive, etc.).

The configured `url` is a `file://` path where bytes are copied; an external
sync tool publishes them. `base_url` is the public HTTP(S) prefix peers use to
fetch the object after sync.

  a8s storage drive file:///home/me/Drive/a8s --base-url https://cdn.example/a8s

`store` copies into the directory and returns `base_url/<key>`. `retrieve`
accepts URLs under `base_url` and reads the local file when this node shares
the same synced folder; otherwise returns False and the receiver's http GET
fallback applies.
"""
from __future__ import annotations

import os
import secrets
import shutil
import urllib.parse
from pathlib import Path
from typing import Any

from services import StorageError, StorageService
from services.attachment_path import bundle_file_path
from services.public_url import (
    join_public_url,
    public_scheme_ok,
    relative_key_under_base,
)

_KNOWN_OPTS: set[str] = {"base_url", "prefix"}

DEFAULT_PREFIX = "a8s"


def _local_root_from_file_url(url: str) -> Path:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() != "file":
        raise ValueError("expected file:// URL")
    path = urllib.parse.unquote(parsed.path)
    if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    root = Path(path)
    if not root.is_absolute():
        raise ValueError("file URL must be an absolute path")
    return root


class FileSyncService(StorageService):
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
        self._root = _local_root_from_file_url(url)
        self._base_url = base_url.rstrip("/")
        self._prefix = str(opts.get("prefix") or DEFAULT_PREFIX).strip("/")

    @property
    def id(self) -> str:
        return self._name

    @classmethod
    def supports_config_url(cls, url: str) -> bool:
        try:
            return urllib.parse.urlsplit(url.strip()).scheme.lower() == "file"
        except ValueError:
            return False

    def _object_key(self, filename: str) -> str:
        token = secrets.token_hex(8)
        safe = Path(filename).name
        return f"{self._prefix}/{token}/{safe}" if self._prefix else f"{token}/{safe}"

    def _local_path_for_key(self, key: str) -> Path:
        root = self._root.resolve()
        dest = (root / key).resolve()
        dest.relative_to(root)
        return dest

    def store(self, src: Path) -> str:
        key = self._object_key(src.name)
        dest = self._local_path_for_key(key)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        except OSError as e:
            raise StorageError(f"file_sync copy failed for {src.name}: {e}") from e
        return join_public_url(self._base_url, key)

    def retrieve(self, url: str, dest: Path) -> bool:
        key = relative_key_under_base(self._base_url, url)
        if key is None:
            return False
        try:
            src = self._local_path_for_key(key)
        except ValueError:
            return False
        if not src.is_file():
            return False
        safe_dest, err = bundle_file_path(dest.parent, dest.name)
        if safe_dest is None:
            raise StorageError(f"file_sync: {err}")
        try:
            safe_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, safe_dest)
        except OSError as e:
            raise StorageError(f"file_sync read failed for {url}: {e}") from e
        return True
