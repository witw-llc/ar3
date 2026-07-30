"""Forbidden-pattern sweep — the 'bones' of the verification round.

Playtest verification (eyes) and a pattern sweep (bones) catch disjoint
failure classes: eyes confirm behavior, bones catch literal escapes and
self-certification phrases the eyes glide past. This module is the bones.

Patterns live OUTSIDE every repo (machine-global, uncommitted, may carry
private strings): `default.txt` applies to every node, `<node>.txt` adds
per-node lines. One Python regex per line; `#` comments and blank lines are
ignored; case-sensitivity is the author's business via inline `(?i)`.

Information asymmetry is the design: the caller gets an opaque pass/fail on
stdout, and the detail (which pattern, which file, which line) goes to stderr
where only the human reads it. An agent cannot game a check whose findings it
cannot see.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import state


class CheckError(Exception):
    """Operational failure (exit 2): a malformed pattern or an unreadable
    workplace. Distinct from findings (exit 1) and a clean pass (exit 0)."""


def checklists_dir() -> Path:
    return state.r4t_home() / "checklists"


def load_patterns(node: str) -> list[tuple[str, re.Pattern[str]]]:
    """Compile `default.txt` + `<node>.txt` into (source, compiled) pairs.
    Raises CheckError naming the file and line on the first malformed regex or
    unreadable checklist."""
    base = checklists_dir()
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for path in (base / "default.txt", base / f"{node}.txt"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise CheckError(f"{path}: cannot read checklist: {e}") from e
        for lineno, raw in enumerate(text.splitlines(), 1):
            pattern = raw.strip()
            if not pattern or pattern.startswith("#"):
                continue
            try:
                compiled.append((pattern, re.compile(pattern)))
            except re.error as e:
                raise CheckError(f"{path}:{lineno}: bad regex: {e}") from e
    return compiled


def sweep(
    workplace: Path, patterns: list[tuple[str, re.Pattern[str]]]
) -> list[tuple[str, int, str]]:
    """Every pattern against every line of every tracked text file. Returns
    (path, lineno, pattern) findings. Undecodable files (binaries) are skipped
    silently. Raises CheckError if `git ls-files` cannot run."""
    try:
        listing = subprocess.run(
            ["git", "ls-files"],
            cwd=str(workplace),
            capture_output=True,
            text=True,
        )
    except OSError as e:
        raise CheckError(f"cannot list files in {workplace}: {e}") from e
    if listing.returncode != 0:
        raise CheckError(
            f"git ls-files failed in {workplace}: {listing.stderr.strip()}"
        )
    findings: list[tuple[str, int, str]] = []
    for rel in listing.stdout.splitlines():
        if not rel:
            continue
        try:
            text = (workplace / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for source, pattern in patterns:
                if pattern.search(line):
                    findings.append((rel, lineno, source))
    return findings


def run(node: str, workplace: Path, *, out=sys.stdout, err=sys.stderr) -> int:
    """The `r4t check` body. stdout carries exactly the opaque verdict; stderr
    carries the findings and operational notes. Returns the exit code."""
    try:
        patterns = load_patterns(node)
    except CheckError as e:
        print(str(e), file=err)
        return 2
    if not patterns:
        print("no checklists configured", file=err)
        print("check passed", file=out)
        return 0
    try:
        findings = sweep(workplace, patterns)
    except CheckError as e:
        print(str(e), file=err)
        return 2
    if not findings:
        print("check passed", file=out)
        return 0
    for rel, lineno, source in findings:
        print(f"{rel}:{lineno}: {source}", file=err)
    print(f"check failed: {len(findings)} finding(s)", file=out)
    return 1
