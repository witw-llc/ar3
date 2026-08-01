#!/usr/bin/env python3
"""K-budget-packing mechanical arm -- does the inject packer, not retrieval,
decide whether a question's evidence reaches the reader?

For every question in the frozen LongMemEval_S 50-question subset (reused from
`../k-next-embeddings/fixtures/k6/subset.json`): build one k7e store from the
haystack (one entry per user/assistant turn-pair, exactly as k6e-run.py does),
embed it, search it once with the bare question, then pack that single
retrieval result with every strategy at every budget. Store building dominates
the wall clock, so the 4x4 matrix costs one store, not sixteen.

The metric is EVIDENCE COVERAGE and needs no LLM: LongMemEval marks its
answer-bearing turns (`has_answer: true`), so a question is fully covered when
the text of *every* one of its gold turns survives into the packed prompt.
Multi-session and temporal-reasoning questions carry gold turns in more than
one session by construction -- they are what a one-entry prompt cannot answer.

Budgets are the #52 T-shirt sizes (2048/8192/32768) plus 4096, so the run also
reads on whether those values are set right.

Alongside the results it writes a packs file (the rendered `## Knowledge`
sections, keyed by question/strategy/budget) that k-budget-agy-run.py reads
for the LLM arm -- no second round of store building.

Usage:
  python3 k-budget-run.py [--limit N] [--budgets 2048,4096,8192,32768]
                          [--strategies s1-greedy-whole,...] [--dataset PATH]
                          [--out DIR] [--ollama-url URL] [--embed-model NAME]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
R4T_DIR = HERE.parents[1]
sys.path.insert(0, str(R4T_DIR))
sys.path.insert(0, str(R4T_DIR.parent / "k7e"))

import engine  # noqa: E402
import packing  # noqa: E402
from knowledge import SEARCH_LIMIT, _entry_snippet  # noqa: E402

K6_DIR = R4T_DIR / "experiments" / "k-next-embeddings"
DEFAULT_SUBSET = K6_DIR / "fixtures" / "k6" / "subset.json"
DEFAULT_DATASET = Path.home() / ".cache" / "k-next-longmemeval" / "longmemeval_s_cleaned.json"
BUDGETS = (2048, 4096, 8192, 32768)
STRATA = (
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
    "single-session-assistant",
    "single-session-preference",
    "single-session-user",
)

_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().lower()


def seed_store(question: dict) -> tuple[dict[str, dict], list[dict]]:
    """Store one entry per turn-pair. Returns (entry_id -> {title}, gold) where
    `gold` is one row per answer-bearing turn with the entry that swallowed it
    -- content-hash dedup can fold two identical turn-pairs onto one entry, so
    the mapping is resolved at write time, not guessed later."""
    entries: dict[str, dict] = {}
    gold: list[dict] = []
    for sid, session in zip(question["haystack_session_ids"], question["haystack_sessions"]):
        for pair_idx in range(0, len(session), 2):
            pair = session[pair_idx:pair_idx + 2]
            content = "\n".join(f"{t['role'].capitalize()}: {t['content']}" for t in pair)
            title = pair[0]["content"].replace("\n", " ").strip()[:80]
            entry_id = engine.store_entry(title or f"{sid} turn {pair_idx}", content, tags=[sid])
            entries.setdefault(entry_id, {"title": title})
            for offset, turn in enumerate(pair):
                if turn.get("has_answer"):
                    gold.append({
                        "session_id": sid,
                        "turn_index": pair_idx + offset,
                        "entry_id": entry_id,
                        "bytes": len(turn["content"].encode("utf-8")),
                        "text": normalize(turn["content"]),
                    })
    return entries, gold


def hit_entries(hits: list[dict]) -> list[dict]:
    """Rank-ordered packer input, built the way `knowledge_section` builds its
    blocks: shipped snippet extraction, shipped provenance stamp."""
    out = []
    for hit in hits:
        date, snippet = _entry_snippet(engine.get(hit["id"]))
        if not snippet:
            continue
        provenance = f"({hit['id']}, {date})" if date else f"({hit['id']})"
        out.append({
            "id": hit["id"],
            "preamble": f"### {hit.get('title') or hit['id']} {provenance}",
            "snippet": snippet,
        })
    return out


def prefix_fraction(needle: str, haystack: str) -> float:
    """Longest prefix of `needle` present in `haystack`, as a fraction. A
    truncated entry keeps its head, so this reads as how much of the gold turn
    the packer let through."""
    if not needle:
        return 1.0
    if needle in haystack:
        return 1.0
    lo, hi = 0, len(needle)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if needle[:mid] in haystack:
            lo = mid
        else:
            hi = mid - 1
    return round(lo / len(needle), 3)


def score_pack(packed: list[dict], entries: list[dict], gold: list[dict]) -> dict:
    text = normalize("\n\n".join(p["block"] for p in packed))
    covered = [g["text"] in text for g in gold]
    fractions = [prefix_fraction(g["text"], text) for g in gold]
    return {
        "entries": len(packed),
        "bytes": sum(p["bytes"] for p in packed),
        "packed_ids": [entries[p["index"]]["id"] for p in packed],
        "gold_covered": sum(covered),
        "gold_total": len(gold),
        "full_coverage": bool(gold) and all(covered),
        "any_coverage": any(covered),
        "gold_fraction": round(statistics.fmean(fractions), 3) if fractions else None,
    }


def run_question(question: dict, args) -> tuple[list[dict], dict, dict]:
    prior = {k: os.environ.get(k) for k in ("K7E_HOME", "OLLAMA_URL", "EMBED_MODEL")}
    try:
        with tempfile.TemporaryDirectory(prefix="kbp-") as tmp:
            os.environ["K7E_HOME"] = tmp
            os.environ["OLLAMA_URL"] = args.ollama_url
            os.environ["EMBED_MODEL"] = args.embed_model
            engine.reset(Path(tmp))
            engine.init()

            t0 = time.monotonic()
            stored, gold = seed_store(question)
            build_s = round(time.monotonic() - t0, 2)

            t0 = time.monotonic()
            engine.reindex(embeddings=True)
            embed_s = round(time.monotonic() - t0, 2)

            hits = engine.search(question["question"], limit=SEARCH_LIMIT)
            entries = hit_entries(hits)
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    hit_ids = {e["id"] for e in entries}
    gold_entry_ids = {g["entry_id"] for g in gold}
    retrieved_all = bool(gold) and gold_entry_ids <= hit_ids
    retrieved_any = bool(gold_entry_ids & hit_ids)

    rows, packs = [], {}
    for name in args.strategies:
        for budget in args.budgets:
            packed = packing.STRATEGIES[name](entries, budget)
            row = {
                "question_id": question["question_id"],
                "stratum": stratum_of(question),
                "strategy": name,
                "budget": budget,
                "retrieved_all": retrieved_all,
                "retrieved_any": retrieved_any,
                **score_pack(packed, entries, gold),
            }
            rows.append(row)
            packs[f"{name}/{budget}"] = "\n\n".join(p["block"] for p in packed)

    meta = {
        "question_id": question["question_id"],
        "stratum": stratum_of(question),
        "n_entries": len(stored),
        "n_gold": len(gold),
        "n_gold_sessions": len({g["session_id"] for g in gold}),
        "gold_bytes": sum(g["bytes"] for g in gold),
        "n_hits": len(entries),
        "hit_block_bytes": [len(e["preamble"].encode()) + 2 + len(e["snippet"].encode())
                            for e in entries],
        "retrieved_all": retrieved_all,
        "build_seconds": build_s,
        "embed_seconds": embed_s,
    }
    pack_record = {
        "question_id": question["question_id"],
        "question": question["question"],
        "question_date": question["question_date"],
        "question_type": question["question_type"],
        "stratum": stratum_of(question),
        "answer": question["answer"],
        "packs": packs,
    }
    return rows, meta, pack_record


def stratum_of(question: dict) -> str:
    return "abstention" if question["question_id"].endswith("_abs") else question["question_type"]


def summarize(rows: list[dict], strategies: list[str], budgets: list[int]) -> dict:
    scored = [r for r in rows if r["gold_total"]]
    summary = {}
    for name in strategies:
        for budget in budgets:
            for stratum in ("all", *STRATA):
                subset = [
                    r for r in scored
                    if r["strategy"] == name and r["budget"] == budget
                    and (stratum == "all" or r["stratum"] == stratum)
                ]
                if not subset:
                    continue
                full = sum(1 for r in subset if r["full_coverage"])
                any_cov = sum(1 for r in subset if r["any_coverage"])
                summary[f"{name}/{budget}/{stratum}"] = {
                    "n": len(subset),
                    "full": full,
                    "partial": any_cov - full,
                    "none": len(subset) - any_cov,
                    "retrieval_ceiling": sum(1 for r in subset if r["retrieved_all"]),
                    "entries_mean": round(statistics.fmean(r["entries"] for r in subset), 2),
                    "entries_median": statistics.median(r["entries"] for r in subset),
                    "bytes_mean": round(statistics.fmean(r["bytes"] for r in subset)),
                    "gold_fraction_mean": round(
                        statistics.fmean(r["gold_fraction"] for r in subset), 3
                    ),
                }
    return summary


def markdown(summary: dict, strategies: list[str], budgets: list[int]) -> str:
    out = ["## Coverage, all scored questions", "",
           "| strategy | budget | n | full | partial | none | retrieval ceiling | "
           "entries mean | entries median | bytes mean |",
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name in strategies:
        for budget in budgets:
            s = summary.get(f"{name}/{budget}/all")
            if not s:
                continue
            out.append(
                f"| {name} | {budget} | {s['n']} | {s['full']}/{s['n']} | {s['partial']} | "
                f"{s['none']} | {s['retrieval_ceiling']}/{s['n']} | {s['entries_mean']} | "
                f"{s['entries_median']} | {s['bytes_mean']} |"
            )
    for stratum in STRATA:
        out += ["", f"## Full coverage — {stratum}", "",
                "| strategy | " + " | ".join(str(b) for b in budgets) + " |",
                "| --- |" + " --- |" * len(budgets)]
        for name in strategies:
            cells = []
            for budget in budgets:
                s = summary.get(f"{name}/{budget}/{stratum}")
                cells.append(f"{s['full']}/{s['n']}" if s else "-")
            out.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--subset", default=str(DEFAULT_SUBSET))
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET))
    ap.add_argument("--limit", type=int, default=0, help="process only the first N questions")
    ap.add_argument("--budgets", default=",".join(str(b) for b in BUDGETS))
    ap.add_argument("--strategies", default=",".join(packing.STRATEGIES))
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--embed-model", default=os.environ.get("EMBED_MODEL", "nomic-embed-text"))
    ap.add_argument(
        "--out",
        default=os.path.expanduser("~/.config/r4t/lab/k-budget-packing/mechanical"),
        help="directory for the timestamped results.json, packs.json and summary.md",
    )
    args = ap.parse_args()
    args.budgets = [int(b) for b in args.budgets.split(",") if b.strip()]
    args.strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    for name in args.strategies:
        assert name in packing.STRATEGIES, f"unknown strategy: {name}"

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    by_id = {x["question_id"]: x for x in dataset}
    subset = json.loads(Path(args.subset).read_text(encoding="utf-8"))
    if args.limit:
        subset = subset[:args.limit]

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    slug = stamp.replace(":", "")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{slug}.json"
    packs_path = out_dir / f"{slug}-packs.json"

    rows: list[dict] = []
    metas: list[dict] = []
    packs: list[dict] = []
    for i, item in enumerate(subset):
        question = by_id[item["question_id"]]
        q_rows, meta, pack_record = run_question(question, args)
        rows.extend(q_rows)
        metas.append(meta)
        packs.append(pack_record)
        print(
            f"  [{i + 1}/{len(subset)}] {meta['question_id']} ({meta['stratum']}) "
            f"entries={meta['n_entries']} gold={meta['n_gold']} in "
            f"{meta['n_gold_sessions']} session(s) hits={meta['n_hits']} "
            f"retrieved_all={meta['retrieved_all']} "
            f"build={meta['build_seconds']}s embed={meta['embed_seconds']}s",
            file=sys.stderr,
        )
        # Flush after every question: a 50-store sweep is long enough that a
        # crash at question 40 must not cost the first 39.
        json_path.write_text(json.dumps({
            "run": stamp, "budgets": args.budgets, "strategies": args.strategies,
            "search_limit": SEARCH_LIMIT, "questions": metas, "rows": rows,
            "summary": summarize(rows, args.strategies, args.budgets),
        }, indent=2), encoding="utf-8")
        packs_path.write_text(json.dumps({"run": stamp, "questions": packs}), encoding="utf-8")

    summary = summarize(rows, args.strategies, args.budgets)
    table = markdown(summary, args.strategies, args.budgets)
    md_path = out_dir / f"{slug}.md"
    md_path.write_text(f"# k-budget-packing mechanical — {stamp}\n\n{table}\n", encoding="utf-8")

    print()
    print(table)
    print(f"\nresults: {json_path}")
    print(f"packs:   {packs_path}")
    print(f"summary: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
