#!/usr/bin/env python3
"""K-budget-packing LLM arm (n-small) -- do the mechanical coverage gains turn
into answers?

Reads the packs file k-budget-run.py wrote (rendered `## Knowledge` sections,
already packed per strategy and budget), wraps each one in the same member
prompt shape the shipped inject produces -- `KNOWLEDGE_HEADER` and
`KNOWLEDGE_FRAMING` imported from apps/r4t/knowledge.py so the arms can never
drift from what ships -- and asks agy the LongMemEval question. Scoring is
containment of the dataset's own `answer` string, plus token recall as the
lenient fallback.

Deliberately small: the theory-predicted movers (multi-session,
temporal-reasoning) at the two budgets a real member actually gets (2048 =
small tier, 8192 = medium tier), control strategy vs challenger. Report it as
n-small; it is a direction check, not a benchmark.

Usage:
  python3 k-budget-agy-run.py --packs PATH
                              [--strategies s1-greedy-whole,s3-head-then-fill]
                              [--budgets 2048,8192] [--strata multi-session,...]
                              [--limit N] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from knowledge import KNOWLEDGE_FRAMING, KNOWLEDGE_HEADER  # noqa: E402

AGY_WRAPPER = (
    'timeout 240 agy --dangerously-skip-permissions '
    '--model gemini-3.6-flash-low --print "$(cat)"'
)
MISSION_LINE = (
    "You are a member of a team assistant. Answer the teammate's question "
    "from your own recalled notes. Answer in one short sentence."
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_WORD_RE = re.compile(r"[a-z0-9]+")
_WS_RE = re.compile(r"\s+")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def build_prompt(record: dict, knowledge: str) -> str:
    parts = [
        MISSION_LINE,
        "",
        f"CURRENT DATE: {record['question_date']}",
        "",
        KNOWLEDGE_HEADER,
        KNOWLEDGE_FRAMING,
        "",
        knowledge,
        "",
        "## Messages since your last turn",
        "",
        "From: Lead",
        "",
        record["question"],
        "",
    ]
    return "\n".join(parts)


def call_agy(prompt: str, timeout: float) -> tuple[str, float, str | None]:
    t0 = time.monotonic()
    try:
        res = subprocess.run(
            ["sh", "-c", AGY_WRAPPER], input=prompt,
            capture_output=True, text=True, timeout=timeout,
        )
        wall = round(time.monotonic() - t0, 1)
        if res.returncode != 0:
            return strip_ansi(res.stdout), wall, f"exit {res.returncode}: {res.stderr.strip()[:200]}"
        return strip_ansi(res.stdout), wall, None
    except subprocess.TimeoutExpired:
        return "", round(time.monotonic() - t0, 1), f"timed out after {timeout}s"


def answer_variants(answer: str) -> list[str]:
    """LongMemEval answers sometimes carry their own alternates -- "30 days.
    31 days (including the last day) is also acceptable." -- so the whole
    string is a containment test no correct response can pass. The first
    sentence is the answer proper; keep both."""
    whole = answer.strip().strip(".").lower()
    first = re.split(r"(?<=[.;])\s+", answer.strip())[0].strip(" .").lower()
    return [whole] if whole == first else [whole, first]


def score(answer: str, response: str) -> dict:
    text = _WS_RE.sub(" ", response).lower()
    gold_words = set(_WORD_RE.findall(answer.lower()))
    return {
        "contains_answer": any(v in text for v in answer_variants(answer)),
        "answer_recall": round(sum(1 for w in gold_words if w in text) / len(gold_words), 3),
    }


def summarize(rows: list[dict], strategies: list[str], budgets: list[int]) -> dict:
    summary = {}
    for name in strategies:
        for budget in budgets:
            for stratum in ["all"] + sorted({r["stratum"] for r in rows}):
                subset = [
                    r for r in rows if r["strategy"] == name and r["budget"] == budget
                    and (stratum == "all" or r["stratum"] == stratum)
                ]
                if not subset:
                    continue
                summary[f"{name}/{budget}/{stratum}"] = {
                    "n": len(subset),
                    "contains_answer": sum(1 for r in subset if r["contains_answer"]),
                    "recall_mean": round(
                        sum(r["answer_recall"] for r in subset) / len(subset), 3
                    ),
                    "full_coverage": sum(1 for r in subset if r["full_coverage"]),
                    "errors": sum(1 for r in subset if r["error"]),
                }
    return summary


def markdown(summary: dict, strategies: list[str], budgets: list[int]) -> str:
    lines = [
        "| strategy | budget | stratum | n | contains answer | answer recall | "
        "full coverage | errors |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for key, s in summary.items():
        name, budget, stratum = key.split("/")
        lines.append(
            f"| {name} | {budget} | {stratum} | {s['n']} | "
            f"{s['contains_answer']}/{s['n']} | {s['recall_mean']} | "
            f"{s['full_coverage']}/{s['n']} | {s['errors']} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--packs", required=True, help="packs.json from k-budget-run.py")
    ap.add_argument("--coverage", help="results.json from the same run, to label rows "
                                       "with their mechanical full-coverage verdict")
    ap.add_argument(
        "--strategies",
        default="s1-greedy-whole,s2-per-entry-cap,s4-rank-proportional",
        help="control plus challengers; the mechanical arm's best strategy is "
             "budget-dependent, so both leaders ride along",
    )
    ap.add_argument("--budgets", default="2048,8192")
    ap.add_argument("--strata", default="multi-session,temporal-reasoning")
    ap.add_argument("--limit", type=int, default=0, help="cap total agy calls")
    ap.add_argument("--agy-timeout", type=float, default=260.0)
    ap.add_argument(
        "--out",
        default=os.path.expanduser("~/.config/r4t/lab/k-budget-packing/agy"),
    )
    args = ap.parse_args()
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    budgets = [int(b) for b in args.budgets.split(",") if b.strip()]
    strata = {s.strip() for s in args.strata.split(",") if s.strip()}

    packs = json.loads(Path(args.packs).read_text(encoding="utf-8"))["questions"]
    questions = [q for q in packs if q["stratum"] in strata]
    for q in questions:
        # LongMemEval answers are not uniformly typed: a count question's
        # answer arrives as a bare JSON number.
        q["answer"] = str(q["answer"])

    coverage = {}
    if args.coverage:
        for row in json.loads(Path(args.coverage).read_text(encoding="utf-8"))["rows"]:
            coverage[(row["question_id"], row["strategy"], row["budget"])] = row

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    slug = stamp.replace(":", "")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{slug}.json"

    cells = [
        (record, name, budget)
        for record in questions for name in strategies for budget in budgets
    ]
    if args.limit:
        cells = cells[:args.limit]

    rows: list[dict] = []
    for i, (record, name, budget) in enumerate(cells):
        prompt = build_prompt(record, record["packs"][f"{name}/{budget}"])
        response, wall, error = call_agy(prompt, args.agy_timeout)
        cov = coverage.get((record["question_id"], name, budget), {})
        row = {
            "question_id": record["question_id"],
            "stratum": record["stratum"],
            "strategy": name,
            "budget": budget,
            "full_coverage": bool(cov.get("full_coverage")),
            "packed_entries": cov.get("entries"),
            "prompt_bytes": len(prompt.encode("utf-8")),
            "wall_seconds": wall,
            "error": error,
            "answer": record["answer"],
            "response": response,
            **score(record["answer"], response),
        }
        rows.append(row)
        print(
            f"  [{i + 1}/{len(cells)}] {record['question_id']} {name}@{budget} "
            f"{'HIT ' if row['contains_answer'] else 'miss'} "
            f"recall={row['answer_recall']} {wall}s"
            f"{' ERROR: ' + error if error else ''}",
            file=sys.stderr,
        )
        # Flush after every call: agy cells run for minutes and the arm is
        # unattended; a crash must not cost the completed rows.
        json_path.write_text(json.dumps({
            "run": stamp, "rows": rows,
            "summary": summarize(rows, strategies, budgets),
        }, indent=2), encoding="utf-8")

    summary = summarize(rows, strategies, budgets)
    table = markdown(summary, strategies, budgets)
    md_path = out_dir / f"{slug}.md"
    md_path.write_text(
        f"# k-budget-packing agy arm (n-small) — {stamp}\n\n{table}\n", encoding="utf-8"
    )
    print()
    print(table)
    print(f"\nresults: {json_path}")
    print(f"summary: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
