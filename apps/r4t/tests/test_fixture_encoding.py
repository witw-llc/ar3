"""Every file this suite's fixtures write names its encoding.

The fake harnesses record the prompt they were given, and r4t's prompts are
full of em-dashes. A bare `open(path, "w")` on Windows writes the locale code
page, where U+2014 is the single byte 0x97 — which is not a valid UTF-8 start
byte, so the strict UTF-8 read on the other side dies. Twenty-two tests, and
the readers were right the whole time: the write was the unencoded end.

The failure mode is worse when it does not raise. Had the reader also been
bare, both ends would have agreed on cp1252 and the tests would have passed on
mojibake — which is how the same defect reaches production code elsewhere,
silently.

`newline=""` is asserted with it. A recording of argv must be what was passed,
and Windows text mode rewrites every `\\n` to `\\r\\n` on the way out.

Asserted on the source text rather than the syntax tree: the calls that matter
are inside generated scripts, which reach the AST as string constants. There
are no exemptions — an ASCII-only write costs nothing to spell correctly, and
a list of blessed exceptions is a list someone has to keep true.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
# `open(` followed by a write-mode literal, on one line. The path argument is
# often `os.path.join(...)`, so the scan cannot stop at the first `)`. Every
# call in this tree is one line; a future multi-line one would need this
# widened, which is why the mode literal is anchored to a comma rather than to
# the end of the call.
WRITE_OPEN = re.compile(r"(?<![\w.])open\(.*?,\s*[\"'](?P<mode>[wax]b?\+?)[\"']")
# This file is excluded from its own scan: it quotes the bad shape in prose
# and carries the pattern that matches it.
SOURCES = sorted(
    p.name for p in TESTS_DIR.glob("*.py") if p.name != Path(__file__).name
)


def test_the_glob_found_the_suite():
    """`parametrize` over an empty list yields no tests and reports green."""
    assert len(SOURCES) >= 20, SOURCES


@pytest.mark.parametrize("name", SOURCES)
def test_every_fixture_write_names_an_encoding(name):
    offenders = []
    for number, line in enumerate((TESTS_DIR / name).read_text(encoding="utf-8").splitlines(), 1):
        match = WRITE_OPEN.search(line)
        if not match or "b" in match.group("mode"):
            continue
        if "encoding=" in line or "encoding='" in line:
            continue
        offenders.append(f"{name}:{number}: {line.strip()}")
    assert not offenders, (
        "a fixture writes with the locale code page:\n" + "\n".join(offenders)
    )
