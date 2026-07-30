"""Echo wake handler behind `definitions/echo.json`.

Replies to the sender with the inbound body verbatim. Attachments are
acknowledged by name on one appended line, never echoed back. The reply is
an envelope written into the node's own outbox (`TELL_OUTBOX_DIR`, injected
on wake) — the same staging path `tell` uses — so routing, transport, and
`from` stamping treat it like any other tell. One tell to an echo node
proves the whole path out and back.
"""
from __future__ import annotations

import sys
from pathlib import Path

from core import _preview
from definitions import ATTACHED_FILE_PREFIX
from tell import find_outbox, write_outbox_envelope


def split_message(message: str) -> tuple[str, list[str]]:
    """Split a wake's `$MESSAGE` into the verbatim body and attachment names.

    `build_command` appends attachments as a blank line plus one
    `ATTACHED FILE: <abs path>` line each; peel those off the tail."""
    lines = message.split("\n")
    names: list[str] = []
    while lines and lines[-1].startswith(ATTACHED_FILE_PREFIX):
        path = lines.pop()[len(ATTACHED_FILE_PREFIX):].strip()
        names.insert(0, Path(path).name)
    if names and lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines), names


def compose_reply(body: str, names: list[str]) -> str:
    if not names:
        return body
    notice = "attachments received: " + ", ".join(names)
    return f"{body}\n{notice}" if body else notice


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: echo_agent.py <sender> <message>", file=sys.stderr)
        return 2
    sender, message = argv
    body, names = split_message(message)
    reply = compose_reply(body, names)
    if not sender:
        print(f"echo: no sender to reply to: {_preview(reply)}")
        return 0
    outbox = find_outbox()
    if outbox is None:
        print("echo: no outbox available", file=sys.stderr)
        return 1
    write_outbox_envelope(outbox, sender, reply, [])
    print(f"echo -> {sender}: {_preview(reply)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
