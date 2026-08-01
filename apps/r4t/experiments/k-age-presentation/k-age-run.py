#!/usr/bin/env python3
"""K-age-presentation driver -- which age-presentation on a `## Knowledge`
provenance stamp actually keys a reader into staleness?

No roster, no r4t process: this driver fakes a minimal member prompt by hand
(mission line + CURRENT DATE line + `## Knowledge` section + `## Messages`
section), reusing `KNOWLEDGE_HEADER`/`KNOWLEDGE_FRAMING` from
apps/r4t/knowledge.py so the prompt shape never drifts from the shipped
inject, and the same qwen3:4b / agy reader plumbing as
experiments/k4e-poisoning/k4e-run.py.

Two task shapes (fixtures/t1_store.json, fixtures/t2_store.json), five age
presentations on every entry stamp (the ONE variable under test), two
readers. The presentation logic (qualitative label thresholds, the status
line, the fixed CURRENT DATE anchor) lives in fixtures/presentation.json as
data, not code -- ages are computed against that fixed anchor, never
datetime.now(), so results are reproducible forever.

Conditions:
  none        (KG-00003)
  absolute    (KG-00003, 2026-06-25)
  relative    (KG-00003, 36d old)
  qualitative (KG-00003, 36d old -- very old)   [label omitted under 7d]
  status      (KG-00003, 36d old) + a "Status: possibly superseded..." line
              appended to entries older than presentation.json's
              status_threshold_days (30)

Tasks:
  t1  update-conflict -- store holds an old and a new entry on the same
      subject (deploy schedule). Correct = states the new fact; stale-
      following = states the old fact; hedged = states/implies both or uses
      uncertainty language. Mechanical phrase detection only.
  t2  stale-alone -- store holds only an old fact (on-call owner, 90d) plus
      unrelated filler. Correct behavior is hedging; asserted_unqualified =
      confident unhedged restatement of the old fact.

Usage:
  python3 k-age-run.py [--trials 8] [--task t1,t2]
                        [--condition none,absolute,relative,qualitative,status]
                        [--reader qwen,agy] [--limit N] [--qwen-model qwen3:4b]
                        [--out DIR] [--seed 4242]

Smoke:
  python3 k-age-run.py --trials 1 --reader qwen
  python3 k-age-run.py --trials 1 --reader agy --limit 1
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # apps/r4t -- knowledge.py

from knowledge import KNOWLEDGE_FRAMING, KNOWLEDGE_HEADER  # noqa: E402

TASKS = ("t1", "t2")
CONDITIONS = ("none", "absolute", "relative", "qualitative", "status")
READERS = ("qwen", "agy")

OLLAMA_URL = "http://localhost:11434/api/generate"
AGY_WRAPPER = (
    'timeout 240 agy --dangerously-skip-permissions '
    '--model gemini-3.6-flash-low --print "$(cat)"'
)

T1_Y_PHRASES = ("nightly", "2am", "2 am", "02:00", "release branch")
T1_X_PHRASES = ("weekly", "friday", "main branch")
T1_HEDGE_PHRASES = (
    "may have changed", "previously", "used to", "now runs", "as of",
    "not sure", "uncertain", "conflicting",
)
T2_HEDGE_PHRASES = (
    "may have changed", "may have been", "may no longer", "might", "as of",
    "was", "verify", "confirm", "stale", "superseded", "outdated", "90",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def load_fixtures() -> tuple[dict, dict, dict]:
    fdir = HERE / "fixtures"
    t1 = json.loads((fdir / "t1_store.json").read_text(encoding="utf-8"))
    t2 = json.loads((fdir / "t2_store.json").read_text(encoding="utf-8"))
    presentation = json.loads((fdir / "presentation.json").read_text(encoding="utf-8"))
    return t1, t2, presentation


def age_days(entry_date: str, current_date: date) -> int:
    return (current_date - date.fromisoformat(entry_date)).days


def qualitative_label(age: int, cfg: dict) -> str | None:
    q = cfg["qualitative"]
    if age < q["fresh_under_days"]:
        return q["fresh_label"]
    if age <= q["old_max_days"]:
        return q["old_label"]
    return q["very_old_label"]


def render_stamp(entry_id: str, entry_date: str, age: int, condition: str, cfg: dict) -> str:
    if condition == "none":
        return f"({entry_id})"
    if condition == "absolute":
        return f"({entry_id}, {entry_date})"
    if condition in ("relative", "status"):
        return f"({entry_id}, {age}d old)"
    if condition == "qualitative":
        label = qualitative_label(age, cfg)
        return f"({entry_id}, {age}d old — {label})" if label else f"({entry_id}, {age}d old)"
    raise ValueError(f"unknown condition: {condition}")


def render_block(entry: dict, condition: str, current_date: date, cfg: dict) -> str:
    age = age_days(entry["date"], current_date)
    stamp = render_stamp(entry["id"], entry["date"], age, condition, cfg)
    block = f"### {entry['title']} {stamp}\n\n{entry['content']}"
    if condition == "status" and age > cfg["status_threshold_days"]:
        block += f"\n{cfg['status_line']}"
    return block


def build_prompt(store: dict, cfg: dict, condition: str, run_tag: str) -> str:
    current_date = date.fromisoformat(cfg["current_date"])

    knowledge_parts = [KNOWLEDGE_HEADER, KNOWLEDGE_FRAMING, ""]
    for e in store["entries"]:
        knowledge_parts.append(render_block(e, condition, current_date, cfg))
        knowledge_parts.append("")

    question = f"{store['task_question']} (ref: {run_tag})"
    parts = [
        store["mission_line"],
        "",
        f"CURRENT DATE: {cfg['current_date']}",
        "",
        *knowledge_parts,
        "## Messages since your last turn",
        "",
        f"From: Lead (thread {run_tag})",
        "",
        question,
        "",
    ]
    return "\n".join(parts)


def call_qwen(prompt: str, model: str, timeout: float) -> tuple[str, float, str | None]:
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        return data.get("response", ""), round(time.monotonic() - t0, 1), None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "", round(time.monotonic() - t0, 1), str(e)


def call_agy(prompt: str, timeout: float) -> tuple[str, float, str | None]:
    t0 = time.monotonic()
    try:
        res = subprocess.run(
            ["sh", "-c", AGY_WRAPPER],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        wall = round(time.monotonic() - t0, 1)
        if res.returncode != 0:
            return strip_ansi(res.stdout), wall, f"exit {res.returncode}: {res.stderr.strip()[:300]}"
        return strip_ansi(res.stdout), wall, None
    except subprocess.TimeoutExpired:
        return "", round(time.monotonic() - t0, 1), f"timed out after {timeout}s"


def score_t1(response: str) -> dict:
    text = response.lower()
    correct = any(p in text for p in T1_Y_PHRASES)
    stale = any(p in text for p in T1_X_PHRASES)
    hedged = (correct and stale) or any(p in text for p in T1_HEDGE_PHRASES)
    return {"correct": correct, "stale_following": stale, "hedged": hedged}


def score_t2(response: str) -> dict:
    text = response.lower()
    hedged = any(p in text for p in T2_HEDGE_PHRASES)
    mentions_target = "priya" in text
    asserted_unqualified = mentions_target and not hedged
    return {"asserted_unqualified": asserted_unqualified, "hedged": hedged}


def run_cell(
    task: str,
    condition: str,
    reader: str,
    trial: int,
    stores: dict,
    cfg: dict,
    args,
    rng: random.Random,
) -> dict:
    store = stores[task]
    run_tag = f"kage-{task}-{condition}-{reader}-{trial}-{rng.randrange(10**6):06d}"
    prompt = build_prompt(store, cfg, condition, run_tag)

    if reader == "qwen":
        response, wall, error = call_qwen(prompt, args.qwen_model, args.qwen_timeout)
    else:
        response, wall, error = call_agy(prompt, args.agy_timeout)

    row = {
        "task": task,
        "condition": condition,
        "reader": reader,
        "trial": trial,
        "run_tag": run_tag,
        "wall_seconds": wall,
        "prompt_bytes": len(prompt.encode("utf-8")),
        "error": error,
        "response": response,
    }
    scorer = score_t1 if task == "t1" else score_t2
    row.update(scorer(response))
    return row


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for task in TASKS:
        for condition in CONDITIONS:
            for reader in READERS:
                subset = [
                    r for r in rows
                    if r["task"] == task and r["condition"] == condition and r["reader"] == reader
                ]
                if not subset:
                    continue
                n = len(subset)
                errors = sum(1 for r in subset if r["error"])
                if task == "t1":
                    row = {
                        "n": n,
                        "errors": errors,
                        "correct": sum(1 for r in subset if r["correct"]),
                        "stale_following": sum(1 for r in subset if r["stale_following"]),
                        "hedged": sum(1 for r in subset if r["hedged"]),
                    }
                else:
                    row = {
                        "n": n,
                        "errors": errors,
                        "asserted_unqualified": sum(1 for r in subset if r["asserted_unqualified"]),
                        "hedged": sum(1 for r in subset if r["hedged"]),
                    }
                summary[f"{task}/{condition}/{reader}"] = row
    return summary


def markdown_table(summary: dict) -> str:
    lines = ["## T1 update-conflict", ""]
    lines.append("| condition | reader | n | correct | stale_following | hedged | errors |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for condition in CONDITIONS:
        for reader in READERS:
            key = f"t1/{condition}/{reader}"
            if key not in summary:
                continue
            s = summary[key]
            lines.append(
                f"| {condition} | {reader} | {s['n']} | {s['correct']}/{s['n']} | "
                f"{s['stale_following']}/{s['n']} | {s['hedged']}/{s['n']} | {s['errors']} |"
            )
    lines += ["", "## T2 stale-alone", ""]
    lines.append("| condition | reader | n | hedged | asserted_unqualified | errors |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for condition in CONDITIONS:
        for reader in READERS:
            key = f"t2/{condition}/{reader}"
            if key not in summary:
                continue
            s = summary[key]
            lines.append(
                f"| {condition} | {reader} | {s['n']} | {s['hedged']}/{s['n']} | "
                f"{s['asserted_unqualified']}/{s['n']} | {s['errors']} |"
            )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trials", type=int, default=8, help="trials per (task, condition, reader) cell")
    ap.add_argument("--task", default="t1,t2", help="comma list from t1,t2")
    ap.add_argument(
        "--condition",
        default="none,absolute,relative,qualitative,status",
        help="comma list from none,absolute,relative,qualitative,status",
    )
    ap.add_argument("--reader", default="qwen,agy", help="comma list from qwen,agy")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many total calls (0 = no cap)")
    ap.add_argument("--qwen-model", default="qwen3:4b")
    ap.add_argument("--qwen-timeout", type=float, default=120.0)
    ap.add_argument("--agy-timeout", type=float, default=260.0)
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument(
        "--out",
        default=os.path.expanduser("~/.config/r4t/lab/k-age-presentation/main"),
        help="directory to write the timestamped results.json + summary.md",
    )
    args = ap.parse_args()

    tasks = [t.strip() for t in args.task.split(",") if t.strip()]
    conditions = [c.strip() for c in args.condition.split(",") if c.strip()]
    readers = [r.strip() for r in args.reader.split(",") if r.strip()]
    for t in tasks:
        assert t in TASKS, f"unknown task: {t}"
    for c in conditions:
        assert c in CONDITIONS, f"unknown condition: {c}"
    for r in readers:
        assert r in READERS, f"unknown reader: {r}"

    t1_store, t2_store, cfg = load_fixtures()
    stores = {"t1": t1_store, "t2": t2_store}
    rng = random.Random(args.seed)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    slug = stamp.replace(":", "")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{slug}.json"

    rows: list[dict] = []
    total = 0
    for task in tasks:
        for condition in conditions:
            for reader in readers:
                for trial in range(args.trials):
                    if args.limit and total >= args.limit:
                        break
                    row = run_cell(task, condition, reader, trial, stores, cfg, args, rng)
                    row["run"] = stamp
                    rows.append(row)
                    total += 1
                    if task == "t1":
                        flag = "CORRECT" if row["correct"] else ("STALE" if row["stale_following"] else "?")
                    else:
                        flag = "HEDGED" if row["hedged"] else ("UNQUAL" if row["asserted_unqualified"] else "?")
                    err = f" ERROR: {row['error']}" if row["error"] else ""
                    print(
                        f"  {task}/{condition}/{reader}#{trial} {flag} "
                        f"{row['wall_seconds']}s{err}",
                        file=sys.stderr,
                    )
                    # Flush progress after every call -- agy calls can each take
                    # minutes and the full matrix runs unattended; a crash mid-run
                    # must not lose completed rows.
                    json_path.write_text(
                        json.dumps({"run": stamp, "rows": rows, "summary": summarize(rows)}, indent=2),
                        encoding="utf-8",
                    )

    summary = summarize(rows)
    table = markdown_table(summary)
    md_path = out_dir / f"{slug}.md"
    md_path.write_text(f"# K-age-presentation -- {stamp}\n\n{table}\n", encoding="utf-8")

    print()
    print(table)
    print(f"\nresults: {json_path}")
    print(f"summary: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
