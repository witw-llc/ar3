"""K6 subset rerun — LongMemEval_S retrieval-only, embeddings OFF vs ON.

Builds one fresh k7e store per question's haystack (chunked one entry per
user/assistant turn-pair), searches the bare question text, and measures
SESSION-LEVEL hit@1/3/5: whether any of the top-K retrieved entries came from
one of the question's gold evidence sessions (`answer_session_ids`). No
reader LLM — retrieval only.

The 50-question subset is frozen in fixtures/k6/subset.json (stratified by
question type with a fixed seed; see fixtures/k6/subset.json and
build_subset() below for how it was drawn). Abstention questions
(`question_id` ending `_abs`) have no genuine evidence session in the
haystack — their `answer_session_ids` entry is a plausible-looking distractor
with no `has_answer: true` turn anywhere in it — so hit@k is not computed for
them; they are reported separately as a count.

Dataset: LongMemEval_S, cleaned Sept-2025 revision (MIT, xiaowu0162). Not
committed to the repo; downloaded once to --dataset (default
~/.cache/k-next-longmemeval/longmemeval_s_cleaned.json) and reused after.

Usage:
  python3 k6e-run.py [--limit N] [--dataset PATH] [--out DIR]
                     [--ollama-url URL] [--embed-model NAME]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
K7E_DIR = HERE.parents[2] / "k7e"
sys.path.insert(0, str(K7E_DIR))

import engine  # noqa: E402

DATASET_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/"
    "resolve/main/longmemeval_s_cleaned.json"
)
DEFAULT_DATASET = Path.home() / ".cache" / "k-next-longmemeval" / "longmemeval_s_cleaned.json"
OFF_OLLAMA_URL = "http://localhost:99999"
SEARCH_LIMIT = 10
KS = (1, 3, 5)
REAL_TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "temporal-reasoning",
    "knowledge-update",
    "multi-session",
)


def ensure_dataset(path: Path) -> Path:
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading LongMemEval_S (cleaned) to {path} ...", file=sys.stderr)
    print(f"  source: {DATASET_URL}", file=sys.stderr)
    try:
        urllib.request.urlretrieve(DATASET_URL, str(path))
    except Exception as e:
        raise SystemExit(
            f"Automatic download failed ({e}).\n"
            f"Manual step: fetch {DATASET_URL} and save it to {path}, "
            "then rerun."
        )
    return path


def build_subset(dataset: list[dict], seed: int = 42) -> list[dict]:
    """Reproduces fixtures/k6/subset.json from the raw dataset. Not called by
    main() — the committed subset.json is the frozen source of truth — but
    kept here so the stratification is auditable and re-derivable."""
    import random

    alloc = {
        "knowledge-update": 7, "multi-session": 12,
        "single-session-assistant": 6, "single-session-preference": 3,
        "single-session-user": 6, "temporal-reasoning": 13, "abstention": 3,
    }
    strata: dict[str, list[tuple[str, str]]] = {k: [] for k in alloc}
    for x in dataset:
        qid, qtype = x["question_id"], x["question_type"]
        strata["abstention" if qid.endswith("_abs") else qtype].append((qid, qtype))

    rng = random.Random(seed)
    chosen = []
    for stratum in alloc:
        for qid, qtype in rng.sample(sorted(strata[stratum]), alloc[stratum]):
            chosen.append({"question_id": qid, "question_type": qtype, "stratum": stratum})
    return sorted(chosen, key=lambda r: r["question_id"])


def chunk_turn_pairs(session: list[dict]) -> list[list[dict]]:
    return [session[i:i + 2] for i in range(0, len(session), 2)]


def seed_store(question: dict) -> dict[str, set[str]]:
    """Store one entry per turn-pair. Returns entry_id -> {session_ids}
    (a set because content-hash dedup can fold identical short turns from
    different sessions onto the same stored entry)."""
    entry_sessions: dict[str, set[str]] = {}
    for sid, session in zip(question["haystack_session_ids"], question["haystack_sessions"]):
        for pair_idx, pair in enumerate(chunk_turn_pairs(session)):
            content = "\n".join(f"{t['role'].capitalize()}: {t['content']}" for t in pair)
            title = pair[0]["content"].replace("\n", " ").strip()[:80]
            entry_id = engine.store_entry(title or f"{sid} turn {pair_idx}", content, tags=[sid])
            entry_sessions.setdefault(entry_id, set()).add(sid)
    return entry_sessions


def score_question(question: dict, entry_sessions: dict[str, set[str]], arm: str) -> dict:
    is_abstention = question["question_id"].endswith("_abs")
    gold_sessions = set() if is_abstention else set(question["answer_session_ids"])

    embed_latency_ms = None
    if arm == "on":
        t0 = time.monotonic()
        engine.embed_text(question["question"])
        embed_latency_ms = round((time.monotonic() - t0) * 1000, 1)

    results = engine.search(question["question"], limit=SEARCH_LIMIT)
    hits = {}
    for k in KS:
        if is_abstention:
            hits[f"hit_{k}"] = None
        else:
            hits[f"hit_{k}"] = any(
                entry_sessions.get(r["id"], set()) & gold_sessions for r in results[:k]
            )
    return {
        "question_id": question["question_id"],
        "question_type": question["question_type"],
        "stratum": "abstention" if is_abstention else question["question_type"],
        "arm": arm,
        "embed_latency_ms": embed_latency_ms,
        **hits,
    }


def run_question(question: dict, args) -> tuple[list[dict], dict]:
    prior_home = os.environ.get("K7E_HOME")
    prior_ollama = os.environ.get("OLLAMA_URL")
    prior_embed_model = os.environ.get("EMBED_MODEL")
    timings = {}
    try:
        with tempfile.TemporaryDirectory(prefix="k6e-") as tmp:
            tmp_path = Path(tmp)
            os.environ["K7E_HOME"] = str(tmp_path)
            engine.reset(tmp_path)
            engine.init()

            t0 = time.monotonic()
            entry_sessions = seed_store(question)
            timings["build_wall_seconds"] = round(time.monotonic() - t0, 2)
            timings["n_entries"] = len(entry_sessions)

            engine.reindex(embeddings=False)
            os.environ["OLLAMA_URL"] = OFF_OLLAMA_URL
            off_row = score_question(question, entry_sessions, "off")

            os.environ["OLLAMA_URL"] = args.ollama_url
            os.environ["EMBED_MODEL"] = args.embed_model
            t0 = time.monotonic()
            engine.reindex(embeddings=True)
            timings["embed_reindex_wall_seconds"] = round(time.monotonic() - t0, 2)
            on_row = score_question(question, entry_sessions, "on")
    finally:
        for key, prior in (
            ("K7E_HOME", prior_home),
            ("OLLAMA_URL", prior_ollama),
            ("EMBED_MODEL", prior_embed_model),
        ):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
    return [off_row, on_row], timings


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for stratum in list(REAL_TYPES) + ["abstention"]:
        for arm in ("off", "on"):
            subset = [r for r in rows if r["stratum"] == stratum and r["arm"] == arm]
            n = len(subset)
            if stratum == "abstention":
                summary[f"{stratum}/{arm}"] = {"n": n, "note": "no evidence session; hit@k N/A"}
                continue
            entry = {"n": n}
            for k in KS:
                entry[f"hit_{k}"] = sum(1 for r in subset if r[f"hit_{k}"])
            summary[f"{stratum}/{arm}"] = entry
    return summary


def markdown_table(summary: dict) -> str:
    lines = [
        "| question type | arm | n | hit@1 | hit@3 | hit@5 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for stratum in list(REAL_TYPES) + ["abstention"]:
        for arm in ("off", "on"):
            s = summary[f"{stratum}/{arm}"]
            n = s["n"]
            if stratum == "abstention":
                lines.append(f"| {stratum} | {arm} | {n} | - | - | - |")
            else:
                lines.append(
                    f"| {stratum} | {arm} | {n} | {s['hit_1']}/{n} | "
                    f"{s['hit_3']}/{n} | {s['hit_5']}/{n} |"
                )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--subset", default=str(HERE / "fixtures" / "k6" / "subset.json"))
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET))
    ap.add_argument("--limit", type=int, default=0, help="process only the first N questions (smoke runs)")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--embed-model", default=os.environ.get("EMBED_MODEL", "nomic-embed-text"))
    ap.add_argument(
        "--out",
        default=os.path.expanduser("~/.config/r4t/lab/k-next-embeddings/k6"),
        help="directory to write the timestamped results.json + summary.md",
    )
    args = ap.parse_args()

    dataset_path = ensure_dataset(Path(args.dataset))
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    by_id = {x["question_id"]: x for x in dataset}

    subset = json.loads(Path(args.subset).read_text(encoding="utf-8"))
    if args.limit:
        subset = subset[:args.limit]

    rows = []
    per_question_timings = []
    for i, item in enumerate(subset):
        question = by_id[item["question_id"]]
        q_rows, timings = run_question(question, args)
        rows.extend(q_rows)
        timings["question_id"] = item["question_id"]
        per_question_timings.append(timings)
        print(
            f"  [{i + 1}/{len(subset)}] {item['question_id']} ({item['stratum']}) "
            f"entries={timings['n_entries']} build={timings['build_wall_seconds']}s "
            f"embed={timings['embed_reindex_wall_seconds']}s",
            file=sys.stderr,
        )

    summary = summarize(rows)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    slug = stamp.replace(":", "")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "run": stamp,
        "n_questions": len(subset),
        "dataset": str(dataset_path),
        "rows": rows,
        "timings": per_question_timings,
        "summary": summary,
    }
    json_path = out_dir / f"{slug}.json"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    table = markdown_table(summary)
    md_path = out_dir / f"{slug}.md"
    md_path.write_text(f"# K6 subset rerun — {stamp}\n\n{table}\n", encoding="utf-8")

    print(table)
    print(f"\nresults: {json_path}")
    print(f"summary: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
