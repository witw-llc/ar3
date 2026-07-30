"""a8s MCP server — one stdio tool, `tell`, over newline-delimited JSON-RPC.

Harnesses namespace MCP tools `<server>_<tool>`, so registering server `a8s`
with tool `tell` presents to the model as `a8s_tell`. The prompt that wakes a
member names that string verbatim; a tool the prompt does not name goes unused
on small models.

The message body arrives as a JSON string argument, so no shell touches it:
`$1.25`, backticks and backslashes reach the envelope byte-exact. Delivery
reuses `a8s tell <recipient> -` with the body on stdin — the same safe path a
shell caller has — and the envelope lands in `TELL_OUTBOX_DIR`, which the
server reads from its own environment (r4t pins it per turn).

`sys.stdin.readline()` is load-bearing: `for line in sys.stdin` read-aheads and
deadlocks the handshake.

Set `A8S_MCP_LOG` to append one JSON line per tool call — the tool-call record
an experiment or a live proof reads back.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "a8s"
SERVER_VERSION = "1.0.0"
TOOL_NAME = "tell"
# What the model sees, and what a prompt teaching this server must name.
QUALIFIED_TOOL_NAME = f"{SERVER_NAME}_{TOOL_NAME}"

A8S_PY = Path(__file__).resolve().parent / "a8s.py"

TELL_TIMEOUT_SECONDS = 60

TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Send a message to a teammate or the human on the a8s network. "
        "The body is delivered byte-exact; no shell is involved."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": "Name of the teammate or human to send to.",
            },
            "body": {
                "type": "string",
                "description": "The message text. Sent verbatim.",
            },
        },
        "required": ["recipient", "body"],
    },
}


def log_call(event: dict) -> None:
    path = os.environ.get("A8S_MCP_LOG", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        pass


def send(recipient: str, body: str) -> tuple[bool, str]:
    """Deliver through `a8s tell <recipient> -`, body on stdin."""
    argv = [sys.executable, str(A8S_PY), "tell", recipient, "-"]
    try:
        proc = subprocess.run(
            argv,
            input=body,
            capture_output=True,
            text=True,
            timeout=TELL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"tell failed to run: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "tell failed").strip()
    return True, (proc.stdout or "sent").strip()


def _text_result(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments")
    if not isinstance(args, dict):
        args = {}
    if name != TOOL_NAME:
        return _text_result(f"unknown tool {name!r}; this server has {TOOL_NAME!r}", is_error=True)
    recipient = str(args.get("recipient") or "").strip()
    body = args.get("body")
    if not recipient:
        return _text_result("recipient is required", is_error=True)
    if not isinstance(body, str) or not body:
        return _text_result("body is required", is_error=True)
    log_call({"tool": name, "recipient": recipient, "body": body})
    ok, detail = send(recipient, body)
    return _text_result(detail, is_error=not ok)


def handle(msg: dict) -> dict | None:
    """One request in, one JSON-RPC response out (None for notifications)."""
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        result: dict = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = msg.get("params")
        result = _call_tool(params if isinstance(params, dict) else {})
    elif msg_id is None:
        # Notification (`notifications/initialized` and friends): nothing to say.
        return None
    else:
        # Anything else a client probes for (ping, resources/list) gets an empty
        # result rather than an error — the shape that connected against every
        # harness measured in #310.
        result = {}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(stdin=None, stdout=None) -> int:
    """Read requests until stdin closes. Unparseable lines are skipped so one
    bad frame does not end the session."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    while True:
        line = stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        response = handle(msg)
        if response is None:
            continue
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()
