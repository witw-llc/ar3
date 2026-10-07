"""a8s CLI — the COMMANDS table, dispatcher, and main argparse entry."""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path


from core import version_line
import settings as sm
from registry import RegistryUnreadable
from commands import (
    cmd_add,
    cmd_alias,
    cmd_aliases,
    cmd_define,
    cmd_definitions,
    cmd_discover,
    cmd_drain,
    cmd_retry,
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
    cmd_transactions,
    cmd_unalias,
    cmd_unnamespace,
    cmd_unremote,
    cmd_unstorage,
    cmd_update,
    cmd_vars,
)


COMMANDS: list[tuple[str, str, str]] = [
    ("add",      "<name> <dir> [<def>] [--K v ...]", "Register a node (optional a8s vars)."),
    ("remove",   "<name>",                    "Unregister a node and delete its mailbox."),
    ("rm",       "<name>",                    "Alias for remove."),
    ("ls",       "[-q]",                      "List all registered nodes, running or not."),
    ("discover", "<path>",                    "Scan a path for nodes and suggest `add` commands."),
    ("define",   "<name> [<def>]",            "Show or set an agent's definition (path or bare name)."),
    ("definitions", "[add|rm|ls ...]",       "Manage user-installed definition templates (~/.config/a8s/definitions)."),
    ("defs",     "[add|rm|ls ...]",          "Alias for definitions."),
    ("vars",     "<name> [set|unset ...]",   "Get/set per-node vars: argv $KEY, or env.<NAME> for the environment."),
    ("alias",    "[<name> [<member>]]",       "Group agents under an alias name; show one with `<name>`."),
    ("unalias",  "<alias> [<member>]",        "Remove a member from an alias, or the whole alias."),
    ("aliases",  "",                          "List aliases and their members."),
    ("namespace", "[<prefix> [<agent>] [--opaque]]", "Bind an address prefix to one agent (`tell <prefix>:<sub> ...`); `--opaque` conceals member attribution outward."),
    ("unnamespace", "<prefix>",               "Remove a namespace binding."),
    ("namespaces", "",                        "List namespace prefixes and their bound agents."),
    ("start",    "<name>...",                 "Run agents in the background, one process per name."),
    ("run",      "<name> [--drain <sec>]",     "Run an agent in the foreground."),
    ("step",     "<name>",                    "Run an agent for one pass and exit."),
    ("stop",     "<name>... [--force]",       "Stop nodes; wait until detached (finish current wake unless --force, which kills it)."),
    ("restart",  "<name>... [--force]",       "Stop (wait) then start each node."),
    ("update",   "[--force]",                 "Restart all running nodes (refresh handlers after git pull)."),
    ("kill",     "<name>",                    "Force-stop a running agent."),
    ("exit",     "",                          "Stop every running agent."),
    ("ps",       "[-q]",                      "List running nodes with status and recent messages."),
    ("tell",     "<name> [<message>]",       "Send a message to an agent or alias."),
    ("tells",    "[-f] [--timeout SEC] [--glow [theme]]", "Wait for inbound messages to this node."),
    ("drain",    "<name>",                   "Move local inbox to trash without invoking."),
    ("retry",    "<name>",                   "Try a failed wake, and its dead letters, again now."),
    ("config",   "[get|set|unset ...]",      "List all knobs or edit ~/.config/a8s/settings.json."),
    ("convo",    "<name> [--limit N] [-f] [--from NAME] [--glow [theme]]", "Show markdown conversation history for an agent."),
    ("transactions", "[--limit N] [-f] [--event E] [--from N] [--to N]", "Show recent routing events (alias: tx)."),
    ("trace",    "<ULID>",                   "Show transaction boundaries for one message."),
    ("logs",     "<name>... [-n N|all] [-f]", "Show the last 1000 lines of per-agent logs."),
    ("remote",   "[<name> [<folder> | <broker> <topic>] [--<k> <v> ...]]", "List, show, or set a cross-machine remote."),
    ("unremote", "<name>",                    "Remove a configured remote."),
    ("storage",  "[<name> [<url> [--<k> <v> ...]]]",            "List, show, or set a cross-cluster file storage service."),
    ("unstorage","<name>",                    "Remove a configured storage service."),
    ("mcp",      "serve",                     "Run the MCP server exposing tell as a tool."),
    ("health",   "",                          "Test connectivity of remotes and storage services."),
]

ALIASES = {"tx": "transactions"}


def _load_extensions() -> list:
    """Modules from `ext/*.py` (or `$A8S_EXT_DIR`), sorted, `_`-prefixed names
    skipped. One that fails to import, or lacks `handle`, is reported and
    skipped; a8s still runs."""
    here = Path(__file__).resolve().parent
    env = os.environ.get("A8S_EXT_DIR")
    ext_dir = Path(env) if env else here / "ext"
    if not ext_dir.is_dir():
        return []
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    loaded = []
    for path in sorted(ext_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"a8s_ext_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not callable(getattr(module, "handle", None)):
                raise AttributeError("no handle(cmd, args)")
        except Exception as e:
            print(f"a8s: extension {path.name}: {e}", file=sys.stderr)
            continue
        loaded.append(module)
    return loaded


EXTENSIONS = _load_extensions()
EXT_COMMANDS: list[tuple[str, str, str]] = [
    row for module in EXTENSIONS for row in getattr(module, "COMMANDS", [])
]

KNOWN_COMMANDS = {name for name, _, _ in COMMANDS + EXT_COMMANDS} | set(ALIASES)


def _format_commands(rows: list[tuple[str, str, str]], indent: int = 2) -> str:
    headers = [(n + " " + a).strip() for n, a, _ in rows]
    width = max(len(h) for h in headers)
    return "\n".join(
        f"{' ' * indent}{header.ljust(width)}    {help_text}"
        for header, (_, _, help_text) in zip(headers, rows)
    )


CLI_EPILOG = "Commands:\n" + _format_commands(COMMANDS)
if EXT_COMMANDS:
    CLI_EPILOG += "\n\nExtensions:\n" + _format_commands(EXT_COMMANDS)


LEARNS_PATH = {"add", "define", "start", "run", "restart", "retry"}


def _learn_path() -> None:
    """The operator's terminal is where the harness already runs, so the
    verbs that set a node going remember its PATH for every later wake."""
    known = bool(str(sm.load_settings_file().get("wake_path") or "").strip())
    added = sm.learn_wake_path()
    if added and known:
        print(f"a8s: wakes now also search {', '.join(added)}")
    elif added:
        print("a8s: remembered this terminal's PATH for every node's wakes")


def dispatch(cmd: str, args: list[str], interval: float) -> int:
    for module in EXTENSIONS:
        claimed = module.handle(cmd, args)
        if claimed is not None:
            return claimed
    if cmd in LEARNS_PATH and sm.at_a_terminal():
        _learn_path()
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
    if cmd == "retry":
        return cmd_retry(args)
    if cmd == "config":
        return cmd_config(args)
    if cmd == "convo":
        return cmd_convo(args)
    if cmd in ("transactions", "tx"):
        return cmd_transactions(args)
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
    parser.add_argument("--version", action="version", version=version_line("a8s"))
    parser.add_argument("command", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    interval = args.interval if args.interval is not None else sm.get_float("loop_interval")

    rest = args.rest
    if args.command == "tell":
        # argparse drops the first `--` from a REMAINDER, and `tell` needs it:
        # it ends options so a recipient that starts with a dash stays a name.
        rest = argv[argv.index("tell") + 1:]

    if args.command in KNOWN_COMMANDS:
        try:
            return dispatch(args.command, rest, interval)
        except RegistryUnreadable as e:
            print(f"a8s: {e}", file=sys.stderr)
            return 1

    print(f"unknown command: {args.command!r}", file=sys.stderr)
    return 2
