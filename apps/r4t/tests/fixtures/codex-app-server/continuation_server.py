"""Offline synthetic scheduling scenarios; only delegation shapes are measured.

The four parent/delegation frames come from delegated-completion.jsonl.
Native restart, merging and reconciliation results below are adversarial
simulations, not claims about observed vendor behavior.
"""
from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

scenario, record_path = sys.argv[1:]
record = Path(record_path)
frames = [json.loads(line)["line"] for line in
          Path(__file__).with_name("delegated-completion.jsonl").read_text().splitlines()]


def note(kind, frame=None):
    with record.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"kind": kind, "frame": frame}) + "\n")


def send(frame):
    note("out", frame)
    print(json.dumps(frame), flush=True)


def event(method, turn):
    send({"method": method, "params": {"threadId": "parent-thread", "turn": {
        "id": turn, "status": "completed" if method == "turn/completed" else "inProgress",
        "items": [],
    }}})


def answer(request, turn):
    send({"id": request["id"], "result": {"turn": {
        "id": turn, "status": "inProgress", "items": [],
    }}})


count = 0
for line in sys.stdin:
    request = json.loads(line)
    note("in", request)
    method = request.get("method")
    if method == "initialize":
        send({"id": request["id"], "result": {"userAgent": "codex/0.163.0"}})
    elif method == "thread/start":
        send({"id": request["id"], "result": {
            "thread": {"id": "parent-thread"}, "approvalPolicy": "never",
            "sandbox": {"type": "workspaceWrite"},
        }})
    elif method == "turn/start":
        count += 1
        text = request["params"]["input"][0]["text"]
        if count == 1:
            answer(request, "parent-turn")
            send(frames[0])
            send(frames[1])
            if scenario == "coalesced":
                second = copy.deepcopy(frames[1])
                second["params"]["item"].update(id="second-spawn", agentThreadId="second-child")
                send(second)
            if scenario == "native":
                event("turn/started", "native-turn")
            send(frames[2])
            receipt = copy.deepcopy(frames[3])
            if scenario == "unknown":
                receipt["params"]["item"]["agentThreadId"] = "unknown-child"
            elif scenario == "generic":
                receipt["params"]["item"] = {"type": "agentMessage", "id": "generic-item",
                                              "text": "child completed"}
            send(receipt)
            send(receipt)
            if scenario == "coalesced":
                second = copy.deepcopy(frames[3])
                second["params"]["item"].update(id="second-receipt", agentThreadId="second-child")
                send(second)
            send({"method": "test/barrier", "params": {}})
            if scenario == "native":
                deadline = time.monotonic() + 5
                while not record.with_suffix(".release").exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("bridge did not inspect native busy state")
                    time.sleep(0.005)
                event("turn/completed", "native-turn")
        elif text.startswith("[Resident session quiet") or text == "real inbound":
            answer(request, f"ordinary-{count}")
            event("turn/completed", f"ordinary-{count}")
        elif scenario == "failed":
            send({"id": request["id"], "error": {"code": -1, "message": "synthetic rejection"}})
            send(frames[3])
        else:
            if scenario in {"merged", "terminal-first"}:
                event("turn/started", "returned-native-turn")
                turn = "returned-native-turn"
            else:
                turn = "reconciliation-turn"
            if scenario == "terminal-first":
                event("turn/completed", turn)
                answer(request, turn)
            else:
                answer(request, turn)
                event("turn/completed", turn)
            send(frames[3])
note("eof")
