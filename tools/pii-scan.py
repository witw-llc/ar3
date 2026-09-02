#!/usr/bin/env python3
"""Fail if any tracked file in the tree carries a PII pattern.

`.github/pii_check.py` scans a git *diff*, which is the right shape for a
pre-push hook: it stops PII on the way in. It is the wrong shape for finding
PII that is already in. A name committed before its pattern existed — or
before anyone thought to add one — appears in no future diff, so the diff
guard passes over it forever while the name ships in every release.

That is not hypothetical. A private machine name sat in
`docs/a8s-filedrop.md` as the worked example for filedrop setup, and the
public wiki recipe sends agents to that page to learn the mechanics, so every
agent that followed it copied the name into its own context. The diff guard
could not have caught it at any point after the commit that introduced it.

This file names no example. It is scanned like every other tracked file —
a scanner that exempts itself is a scanner that cannot see its own leak, and
this docstring is exactly where one would land.

Repo-wide by nature, which is why it is here rather than in a suite: the
per-PR workflow routes by path, so a whole-tree scan living in one app's tests
would stay green by not running. `release.yml` runs it over the tree on every
merge, and it costs nothing by hand:

    python3 tools/pii-scan.py

Patterns come from the same place the diff guard reads them — the
`PII_PATTERNS` secret in CI, `.github/pii-patterns.local.txt` locally — so
there is one list to maintain, not two. With neither configured this exits 1
and says so, because a PII scan that silently checks nothing is worse than no
scan: it reports success.

Only tracked files are scanned. Untracked and ignored paths do not ship, and
`.files/` in particular holds received mail, which is a record rather than
source.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".github"))

from pii_check import load_patterns  # noqa: E402

# The scan is about what ships as text. Everything here is either binary or a
# pattern list that names the very strings it exists to catch.
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".m4a",
    ".mp3", ".mp4", ".woff", ".woff2", ".db", ".pyc",
}
# Only the pattern lists are exempt, because they ARE the strings being
# hunted. Nothing else is, this file included.
SKIP_PATHS = {
    ".github/pii-patterns.example.txt",
    ".github/pii-patterns.local.txt",
}
MAX_REPORTED = 40


class GitUnavailable(RuntimeError):
    """The tree could not be enumerated, which is not the same as empty."""


def tracked_files() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        # Fail closed. An empty file list and an unreadable repository are
        # indistinguishable downstream, and one of them would print "clean".
        raise GitUnavailable(
            f"git ls-files failed in {REPO_ROOT} "
            f"({proc.stderr.strip() or f'exit {proc.returncode}'})"
        )
    files = [f for f in proc.stdout.split("\0") if f]
    if not files:
        raise GitUnavailable(f"git ls-files listed no tracked files in {REPO_ROOT}")
    return files


def scan(patterns: list[str]) -> list[tuple[str, int, int]]:
    compiled = [(i, re.compile(p, re.IGNORECASE)) for i, p in enumerate(patterns, 1)]
    hits: list[tuple[str, int, str]] = []
    for name in tracked_files():
        if name in SKIP_PATHS or Path(name).suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for index, rx in compiled:
                if rx.search(line):
                    hits.append((name, lineno, index))
                    break
    return hits


def main() -> int:
    try:
        patterns = load_patterns()
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        print(
            "pii-scan: refusing to report clean with no patterns loaded.",
            file=sys.stderr,
        )
        return 1
    try:
        hits = scan(patterns)
    except GitUnavailable as e:
        print(f"pii-scan: {e}", file=sys.stderr)
        print(
            "pii-scan: refusing to report clean on a tree it could not read.",
            file=sys.stderr,
        )
        return 1
    if not hits:
        print(f"pii-scan: clean across the tracked tree ({len(patterns)} pattern(s))")
        return 0
    # Neither the matched line nor the pattern is printed. Both are the PII,
    # and this output lands in CI logs — a pattern is a bare name or hostname,
    # and GitHub masks a secret's whole value, not the individual lines inside
    # it. The pattern's 1-based position in the loaded list is enough to find
    # it in the local file, and means nothing to anyone reading the log.
    for name, lineno, index in hits[:MAX_REPORTED]:
        print(f"{name}:{lineno}: matches PII pattern #{index}", file=sys.stderr)
    if len(hits) > MAX_REPORTED:
        print(f"... and {len(hits) - MAX_REPORTED} more", file=sys.stderr)
    print(f"\npii-scan: {len(hits)} hit(s) in the tracked tree.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
