"""K0 driver — hermetic A/B trials of the member-level Knowledge inject.

Each trial builds a throwaway R4T_HOME and repo, seeds the member's private
k7e store with one codeword fact (unique per trial) plus five distractor
entries, wakes the member once with the probe question through real dispatch,
and scores the raw turn output for the codeword. Arm A (`Knowledge: off`) and
arm B (`Knowledge: on`) differ by that one roster line; the codeword never
appears in the prompt, the history, or the message — the inject block is the
only path from store to model.

`lab run` refuses org-class experiments, so this driver owns its own ledger
(JSONL, one row per trial) and report. `--fake` swaps the model for
fixtures/fake-mem.py, which answers by grepping its own prompt: B passing and
A failing under --fake validates the chassis with zero LLM calls.

Usage:
  python3 k0-run.py [--trials 6] [--model qwen3:1.7b] [--preset claude]
                    [--task memory|control] [--budget on] [--fake]
                    [--ledger PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # apps/r4t — dispatch, state, knowledge

import dispatch
import knowledge
import state
from dispatch import DispatchContext, drain, handle_message, run_harness
from rig import build_preset_invoke

NODE = "k0"
MEMBER = "mem"
WORDS = [
    "AMBER", "BASALT", "CINDER", "DELPHI", "EMBER", "FATHOM", "GARNET",
    "HALCYON", "INDIGO", "JUNIPER", "KESTREL", "LANTERN", "MERIDIAN",
    "NIMBUS", "OBSIDIAN", "PLOVER", "QUARRY", "ROOKERY", "SABLE", "TALON",
]
PROBE = (
    "What is the launch codeword for Project Foxglove? State it in one "
    "sentence. If you have no record of one, say so."
)
# The no-memory control (K1): a task the store cannot help with, asked in the
# same environment (store seeded, inject firing in arm B). Measures whether an
# irrelevant Knowledge block costs accuracy or latency — the distraction side.
CONTROL_PROBE = "What is 17 + 25? Reply with just the number."
CONTROL_ANSWER = "42"


def roster_text(arm: str, budget: str) -> str:
    lines = [
        "# K0 roster", "",
        "### Mem",
        "- **Rig:** probe",
        "- **Leader:** yes",
    ]
    if arm == "B":
        lines.append(f"- **Knowledge:** {budget}")
    lines += [
        "", "### Seat",
        "- **Human:** yes",
        "- **Address:** seat",
        "",
    ]
    return "\n".join(lines)


def rig_config(invoke: list[str]) -> dict:
    return {
        "_comment": "K0 probe rig — single member, gates open",
        "throttle": {"max_concurrent": 0, "min_seconds_between_turn_starts": 0},
        "cell_budget_max": 200,
        "cell_budget_earn_per_hour": 100,
        "probe": {
            "invoke": invoke,
            "timeout_seconds": 300,
            "concurrency": 1,
            "budget_max": 100,
            "budget_earn_per_hour": 100,
        },
        "pins": {"_comment": "x"},
    }


def seed_store(codeword: str) -> None:
    home = knowledge.store_home(NODE, MEMBER)
    entries = json.loads(
        (HERE / "fixtures" / "distractors.json").read_text(encoding="utf-8")
    )
    entries.insert(
        len(entries) // 2,
        {
            "title": "Project Foxglove launch codeword",
            "content": f"The launch codeword for Project Foxglove is {codeword}.",
        },
    )
    for e in entries:
        res = knowledge._run_k7e(
            home, "store", e["title"], "--content", e["content"]
        )
        if res.returncode != 0:
            raise RuntimeError(f"k7e seed failed: {res.stderr}")


def run_trial(arm: str, trial: int, rng: random.Random, args) -> dict:
    codeword = "-".join(rng.sample(WORDS, 2))
    with tempfile.TemporaryDirectory(prefix=f"k0-{arm}{trial}-") as tmp:
        tmp_path = Path(tmp)
        os.environ["R4T_HOME"] = str(tmp_path / "r4t-home")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "ROSTER.md").write_text(
            roster_text(arm, args.budget), encoding="utf-8"
        )
        if args.fake:
            invoke = [sys.executable, str(HERE / "fixtures" / "fake-mem.py"), "{prompt}"]
        elif args.preset:
            invoke = build_preset_invoke(args.preset, model=args.model or None)
        else:
            invoke = ["ollama", "run", args.model or "qwen3:1.7b", "{prompt}"]
        config_path = tmp_path / "rigs.json"
        config_path.write_text(
            json.dumps(rig_config(invoke), indent=2), encoding="utf-8"
        )
        seed_store(codeword)

        replies: list[tuple[str, str]] = []
        ctx = DispatchContext(
            root=repo,
            node=NODE,
            roster_path=repo / "ROSTER.md",
            config_path=config_path,
            tell_fn=lambda agent, body: replies.append((agent, body)),
        )
        probe = CONTROL_PROBE if args.task == "control" else PROBE
        target = CONTROL_ANSWER if args.task == "control" else codeword
        started = time.monotonic()
        handle_message(ctx, "seat", f"{NODE}:{MEMBER}", probe, drain_after=False)
        turns = drain(ctx, run_fn=run_harness)
        wall = time.monotonic() - started

        captures = state.list_turn_captures(NODE, MEMBER)
        output = captures[-1].read_text(encoding="utf-8") if captures else ""
        # A streaming CLI re-renders lines (spinners, wraps), so the raw
        # capture can hold the codeword only in split fragments;
        # clean_transcript keeps the final render, and the staged replies
        # already passed through it.
        answered = "\n".join(
            [dispatch.clean_transcript(output.partition("## Output")[2])]
            + [body for _a, body in replies]
        )
        log = "".join(
            f.read_text(encoding="utf-8")
            for f in sorted((state.roster_dir(NODE) / "log").glob("*.md"))
        )
        prompt_line = next(
            (l for l in log.splitlines() if "r4t: PROMPT mem" in l), ""
        )
        knowledge_injected = "knowledge" in prompt_line
        codeword_in_prompt = codeword in "".join(
            c.read_text(encoding="utf-8").partition("## Output")[0]
            for c in captures
        )
        return {
            "arm": arm,
            "trial": trial,
            "task": args.task,
            "budget": args.budget,
            "preset": args.preset or "",
            "codeword": codeword,
            "success": target.lower() in answered.lower(),
            "turns": turns,
            "wall_seconds": round(wall, 1),
            "knowledge_injected": knowledge_injected,
            "codeword_in_prompt": codeword_in_prompt,
            "codeword_stated": codeword.lower() in answered.lower(),
            "prompt_line": prompt_line.strip(),
            "replies": [body[:200] for _a, body in replies],
        }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trials", type=int, default=6, help="trials per arm")
    ap.add_argument("--model", default="", help="model: ollama name, or --preset's --model")
    ap.add_argument("--preset", default="", help="r4t rig preset (claude, codex, agy, cursor, ...)")
    ap.add_argument("--task", default="memory", choices=["memory", "control"],
                    help="memory = codeword recall; control = no-memory task (distraction cost)")
    ap.add_argument("--budget", default="on", help="arm B Knowledge: value (on, 4k, ...)")
    ap.add_argument("--fake", action="store_true", help="chassis check, no LLM")
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument(
        "--ledger",
        default=os.path.expanduser(
            "~/.config/r4t/lab/k0-knowledge-inject/ledger.jsonl"
        ),
    )
    args = ap.parse_args()

    prior_home = os.environ.get("R4T_HOME")
    ledger = Path(args.ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mode = "fake" if args.fake else (args.preset or args.model or "qwen3:1.7b")
    if args.model and args.preset:
        mode = f"{args.preset}:{args.model}"

    rows = []
    try:
        for trial in range(args.trials):
            for arm in ("A", "B"):
                row = run_trial(arm, trial, rng, args)
                row["run"] = stamp
                row["mode"] = mode
                rows.append(row)
                with ledger.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
                flag = "HIT " if row["success"] else "miss"
                print(
                    f"  {arm}{trial} {flag} {row['codeword']:<18} "
                    f"inject={str(row['knowledge_injected']).lower():<5} "
                    f"{row['wall_seconds']}s",
                    file=sys.stderr,
                )
    finally:
        if prior_home is None:
            os.environ.pop("R4T_HOME", None)
        else:
            os.environ["R4T_HOME"] = prior_home

    for arm in ("A", "B"):
        arm_rows = [r for r in rows if r["arm"] == arm]
        hits = sum(r["success"] for r in arm_rows)
        print(f"arm {arm} ({mode}): {hits}/{len(arm_rows)}")
    # A leak is the codeword reaching an arm-A member, never task success:
    # on --task control success is correct arithmetic, which both arms should
    # get right.
    leaks = [
        r
        for r in rows
        if r["arm"] == "A" and (r["codeword_in_prompt"] or r["codeword_stated"])
    ]
    if leaks:
        print(f"WARNING: arm A leak — {len(leaks)} trial(s) saw the codeword")
    print(f"ledger: {ledger}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
