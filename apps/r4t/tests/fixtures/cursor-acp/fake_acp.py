"""A fake `agent acp` for the resident bridge.

It speaks the handshake proven against agent 2026.10.22-27dcf5f: initialize,
authenticate, session/new, an optional session/set_config_option, then one
session/prompt at a time. Stdin EOF ends the process.

Settings:
  FAKE_CURSOR_RECORD        JSON file of argv, stdin messages, and turn texts
  FAKE_CURSOR_TURN_SECONDS  how long a prompt runs before it answers
  FAKE_CURSOR_PERMISSION    1: the first prompt asks session/request_permission
  FAKE_CURSOR_VERSION       text printed for --version
  FAKE_CURSOR_PROTOCOL      protocolVersion returned by initialize (default 1)
  FAKE_CURSOR_IGNORE_EOF    1: stay up after stdin closes, like agent acp
  FAKE_CURSOR_IGNORE_SIGTERM 1: record SIGTERM and return; only SIGKILL ends
                            the process. The handler does no file I/O. A daemon
                            started at load writes the record.
  FAKE_CURSOR_SIGTERM_CODE  exit code on SIGTERM (default 0; real agent acp is 143)
  FAKE_CURSOR_MALFORMED     1 or 2: that prompt emits session/update params
                            that are not an object, then stays up
  FAKE_CURSOR_CHILD         1: spawn a child that ignores SIGTERM
  FAKE_CURSOR_CHILD_PID     file that receives that child's pid
  FAKE_CURSOR_EXIT_AFTER_PROMPT 1: leave that child and exit after the first prompt
  FAKE_CURSOR_CLOSE_AFTER_TURN 1: answer the first prompt, close stdin, stay up
  FAKE_CURSOR_CRASH         1: answer the first prompt, then exit 1
  FAKE_CURSOR_EXIT_ON_PROMPT exit with this code when the prompt arrives, before any reply
  FAKE_CURSOR_FATAL_AFTER_EOF 1: after stdin closes, emit a session/update
                            whose params are not an object
  FAKE_CURSOR_CLOSE_STDIN   1: close stdin after session/new
  FAKE_CURSOR_PERMISSION_TIMEOUT seconds to wait for a permission reply
  FAKE_CURSOR_CANCEL_STOP   1: a session/cancel during the turn answers that
                            prompt with stopReason "cancelled"

The permission options are the three the proven agent offered on
2026-10-23 (see permission-no-force.jsonl). This process is still a fake.

Waits are 0.05 s steps, and the main loop returns that often too. A SIGTERM
that lands in the gap before one long sleep or one blocking read is handled
only when that call returns.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def _sleep_in_steps(seconds: float) -> None:
    """Sleep so a pending SIGTERM handler runs within 50 ms."""
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


if "--help" in sys.argv:
    sys.stdout.write("--model\n--force\n--trust\n")
    raise SystemExit(0)
if "--version" in sys.argv:
    sys.stdout.write(os.environ.get("FAKE_CURSOR_VERSION", "2026.10.22-27dcf5f") + "\n")
    raise SystemExit(0)

MODEL = "composer-2.5[fast=true]"
OPTIONS = [
    {
        "id": "mode",
        "type": "select",
        "currentValue": "agent",
        "options": [{"value": "agent", "name": "Agent"}],
    },
    {
        "id": "model",
        "type": "select",
        "currentValue": MODEL,
        "options": [
            {"value": MODEL, "name": "composer-2.5"},
            {"value": "grok-4.6[effort=high,fast=true]", "name": "grok-4.6"},
        ],
    },
]

record_path = os.environ.get("FAKE_CURSOR_RECORD")
record = {
    "argv": sys.argv[1:], "in": [], "turns": [], "replies": [], "events": [],
    "overlap": False,
}
# The SIGTERM handler runs on the main thread, which may already hold this.
lock = threading.RLock()
out_lock = threading.Lock()
incoming: queue.Queue = queue.Queue()
busy = False
permission_ok = threading.Event()
# Set by the ignore handler. The daemon below is the only writer for that path.
sigterm_noted = threading.Event()
OFFERED_OPTION = "allow-once"


def save() -> None:
    """Replace a finished temp file so a reader never sees a truncated record."""
    if not record_path:
        return
    path = Path(record_path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)


def _save_after_ignored_sigterm() -> None:
    """Write the record after an ignored SIGTERM. The handler does not."""
    while True:
        sigterm_noted.wait()
        with lock:
            sigterm_noted.clear()
            save()


threading.Thread(
    target=_save_after_ignored_sigterm, name="sigterm-save", daemon=True,
).start()


def _on_sigterm(_signum, _frame) -> None:
    if os.environ.get("FAKE_CURSOR_IGNORE_SIGTERM") == "1":
        with lock:
            record["sigterm_at"] = time.time()
        sigterm_noted.set()
        return
    with lock:
        record["sigterm_at"] = time.time()
        save()
    # Real `agent acp` dies 143 on SIGTERM. The default stays 0 so a test
    # that records the signal is not also a non-zero exit. os._exit ends the
    # process before an interrupted save can continue.
    try:
        code = int(os.environ.get("FAKE_CURSOR_SIGTERM_CODE", "0"))
    except ValueError:
        code = 0
    os._exit(code)


signal.signal(signal.SIGTERM, _on_sigterm)


def send(msg: dict) -> None:
    with out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def model_options(current: str) -> list[dict]:
    options = json.loads(json.dumps(OPTIONS))
    for opt in options:
        if opt.get("id") == "model":
            opt["currentValue"] = current
    return options


def note(msg: dict) -> None:
    with lock:
        record["events"].append({
            "t": time.time(),
            "method": msg.get("method"),
            "id": msg.get("id"),
        })
        save()


def read_stdin() -> None:
    """Keep reading while a prompt is in flight, so a permission reply and a
    second prompt are visible before this prompt's result is sent."""
    if os.environ.get("FAKE_CURSOR_CLOSE_AFTER_TURN") == "1":
        _read_until_main_closes()
        return
    global busy
    try:
        _read_stdin_lines()
    except OSError:
        pass


def _read_until_main_closes() -> None:
    """Poll, so the main thread can close stdin while this thread is not
    blocked in a read. A blocked read still accepts one write after close."""
    import select

    fd = sys.stdin.fileno()
    buf = ""
    while True:
        with lock:
            if record.get("close_now"):
                os.close(fd)
                record["stdin_closed"] = True
                save()
                return
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            incoming.put(None)
            return
        buf += chunk.decode("utf-8", "replace")
        while "\n" in buf:
            raw, buf = buf.split("\n", 1)
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            note(msg)
            with lock:
                record["in"].append(msg)
                save()
            incoming.put(msg)


def _read_stdin_lines() -> None:
    global busy
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        with lock:
            record["in"].append(msg)
            if msg.get("method") == "session/prompt" and busy:
                record["overlap"] = True
            if msg.get("method") is None and "result" in msg:
                record["replies"].append(msg)
                outcome = (msg.get("result") or {}).get("outcome") or {}
                option_id = outcome.get("optionId")
                offered = ("allow-once", "allow-always", "reject-once")
                # Only an id this fake offered unblocks the prompt.
                if isinstance(option_id, str) and option_id in offered:
                    record["optionId"] = option_id
                    permission_ok.set()
                elif isinstance(option_id, str) and option_id:
                    record["rejectedOption"] = option_id
            record["events"].append({
                "t": time.time(),
                "method": msg.get("method"),
                "id": msg.get("id"),
            })
            save()
        incoming.put(msg)
    if os.environ.get("FAKE_CURSOR_IGNORE_EOF") == "1":
        # The real agent stays up after stdin closes. SIGTERM ends it.
        with lock:
            record["eof_at"] = time.time()
            save()
        if os.environ.get("FAKE_CURSOR_FATAL_AFTER_EOF") == "1":
            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": ["not-an-object"],
            })
        while True:
            _sleep_in_steps(3600)
    incoming.put(None)


def _wait_for_cancel(seconds: float) -> bool:
    """True when session/cancel arrives before `seconds` elapse.

    Any other line goes back on the queue. The prompt handler is the thread
    that reads `incoming`, so a cancel sent while the turn sleeps would
    otherwise sit there until the turn finished.
    """
    stashed: list = []
    deadline = time.monotonic() + seconds
    found = False
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            nxt = incoming.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        if nxt is None:
            incoming.put(None)
            break
        if isinstance(nxt, dict) and nxt.get("method") == "session/cancel":
            found = True
            break
        stashed.append(nxt)
    for item in stashed:
        incoming.put(item)
    return found


def _close_after_session_new() -> None:
    """The handshake is read on this thread. Stdin is closed before the
    session/new result is written, then this process stays up. A second
    thread blocked in read would keep the pipe open."""
    while True:
        raw = sys.stdin.readline()
        if not raw:
            return
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        note(msg)
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": int(os.environ.get("FAKE_CURSOR_PROTOCOL", "1")),
                    "authMethods": [{"id": "cursor_login"}],
                    "agentCapabilities": {},
                },
            })
        elif method == "authenticate":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/new":
            # sys.stdin.close() does not close the fd. The bridge's next
            # write has to see a broken pipe.
            os.close(sys.stdin.fileno())
            send({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "sessionId": "sess-1",
                    "configOptions": model_options(MODEL),
                    "models": {"currentModelId": MODEL},
                    "modes": {},
                },
            })
            while True:
                _sleep_in_steps(3600)


def main() -> None:
    global busy
    if os.environ.get("FAKE_CURSOR_CHILD") == "1":
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import signal, time\n"
             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
             "deadline = time.monotonic() + 3600\n"
             "while time.monotonic() < deadline:\n"
             "    time.sleep(0.05)\n"],
        )
        pid_path = os.environ.get("FAKE_CURSOR_CHILD_PID")
        if pid_path:
            path = Path(pid_path)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(str(child.pid), encoding="utf-8")
            os.replace(tmp, path)
    if os.environ.get("FAKE_CURSOR_CLOSE_STDIN") == "1":
        _close_after_session_new()
        return
    session_id = "sess-1"
    current = MODEL
    threading.Thread(target=read_stdin, daemon=True).start()
    while True:
        try:
            msg = incoming.get(timeout=0.05)
        except queue.Empty:
            continue
        if msg is None:
            return
        if msg.get("method") is None:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": int(os.environ.get("FAKE_CURSOR_PROTOCOL", "1")),
                    "authMethods": [{"id": "cursor_login"}],
                    "agentCapabilities": {},
                },
            })
        elif method == "authenticate":
            if os.environ.get("FAKE_CURSOR_AUTH_ERROR") == "1":
                send({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32000, "message": "login refused"},
                })
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/new":
            send({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "sessionId": session_id,
                    "configOptions": model_options(current),
                    "models": {"currentModelId": current},
                    "modes": {},
                },
            })
        elif method == "session/set_config_option":
            value = (msg.get("params") or {}).get("value")
            if isinstance(value, str):
                current = value
            send({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"configOptions": model_options(current)},
            })
        elif method == "session/prompt":
            prompt_n = 1
            with lock:
                prompt_n = len(record["turns"]) + 1
            malformed_on = os.environ.get("FAKE_CURSOR_MALFORMED")
            if malformed_on in ("1", "2") and prompt_n == int(malformed_on):
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": ["not-an-object"],
                })
                while True:
                    _sleep_in_steps(3600)
            prompt = (msg.get("params") or {}).get("prompt") or []
            text = ""
            if prompt and isinstance(prompt[0], dict):
                text = prompt[0].get("text") or ""
            with lock:
                record["turns"].append(text)
                ask = (
                    os.environ.get("FAKE_CURSOR_PERMISSION") == "1"
                    and len(record["turns"]) == 1
                )
                busy = True
                save()
            exit_on_prompt = os.environ.get("FAKE_CURSOR_EXIT_ON_PROMPT")
            if exit_on_prompt is not None:
                os._exit(int(exit_on_prompt))
            if ask:
                permission_ok.clear()
                send({
                    "jsonrpc": "2.0",
                    "id": 90,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": session_id,
                        "options": [
                            {
                                "optionId": OFFERED_OPTION,
                                "kind": "allow_once",
                                "name": "Allow once",
                            },
                            {
                                "optionId": "allow-always",
                                "kind": "allow_always",
                                "name": "Allow always",
                            },
                            {
                                "optionId": "reject-once",
                                "kind": "reject_once",
                                "name": "Reject",
                            },
                        ],
                    },
                })
                wait_s = float(os.environ.get("FAKE_CURSOR_PERMISSION_TIMEOUT", "10"))
                deadline = time.monotonic() + wait_s
                while not permission_ok.is_set() and time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    permission_ok.wait(timeout=min(0.05, remaining))
                if not permission_ok.is_set():
                    send({
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {"code": -1, "message": "permission reply missing"},
                    })
                    with lock:
                        busy = False
                        save()
                    continue
            turn_seconds = float(os.environ.get("FAKE_CURSOR_TURN_SECONDS", "0.05"))
            if (
                os.environ.get("FAKE_CURSOR_CANCEL_STOP") == "1"
                and _wait_for_cancel(turn_seconds)
            ):
                with lock:
                    record["events"].append({
                        "t": time.time(),
                        "method": "stopReason",
                        "id": mid,
                        "stopReason": "cancelled",
                    })
                    save()
                send({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"stopReason": "cancelled"},
                })
                with lock:
                    busy = False
                    save()
                continue
            _sleep_in_steps(turn_seconds)
            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "ok"},
                    },
                },
            })
            note({"method": "stopReason", "id": mid})
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
            with lock:
                busy = False
                save()
            if os.environ.get("FAKE_CURSOR_CRASH") == "1":
                os._exit(1)
            if os.environ.get("FAKE_CURSOR_EXIT_AFTER_PROMPT") == "1":
                os._exit(0)
            if os.environ.get("FAKE_CURSOR_CLOSE_AFTER_TURN") == "1":
                with lock:
                    record["close_now"] = True
                    save()
                while True:
                    _sleep_in_steps(3600)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
