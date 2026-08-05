"""Map between public download URLs and relative object keys."""
from __future__ import annotations

import urllib.parse


def public_scheme_ok(scheme: str) -> bool:
    """https always; plaintext http only when the operator opted in."""
    s = (scheme or "").lower()
    if s == "https":
        return True
    if s != "http":
        return False
    # get_int clamps to a minimum of 1, which would make 0 read as true.
    from settings import get_setting

    try:
        return int(get_setting("storage_allow_http")) != 0
    except (TypeError, ValueError):
        return False


def join_public_url(base_url: str, relative_key: str) -> str:
    base = base_url.strip().rstrip("/")
    parts = [p for p in relative_key.strip("/").split("/") if p]
    if not parts:
        return base
    encoded = "/".join(urllib.parse.quote(p, safe="") for p in parts)
    return f"{base}/{encoded}"


def relative_key_under_base(base_url: str, url: str) -> str | None:
    """When `url` is under the public `base_url`, return the relative key."""
    try:
        base = urllib.parse.urlsplit(base_url.strip())
        got = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if not public_scheme_ok(got.scheme) or not public_scheme_ok(base.scheme):
        return None
    base_host = (base.netloc or "").lower()
    got_host = (got.netloc or "").lower()
    if base_host != got_host:
        return None
    base_path = (base.path or "").rstrip("/")
    got_path = got.path or ""
    if base_path:
        prefix = base_path + "/"
        if not got_path.startswith(prefix) and got_path != base_path:
            return None
        rel = got_path[len(base_path) :].lstrip("/")
    else:
        rel = got_path.lstrip("/")
    if not rel or rel.endswith("/"):
        return None
    return urllib.parse.unquote(rel)
