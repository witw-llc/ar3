"""A fake `codex app-server` that answers from the recorded frames.

The frames beside this file were recorded from codex-cli 0.163.0 (issue #344)
and redacted. This server replays their shapes: the initialize and
thread/start answers verbatim, and per turn the recorded turn/started,
item/completed (agentMessage) and turn/completed frames with this turn's ids
and text put in. It keeps the behaviors the resident bridge depends on:

- a turn/start sent while a turn runs joins that turn (same turn id, ONE
  turn/completed), as the real server does;
- an unknown threadId gets the recorded JSON-RPC error;
- stdin EOF ends the server with exit 0;
- `--help` anywhere in argv prints help and exits 0 (the check's parse probe).

Settings come from the environment:
  FAKE_CODEX_RECORD         JSON file: argv, every stdin line, steered count
  FAKE_CODEX_POLICY         approvalPolicy to report (default: the recorded one)
  FAKE_CODEX_SANDBOX        sandbox.type to report (default: from argv)
  FAKE_CODEX_VERSION        version in userAgent (default 0.163.0)
  FAKE_CODEX_TURN_SECONDS   how long a turn runs (default 0.05)
  FAKE_CODEX_APPROVAL       1: ask one approval request in the first turn
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SANDBOX_TYPES = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
}


def frames(name: str) -> list[dict]:
    out = []
    for line in (HERE / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if isinstance(record.get("line"), dict):
            out.append(record)
    return out


def first(name: str, pred) -> dict:
    return copy.deepcopy(next(r["line"] for r in frames(name) if pred(r)))


PING = "pingpong"
INIT = first(PING, lambda r: r["dir"] == "out" and r["line"].get("id") == 1)
THREAD = first(PING, lambda r: r["dir"] == "out" and r["line"].get("id") == 2)
TURN_STARTED = first(PING, lambda r: r["line"].get("method") == "turn/started")
AGENT_ITEM = first(PING, lambda r: r["line"].get("method") == "item/completed"
                   and r["line"]["params"]["item"]["type"] == "agentMessage")
TURN_DONE = first(PING, lambda r: r["line"].get("method") == "turn/completed")
TURN_ERROR = first("turn-error", lambda r: r["dir"] == "out" and "error" in r["line"])
APPROVAL = first("approval", lambda r: r["line"].get("method")
                 == "item/commandExecution/requestApproval")

out_lock = threading.Lock()
rec_lock = threading.Lock()
record_path = os.environ.get("FAKE_CODEX_RECORD")
record = {"argv": sys.argv[1:], "pid": os.getpid(), "in": [], "steered": 0,
          "replies": [], "turns": []}


def save() -> None:
    if record_path:
        with rec_lock:
            Path(record_path).write_text(json.dumps(record), encoding="utf-8")


def send(msg: dict) -> None:
    with out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def sandbox_from_argv() -> str:
    for token in sys.argv[1:]:
        if token.startswith("sandbox_mode="):
            return token.split("=", 1)[1].strip('"')
    return "read-only"


if "--help" in sys.argv[1:]:
    print("Usage: codex app-server [OPTIONS]")
    sys.exit(0)

thread_id = str(uuid.uuid4())
state = {"active": None, "texts": [], "approval_asked": False}
state_lock = threading.Lock()
answered = threading.Event()
turn_seconds = float(os.environ.get("FAKE_CODEX_TURN_SECONDS", "0.05"))
save()


def run_turn(turn_id: str) -> None:
    started = copy.deepcopy(TURN_STARTED)
    started["params"]["threadId"] = thread_id
    started["params"]["turn"]["id"] = turn_id
    send(started)
    if os.environ.get("FAKE_CODEX_APPROVAL") == "1" and not state["approval_asked"]:
        state["approval_asked"] = True
        ask = copy.deepcopy(APPROVAL)
        ask["params"]["threadId"] = thread_id
        ask["params"]["turnId"] = turn_id
        send(ask)
        answered.wait(timeout=10)
    time.sleep(turn_seconds)
    with state_lock:
        texts = list(state["texts"])
        state["active"] = None
        state["texts"] = []
    if any("wrap up and exit" in t for t in texts):
        Path("STATUS.md").write_text("# Current State\nWrapped up cleanly.\n", encoding="utf-8")
    reply = f"ack {len(record['turns'])}"
    item = copy.deepcopy(AGENT_ITEM)
    item["params"]["threadId"] = thread_id
    item["params"]["turnId"] = turn_id
    item["params"]["item"]["text"] = reply
    send(item)
    done = copy.deepcopy(TURN_DONE)
    done["params"]["threadId"] = thread_id
    done["params"]["turn"]["id"] = turn_id
    done["params"]["turn"]["items"] = [dict(item["params"]["item"])]
    send(done)


for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    msg = json.loads(line)
    with rec_lock:
        record["in"].append(msg)
    save()
    method = msg.get("method")
    if method is None:
        record["replies"].append(msg)
        save()
        answered.set()
        continue
    if method == "initialize":
        answer = copy.deepcopy(INIT)
        version = os.environ.get("FAKE_CODEX_VERSION", "0.163.0")
        name = msg["params"]["clientInfo"]["name"]
        answer["result"]["userAgent"] = f"{name}/{version} (Fake 1.0; x86_64)"
        answer["id"] = msg["id"]
        send(answer)
    elif method == "thread/start":
        answer = copy.deepcopy(THREAD)
        answer["id"] = msg["id"]
        result = answer["result"]
        result["thread"]["id"] = thread_id
        result["approvalPolicy"] = os.environ.get("FAKE_CODEX_POLICY", "never")
        sandbox = SANDBOX_TYPES.get(sandbox_from_argv(), "readOnly")
        result["sandbox"] = {"type": os.environ.get("FAKE_CODEX_SANDBOX", sandbox)}
        send(answer)
    elif method == "turn/start":
        params = msg.get("params") or {}
        if params.get("threadId") != thread_id:
            error = copy.deepcopy(TURN_ERROR)
            error["id"] = msg["id"]
            send(error)
            continue
        text = params["input"][0]["text"]
        with rec_lock:
            record["turns"].append(text)
        with state_lock:
            joined = state["active"] is not None
            if joined:
                record["steered"] += 1
            else:
                state["active"] = str(uuid.uuid4())
            state["texts"].append(text)
            turn_id = state["active"]
        save()
        send({"id": msg["id"], "result": {"turn": {"id": turn_id, "status": "inProgress",
                                                    "items": [], "error": None}}})
        if not joined:
            threading.Thread(target=run_turn, args=(turn_id,), daemon=True).start()
sys.exit(0)
