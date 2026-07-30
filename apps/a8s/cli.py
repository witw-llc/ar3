"""a8s CLI — the COMMANDS table, dispatcher, and main argparse entry."""
from __future__ import annotations

import argparse
import sys

import settings as sm
from commands import (
    cmd_add,
    cmd_alias,
    cmd_aliases,
    cmd_define,
    cmd_definitions,
    cmd_discover,
    cmd_drain,
    cmd_config,
    cmd_convo,
    cmd_exit,
    cmd_health,
    cmd_kill,
    cmd_logs,
    cmd_ls,
    cmd_mcp,
    cmd_ps,
    cmd_namespace,
    cmd_namespaces,
    cmd_remote,
    cmd_remove,
    cmd_restart,
    cmd_run,
    cmd_start,
    cmd_step,
    cmd_stop,
    cmd_storage,
    cmd_tell,
    cmd_tells,
    cmd_trace,
    cmd_unalias,
    cmd_unnamespace,
    cmd_unremote,
    cmd_unstorage,
    cmd_update,
    cmd_vars,
)


COMMANDS: list[tuple[str, str, str]] = [
    ("add",      "<name> <dir> [<def>] [--K=v ...]", "Register a node (optional a8s vars)."),
    ("remove",   "<name>",                    "Unregister a node and delete its mailbox."),
    ("rm",       "<name>",                    "Alias for remove."),
    ("ls",       "[-q]",                      "List all registered nodes, running or not."),
    ("discover", "<path>",                    "Scan a path for nodes and suggest `add` commands."),
    ("define",   "<name> [<def>]",            "Show or set an agent's definition (path or bare name)."),
    ("definitions", "[add|rm|ls ...]",       "Manage user-installed definition templates (~/.a8s/definitions)."),
    ("defs",     "[add|rm|ls ...]",          "Alias for definitions."),
    ("vars",     "<name> [set|unset ...]",   "Get/set per-node a8s vars for definition $KEY interpolation."),
    ("alias",    "[<name> [<member>]]",       "Group agents under an alias name; show one with `<name>`."),
    ("unalias",  "<alias> [<member>]",        "Remove a member from an alias, or the whole alias."),
    ("aliases",  "",                          "List aliases and their members."),
    ("namespace", "[<prefix> [<agent>] [--opaque]]", "Bind an address prefix to one agent (`tell <prefix>:<sub> ...`); `--opaque` conceals member attribution outward."),
    ("unnamespace", "<prefix>",               "Remove a namespace binding."),
    ("namespaces", "",                        "List namespace prefixes and their bound agents."),
    ("start",    "<name>",                    "Run an agent in the background."),
    ("run",      "<name> [--drain <sec>]",     "Run an agent in the foreground."),
    ("step",     "<name>",                    "Run an agent for one pass and exit."),
    ("stop",     "<name> [--force]",          "Stop a node; wait until detached (finish current wake unless --force)."),
    ("restart",  "<name> [--force]",          "Stop (wait) then start a node."),
    ("update",   "[--force]",                 "Restart all running nodes (refresh handlers after git pull)."),
    ("kill",     "<name>",                    "Force-stop a running agent."),
    ("exit",     "",                          "Stop every running agent."),
    ("ps",       "[-q]",                      "List running node processes."),
    ("tell",     "<name> [<message>]",       "Send a message to an agent or alias."),
    ("tells",    "[-f] [--timeout SEC] [--glow [theme]]", "Wait for inbound messages to this node."),
    ("drain",    "<name>",                   "Move local inbox to trash without invoking."),
    ("config",   "[get|set|unset ...]",      "List all knobs or edit ~/.a8s/settings.json."),
    ("convo",    "<name> [--limit N] [-f] [--glow [theme]]", "Show markdown conversation history for an agent."),
    ("trace",    "<ULID>",                   "Show transaction boundaries for one message."),
    ("logs",     "<name>... [--tail N] [-f]", "Show per-agent logs."),
    ("remote",   "[<name> [<broker> <topic> [--<k> <v> ...]]]", "List, show, or set a cross-machine remote."),
    ("unremote", "<name>",                    "Remove a configured remote."),
    ("storage",  "[<name> [<url> [--<k> <v> ...]]]",            "List, show, or set a cross-cluster file storage service."),
    ("unstorage","<name>",                    "Remove a configured storage service."),
    ("mcp",      "serve",                     "Run the MCP server exposing tell as a tool."),
    ("health",   "",                          "Test connectivity of remotes and storage services."),
]

KNOWN_COMMANDS = {name for name, _, _ in COMMANDS}


def _format_commands(rows: list[tuple[str, str, str]], indent: int = 2) -> str:
    headers = [(n + " " + a).strip() for n, a, _ in rows]
    width = max(len(h) for h in headers)
    return "\n".join(
        f"{' ' * indent}{header.ljust(width)}    {help_text}"
        for header, (_, _, help_text) in zip(headers, rows)
    )


CLI_EPILOG = "Commands:\n" + _format_commands(COMMANDS)


def dispatch(cmd: str, args: list[str], interval: float) -> int:
    if cmd == "add":
        return cmd_add(args)
    if cmd in ("remove", "rm"):
        return cmd_remove(args)
    if cmd == "ls":
        return cmd_ls(args)
    if cmd == "discover":
        return cmd_discover(args)
    if cmd == "define":
        return cmd_define(args)
    if cmd in ("definitions", "defs"):
        return cmd_definitions(args)
    if cmd == "vars":
        return cmd_vars(args)
    if cmd == "alias":
        return cmd_alias(args)
    if cmd == "unalias":
        return cmd_unalias(args)
    if cmd == "aliases":
        return cmd_aliases()
    if cmd == "namespace":
        return cmd_namespace(args)
    if cmd == "unnamespace":
        return cmd_unnamespace(args)
    if cmd == "namespaces":
        return cmd_namespaces()
    if cmd == "start":
        return cmd_start(args)
    if cmd == "run":
        return cmd_run(args, interval)
    if cmd == "step":
        return cmd_step(args, interval)
    if cmd == "stop":
        return cmd_stop(args)
    if cmd == "restart":
        return cmd_restart(args)
    if cmd == "update":
        return cmd_update(args)
    if cmd == "kill":
        return cmd_kill(args)
    if cmd == "exit":
        return cmd_exit()
    if cmd == "ps":
        return cmd_ps(args)
    if cmd == "tell":
        return cmd_tell(args)
    if cmd == "tells":
        return cmd_tells(args)
    if cmd == "drain":
        return cmd_drain(args)
    if cmd == "config":
        return cmd_config(args)
    if cmd == "convo":
        return cmd_convo(args)
    if cmd == "trace":
        return cmd_trace(args)
    if cmd == "mcp":
        return cmd_mcp(args)
    if cmd == "logs":
        return cmd_logs(args)
    if cmd == "remote":
        return cmd_remote(args)
    if cmd == "unremote":
        return cmd_unremote(args)
    if cmd == "storage":
        return cmd_storage(args)
    if cmd == "unstorage":
        return cmd_unstorage(args)
    if cmd == "health":
        return cmd_health()
    raise ValueError(f"unknown command: {cmd!r}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="a8s",
        description="Agent Infinity System — route messages between Claude / Gemini / Codex projects.",
        epilog=CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=f"loop poll interval seconds (default from settings: {sm.DEFAULTS['loop_interval']})",
    )
    parser.add_argument("command", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    interval = args.interval if args.interval is not None else sm.get_float("loop_interval")

    if args.command in KNOWN_COMMANDS:
        return dispatch(args.command, args.rest, interval)

    print(f"unknown command: {args.command!r}", file=sys.stderr)
    return 2
