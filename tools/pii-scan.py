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

Only tracked files are scanned, and what is scanned is the *tracked
representation*, not the working tree: the index's blob for each path, plus
the path name itself. A file name carries PII as readily as a line does, a
symlink ships its target text rather than whatever it points at, and a file
whose bytes are not valid UTF-8 still carries readable ASCII. Reading the
working tree with `read_text` saw none of those three. Untracked and ignored
paths do not ship, and `.files/` in particular holds received mail, which is
a record rather than source.

What git has is the index, so a working-tree edit is scanned once it is
staged. CI checks out a clean tree, where the two are the same thing.

Nothing that could be the PII is printed — not the pattern, not the matched
line, and not the path when the path is itself the hit. A redacted path is
reported by its position in `git ls-files`, which resolves locally with
`git ls-files | sed -n '<N>p'` and means nothing in a CI log.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".github"))

from pii_check import NoPatterns, load_patterns  # noqa: E402

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
    """The tree could not be read, which is not the same as clean."""


class Entry(NamedTuple):
    """One tracked path, as git stores it."""

    position: int  # 1-based place in `git ls-files`, the redacted path's name
    mode: str  # 120000 is a symlink; its blob is the link target text
    sha: str  # the index blob; a symlink's blob is its target text
    path: str


class Hit(NamedTuple):
    entry: Entry
    pattern: int  # 1-based position in the loaded list — never the pattern
    lineno: int | None  # None when the path name itself is what matched


def tracked_entries() -> list[Entry]:
    proc = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        cwd=REPO_ROOT, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        # Fail closed. An empty file list and an unreadable repository are
        # indistinguishable downstream, and one of them would print "clean".
        raise GitUnavailable(
            f"git ls-files failed in {REPO_ROOT} "
            f"({proc.stderr.decode('utf-8', 'replace').strip() or f'exit {proc.returncode}'})"
        )
    entries: list[Entry] = []
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        meta, tab, raw_path = record.partition(b"\t")
        if not tab:
            raise GitUnavailable(f"unparsable git ls-files record in {REPO_ROOT}")
        fields = meta.split()
        if len(fields) != 3:
            raise GitUnavailable(f"unparsable git ls-files record in {REPO_ROOT}")
        # A path is bytes on POSIX and need not be UTF-8. Surrogate-escaping
        # keeps every byte addressable instead of dropping the name.
        entries.append(
            Entry(len(entries) + 1, fields[0].decode("ascii"), fields[1].decode("ascii"),
                  raw_path.decode("utf-8", "surrogateescape"))
        )
    if not entries:
        raise GitUnavailable(f"git ls-files listed no tracked files in {REPO_ROOT}")
    return entries


def _read_exactly(stream, count: int) -> bytes:
    chunks: list[bytes] = []
    while count:
        chunk = stream.read(count)
        if not chunk:
            raise GitUnavailable("git cat-file stopped mid-object")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def blob_bytes(entries: list[Entry]) -> Iterator[tuple[Entry, bytes]]:
    """The stored blob for each entry, straight from the object store.

    Not `read_text` on the working tree. That follows a symlink, so the scan
    read whatever the link pointed at — a directory, a file outside the repo,
    or nothing — while what git ships is the target *string*. It also raised
    `UnicodeDecodeError` on any file that is not valid UTF-8, which the old
    scan swallowed and skipped.

    One `git cat-file --batch` serves the whole tree: a subprocess per file
    costs a second per thousand files for the same bytes. Each request is
    written and its answer read before the next goes out, so neither pipe
    fills.
    """
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        for entry in entries:
            proc.stdin.write(entry.sha.encode("ascii") + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline()
            fields = header.split()
            if len(fields) != 3 or fields[1] != b"blob":
                raise GitUnavailable(
                    f"git cat-file could not read tracked object {entry.sha}"
                )
            data = _read_exactly(proc.stdout, int(fields[2]))
            _read_exactly(proc.stdout, 1)  # the record's trailing newline
            yield entry, data
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        proc.stdout.close()
        proc.wait()


def scan(patterns: list[str]) -> list[Hit]:
    compiled = [(i, re.compile(p, re.IGNORECASE)) for i, p in enumerate(patterns, 1)]
    entries = tracked_entries()
    hits: list[Hit] = []

    # The name first. A path carries PII as readily as a line does, and it is
    # the one string that ships even when the file's contents are skipped as
    # binary.
    for entry in entries:
        for index, rx in compiled:
            if rx.search(entry.path):
                hits.append(Hit(entry, index, None))
                break

    # The suffix skip is for binary regular-file content — a `.png`'s bytes
    # are not text. A symlink's blob is never binary: it is the target path
    # as text, however the link's own name ends, so the suffix skip does not
    # apply to it. Mode 120000 is a symlink; git has no other non-regular
    # blob-bearing mode among tracked entries.
    readable = [
        e for e in entries
        if e.path not in SKIP_PATHS
        and (e.mode == "120000" or Path(e.path).suffix.lower() not in SKIP_SUFFIXES)
    ]
    for entry, data in blob_bytes(readable):
        # Surrogate-escaped rather than strict: a file whose bytes are not
        # valid UTF-8 still carries readable ASCII, and refusing to decode it
        # was how an undecodable blob went unscanned.
        for lineno, line in enumerate(
            data.decode("utf-8", "surrogateescape").splitlines(), 1
        ):
            for index, rx in compiled:
                if rx.search(line):
                    hits.append(Hit(entry, index, lineno))
                    break
    return hits


def report_line(hit: Hit, redacted: set[str]) -> str:
    """One hit, with nothing in it that could be the PII.

    Neither the matched line nor the pattern is printed. Both are the PII,
    and this output lands in CI logs — a pattern is a bare name or hostname,
    and GitHub masks a secret's whole value, not the individual lines inside
    it. The pattern's 1-based position in the loaded list is enough to find it
    in the local file and means nothing to anyone reading the log.

    The path goes the same way when the path is itself a hit: naming the file
    would print the very string the scan exists to catch. Its position in
    `git ls-files` stands in.
    """
    where = (
        f"tracked file #{hit.entry.position}"
        if hit.entry.path in redacted else hit.entry.path
    )
    if hit.lineno is None:
        return f"{where}: path name matches PII pattern #{hit.pattern}"
    return f"{where}:{hit.lineno}: matches PII pattern #{hit.pattern}"


def main() -> int:
    try:
        patterns = load_patterns()
    except NoPatterns as e:
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
    redacted = {h.entry.path for h in hits if h.lineno is None}
    for hit in hits[:MAX_REPORTED]:
        print(report_line(hit, redacted), file=sys.stderr)
    if len(hits) > MAX_REPORTED:
        print(f"... and {len(hits) - MAX_REPORTED} more", file=sys.stderr)
    if redacted:
        print(
            "\npii-scan: a numbered file is one whose own path matched; "
            "`git ls-files | sed -n '<N>p'` names it locally.",
            file=sys.stderr,
        )
    print(f"\npii-scan: {len(hits)} hit(s) in the tracked tree.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
