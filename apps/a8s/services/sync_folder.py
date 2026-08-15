"""Attachment transfer through a folder some other program already syncs.

Desktops and laptops already run a sync client — OneDrive, Drive, Dropbox.
Point two machines at the same folder inside it and the bytes cross by
themselves. a8s copies an attachment in on send and looks for it on receive,
and neither side needs to know how it travelled:

  a8s storage onedrive_work "~/OneDrive - Contoso/A8S"

Nothing is published. There is no bucket, no host, no credential, and no URL
that resolves for anyone outside the folder — which is the point for work
files. `rclone` remains the answer on headless and VM machines, where there is
no sync client to ride along with.

Objects are keyed by message ULID, so one message's attachments stay together
and a retention sweep has something to group on:

  <root>/<prefix>/<ULID>/<filename>
  <root>/<prefix>/<ULID>/manifest.json

The marker that travels in the envelope is `a8s+sync:<ULID>/<filename>` — a
bare reference carrying no host, no path, and not even the name of the service
that wrote it. Every configured sync folder claims that marker and looks in its
own root, so the existing first-to-answer download loop already does what a
redundant pair of folders is for: whichever one syncs first wins.

The hazard here is not the network, it is a half-arrived file. Sync clients
publish a name before the bytes behind it are complete, and OneDrive's
Files On-Demand deliberately shows a placeholder that only materializes when
something reads it. So a file is written under a `.part` name and renamed, and
the manifest records the size the receiver must see before it will believe the
copy is whole. A file that does not match yet is simply not here yet: `retrieve`
answers False and the receiver's poll loop tries again.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any

from services import StorageError, StorageService, resolve_prefix
from services.attachment_path import bundle_file_path
from ark.fsio import replace_with_retry
from ark.ulid import new as new_ulid

_KNOWN_OPTS: set[str] = {"prefix", "retain_days"}

MARKER_SCHEME = "a8s+sync"
MANIFEST_NAME = "manifest.json"

_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _looks_like_a_local_path(url: str) -> bool:
    """A bare filesystem path, which is what an operator naturally types.

    Every other storage service is identified by a scheme, so anything with
    one belongs to somebody else — including `file://`, which is `file_sync`
    publishing to a public base URL rather than this.
    """
    raw = (url or "").strip()
    if not raw:
        return False
    if raw.startswith("~") or raw.startswith("/"):
        return True
    # A Windows drive letter: C:\Users\... . Two characters before the colon
    # is not a URL scheme, so this cannot collide with one.
    return len(raw) > 2 and raw[1] == ":" and raw[0].isalpha()


def marker_for(msg_id: str, filename: str) -> str:
    name = urllib.parse.quote(Path(filename).name, safe="")
    return f"{MARKER_SCHEME}:{msg_id}/{name}"


def parse_marker(url: str) -> tuple[str, str] | None:
    """`(msg_id, filename)` when `url` is one of our markers, else None."""
    raw = (url or "").strip()
    head, sep, rest = raw.partition(":")
    if not sep or head.lower() != MARKER_SCHEME:
        return None
    msg_id, slash, encoded = rest.partition("/")
    if not slash or not _ULID_RE.match(msg_id):
        return None
    filename = urllib.parse.unquote(encoded)
    if not filename or filename != Path(filename).name:
        return None
    return msg_id, filename


class SyncFolderService(StorageService):
    def __init__(self, name: str, *, url: str, **opts: Any) -> None:
        unknown = set(opts) - _KNOWN_OPTS
        if unknown:
            raise ValueError(
                f"storage {name!r}: unknown option(s) {', '.join(sorted(unknown))}"
            )
        raw = (url or "").strip()
        if not _looks_like_a_local_path(raw):
            raise ValueError(f"storage {name!r}: expected a local folder path")
        root = Path(raw).expanduser()
        if not root.is_absolute():
            raise ValueError(f"storage {name!r}: folder path must be absolute")
        self._name = name
        self._root = root
        self._prefix = resolve_prefix(opts, default="")
        self._retain_days = self._resolve_retain_days(name, opts)

    @staticmethod
    def _resolve_retain_days(name: str, opts: dict) -> int:
        raw = opts.get("retain_days")
        if raw is None or str(raw).strip() == "":
            return 0
        try:
            days = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"storage {name!r}: retain_days must be a whole number")
        if days < 0:
            raise ValueError(f"storage {name!r}: retain_days cannot be negative")
        return days

    @property
    def id(self) -> str:
        return self._name

    @classmethod
    def supports_config_url(cls, url: str) -> bool:
        return _looks_like_a_local_path(url)

    def _bundle_dir(self, msg_id: str) -> Path:
        base = self._root / self._prefix if self._prefix else self._root
        return base / msg_id

    def _manifest_path(self, msg_id: str) -> Path:
        return self._bundle_dir(msg_id) / MANIFEST_NAME

    def _read_manifest(self, msg_id: str) -> dict:
        try:
            raw = json.loads(self._manifest_path(msg_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_manifest(self, msg_id: str, entries: dict) -> None:
        """Replace the manifest, staged and renamed so a receiver never reads
        one that promises a file the folder does not hold yet."""
        path = self._manifest_path(msg_id)
        staging = path.with_name(f".{MANIFEST_NAME}.part")
        try:
            staging.write_text(json.dumps(entries), encoding="utf-8")
            replace_with_retry(staging, path)
        except OSError as e:
            staging.unlink(missing_ok=True)
            raise StorageError(f"sync folder manifest write failed: {e}") from e

    def store(self, src: Path, *, msg_id: str = "") -> str:
        # `a8s health` has no envelope, so it gets a ULID of its own rather
        # than a second code path.
        msg_id = (msg_id or "").strip() or new_ulid()
        filename = Path(src.name).name
        bundle = self._bundle_dir(msg_id)
        try:
            bundle.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StorageError(f"sync folder unreachable: {e}") from e
        dest, err = bundle_file_path(bundle, filename)
        if dest is None:
            raise StorageError(f"sync folder: {err}")
        staging = dest.with_name(f".{dest.name}.part")
        try:
            shutil.copy2(src, staging)
            size = staging.stat().st_size
            replace_with_retry(staging, dest)
        except OSError as e:
            staging.unlink(missing_ok=True)
            raise StorageError(f"sync folder copy failed for {filename}: {e}") from e
        entries = self._read_manifest(msg_id)
        entries[filename] = {"bytes": size}
        self._write_manifest(msg_id, entries)
        self._sweep()
        return marker_for(msg_id, filename)

    def _complete_source(self, msg_id: str, filename: str) -> Path | None:
        """The local copy, once it is whole. None while it is still arriving.

        Presence is not arrival. A sync client publishes a name before the
        bytes behind it land, so the manifest's recorded size is what settles
        whether this copy can be trusted yet.
        """
        bundle = self._bundle_dir(msg_id)
        src, _ = bundle_file_path(bundle, filename)
        if src is None or not src.is_file():
            return None
        entry = self._read_manifest(msg_id).get(filename)
        if not isinstance(entry, dict):
            return None
        try:
            expected = int(entry.get("bytes"))
        except (TypeError, ValueError):
            return None
        try:
            if src.stat().st_size != expected:
                return None
        except OSError:
            return None
        return src

    def retrieve(self, url: str, dest: Path) -> bool:
        parsed = parse_marker(url)
        if parsed is None:
            return False
        src = self._complete_source(*parsed)
        if src is None:
            return False
        safe_dest, err = bundle_file_path(dest.parent, dest.name)
        if safe_dest is None:
            raise StorageError(f"sync folder: {err}")
        try:
            safe_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, safe_dest)
        except OSError as e:
            # A placeholder that could not materialize reads as a failure here
            # rather than as absence, so the operator hears about it.
            raise StorageError(f"sync folder read failed for {url}: {e}") from e
        return True

    def delete(self, url: str) -> bool:
        parsed = parse_marker(url)
        if parsed is None:
            return False
        msg_id, filename = parsed
        bundle = self._bundle_dir(msg_id)
        target, _ = bundle_file_path(bundle, filename)
        if target is None:
            return False
        target.unlink(missing_ok=True)
        entries = self._read_manifest(msg_id)
        entries.pop(filename, None)
        if entries:
            self._write_manifest(msg_id, entries)
        else:
            self._drop_bundle(bundle)
        return True

    @staticmethod
    def _drop_bundle(bundle: Path) -> None:
        """Remove a bundle once its last attachment is gone. Only files this
        service writes are removed, so an unexpected sibling keeps the
        directory alive rather than being swept up with it."""
        (bundle / MANIFEST_NAME).unlink(missing_ok=True)
        try:
            bundle.rmdir()
        except OSError:
            pass

    def _sweep(self) -> None:
        """Drop bundles past `retain_days`.

        Off unless the operator asks for it. A sync folder is shared, and
        deleting from one machine deletes from every machine — that is not a
        default anybody should get by accident.
        """
        if not self._retain_days:
            return
        base = self._root / self._prefix if self._prefix else self._root
        cutoff = time.time() - self._retain_days * 86400
        try:
            bundles = list(base.iterdir())
        except OSError:
            return
        for bundle in bundles:
            if not _ULID_RE.match(bundle.name) or not bundle.is_dir():
                continue
            try:
                if bundle.stat().st_mtime >= cutoff:
                    continue
                for child in bundle.iterdir():
                    child.unlink(missing_ok=True)
                bundle.rmdir()
            except OSError:
                continue
