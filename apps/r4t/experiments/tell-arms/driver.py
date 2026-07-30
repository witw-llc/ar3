#!/usr/bin/env python3
"""TELL-ARMS driver — deliver the 20-message batch to one arm, one at a time.

Usage: driver.py <A|B|C> [--start N] [--limit N]

Writes one JSON line per message to arm<X>/records.jsonl. Respects the
protocol's stop conditions: 3 consecutive hangs aborts the arm; ollama
unreachable aborts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch import messages  # noqa: E402

SCRATCH = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[4]
R4T_PY = REPO / "apps" / "r4t" / "r4t.py"
A8S_PY = REPO / "apps" / "a8s" / "a8s.py"
MCP_PY = SCRATCH / "mcp_a8s_tell.py"
NODE = "tellarms"
TURN_TIMEOUT = 420
HARNESS_TIMEOUT = 300


def ollama_up() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def seat_files(r4t_home: Path) -> set[Path]:
    d = r4t_home / "rosters" / NODE / "seat" / "owner" / "inbox"
    if not d.is_dir():
        return set()
    return {p for p in d.iterdir() if p.name.endswith(".json")}


def dead_files(r4t_home: Path) -> set[Path]:
    d = r4t_home / "rosters" / NODE / "dead-letter"
    if not d.is_dir():
        return set()
    return {p for p in d.iterdir() if p.is_file()}


def turn_files(r4t_home: Path) -> set[Path]:
    d = r4t_home / "rosters" / NODE / "agents" / "wren" / "turns"
    if not d.is_dir():
        return set()
    return {p for p in d.iterdir() if p.is_file()}


def main() -> int:
    arm = sys.argv[1].upper()
    start = 0
    limit = 999
    argv = sys.argv[2:]
    for i, a in enumerate(argv):
        if a == "--start":
            start = int(argv[i + 1])
        if a == "--limit":
            limit = int(argv[i + 1])

    root = SCRATCH / f"arm{arm}"
    repo = root / "repo"
    r4t_home = root / "r4t-home"
    mcp_log = root / "mcp-calls.jsonl"
    records = root / "records.jsonl"

    env = dict(os.environ)
    env["R4T_HOME"] = str(r4t_home)
    env["A8S_HOME"] = str(root / "a8s-home")
    env.pop("TELL_OUTBOX_DIR", None)
    env["A8S_PY"] = str(A8S_PY)
    env["A8S_MCP_LOG"] = str(mcp_log)
    if arm == "C":
        # `ollama launch opencode` sets OPENCODE_CONFIG_CONTENT itself (provider
        # + model), clobbering anything r4t puts there. OPENCODE_CONFIG (a file
        # path) survives the launcher and merges with it.
        cfg = root / "opencode.json"
        cfg.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "a8s": {
                    "type": "local",
                    "command": [sys.executable, str(MCP_PY)],
                    "enabled": True,
                }
            },
        }, indent=2), encoding="utf-8")
        env["OPENCODE_CONFIG"] = str(cfg)

    cmd_base = [
        sys.executable, str(R4T_PY), "seat", "send",
        "--node", NODE,
        "--root", str(repo),
        "--rig-config", str(root / "rigs.json"),
    ]
    definition = root / "definition.json"
    if definition.is_file():
        cmd_base += ["--definition", str(definition)]
    cmd_base += ["--to", "wren"]

    consecutive_hangs = 0
    all_msgs = list(messages())
    for idx, m in enumerate(all_msgs):
        if idx < start or idx >= start + limit:
            continue
        if not ollama_up():
            print(f"ABORT RUN: ollama unreachable before {m['id']}", file=sys.stderr)
            return 3

        before_seat = seat_files(r4t_home)
        before_dead = dead_files(r4t_home)
        before_turns = turn_files(r4t_home)
        before_mcp = mcp_log.stat().st_size if mcp_log.exists() else 0

        t0 = time.time()
        hang = False
        rc = None
        stderr = ""
        try:
            proc = subprocess.run(
                cmd_base + [m["instruction"]],
                env=env, capture_output=True, text=True, timeout=TURN_TIMEOUT,
            )
            rc = proc.returncode
            stderr = proc.stderr[-2000:]
        except subprocess.TimeoutExpired:
            hang = True
        dur = round(time.time() - t0, 1)
        if dur >= HARNESS_TIMEOUT - 5:
            hang = True

        envelopes = []
        for p in sorted(seat_files(r4t_home) - before_seat):
            try:
                envelopes.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        deads = []
        for p in sorted(dead_files(r4t_home) - before_dead):
            try:
                deads.append(p.read_text(encoding="utf-8")[:2000])
            except Exception:
                pass
        new_turns = sorted(str(p) for p in (turn_files(r4t_home) - before_turns))
        mcp_calls = []
        if mcp_log.exists() and mcp_log.stat().st_size > before_mcp:
            with mcp_log.open(encoding="utf-8") as fh:
                fh.seek(before_mcp)
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            mcp_calls.append(json.loads(line))
                        except Exception:
                            pass

        rec = {
            "arm": arm,
            "index": idx,
            "id": m["id"],
            "shape": m["shape"],
            "payload": m["payload"],
            "duration_s": dur,
            "returncode": rc,
            "hang": hang,
            "stderr_tail": stderr,
            "envelopes": [
                {"to": e.get("to"), "content": e.get("content", "")} for e in envelopes
            ],
            "dead_letters": deads,
            "turn_captures": new_turns,
            "mcp_calls": mcp_calls,
        }
        with records.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

        ok = envelopes and m["payload"] in (envelopes[0].get("content") or "")
        print(
            f"[{arm}] {idx:02d} {m['id']} {dur}s rc={rc} "
            f"env={len(envelopes)} mcp={len(mcp_calls)} "
            f"{'EXACT' if ok else 'MISS'}{' HANG' if hang else ''}",
            flush=True,
        )

        if hang:
            consecutive_hangs += 1
            if consecutive_hangs >= 3:
                print(f"ABORT ARM {arm}: 3 consecutive hangs", file=sys.stderr)
                return 4
        else:
            consecutive_hangs = 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
