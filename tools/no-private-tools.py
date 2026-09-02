#!/usr/bin/env python3
"""Fail if a shipped source names a tool the reader does not have.

The owner ran `r4t engine agy quota` and was told to run a private program
that exists on one machine. A note pointing somewhere the reader cannot go is
worse than no note.

Repo-wide by nature, which is why it is here rather than in a suite. The
per-PR workflow routes by path, so a check that scans every app while living
in one app's tests stays green by not running — the same hole that let a shim
guard sleep through a shim change. `release.yml` runs this over the whole tree
on every merge, and it costs nothing to run by hand:

    python3 tools/no-private-tools.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Names that exist only on the owner's machine, or paths outside the suite that
# an installed copy never has.
FORBIDDEN = re.compile(r"\b(n0b)\b|~/bin/|\$HOME/bin/")

SCANNED = ("apps", "lib", "docs", "guide")
SKIP_PARTS = {"_vendor", "__pycache__", ".venv", "tests"}


def sources():
    for top in SCANNED:
        root = REPO_ROOT / top
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".py", ".md", ".json", ".sh"):
                continue
            if SKIP_PARTS & set(path.parts):
                continue
            yield path


def main() -> int:
    offenders = []
    for path in sources():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if FORBIDDEN.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{number}: {line.strip()}")
    if offenders:
        print("a shipped source names a tool the reader does not have:", file=sys.stderr)
        for line in offenders:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"no-private-tools: clean across {len(list(sources()))} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
