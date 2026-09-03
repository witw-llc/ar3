#!/usr/bin/env python3
"""Fail if a public surface states a configured number as a promise.

A published cadence reads "every few minutes". It does not read "every 30
seconds", because 30 seconds is what a config file says today and what a
reader will hold the product to tomorrow. The 2026-08-30 cadence correction
set that standard; this tool keeps it. The rule generalises past cadence: an
absolute reliability word is the same defect with the number removed, and the
one-pager's banned vocabulary is the same defect in the register above it.

Three rules, and each one fails only on a claim that nothing backs:

    cadence   a bare interval or latency figure stated as behaviour, with no
              measurement marker in its sentence
    absolute  a reliability absolute -- never fails, guaranteed, zero
              downtime, instantly, always <works> -- asserted, not denied
    banned    the one-pager's forbidden register, on the two READMEs only
    dash      an em-dash or en-dash on README.md, the owner's no-dash rule

A sentence escapes the cadence rule by saying where its number came from:
"measured", "observed", a date, a `~`/about/roughly hedge, or a link to the
run. That is the whole point -- the tool does not ban numbers, it bans
numbers with no provenance.

Prose only. Fenced blocks and inline code are blanked before the scan,
because `--always-approve` is a flag name and `every 5 minutes` inside a
config sample is the configuration itself rather than a claim about it.

`tools/claim-sweep.allow` holds the lines a human has already judged, one
regex per line, each with its reason in the comment directly above it. A
pattern with no reason above it is a silent suppression, which is the thing
this tool exists to prevent, so it fails the run instead of taking effect.

Repo-wide by nature, which is why it is here rather than in a suite: the
per-PR workflow routes by path, so a whole-tree scan living in one app's
tests would stay green by not running. `release.yml` runs it on every merge,
and it costs nothing by hand:

    python3 tools/claim-sweep.py

Exit 0 when clean, 1 when a surface carries an unmeasured claim.
"""
from __future__ import annotations

import argparse
import bisect
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CADENCE = (
    re.compile(r"\bevery \d+(?:\.\d+)? ?(?:seconds?|minutes?|hours?)\b", re.I),
    re.compile(r"\bwithin \d+(?:\.\d+)? ?(?:seconds?|minutes?)\b", re.I),
    re.compile(r"\bin under \d+", re.I),
    re.compile(r"\b\d+ ?(?:s|ms) latency\b", re.I),
)

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
MEASUREMENT = re.compile(
    r"\bmeasured\b|\bmeasurement\b|\bobserved\b|\bbenchmark|\babout\b"
    r"|\broughly\b|~|\bon \d{4}-\d{2}-\d{2}\b|\bon (?:%s) \d{1,2}\b|\]\([^)]+\)"
    % MONTHS,
    re.I,
)

# `guarantee` as a noun is how this repo's docs name a contract, and the docs
# are full of it. Only the participle claims one.
ABSOLUTE = (
    re.compile(r"\bnever fails\b", re.I),
    re.compile(r"\bguaranteed\b", re.I),
    re.compile(r"\bzero downtime\b", re.I),
    re.compile(r"\binstantly\b", re.I),
    re.compile(
        r"\balways(?:\s+\w+){0,2}\s+"
        r"(?:works?|working|succeeds?|delivers?|arrives?|connects?"
        r"|available|online|reliable|up)\b",
        re.I,
    ),
)

# "environment-specific rather than guaranteed" denies the guarantee. Denials
# are the correct form of the claim, so they are not findings.
NEGATION = re.compile(
    r"\b(?:not|never|no|nothing|rather than|instead of|without|isn't|aren't)\b"
    r"(?:\s+\w+){0,3}\s*$",
    re.I,
)

BANNED = (
    re.compile(r"\borchestrate a team\b", re.I),
    re.compile(r"\bno-code\b", re.I),
    re.compile(r"\bdashboard\b", re.I),
    re.compile(r"\bframework\b", re.I),
)
# `framework` is banned as self-description, not as a word: a docs page may
# call someone else's framework a framework. The ban is scoped to the two
# surfaces that describe ar3 to a stranger.
BANNED_SURFACES = ("README.md", "guide/README.md")

# The owner's rule for public copy: no em-dash and no en-dash, ever; they
# are the first tell a reader uses to decide a machine wrote the page. The
# one-pager only, until the guide's own pass (#258); the docs keep theirs.
DASH = (re.compile("[\u2014\u2013]"),)
DASH_SURFACES = ("README.md",)

FENCE = re.compile(r"^\s*(?:```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
SEGMENT_BREAK = re.compile(
    r"(?<=[.!?])\s+|\n\s*\n|\n(?=\s*(?:[-*+] |\d+\. |#{1,6} |> ))|\|"
)


def surfaces(root: Path):
    readme = root / "README.md"
    if readme.is_file():
        yield readme
    yield from sorted((root / "docs").glob("*.md"))
    yield from sorted((root / "guide").rglob("*.md"))


def mask_code(text: str) -> str:
    """Blank every code span, keeping length so offsets still name a line."""
    lines = []
    fenced = False
    for line in text.split("\n"):
        if FENCE.match(line):
            fenced = not fenced
            lines.append(" " * len(line))
            continue
        lines.append(" " * len(line) if fenced else line)
    return INLINE_CODE.sub(lambda m: " " * len(m.group()), "\n".join(lines))


def segments(text: str):
    position = 0
    for lump in SEGMENT_BREAK.finditer(text):
        if lump.start() > position:
            yield position, text[position:lump.start()]
        position = lump.end()
    if position < len(text):
        yield position, text[position:]


def load_allowlist(root: Path):
    """The suppression patterns, and the entries that gave no reason.

    An entry is a regex, so it cannot carry a trailing `# why` the way
    `surface-audit.allow` does — `#` is a character a regex may need. The
    reason is therefore the comment block directly above the pattern.
    Consuming a pattern spends the reason: it is cleared the moment it backs
    one entry, so a second pattern stacked directly under the first — no
    blank line, no comment of its own — gets none of it. Each pattern needs
    its own comment immediately above it. A blank line also ends a reason,
    which keeps this file's own header from standing in as one for the
    first entry.
    """
    path = root / "tools" / "claim-sweep.allow"
    if not path.is_file():
        return [], []
    patterns, reasonless = [], []
    reason = ""
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            reason = ""
        elif line.startswith("#"):
            reason = line.lstrip("#").strip() or reason
        elif reason:
            patterns.append(re.compile(line))
            reason = ""
        else:
            reasonless.append((lineno, line))
    return patterns, reasonless


def scan_file(path: Path, relative: str):
    text = path.read_text(encoding="utf-8")
    line_starts = [0]
    for line in text.split("\n")[:-1]:
        line_starts.append(line_starts[-1] + len(line) + 1)
    lines = text.split("\n")
    masked = mask_code(text)

    banned_surface = relative in BANNED_SURFACES
    for start, segment in segments(masked):
        checks = [("cadence", CADENCE), ("absolute", ABSOLUTE)]
        if banned_surface:
            checks.append(("banned", BANNED))
        if relative in DASH_SURFACES:
            checks.append(("dash", DASH))
        for rule, patterns in checks:
            for pattern in patterns:
                for hit in pattern.finditer(segment):
                    if rule == "cadence" and MEASUREMENT.search(segment):
                        continue
                    if rule == "absolute" and NEGATION.search(segment[: hit.start()]):
                        continue
                    number = bisect.bisect_right(line_starts, start + hit.start())
                    yield rule, number, hit.group().strip(), lines[number - 1].strip()


def sweep(root: Path):
    allowlist, _ = load_allowlist(root)
    findings = []
    for path in surfaces(root):
        relative = path.relative_to(root).as_posix()
        for rule, number, matched, line in scan_file(path, relative):
            context = f"{relative}: {line}"
            if any(pattern.search(context) for pattern in allowlist):
                continue
            findings.append((relative, number, rule, matched))
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=REPO_ROOT, type=Path)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    _, reasonless = load_allowlist(root)
    findings = sweep(root)
    for relative, number, rule, matched in findings:
        print(f"{relative}:{number}: {rule} — {matched}")
    for lineno, entry in reasonless:
        print(f"tools/claim-sweep.allow:{lineno}: no reason given for {entry}")
    scanned = len(list(surfaces(root)))
    if findings:
        print(
            f"claim-sweep: {len(findings)} unmeasured "
            f"claim{'s' if len(findings) != 1 else ''} across {scanned} surfaces",
            file=sys.stderr,
        )
    if reasonless:
        print(
            f"claim-sweep: {len(reasonless)} allowlist "
            f"entr{'ies' if len(reasonless) != 1 else 'y'} with no reason above "
            "the pattern",
            file=sys.stderr,
        )
    if findings or reasonless:
        return 1
    print(f"claim-sweep: clean across {scanned} surfaces")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
