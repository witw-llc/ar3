#!/usr/bin/env python3
"""Live sandbox harness wrapper.

Runs the configured LLM harness (see R4T_SANDBOX_INVOKE), then enforces
sandbox mechanical invariants: Dev must leave battleship.py in cwd; Tester
must mechanically verify it and tell the Lead (not Dev). Staged tells use
$TELL_OUTBOX_DIR like real agents.

Weaker models may ignore the prompt; post-turn protocol staging mirrors
fake-agent.py so the pipeline still completes when the harness exits.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

BATTLESHIP = '''\
import sys

SHIPS = {(0, 0), (2, 3), (4, 4)}


def main() -> int:
    hits = set()
    for line in sys.stdin:
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            guess = (int(parts[0]), int(parts[1]))
        except ValueError:
            continue
        if guess in SHIPS:
            hits.add(guess)
            print(f"HIT {guess[0]} {guess[1]}")
            if hits == SHIPS:
                print("YOU WIN — all ships sunk!")
                return 0
        else:
            print(f"MISS {guess[0]} {guess[1]}")
    print("GAME OVER — you lose.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

DEV_RETRY = (
    "Create battleship.py in the current directory now. "
    "Read GOAL.md. Use your file write tool. "
    "The file must be a runnable Python script. "
    "When done, run: tell tester \"battleship.py is ready\""
)

TEAM = {"lead", "dev", "tester", "owner"}


def role_name(prompt: str) -> str:
    match = re.search(r"You are (\w+),", prompt)
    if not match:
        raise SystemExit("live-agent: cannot parse agent name from prompt")
    return match.group(1).lower()


def incoming_block(prompt: str) -> str:
    return prompt.split("## Messages since your last turn", 1)[-1].split(
        "## How to work", 1
    )[0]


def sender_from(prompt: str) -> str:
    match = re.search(r"(?m)^From: (\S+)", incoming_block(prompt))
    return match.group(1) if match else ""


def is_external(sender: str) -> bool:
    return sender.strip().lower() not in TEAM


def harness_invoke() -> list[str]:
    raw = os.environ.get("R4T_SANDBOX_INVOKE", "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"live-agent: invalid R4T_SANDBOX_INVOKE JSON: {e}") from e
        if not isinstance(data, list) or not data or not all(isinstance(a, str) for a in data):
            raise SystemExit("live-agent: R4T_SANDBOX_INVOKE must be a JSON string array")
        if not any("{prompt}" in a for a in data):
            raise SystemExit("live-agent: R4T_SANDBOX_INVOKE has no {prompt} placeholder")
        return data
    return ["opencode", "run", "--auto", "--dir", ".", "{prompt}"]


def run_harness(prompt: str) -> int:
    argv = [a.replace("{prompt}", prompt) for a in harness_invoke()]
    print(f"live-agent: running harness ({argv[0]})", file=sys.stderr, flush=True)
    llm_timeout = int(os.environ.get("R4T_SANDBOX_LLM_TIMEOUT", "480"))
    proc = subprocess.Popen(argv, text=True, start_new_session=True)
    try:
        proc.wait(timeout=llm_timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        proc.wait()
        print(f"live-agent: harness timed out after {llm_timeout}s", file=sys.stderr, flush=True)
        return -9
    return proc.returncode or 0


def _kill_process_tree(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def protocol_only(name: str, sender: str, incoming: str) -> bool:
    """Roles/turns where mechanical staging is enough — skip the LLM."""
    if name == "tester":
        return True
    if (
        name == "lead"
        and re.search(r"VERIFIED:", incoming, re.I)
        and sender.strip().lower() == "tester"
    ):
        return True
    return False


def staged_tos() -> set[str]:
    outbox = Path(os.environ.get("TELL_OUTBOX_DIR", ""))
    if not outbox.is_dir():
        return set()
    tos: set[str] = set()
    for path in outbox.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            to = str(data.get("to", "")).strip().lower()
            if to:
                tos.add(to)
    return tos


def stage_tell(to: str, content: str) -> None:
    outbox = Path(os.environ["TELL_OUTBOX_DIR"])
    outbox.mkdir(parents=True, exist_ok=True)
    msg_id = f"{time.time_ns():026d}"
    payload = {"id": msg_id, "to": to, "content": content, "files": []}
    tmp = outbox / f".{msg_id}.tmp"
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, outbox / f"{msg_id}.json")
    print(f"live-agent: staged tell to {to}: {content[:80]}", file=sys.stderr, flush=True)


def sandbox_directive(name: str, sender: str, incoming: str) -> str:
    verified = re.search(r"VERIFIED:", incoming, re.I) and sender.strip().lower() == "tester"
    if name == "lead":
        if verified:
            return (
                "## DO THIS NOW (Lead — final answer)\n"
                "Tester verified the game. Run exactly one shell command, then stop:\n"
                '  tell human "Done: battleship.py is built and verified."\n'
                "Do not delegate."
            )
        if sender.strip().lower() == "dev":
            return (
                "## DO THIS NOW (Lead — send to Tester)\n"
                "Dev finished implementation. Run exactly one shell command, then stop:\n"
                '  tell tester "Run battleship.py and report VERIFIED or FAILED."\n'
                "Do not write code."
            )
        return (
            "## DO THIS NOW (Lead — delegate to Dev)\n"
            "Run exactly one shell command via your shell tool, then stop:\n"
            '  tell dev "Build battleship.py in this directory exactly as GOAL.md says."\n'
            "Do NOT write code yourself. Do NOT reply to the human yet."
        )
    if name == "dev":
        return (
            "## DO THIS NOW (Dev — implement)\n"
            "1. Read GOAL.md and WORKSPACE.md.\n"
            "2. Create battleship.py in the current directory using your write/edit tool.\n"
            '3. Run: tell tester "battleship.py is ready for verification."\n'
            "4. Stop. Do not message the Lead or the human."
        )
    if name == "tester":
        return (
            "## DO THIS NOW (Tester — verify)\n"
            "1. Run: python3 battleship.py with stdin guesses (pipe row col lines 0-4).\n"
            "2. Run: tell lead \"VERIFIED: ...\" if exit 0 and you see WIN, else FAILED.\n"
            "3. Stop. Do not message Dev."
        )
    return ""


def augment_prompt(prompt: str, name: str) -> str:
    sender = sender_from(prompt)
    incoming = incoming_block(prompt)
    directive = sandbox_directive(name, sender, incoming)
    if not directive:
        return prompt
    return directive + "\n\n---\n\n" + prompt


def ensure_battleship() -> None:
    if Path("battleship.py").is_file():
        return
    code = run_harness(DEV_RETRY)
    if code != 0:
        print(f"live-agent: dev retry harness exited {code}", file=sys.stderr)
    if Path("battleship.py").is_file():
        return
    Path("battleship.py").write_text(BATTLESHIP, encoding="utf-8")
    print("live-agent: seeded battleship.py after dev left no file", file=sys.stderr)


def verify_battleship() -> tuple[bool, str]:
    if not Path("battleship.py").is_file():
        return False, "battleship.py missing"
    guesses = "\n".join(f"{r} {c}" for r in range(5) for c in range(5)) + "\n"
    try:
        result = subprocess.run(
            [sys.executable, "battleship.py"],
            input=guesses,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "battleship.py timed out"
    ok = result.returncode == 0 and "WIN" in result.stdout.upper()
    detail = f"exit {result.returncode}; tail={(result.stdout + result.stderr)[-120:]}"
    return ok, detail


def enforce_protocol(name: str, sender: str, incoming: str) -> None:
    """Stage tells the model should have sent — keeps the pipeline moving."""
    sender_name = sender.strip().lower()
    staged = staged_tos()

    if name == "lead":
        if re.search(r"VERIFIED:", incoming, re.I) and sender_name == "tester" and "human" not in staged:
            stage_tell(
                "human",
                "Done: battleship.py is built and verified. Dev implemented the "
                "5x5 game with 3 ships and Tester confirmed a winning "
                "playthrough exits 0. Play it with: python3 battleship.py",
            )
        elif sender_name == "dev" and "tester" not in staged:
            stage_tell(
                "tester",
                "Run battleship.py (pipe all row col guesses 0-4 on stdin) and "
                "report VERIFIED or FAILED to the Lead.",
            )
        elif is_external(sender) and "dev" not in staged:
            stage_tell(
                "dev",
                "Build battleship.py in this directory exactly as GOAL.md specifies: "
                "5x5 grid, 3 ships, stdin guesses `row col`, exit 0 on win.",
            )
    elif name == "dev":
        ensure_battleship()
        if Path("battleship.py").is_file() and "tester" not in staged:
            stage_tell(
                "tester",
                "battleship.py is ready — run it and verify a winning playthrough "
                "exits 0, then report VERIFIED or FAILED to the Lead.",
            )
    elif name == "tester":
        ok, detail = verify_battleship()
        if "lead" not in staged:
            if ok:
                stage_tell(
                    "lead",
                    "VERIFIED: battleship.py runs, reports HIT/MISS, and exits 0 "
                    "on a winning playthrough.",
                )
            else:
                stage_tell("lead", f"FAILED: battleship.py — {detail}")


def main() -> int:
    prompt = sys.argv[1]
    name = role_name(prompt)
    sender = sender_from(prompt)
    incoming = incoming_block(prompt)

    if protocol_only(name, sender, incoming):
        print(
            f"live-agent: {name} — mechanical protocol only (skipping LLM)",
            file=sys.stderr,
            flush=True,
        )
        enforce_protocol(name, sender, incoming)
        return 0

    prompt = augment_prompt(prompt, name)
    code = run_harness(prompt)
    if code != 0:
        print(
            f"live-agent: harness exited {code} — applying protocol fallback",
            file=sys.stderr,
            flush=True,
        )

    enforce_protocol(name, sender, incoming)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
