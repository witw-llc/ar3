#!/usr/bin/env python3
"""Deterministic fake judge for `r4t lab run --fake`.

Reads the judge prompt from argv[1], finds every `Qn: <text>` line, and emits
one parseable `Qn: yes|no — fake` answer per question — zero LLM calls, so the
whole lab pipeline (probe, invoke, parse, grade, ledger, report) runs at no
cost. The answer is a pure function of the question's *position* in the prompt
(odd = yes, even = no), so it is identical across trials (perfect within-arm
consistency) and identical across arms question-for-question (perfect
cross-arm agreement) — the clean baseline a real judge is measured against.

Set R4T_LAB_FAKE_PARSE_ERROR=1 to garble the first answer, exercising the
exclusion path. Set R4T_LAB_FAKE_SCRATCH=1 to write a scratch file into the
judge's cwd (mimicking a tool-enabled harness like `opencode --auto`) and
report its path, exercising the hermetic-cwd guarantee.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

QUESTION_RE = re.compile(r"(?im)^\s*(Q\d+)\s*:\s*\S")


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if os.environ.get("R4T_LAB_FAKE_SCRATCH") == "1":
        scratch = pathlib.Path.cwd() / "transcript_grading.md"
        scratch.write_text("scratch notes\n", encoding="utf-8")
        print(f"scratch: {scratch}")
    qids = []
    for qid in QUESTION_RE.findall(prompt):
        if qid.upper() not in qids:
            qids.append(qid.upper())
    garble = os.environ.get("R4T_LAB_FAKE_PARSE_ERROR") == "1"
    for i, qid in enumerate(qids, 1):
        if garble and i == 1:
            print(f"{qid}: maybe, hard to say")
            continue
        verdict = "yes" if i % 2 == 1 else "no"
        print(f"{qid}: {verdict} — fake deterministic judge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
