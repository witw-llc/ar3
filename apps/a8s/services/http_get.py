"""Plain HTTP(S) download for storage URLs that carry their own auth.

Presigned S3 URLs and similar links are fetched with a normal GET; no service-
specific credentials on the receiver."""
from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from services import StorageError

_CHUNK = 65536


def _http_allowed() -> bool:
    """A peer chooses the URL its attachments are fetched from, and these URLs
    carry their own authorization in the query string. https is the default;
    the knob exists for a self-hosted store on a LAN with no certificate."""
    # get_int clamps to a minimum of 1, which would make 0 read as true.
    from settings import get_setting

    try:
        return int(get_setting("storage_allow_http")) != 0
    except (TypeError, ValueError):
        return False


MAX_REDIRECTS = 3


class _LimitedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a small number of redirects, and hold the scheme rule at each one.

    Object stores redirect as a matter of course: a share URL points at the
    CDN host that has the bytes. Refusing all redirects breaks those links.
    Following them without a limit lets the sender of an envelope choose where
    a receiver goes, so the count is small and every step obeys the same
    https rule as the first URL."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        if scheme not in ("http", "https"):
            return None
        if scheme != "https" and not _http_allowed():
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get_url_to_path(
    url: str,
    dest: Path,
    *,
    timeout_s: int = 60,
    max_bytes: int | None = None,
) -> bool:
    """Download `url` into `dest` when the scheme is https.

    Returns False if the URL is neither http nor https (not ours — the caller
    should try another path). Raises `StorageError` when the transfer failed,
    including for plaintext `http://`: these URLs carry their own
    authorization in the query string, and the peer chose the host. A
    redirect is followed up to MAX_REDIRECTS times, and each step must obey
    the same scheme rule."""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return False
    if scheme != "https" and not _http_allowed():
        raise StorageError(
            f"refusing plaintext http:// attachment URL: {url} "
            "(set storage_allow_http=1 for a store without TLS)"
        )
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "a8s/1"},
    )
    opener = urllib.request.build_opener(_LimitedRedirectHandler)
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            if max_bytes is not None:
                cl = resp.headers.get("Content-Length")
                if cl is not None:
                    try:
                        if int(cl) > max_bytes:
                            raise StorageError(
                                f"download Content-Length {cl} exceeds max_file_bytes ({max_bytes})"
                            )
                    except ValueError:
                        pass
            tmp = dest.with_name(dest.name + ".part")
            dest.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with tmp.open("wb") as out:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        tmp.unlink(missing_ok=True)
                        raise StorageError(
                            f"download exceeded max_file_bytes ({max_bytes})"
                        )
                    out.write(chunk)
            os.replace(str(tmp), str(dest))
    except StorageError:
        raise
    except urllib.error.HTTPError as e:
        raise StorageError(
            f"download HTTP {e.code} for {url}: {e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise StorageError(
            f"download network error for {url}: {e.reason}"
        ) from e
    except OSError as e:
        raise StorageError(f"write failed for {dest}: {e}") from e
    return True
