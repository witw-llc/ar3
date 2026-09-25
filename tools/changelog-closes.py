#!/usr/bin/env python3
"""Print the issue numbers a CHANGELOG.md version section closes.

A squash merge does not honour a `Closes #N` written in a commit body, so a
changelog entry is the only surviving record of which issue a release closes
(#246). `release.yml`'s publish job reads this after a release ships and
closes each issue itself; this file is where that parsing lives, so it is
testable without a release.

    tools/changelog-closes.py 0.1.96              # reads CHANGELOG.md
    tools/changelog-closes.py 0.1.96 --changelog PATH

Prints one issue number per line, ascending, de-duplicated. A version with no
section, or no `Closes #N` lines, prints nothing. Exit 0 always — an empty
release note is not a failure.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANGELOG = REPO_ROOT / "CHANGELOG.md"

_HEADING_RE = re.compile(r"^##\s+(\S+)(?:\s.*)?$", re.MULTILINE)
_CLOSES_RE = re.compile(r"[Cc]loses\s+#(\d+)")


def section_for_version(text: str, version: str) -> str | None:
    """Return the body of the `## <version>` section, or None when it is absent."""
    headings = list(_HEADING_RE.finditer(text))
    for index, match in enumerate(headings):
        if match.group(1) != version:
            continue
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        return text[start:end]
    return None


def closes_for_version(text: str, version: str) -> list[int]:
    section = section_for_version(text, version)
    if section is None:
        return []
    return sorted({int(n) for n in _CLOSES_RE.findall(section)})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the released VERSION to read the section of")
    parser.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG)
    args = parser.parse_args(argv)

    text = args.changelog.read_text(encoding="utf-8")
    for number in closes_for_version(text, args.version):
        print(number)
    return 0


if __name__ == "__main__":
    sys.exit(main())
