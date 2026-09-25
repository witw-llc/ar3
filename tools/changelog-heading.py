#!/usr/bin/env python3
"""Fail if CHANGELOG.md has no heading for VERSION, or Unreleased is not empty.

0.1.80, 0.1.81 and 0.1.82 each merged with their notes still under
`## Unreleased`; the next batch renamed the heading after the fact each time.
CLAUDE.md's convention ("rename that heading to the version when the batch is
ready to merge") was a human step at merge time, and merge time is exactly
when nobody is looking at the changelog. This tool makes the rename a gate
instead of a reminder (#246).

    tools/changelog-heading.py 0.1.96              # reads CHANGELOG.md
    tools/changelog-heading.py 0.1.96 --changelog PATH

Exit 0 and silent when `## <VERSION>` exists and `## Unreleased`, if present,
has nothing under it. Exit 1 with a one-line message naming the fix otherwise.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# A changelog heading names its version first, then an optional " — date" or
# other trailer, so only the first token after "## " is the version.
_HEADING_RE = re.compile(r"^##\s+(\S+)(?:\s.*)?$", re.MULTILINE)


def check(text: str, version: str) -> str | None:
    """Return an error message, or None when the changelog is ready to ship `version`."""
    headings = list(_HEADING_RE.finditer(text))
    names = [m.group(1) for m in headings]
    if version not in names:
        return (
            f"CHANGELOG.md has no `## {version}` heading — "
            f"rename `## Unreleased` to `## {version} — <date>`"
        )
    for index, match in enumerate(headings):
        if match.group(1) != "Unreleased":
            continue
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        if text[start:end].strip():
            return (
                f"CHANGELOG.md still has entries under `## Unreleased` — "
                f"rename `## Unreleased` to `## {version} — <date>`"
            )
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the VERSION this changelog must be ready for")
    parser.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG)
    args = parser.parse_args(argv)

    text = args.changelog.read_text(encoding="utf-8")
    message = check(text, args.version)
    if message is None:
        return 0
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
