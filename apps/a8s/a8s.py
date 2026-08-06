"""a8s — Agent Infinity System.

Filesystem-based message router for independent Claude Code, Gemini CLI,
and Codex CLI project directories ("participants") to communicate.

This file is the script entry point; functionality lives in sibling modules:

  core.py         paths, logging, Participant, helpers, MARKER_FILES
  registry.py     ~/.config/a8s/a8s.json I/O + alias resolution + sender_from_cwd
  mailbox.py      inbox/outbox/trash routing + queue helpers
  definitions.py  invoke* verbs, prompt formatting, definition loading
  daemon.py       wake subprocess, pid attachment, signal handling
  tell.py          outbox drop + CLI parsing (stdin, --attach)
  tells.py         wait for the next inbound message (receive-side of tell)
  mcp_server.py   stdio MCP server exposing tell as a tool
  commands.py     every cmd_* function
  cli.py          COMMANDS table, dispatch, main

Surface (CLI):
  add <name> <dir> [<def>]    explicit registration (auto-detects definition)
  agents / discover / define  registry inspection + setup
  alias / unalias / aliases   group resolution
  start / run / step          handler attachment (1+ agents per process)
  stop / kill / exit / ls     handler control
  tell / tells                send a message / wait for the next one
  mcp serve                   stdio MCP server: tell as a tool (a8s_tell)
  logs <name>... [--tail N] [-f]   per-agent log readout (merge-sorted)

`a8s` with no command prints help. There is no auto-discovery — agents must
be explicitly registered with `a8s add` (use `a8s discover` to find candidates).

State:
  ~/.config/a8s/a8s.json             registry: {agents, aliases}
  ~/.config/a8s/agents/<NAME>/       per-agent: inbox/, trash/, log.txt, pid
  ~/.config/a8s/log.txt              process-scoped supervisor log
  <agent-root>/.outbox/       agent-writable; routing re-stamps `from`
"""
from __future__ import annotations

import sys

from cli import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
