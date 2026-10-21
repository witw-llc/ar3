"""Shared runtime for a8s tool nodes.

A tool node is an a8s agent whose wake is a small program, not a model: a8s
delivers a message, the tool does one thing, and it replies. Each tool lives in
its own module beside this one (`command_shell.py`, and later `browser.py`,
`vault.py`) and shares this runtime for the three things every tool node does
the same way — read the wake, enforce the per-node sender ACL, and reply
through `tell`.

The boundaries Neil drew for growing this cleanly:

- Cross-platform process control is the foundation (`ar3.proc`): a tool that
  runs a child uses `spawn` / `terminate_group`, never its own signal code.
- The a8s wake, ACL, and reply contract is here, once, for every tool.
- A tool's own behaviour is its own module. a8s owns routing and the
  definition JSON; a tool owns its verb and nothing of a8s's internals.

A tool never imports an a8s module. It replies by running the `a8s` CLI
(`a8s tell`) as a subprocess, the same way r4t reaches a8s through `a8s.py`, so
attachment staging, the size cap, and splitting stay tell's job and are never
duplicated. `a8s` injects `TELL_OUTBOX_DIR` into the wake environment, and
`a8s tell` reads it to find the outbox this wake writes into.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Print mode. `ar3 tool` sets this to a directory when it runs a tool by hand:
# the reply then goes to stdout and attachments are copied into the directory,
# so a tool can be tried with no router, no outbox and no `a8s tell`.
REPLY_DIR_ENV = "AR3_TOOL_REPLY_DIR"

# a8s appends attachments to `$MESSAGE` as a blank line plus one
# `ATTACHED FILE: <abs path>` line each (see apps/a8s/definitions.py). The
# prefix is restated here rather than imported: the no-a8s-import boundary is
# the point, and this one short constant is the whole coupling.
ATTACHED_FILE_PREFIX = "ATTACHED FILE: "


def split_attachments(message: str) -> tuple[str, list[str]]:
    """Split a wake's `$MESSAGE` into the verbatim body and attachment paths.

    Peels the trailing `ATTACHED FILE:` lines a8s appends, plus the single
    blank line that separates them from the body. Mirrors echo_agent's
    `split_message`, but returns absolute paths, not basenames, because a tool
    that acts on an attachment needs to open it."""
    lines = message.split("\n")
    paths: list[str] = []
    while lines and lines[-1].startswith(ATTACHED_FILE_PREFIX):
        paths.insert(0, lines.pop()[len(ATTACHED_FILE_PREFIX):].strip())
    if paths and lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines), paths


def allowed(sender: str, allow_spec: str) -> bool:
    """Whether `sender` is on a node's ACL. `allow_spec` is the `$ALLOW` var —
    names separated by commas or whitespace. Matching is case-insensitive, as
    a8s name resolution is. A senderless wake is never allowed, and an empty
    allowlist allows no one: a tool node fails closed."""
    if not sender:
        return False
    names = {n.strip().lower() for part in allow_spec.split(",") for n in part.split()}
    names.discard("")
    return sender.strip().lower() in names


def _a8s_path() -> Path:
    return Path(__file__).resolve().parent.parent / "a8s" / "a8s.py"


def print_mode() -> bool:
    """True when this run is a hand-run wake that prints its reply (`ar3 tool`)."""
    return bool(os.environ.get(REPLY_DIR_ENV))


def send_reply(recipient: str, body: str, attachments: tuple[str, ...] = ()) -> int:
    """Reply to `recipient` by running `a8s tell`, which reads `TELL_OUTBOX_DIR`
    from the environment a8s injected on wake. `body` is sent verbatim; large
    or binary output belongs in `attachments`, which tell stages, size-caps,
    and splits. Returns the CLI's exit code; a senderless wake sends nothing.

    When `AR3_TOOL_REPLY_DIR` is set (print mode, see `ar3 tool`) nothing is
    sent: the reply prints as `to <recipient>:` plus the body, each attachment
    is copied into that directory, and each copy prints as `attached: <path>`."""
    if not recipient:
        return 0
    reply_dir = os.environ.get(REPLY_DIR_ENV)
    if reply_dir:
        print(f"to {recipient}:")
        print(body)
        dest = Path(reply_dir)
        dest.mkdir(parents=True, exist_ok=True)
        for src in attachments:
            copied = dest / Path(src).name
            shutil.copyfile(src, copied)
            print(f"attached: {copied}")
        return 0
    argv = [sys.executable, str(_a8s_path()), "tell", "--verbatim"]
    argv += [f"--attach={p}" for p in attachments]
    argv += ["--", recipient, body]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
    return proc.returncode
