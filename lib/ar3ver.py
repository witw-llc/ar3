"""The suite's version, and whether a newer one has been published.

Every merge to `main` bumps the repo-root `VERSION` file and pushes a
`v<version>` tag to the public mirror, so that file is what a running copy is,
and the mirror's newest tag is what the world can get. One module because all
four CLIs answer `--version` the same way.

The update check is never on the path of ordinary work: it runs only when a
command asks for it (`ar3 doctor`), it has a short timeout, and it returns
None on any failure. A tool that cannot reach GitHub is not a broken tool.
"""
from __future__ import annotations

import json
import platform
import urllib.error
import urllib.request
from pathlib import Path

PUBLIC_REPO = "witw-llc/ar3"
_TAGS_URL = f"https://api.github.com/repos/{PUBLIC_REPO}/tags?per_page=100"
DEFAULT_TIMEOUT_S = 3.0

UNKNOWN = "unknown"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def suite_version() -> str:
    """The version of this working copy, or `unknown` with no VERSION file."""
    try:
        text = (repo_root() / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return UNKNOWN
    return text or UNKNOWN


def version_line(app: str) -> str:
    """What every `<app> --version` prints: the app, the suite semver, and the
    Python running it — argparse behavior differs by interpreter version, so a
    field report's `--version` paste should answer that question by itself."""
    return f"{app} {suite_version()} (ar3, python {platform.python_version()})"


def parse_version(text: str) -> tuple[int, ...] | None:
    """`0.1.58` or `v0.1.58` as a comparable tuple, else None."""
    raw = (text or "").strip().lstrip("vV")
    if not raw:
        return None
    parts = raw.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def latest_public_version(timeout_s: float = DEFAULT_TIMEOUT_S) -> str | None:
    """The newest `v<semver>` tag on the public mirror, or None if unreachable.

    Tags are listed rather than releases: the release workflow pushes a tag and
    does not always cut a GitHub Release, so `releases/latest` can 404 on a
    mirror that is perfectly up to date.
    """
    req = urllib.request.Request(
        _TAGS_URL,
        headers={"User-Agent": "ar3/1", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            tags = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    best: tuple[int, ...] | None = None
    best_name = None
    for tag in tags if isinstance(tags, list) else []:
        parsed = parse_version(str(tag.get("name", "")))
        if parsed and (best is None or parsed > best):
            best, best_name = parsed, str(tag["name"]).lstrip("vV")
    return best_name


def update_note(timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    """One line for `doctor`: this version, and a newer public one if there is
    one. Silent about the network when the check simply could not run."""
    mine = suite_version()
    latest = latest_public_version(timeout_s)
    if latest is None:
        return f"{mine} (could not reach {PUBLIC_REPO} to check for a newer one)"
    ours, theirs = parse_version(mine), parse_version(latest)
    if ours is None or theirs is None or ours >= theirs:
        return f"{mine} (latest)"
    return f"{mine} — {latest} is available at github.com/{PUBLIC_REPO}"
