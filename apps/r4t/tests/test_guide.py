"""The guide quotes r4t's own output; these pin the quotes to the code.

*The Ark Raising* is a tutorial whose promise is that a reader who types the
commands sees the printed lines. That promise decays silently: a receipt gets
reworded in `r4t.py`, every suite stays green, and the chapter teaches output
the build no longer produces. Each check below names one string the code owns
and asserts it in both places — the chapter that quotes it and the source that
prints it. Rename the string and the source half fails, which is the reminder
to re-run the chapter and re-capture its transcript.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUIDE = REPO / "guide"
R4T_PY = REPO / "apps" / "r4t" / "r4t.py"
DISPATCH_PY = REPO / "apps" / "r4t" / "dispatch.py"
KNOWLEDGE_PY = REPO / "apps" / "r4t" / "knowledge.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# (needle, chapter, the source file that prints it)
QUOTED = [
    # `r4t init` — chapter 2's first command.
    ("runbook: wrote starter", "02-the-founding.md", R4T_PY),
    ("next: r4t add", "02-the-founding.md", R4T_PY),
    # `r4t add` — the success block that replaced chapter 2's a8s trio.
    ("  runbook:   ", "02-the-founding.md", R4T_PY),
    ("  address:   ", "02-the-founding.md", R4T_PY),
    ("  ceiling:   permissions ", "02-the-founding.md", R4T_PY),
    # The ticker, one line per lifecycle event.
    ("r4t: QUEUED ", "02-the-founding.md", DISPATCH_PY),
    ("r4t: PROMPT ", "02-the-founding.md", DISPATCH_PY),
    ("r4t: ECHO-REPLY ", "02-the-founding.md", DISPATCH_PY),
    ("r4t: RELEASED ", "02-the-founding.md", DISPATCH_PY),
    # Chapter 3 reads the same ticker around a flush and a rig swap.
    ("r4t: FLUSH ", "03-the-long-memory.md", DISPATCH_PY),
    ("r4t: SILENT ", "03-the-long-memory.md", DISPATCH_PY),
    ("r4t: CONTINUE-SWAP ", "03-the-long-memory.md", DISPATCH_PY),
    # Chapter 4 reads a delegation, both gates, and the drain behind them.
    ("r4t: STDOUT-REPLY ", "04-a-second-pair-of-hands.md", DISPATCH_PY),
    ("r4t: DEFERRED ", "04-a-second-pair-of-hands.md", DISPATCH_PY),
    ("RESTING", "04-a-second-pair-of-hands.md", DISPATCH_PY),
    # Chapter 6 watches the dreaming skip, then run.
    ("r4t: DREAM", "06-the-dreaming.md", KNOWLEDGE_PY),
]


@pytest.mark.parametrize("needle,chapter,source", QUOTED)
def test_the_chapter_quotes_what_the_code_prints(needle, chapter, source):
    assert needle in read(GUIDE / chapter), f"{chapter} no longer quotes {needle!r}"
    assert needle in read(source), (
        f"{source.name} no longer prints {needle!r} — re-run {chapter} and "
        "re-capture its transcript"
    )


# Surfaces this release retired. A tutorial that still teaches them hands the
# reader a command that does not exist.
RETIRED = ("r4t seat", "r4t task", "r4t chat", "r4t tui")


@pytest.mark.parametrize("verb", RETIRED)
def test_no_chapter_teaches_a_retired_verb(verb):
    for chapter in sorted(GUIDE.glob("*.md")) + sorted(GUIDE.glob("templates/**/*")):
        if not chapter.is_file():
            continue
        assert verb not in read(chapter), f"{chapter.name} still teaches `{verb}`"


def test_every_roster_template_parses_and_marks_one_leader():
    """A template is a copy-paste starting point; a broken one wastes the
    reader's chapter. The roster templates are `r4t.md` runbooks now, so hold
    them to the parser the chapters run."""
    import runbook

    templates = sorted(GUIDE.glob("templates/*/r4t.md"))
    assert templates, "no roster templates found — did they move?"
    for path in templates:
        book = runbook.parse_file(read(path), path, path.parent.name)
        roster = book.sections.get("Roster")
        assert roster, f"{path} has no `## Roster` section"
        leaders = [b.name for b in roster.blocks if "leader" in b.fields]
        assert len(leaders) == 1, f"{path} marks {len(leaders)} leaders, want 1"
