#!/usr/bin/env python3
"""Enforce the suite's version-bump shape: major.minor.build.

Ruled 2026-09-25 (#272): the build increments on every merge to main; the
minor is the current phase, bumped with build reset to 0 when the owner closes
a phase; the major counts toward the 1.0 hand-off. Exactly one of the three
moves at a time — a PR's VERSION is always the prior one with one digit moved.

    tools/version-bump.py 0.1.95 0.1.96      # main's version, then the PR's
    tools/version-bump.py 0.1.95 0.2.0
    tools/version-bump.py 0.1.95 1.0.0

Exit 0 and silent when `head` is one of the three allowed next versions from
`main`. Exit 1 with a one-line message naming all three otherwise.
"""
from __future__ import annotations

import argparse
import sys


def parse(version: str) -> tuple[int, int, int]:
    parts = version.strip().split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"not a major.minor.build version: {version!r}")
    major, minor, build = (int(part) for part in parts)
    return major, minor, build


def allowed_next(main_version: str) -> dict[str, str]:
    """The three versions `main_version` is allowed to become, keyed by which digit moves."""
    major, minor, build = parse(main_version)
    return {
        "build": f"{major}.{minor}.{build + 1}",
        "minor": f"{major}.{minor + 1}.0",
        "major": f"{major + 1}.0.0",
    }


def check(main_version: str, head_version: str) -> str | None:
    """Return an error message, or None when `head_version` is a valid next version."""
    allowed = allowed_next(main_version)
    if head_version.strip() in allowed.values():
        return None
    return (
        f"VERSION {head_version} is not a valid next version from {main_version}. "
        f"Allowed: {allowed['build']} (build — every merge), "
        f"{allowed['minor']} (minor — a new phase, build resets to 0), "
        f"{allowed['major']} (major — the 1.0 hand-off)."
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("main_version", help="VERSION on main")
    parser.add_argument("head_version", help="VERSION on the PR branch")
    args = parser.parse_args(argv)

    message = check(args.main_version, args.head_version)
    if message is None:
        return 0
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
