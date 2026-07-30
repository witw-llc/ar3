#!/usr/bin/env python3
"""Mechanical scoring for TELL-ARMS. No judge, no model.

Classification per message, in order:

  NO-SEND    no envelope reached the human's seat (turn ended, nothing staged)
  EXACT      the payload appears verbatim in an envelope body
             (whitespace runs normalized; every hazard char byte-exact)
  GARBLED    the payload does not appear verbatim, but the payload's
             hazard-free ANCHORS all do — the line was relayed and the hazard
             characters are what changed
  OFF-SCRIPT an envelope exists but the anchors are absent — the member
             answered with something else entirely

ANCHORS are derived from the payload with no human choice. Each hazard
character is deleted together with the RISK REGION a shell would destroy:

  $   the `$` plus the following [A-Za-z0-9_]* (the parameter name)
  `   from the backtick to the next backtick, inclusive (command substitution)
  \\   the `\\` plus the single character after it (the escape)

What remains splits into fragments; a fragment with 4+ alphabetic characters is
an anchor. The test uses the OUTERMOST anchors (first and last) — the text
farthest from any hazard, so a shell bug cannot touch it. When a payload yields
no anchor at all, any non-exact envelope counts GARBLED: the conservative call,
because GARBLED is a failure in the pre-registered rule and OFF-SCRIPT is not.

The protocol's two metrics are garbled-body rate and no-send rate. OFF-SCRIPT
is reported as its own column rather than folded into either.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch import messages  # noqa: E402

SCRATCH = Path(__file__).resolve().parent
RISK = re.compile(r"\$[A-Za-z0-9_]*|`[^`]*`?|\\.?")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def anchors(payload: str) -> list[str]:
    frags = [norm(f) for f in RISK.split(payload)]
    anc = [f for f in frags if len(re.findall(r"[A-Za-z]", f)) >= 4]
    if not anc:
        return []
    return [anc[0]] if len(anc) == 1 else [anc[0], anc[-1]]


def classify(payload: str, contents: list[str]) -> tuple[str, str]:
    """Returns (verdict, the envelope content it was decided on)."""
    if not contents:
        return "NO-SEND", ""
    p = norm(payload)
    anc = anchors(payload)
    best = ("OFF-SCRIPT", contents[0])
    rank = {"OFF-SCRIPT": 0, "GARBLED": 1, "EXACT": 2}
    for c in contents:
        n = norm(c)
        if p in n:
            v = "EXACT"
        elif not anc or all(a in n for a in anc):
            v = "GARBLED"
        else:
            v = "OFF-SCRIPT"
        if rank[v] > rank[best[0]]:
            best = (v, c)
    return best


def score_arm(arm: str) -> dict:
    path = SCRATCH / f"arm{arm}" / "records.jsonl"
    if not path.is_file():
        return {"arm": arm, "n": 0, "rows": []}
    records = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_id = {m["id"]: m for m in messages()}
    rows = []
    for r in records:
        payload = by_id[r["id"]]["payload"]
        contents = [e.get("content", "") for e in r["envelopes"]]
        verdict, decided = classify(payload, contents)
        rows.append({
            "index": r["index"],
            "id": r["id"],
            "shape": r["shape"],
            "payload": payload,
            "verdict": verdict,
            "content": decided,
            "n_envelopes": len(contents),
            "mcp_calls": len(r.get("mcp_calls") or []),
            "duration_s": r["duration_s"],
            "hang": r.get("hang", False),
            "dead_letters": len(r.get("dead_letters") or []),
        })
    n = len(rows)
    return {
        "arm": arm,
        "n": n,
        "exact": sum(1 for r in rows if r["verdict"] == "EXACT"),
        "garbled": sum(1 for r in rows if r["verdict"] == "GARBLED"),
        "no_send": sum(1 for r in rows if r["verdict"] == "NO-SEND"),
        "off_script": sum(1 for r in rows if r["verdict"] == "OFF-SCRIPT"),
        "tool_calls": sum(1 for r in rows if r["mcp_calls"] > 0),
        "hangs": sum(1 for r in rows if r["hang"]),
        "total_s": round(sum(r["duration_s"] for r in rows), 1),
        "rows": rows,
    }


if __name__ == "__main__":
    out = {}
    for arm in ("A", "B", "C"):
        s = score_arm(arm)
        out[arm] = s
        if not s["n"]:
            continue
        print(
            f"arm {arm}: N={s['n']} exact={s['exact']} garbled={s['garbled']} "
            f"no-send={s['no_send']} off-script={s['off_script']} "
            f"tool-calls={s['tool_calls']} hangs={s['hangs']} "
            f"wall={s['total_s']}s"
        )
    (SCRATCH / "scores.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
