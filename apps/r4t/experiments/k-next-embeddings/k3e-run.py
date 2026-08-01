"""K3 rerun — does the semantic track close the lexical gap?

Builds one k7e store from fixtures/k3/store.json (31 ops notes: incidents,
configs, decisions, roles), then for each of the 30 fixtures/k3/queries.json
queries (10 gold entries x easy/hard/hard-neutral) measures the RANK of the
known-gold entry in k7e's search results. No LLM, no judge — the rank is
read straight off engine.search().

The store is identical for both arms; only the index differs:
  - arm OFF: FTS5/BM25 only. reindex(embeddings=False) empties the embeddings
    table, and OLLAMA_URL is pointed at a dead port so the semantic track's
    query-time embed call fails immediately and contributes nothing.
  - arm ON:  reindex(embeddings=True) batch-embeds every entry (timed), then
    every query is searched against the real RRF-fused BM25 + semantic index.

Usage:
  python3 k3e-run.py [--store PATH] [--queries PATH] [--out DIR]
                     [--ollama-url URL] [--embed-model NAME]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
K7E_DIR = HERE.parents[2] / "k7e"
sys.path.insert(0, str(K7E_DIR))

import engine  # noqa: E402

OFF_OLLAMA_URL = "http://localhost:99999"
CONDITIONS = ("easy", "hard", "hard-neutral")
ARMS = ("off", "on")
SEARCH_LIMIT = 10


def seed_store(store_path: Path) -> dict[str, str]:
    entries = json.loads(store_path.read_text(encoding="utf-8"))
    key_to_id = {}
    for e in entries:
        key_to_id[e["key"]] = engine.store_entry(
            e["title"], e["content"], tags=e.get("tags", [])
        )
    return key_to_id


def run_arm(queries: list[dict], key_to_id: dict[str, str], arm: str) -> list[dict]:
    rows = []
    for q in queries:
        embed_latency_ms = None
        if arm == "on":
            t0 = time.monotonic()
            engine.embed_text(q["query"])
            embed_latency_ms = round((time.monotonic() - t0) * 1000, 1)
        results = engine.search(q["query"], limit=SEARCH_LIMIT)
        gold_id = key_to_id[q["gold_key"]]
        rank = next((i + 1 for i, r in enumerate(results) if r["id"] == gold_id), None)
        rows.append({
            "round": q["round"],
            "condition": q["condition"],
            "arm": arm,
            "query": q["query"],
            "gold_key": q["gold_key"],
            "gold_id": gold_id,
            "rank": rank,
            "hit_1": rank == 1,
            "hit_5": rank is not None and rank <= 5,
            "embed_latency_ms": embed_latency_ms,
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for cond in CONDITIONS:
        for arm in ARMS:
            subset = [r for r in rows if r["condition"] == cond and r["arm"] == arm]
            n = len(subset)
            latencies = [r["embed_latency_ms"] for r in subset if r["embed_latency_ms"] is not None]
            summary[f"{cond}/{arm}"] = {
                "n": n,
                "hit_1": sum(r["hit_1"] for r in subset),
                "hit_5": sum(r["hit_5"] for r in subset),
                "misses": sum(1 for r in subset if r["rank"] is None),
                "avg_embed_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            }
    return summary


def markdown_table(summary: dict, reindex_wall: float) -> str:
    lines = [
        f"Embedding reindex wall time (31 entries): {reindex_wall}s",
        "",
        "| condition | arm | n | hit@1 | hit@5 | misses | avg embed latency (ms) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for cond in CONDITIONS:
        for arm in ARMS:
            s = summary[f"{cond}/{arm}"]
            lat = s["avg_embed_latency_ms"]
            lat_str = f"{lat}" if lat is not None else "-"
            lines.append(
                f"| {cond} | {arm} | {s['n']} | {s['hit_1']}/{s['n']} | "
                f"{s['hit_5']}/{s['n']} | {s['misses']} | {lat_str} |"
            )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", default=str(HERE / "fixtures" / "k3" / "store.json"))
    ap.add_argument("--queries", default=str(HERE / "fixtures" / "k3" / "queries.json"))
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--embed-model", default=os.environ.get("EMBED_MODEL", "nomic-embed-text"))
    ap.add_argument(
        "--out",
        default=os.path.expanduser("~/.config/r4t/lab/k-next-embeddings/k3"),
        help="directory to write the timestamped results.json + summary.md",
    )
    args = ap.parse_args()

    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))

    prior_home = os.environ.get("K7E_HOME")
    prior_ollama = os.environ.get("OLLAMA_URL")
    prior_embed_model = os.environ.get("EMBED_MODEL")
    reindex_wall = 0.0

    try:
        with tempfile.TemporaryDirectory(prefix="k3e-") as tmp:
            tmp_path = Path(tmp)
            os.environ["K7E_HOME"] = str(tmp_path)
            engine.reset(tmp_path)
            engine.init()

            key_to_id = seed_store(Path(args.store))

            engine.reindex(embeddings=False)
            os.environ["OLLAMA_URL"] = OFF_OLLAMA_URL
            off_rows = run_arm(queries, key_to_id, "off")

            os.environ["OLLAMA_URL"] = args.ollama_url
            os.environ["EMBED_MODEL"] = args.embed_model
            t0 = time.monotonic()
            engine.reindex(embeddings=True)
            reindex_wall = round(time.monotonic() - t0, 2)
            on_rows = run_arm(queries, key_to_id, "on")
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

    rows = off_rows + on_rows
    summary = summarize(rows)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    slug = stamp.replace(":", "")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "run": stamp,
        "n_store_entries": len(key_to_id),
        "n_queries": len(queries),
        "embed_reindex_wall_seconds": reindex_wall,
        "rows": rows,
        "summary": summary,
    }
    json_path = out_dir / f"{slug}.json"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    table = markdown_table(summary, reindex_wall)
    md_path = out_dir / f"{slug}.md"
    md_path.write_text(f"# K3 rerun — {stamp}\n\n{table}\n", encoding="utf-8")

    print(table)
    print(f"\nresults: {json_path}")
    print(f"summary: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
