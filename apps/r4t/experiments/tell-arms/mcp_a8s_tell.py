#!/usr/bin/env python3
"""Minimal stdlib stdio MCP server exposing one tool: a8s_tell.

Built for the TELL-ARMS experiment (arm C) from the #290 research notes.
Newline-delimited JSON-RPC over stdin/stdout. Uses sys.stdin.readline() —
`for line in sys.stdin` read-aheads and deadlocks the handshake.

The message body arrives as a JSON string argument: no shell, ever. Delivery
reuses `a8s tell` ($A8S_PY) invoked as an argv list with the body on stdin, so
the envelope lands in $TELL_OUTBOX_DIR exactly as a shell `tell` would.

Every tools/call is appended to $A8S_MCP_LOG as one JSON line — that file is
the tool-call rate evidence.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

A8S_PY = os.environ.get("A8S_PY", "")
LOG = os.environ.get("A8S_MCP_LOG", "")

# Harnesses namespace MCP tools as <server>_<tool>; the server is registered as
# `a8s`, so this is the `a8s_tell` the prompt names.
TOOL = {
    "name": "tell",
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


def log(event: dict) -> None:
    if not LOG:
        return
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        pass


def send(recipient: str, body: str) -> tuple[bool, str]:
    argv = [sys.executable, A8S_PY, "tell", recipient, "-"]
    try:
        proc = subprocess.run(
            argv, input=body, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"tell failed to run: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "tell failed").strip()
    return True, (proc.stdout or "sent").strip()


def reply(msg_id, result) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "a8s", "version": "0.1.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(msg_id, {"tools": [TOOL]})
        elif method == "tools/call":
            params = msg.get("params") or {}
            args = params.get("arguments") or {}
            recipient = str(args.get("recipient", ""))
            body = str(args.get("body", ""))
            log({"tool": params.get("name"), "recipient": recipient, "body": body})
            ok, detail = send(recipient, body)
            reply(msg_id, {
                "content": [{"type": "text", "text": detail}],
                "isError": not ok,
            })
        elif msg_id is not None:
            reply(msg_id, {})


if __name__ == "__main__":
    main()
