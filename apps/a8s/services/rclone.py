"""Storage through an rclone remote the operator has already configured.

The config URL is `rclone://<remote>/<base-path>`:

  a8s storage drive rclone://gdrive/A8S

`store` shells out to `rclone copyto` and then `rclone link`. Both are
synchronous — rclone performs the transfer itself rather than dropping bytes in
a folder for a background daemon — so the public URL exists by the time `store`
returns and nothing has to wait for publication.

`retrieve` returns False on purpose. The link is public https, so the
receiver's plain GET fetches it: a receiving node needs neither rclone nor any
credential of its own, which is the same property that makes presigned S3 URLs
work.

Nothing here deletes remote objects. Expiry belongs to the remote, the way
bucket lifecycle rules own it for `s3`.
"""
from __future__ import annotations

import secrets
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

from services import StorageError, StorageService, resolve_prefix

_KNOWN_OPTS: set[str] = {"prefix", "timeout_s", "rclone_path"}

DEFAULT_PREFIX = "a8s"
DEFAULT_TIMEOUT_S = 300
DEFAULT_RCLONE_PATH = "rclone"


def _drive_direct_link(parsed: urllib.parse.SplitResult) -> str | None:
    """`rclone link` on Drive returns a viewer URL that 307s to the real host.
    A receiver follows that redirect, but a direct URL is better: it costs one
    less round trip and one less chance for Drive to answer with an interstitial
    page instead of the bytes."""
    file_id = ""
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("id"):
        file_id = query["id"][0]
    else:
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "file" and parts[1] == "d":
            file_id = parts[2]
    if not file_id:
        return None
    return (
        "https://drive.usercontent.google.com/download"
        f"?id={urllib.parse.quote(file_id, safe='')}&export=download"
    )


# A public link is only useful if it serves the bytes. rclone hands back
# whatever the backend calls a share URL, which for several backends is a
# preview page — saving that as the attachment would be silent corruption, so
# an unrecognized host fails loud instead. Adding a backend means adding its
# direct-download rule here.
_DIRECT_LINK_HOSTS = {
    "drive.google.com": _drive_direct_link,
    "www.drive.google.com": _drive_direct_link,
}


def direct_download_url(link: str) -> str:
    parsed = urllib.parse.urlsplit(link.strip())
    host = (parsed.hostname or "").lower()
    rule = _DIRECT_LINK_HOSTS.get(host)
    if rule is None:
        raise StorageError(
            f"rclone link host {host or link!r} has no known direct-download "
            "form; a8s would store the preview page instead of the file"
        )
    direct = rule(parsed)
    if not direct:
        raise StorageError(f"cannot derive a direct-download URL from {link!r}")
    return direct


class RcloneService(StorageService):
    def __init__(self, name: str, *, url: str, **opts: Any) -> None:
        unknown = set(opts) - _KNOWN_OPTS
        if unknown:
            raise ValueError(
                f"storage {name!r}: unknown option(s) {', '.join(sorted(unknown))}"
            )
        parsed = urllib.parse.urlsplit(url.strip())
        if parsed.scheme.lower() != "rclone":
            raise ValueError(f"storage {name!r}: expected an rclone:// URL")
        remote = (parsed.netloc or "").strip()
        if not remote:
            raise ValueError(
                f"storage {name!r}: rclone://<remote>/<path> needs a remote name"
            )
        self._name = name
        self._remote = remote
        self._base_path = urllib.parse.unquote(parsed.path).strip("/")
        self._prefix = resolve_prefix(opts)
        self._rclone = str(opts.get("rclone_path") or DEFAULT_RCLONE_PATH)
        raw_timeout = opts.get("timeout_s")
        self._timeout_s = int(DEFAULT_TIMEOUT_S if raw_timeout is None else raw_timeout)
        if self._timeout_s < 1:
            raise ValueError(f"storage {name!r}: timeout_s must be positive")
        self._minted: dict[str, str] = {}

    @property
    def id(self) -> str:
        return self._name

    @classmethod
    def supports_config_url(cls, url: str) -> bool:
        try:
            return urllib.parse.urlsplit(url.strip()).scheme.lower() == "rclone"
        except ValueError:
            return False

    def _remote_target(self, filename: str) -> str:
        token = secrets.token_hex(8)
        parts = [p for p in (self._base_path, self._prefix, token) if p]
        parts.append(Path(filename).name)
        return f"{self._remote}:{'/'.join(parts)}"

    def _run(self, args: list[str]) -> str:
        try:
            done = subprocess.run(
                [self._rclone, *args],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
        except FileNotFoundError as e:
            raise StorageError(
                f"rclone not found at {self._rclone!r}; set --rclone_path"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise StorageError(
                f"rclone {args[0]} timed out after {self._timeout_s}s"
            ) from e
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            reason = detail[-1] if detail else f"exit {done.returncode}"
            raise StorageError(f"rclone {args[0]} failed: {reason}")
        return done.stdout.strip()

    def store(self, src: Path, *, msg_id: str = "") -> str:
        target = self._remote_target(src.name)
        self._run(["copyto", str(src), target])
        link = self._run(["link", target])
        if not link:
            raise StorageError(f"rclone link returned nothing for {target}")
        url = direct_download_url(link)
        self._minted[url] = target
        return url

    def retrieve(self, url: str, dest: Path) -> bool:
        # Not ours to fetch: the URL is public https and the receiver's plain
        # GET handles it without rclone or credentials.
        return False

    def delete(self, url: str) -> bool:
        """Only for a URL this process minted.

        A share link from Drive or OneDrive carries an opaque file id, not the
        path the object was written to, so there is no way back from an
        arbitrary URL to a remote target. `store` remembers what it just
        uploaded, which is all `health` needs.
        """
        target = self._minted.pop(url, None)
        if target is None:
            return False
        try:
            self._run(["deletefile", target])
        except StorageError:
            return False
        return True
