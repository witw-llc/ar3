"""Chapter 1 opens by pasting `ar3`'s banner; these pin it to the source.

The guide's promise is that what a reader types produces what the page shows.
The banner is the very first output in the very first chapter, so a silent
drift there is the worst one available. `cli.py` owns the strings; the chapter
and the guide's own README quote them.
"""
from __future__ import annotations

from pathlib import Path

import cli as ar3

GUIDE = Path(__file__).resolve().parents[3] / "guide"


def read(name: str) -> str:
    return (GUIDE / name).read_text(encoding="utf-8")


def test_chapter_one_quotes_the_wordmark_and_tagline():
    chapter = read("01-hello-agent.md")
    assert "\n".join(ar3.WORDMARK) in chapter
    assert "\n".join(ar3.TAGLINE) in chapter


def test_the_guide_readme_quotes_the_wordmark():
    assert "\n".join(ar3.WORDMARK) in read("README.md")
