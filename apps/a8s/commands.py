"""a8s commands — every cmd_* function dispatched by cli.py.

Grouped by section:
  registry mgmt    — add, define, ls, discover
  aliases          — alias, unalias, aliases
  namespaces       — namespace, unnamespace, namespaces
  process control  — start, run, step, stop, kill, exit, ps
  messaging        — tell
  logs             — logs
  remotes          — remote, unremote

`cmd_start` re-execs the entry script via `core.ENTRYPOINT` (NOT __file__,
which would resolve to commands.py after the modular split).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from core import (
    ENTRYPOINT,
    _pid_alive,
    _preview,
    agent_dir,
    agent_log_path,
    canonical_name,
    inbox_dir,
    out,
    out_agent,
    pid_path,
    trash_dir,
    unique_path,
    user_definitions_dir,
)
from definitions import (
    _autodiscover_definition,
    builtin_definition_stems,
    default_definition_path,
    definition_stem,
    harness_is_resolvable,
    harness_program,
    list_definition_entries,
    load_definition,
    resolve_definition_arg,
    validate_var_name,
    wake_env,
    wake_shell,
    wrap_wake_argv,
)
from daemon import (
    _clear_kill_request,
    _read_handler_pid,
    _write_kill_request,
    attached_loop,
)
from network import (
    _build_service as build_service,
    configured_remote_ids,
    delete_remote_secrets,
    delete_spec_secrets,
    detect_service_kind,
    load_network_config,
    merge_remote_secrets,
    merge_spec_secrets,
    put_remote_secrets,
    put_spec_secrets,
    save_network_config,
    split_secret_keys,
)
from registry import (
    _scan_for_markers,
    find_participant,
    load_aliases,
    load_namespaces,
    load_registry,
    participants_from_registry,
    resolve_name,
    resolve_recipient,
    save_aliases,
    save_namespaces,
    save_registry,
    load_namespace_options,
    save_namespace_options,
)
from txlog import read_events
from ulid import is_ulid


# ---------- registry management commands ----------

def _unresolved_definition_error(spec: str) -> str:
    """Message for a definition argument that resolved to nothing. A bare name
    (`r4t`) was meant to name a bundled/user definition, so list what exists;
    anything else was meant to be a file. Mirrors `resolve_definition_arg`'s
    own name-vs-path test."""
    if Path(spec).expanduser().name != spec or spec.endswith(".json"):
        return f"not a file: {spec}"
    names = ", ".join(name for name, _, _ in list_definition_entries())
    return f"unknown definition: {spec}\navailable: {names}\n(or pass a path to a .json file)"


def cmd_add(args: list[str]) -> int:
    """`a8s add <name> <dir> [<definition>] [--KEY=value ...]` — register a node.

    The name is canonicalized (lowercase, alphanumeric) at registration so
    `a8s add CLAUDE` and `a8s add claude` collapse to the same agent — closes
    the case-collision footgun where independent registry entries each got
    their own dir but lookups conflated them.

    Without `<definition>`, `<dir>` is scanned for a marker file
    (CLAUDE.md/GEMINI.md/CODEX.md) and the matching built-in definition is
    auto-linked. Multiple or zero markers fall back to the bundled default.

    With `<definition>`, the JSON file is validated and set as the agent's
    definition. A bare name (`filedrop`, `claude`, `ollama-opencode`) resolves
    against bundled `apps/a8s/definitions/`, then user-installed
    ``~/.config/a8s/definitions/`` (`a8s defs add`); any other path is used as-is.

    Trailing ``--KEY value`` or ``--KEY=value`` flags set per-node a8s vars
    (same as ``a8s vars <name> set KEY value``). Keys are case-insensitive.
    The first flag ends the positional arguments, so the definition, if given,
    comes before it.

    Errors on duplicate name (vs. agents or aliases) or non-directory path."""
    if len(args) < 2:
        print(
            "usage: a8s add <name> <dir> [<definition>] [--KEY value ...]",
            file=sys.stderr,
        )
        return 2
    raw_name, dir_str = args[0], args[1]
    rest = args[2:]
    # The definition is positional and the vars are flags, so the first `--`
    # is the boundary: everything after it belongs to the option parser, which
    # is what lets `--KEY value` work here without swallowing the definition.
    first_flag = next((n for n, t in enumerate(rest) if t.startswith("--")), len(rest))
    positionals, var_tokens = rest[:first_flag], rest[first_flag:]
    if len(positionals) > 1:
        print(
            "usage: a8s add <name> <dir> [<definition>] [--KEY value ...]",
            file=sys.stderr,
        )
        return 2
    definition_arg: str | None = positionals[0] if positionals else None
    try:
        initial_vars = _parse_add_var_flags(var_tokens)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        name = canonical_name(raw_name)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    root = Path(dir_str).expanduser()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1
    root = root.resolve()
    reg = load_registry()
    for k in reg:
        if k.lower() == name:
            print(f"agent already exists with name: {k}", file=sys.stderr)
            return 1
    aliases = load_aliases()
    for k in aliases:
        if k.lower() == name:
            print(f"alias already exists with name: {k} — pick a different agent name", file=sys.stderr)
            return 1
    namespaces = load_namespaces()
    # A prefix already bound to this exact name is the agent's own namespace
    # — re-adding the node is fine. A prefix bound to a *different* agent
    # would be shadowed by `tell <name>` (namespace beats agent), so it stands.
    for k, bound in namespaces.items():
        if k.lower() == name and str(bound).strip().lower() != name:
            print(f"namespace already exists with prefix: {k} (bound to {bound}) — pick a different agent name", file=sys.stderr)
            return 1

    if definition_arg:
        try:
            path = resolve_definition_arg(definition_arg)
        except FileNotFoundError:
            print(_unresolved_definition_error(definition_arg), file=sys.stderr)
            return 1
        try:
            with path.open("r", encoding="utf-8") as f:
                json.loads(f.read())
        except (OSError, json.JSONDecodeError) as e:
            print(f"definition is not valid JSON: {e}", file=sys.stderr)
            return 1
        definition_path = str(path)
        note = "explicit"
    else:
        definition_path, note = _autodiscover_definition(root)

    entry: dict = {"root": str(root), "definition": definition_path}
    if initial_vars:
        entry["vars"] = dict(sorted(initial_vars.items()))
    reg[name] = entry
    save_registry(reg)
    from settings import capture_wake_path

    captured_path = capture_wake_path()
    print(f"added {name} -> {root}")
    print(f"definition: {definition_path}  ({note})")
    if captured_path:
        print("wake_path: recorded this shell's PATH for every node's wakes")
    for k, v in sorted(initial_vars.items()):
        print(f"var: {k}={v}")
    return 0


def parse_option_tokens(
    tokens: list[str],
    *,
    aliases: dict[str, str] | None = None,
) -> dict[str, str]:
    """Parse ``--key value`` and ``--key=value`` tokens into an option map.

    Both spellings are accepted everywhere. An operator who has used one a8s
    command has no way to guess that the next one wants the other spelling,
    and the failure was silent in the worst way: `--user=me --password=x`
    parsed as one option literally named `user=me` whose value was
    `--password=x`, so the error named an option nobody had typed and the
    password never reached the config at all.

    A space-form value that itself looks like an option is therefore an
    error, never a capture. ``--key=value`` remains the only spelling that
    can carry a value starting with a dash, and the message says so.

    Keys are lowercased in the sense that `-` becomes `_`; `aliases` maps
    operator-facing spellings onto canonical ones. Raises ValueError with a
    message written for the person at the terminal.
    """
    out: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("--") or len(tok) <= 2:
            if tok.startswith("-") and len(tok) > 1:
                raise ValueError(
                    f"{tok!r}: options here take two dashes and a full name "
                    f"(try --{tok.lstrip('-')})"
                )
            raise ValueError(f"expected --<opt> <value> or --<opt>=<value>, got: {tok!r}")
        body = tok[2:]
        if "=" in body:
            raw_key, _, value = body.partition("=")
            if not raw_key:
                raise ValueError(f"missing option name before '=': {tok!r}")
            i += 1
        else:
            raw_key = body
            if i + 1 >= len(tokens):
                raise ValueError(f"missing value for {tok}")
            value = tokens[i + 1]
            if value.startswith("--"):
                raise ValueError(
                    f"missing value for {tok}: the next token {value!r} looks like "
                    f"another option. Use {tok}=<value> if the value really starts "
                    "with a dash."
                )
            i += 2
        key = (aliases or {}).get(raw_key, raw_key).replace("-", "_")
        if key in out:
            raise ValueError(f"duplicate option: --{key}")
        out[key] = value
    return out


def _parse_add_var_flags(tokens: list[str]) -> dict[str, str]:
    """Parse agent-var option tokens into a canonical (uppercase) vars map."""
    return {
        validate_var_name(k): v for k, v in parse_option_tokens(tokens).items()
    }


def cmd_remove(args: list[str]) -> int:
    """`a8s remove <name>` — unregister an agent. Refuses if a handler is
    running (the user must `a8s stop` it first). Cascades into aliases:
    drops <name> from any alias's member list, and deletes any alias that
    becomes empty as a result. Cascades into namespaces the same way: any
    prefix bound to <name> is unbound (no orphans). Wipes the on-disk
    per-agent dir (~/.config/a8s/agents/<NAME>/) — inbox, trash, log, pid file
    all gone."""
    if len(args) != 1:
        print("usage: a8s remove <name>", file=sys.stderr)
        return 2
    raw = args[0]
    try:
        canonical_name(raw)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    match = resolve_recipient(raw)
    if match is None:
        print(f"no agent named {raw!r}", file=sys.stderr)
        return 1
    name = match[0]
    reg = load_registry()
    holder = _read_handler_pid(name)
    if holder is not None:
        print(f"{name} is running (PID {holder}); stop it first: `a8s stop {name}`", file=sys.stderr)
        return 1
    aliases = load_aliases()
    pruned: list[str] = []
    dropped: list[str] = []
    for alias_name in list(aliases.keys()):
        members = aliases[alias_name]
        kept = [m for m in members if m.lower() != name.lower()]
        if len(kept) == len(members):
            continue
        if kept:
            aliases[alias_name] = kept
            pruned.append(alias_name)
        else:
            del aliases[alias_name]
            dropped.append(alias_name)
    if pruned or dropped:
        save_aliases(aliases)
    namespaces = load_namespaces()
    unbound = sorted(
        p for p, target in namespaces.items()
        if str(target).lower() == name.lower()
    )
    if unbound:
        for p in unbound:
            del namespaces[p]
        save_namespaces(namespaces)
    d = agent_dir(name)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    del reg[name]
    save_registry(reg)
    print(f"removed {name}")
    if pruned:
        print(f"  pruned from aliases: {', '.join(sorted(pruned))}")
    if dropped:
        print(f"  dropped now-empty aliases: {', '.join(sorted(dropped))}")
    if unbound:
        print(f"  unbound namespaces: {', '.join(unbound)}")
    return 0


def cmd_define(args: list[str]) -> int:
    """`a8s define <name>`           — show <name>'s effective definition + source.
    `a8s define <name> <path>`       — set <name>'s definition file path in the registry."""
    if not args:
        print("usage: a8s define <name> [<definition>]", file=sys.stderr)
        return 2
    name = args[0]
    reg = load_registry()
    target_key: str | None = None
    for k in reg:
        if k.lower() == name.lower():
            target_key = k
            break
    if target_key is None:
        print(f"no agent named {name!r}", file=sys.stderr)
        return 1
    info = reg[target_key]

    if len(args) == 1:
        custom = info.get("definition")
        if not custom:
            print(f"{target_key}: no definition set", file=sys.stderr)
            print(f"hint: a8s define {target_key} filedrop   # or path to *.json", file=sys.stderr)
            return 1
        source = Path(custom).expanduser()
        print(f"{target_key}: {source}")
        try:
            with source.open("r", encoding="utf-8") as f:
                sys.stdout.write(f.read())
        except OSError as e:
            print(f"(could not read: {e})", file=sys.stderr)
            return 1
        return 0

    if len(args) > 2:
        print("usage: a8s define <name> [<definition>]", file=sys.stderr)
        return 2
    path = None
    try:
        path = resolve_definition_arg(args[1])
    except FileNotFoundError:
        print(_unresolved_definition_error(args[1]), file=sys.stderr)
        return 1
    try:
        with path.open("r", encoding="utf-8") as f:
            json.loads(f.read())
    except (OSError, json.JSONDecodeError) as e:
        print(f"definition is not valid JSON: {e}", file=sys.stderr)
        return 1
    info["definition"] = str(path)
    save_registry(reg)
    print(f"{target_key}: definition set to {path}")
    return 0


_DEF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _definitions_usage() -> int:
    print(
        "usage: a8s definitions|defs                        # list all\n"
        "       a8s definitions|defs list|ls                # list all\n"
        "       a8s definitions|defs add <path.json>        # install as bare name\n"
        "       a8s definitions|defs remove|rm <name>       # uninstall user definition\n"
        "\n"
        "Copies into ~/.config/a8s/definitions/<basename>.json. Basename must not\n"
        "collide with a repo built-in. Bare names then work with add/define.",
        file=sys.stderr,
    )
    return 2


def cmd_definitions(args: list[str]) -> int:
    """`a8s definitions|defs` — manage user-installed definition templates.

    Forms:
      a8s defs                         list builtin + user
      a8s defs list|ls                 list
      a8s defs add <path.json>         copy into ~/.config/a8s/definitions/
      a8s defs remove|rm <name>        delete a user definition (not builtins)
    """
    if len(args) == 0:
        return _cmd_definitions_list()
    sub = args[0]
    if sub in ("list", "ls"):
        if len(args) != 1:
            return _definitions_usage()
        return _cmd_definitions_list()
    if sub == "add":
        if len(args) != 2:
            return _definitions_usage()
        return _cmd_definitions_add(args[1])
    if sub in ("remove", "rm"):
        if len(args) != 2:
            return _definitions_usage()
        return _cmd_definitions_remove(args[1])
    return _definitions_usage()


def _cmd_definitions_list() -> int:
    rows = [
        (name, source, str(path))
        for name, source, path in list_definition_entries()
    ]
    if not rows:
        print("(no definitions)")
        return 0
    _print_table(["NAME", "SOURCE", "PATH"], rows)
    return 0


def _cmd_definitions_add(src: str) -> int:
    path = Path(src).expanduser()
    if not path.is_file():
        print(f"not a file: {src}", file=sys.stderr)
        return 1
    if path.suffix != ".json":
        print(f"definition must be a .json file: {src}", file=sys.stderr)
        return 2
    name = definition_stem(path.name)
    if not _DEF_NAME_RE.match(name):
        print(
            f"definition name must be alphanumeric (with -, _, .): {name!r}",
            file=sys.stderr,
        )
        return 2
    builtins = {s.lower() for s in builtin_definition_stems()}
    if name.lower() in builtins:
        print(
            f"cannot install {name!r}: conflicts with a repo built-in",
            file=sys.stderr,
        )
        return 1
    try:
        with path.open("r", encoding="utf-8") as f:
            json.loads(f.read())
    except (OSError, json.JSONDecodeError) as e:
        print(f"definition is not valid JSON: {e}", file=sys.stderr)
        return 1
    dest_dir = user_definitions_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}.json"
    overwriting = dest.is_file()
    shutil.copy2(path, dest)
    verb = "updated" if overwriting else "added"
    print(f"{verb} definition {name} -> {dest}")
    return 0


def _cmd_definitions_remove(name: str) -> int:
    stem = definition_stem(name)
    if not _DEF_NAME_RE.match(stem):
        print(
            f"definition name must be alphanumeric (with -, _, .): {stem!r}",
            file=sys.stderr,
        )
        return 2
    builtins = {s.lower() for s in builtin_definition_stems()}
    if stem.lower() in builtins:
        print(f"cannot remove built-in definition: {stem}", file=sys.stderr)
        return 1
    dest = user_definitions_dir() / f"{stem}.json"
    if not dest.is_file():
        print(f"no user definition named {stem!r}", file=sys.stderr)
        return 1
    dest.unlink()
    print(f"removed definition {stem}")
    return 0


def _vars_usage() -> int:
    print(
        "usage: a8s vars <name>                 # list a8s vars for a node\n"
        "       a8s vars <name> set <KEY> <val> # set (not OS environment)\n"
        "       a8s vars <name> unset <KEY>     # remove\n"
        "\n"
        "Per-node placeholders for definition argv ($KEY). Names are\n"
        "case-insensitive (stored uppercase). Built-in names ($SENDER,\n"
        "$MESSAGE, …) are reserved. Used-but-unset is a wake error.",
        file=sys.stderr,
    )
    return 2


def cmd_vars(args: list[str]) -> int:
    """`a8s vars` — per-node a8s variables for definition interpolation.

    Stored on the agent in the registry (`vars` map). Expanded as `$KEY` in
    invoke argv; never read from or written to the process environment.
    """
    if len(args) < 1:
        return _vars_usage()
    match = resolve_recipient(args[0])
    if match is None:
        print(f"no agent named {args[0]!r}", file=sys.stderr)
        return 1
    agent_key, info = match
    if len(args) == 1:
        return _cmd_vars_list(agent_key, info)
    sub = args[1]
    if sub == "set":
        if len(args) != 4:
            return _vars_usage()
        return _cmd_vars_set(agent_key, info, args[2], args[3])
    if sub == "unset":
        if len(args) != 3:
            return _vars_usage()
        return _cmd_vars_unset(agent_key, info, args[2])
    return _vars_usage()


def _cmd_vars_list(agent_key: str, info: dict) -> int:
    raw = info.get("vars")
    vars_map = raw if isinstance(raw, dict) else {}
    items: dict[str, str] = {}
    for k, v in vars_map.items():
        if isinstance(k, str) and isinstance(v, str):
            items[k.upper()] = v
    if not items:
        print(f"{agent_key}: (no vars)")
        return 0
    width = max(len(k) for k in items)
    for k in sorted(items):
        print(f"{k.ljust(width)}  {items[k]}")
    return 0


def _cmd_vars_set(agent_key: str, info: dict, key: str, value: str) -> int:
    try:
        key = validate_var_name(key)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    reg = load_registry()
    entry = reg.get(agent_key)
    if entry is None:
        print(f"no agent named {agent_key!r}", file=sys.stderr)
        return 1
    vars_map = entry.get("vars")
    if not isinstance(vars_map, dict):
        vars_map = {}
        entry["vars"] = vars_map
    overwriting = False
    for existing in list(vars_map):
        if isinstance(existing, str) and existing.upper() == key:
            del vars_map[existing]
            overwriting = True
    vars_map[key] = value
    save_registry(reg)
    verb = "updated" if overwriting else "set"
    print(f"{agent_key}: {verb} {key}={value}")
    return 0


def _cmd_vars_unset(agent_key: str, info: dict, key: str) -> int:
    try:
        key = validate_var_name(key)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    reg = load_registry()
    entry = reg.get(agent_key)
    if entry is None:
        print(f"no agent named {agent_key!r}", file=sys.stderr)
        return 1
    vars_map = entry.get("vars")
    if not isinstance(vars_map, dict):
        print(f"{agent_key}: no var named {key!r}", file=sys.stderr)
        return 1
    found = False
    for existing in list(vars_map):
        if isinstance(existing, str) and existing.upper() == key:
            del vars_map[existing]
            found = True
    if not found:
        print(f"{agent_key}: no var named {key!r}", file=sys.stderr)
        return 1
    if not vars_map:
        entry.pop("vars", None)
    save_registry(reg)
    print(f"{agent_key}: unset {key}")
    return 0


def _print_table(headers: list[str], rows: list[tuple[str, ...]]) -> None:
    """Docker-style aligned table: left-justified columns, three-space gutters,
    no padding on the trailing column so lines don't carry dead whitespace."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: tuple[str, ...]) -> str:
        last = len(cells) - 1
        return "   ".join(
            cell.ljust(widths[i]) if i < last else cell
            for i, cell in enumerate(cells)
        )

    print(fmt(tuple(headers)))
    for row in rows:
        print(fmt(row))


def _pid_uptime(name: str) -> str:
    """Coarse uptime from the pid file's mtime — a cheap stat, no bookkeeping.
    The pid file is written when a process claims the node, so its age tracks
    how long the node has been running under that handler."""
    try:
        mtime = pid_path(name).stat().st_mtime
    except OSError:
        return "?"
    secs = max(0, int(time.time() - mtime))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def cmd_ls(args: list[str] | None = None) -> int:
    """`a8s ls` — list every registered node, running or not (docker/ollama
    style). Columns: NAME, STATUS, DEFINITION, ROOT, plus NAMESPACES when any
    prefix is bound. `-q` prints just names, one per line, for scripting.

    STATUS is `running (pid N)` or `stopped`; DEFINITION is the definition
    basename (default fallback when the registry has no `definition` field)."""
    args = args or []
    quiet = "-q" in args
    reg = load_registry()
    if not reg:
        if not quiet:
            print("(no nodes registered — use `a8s add <name> <dir>`)")
        return 0

    names = sorted(reg, key=str.lower)
    if quiet:
        for name in names:
            print(name)
        return 0

    bindings: dict[str, list[str]] = {}
    for prefix, agent in load_namespaces().items():
        bindings.setdefault(agent.lower(), []).append(f"{prefix}:")

    rows: list[tuple[str, ...]] = []
    for name in names:
        info = reg[name]
        pid = _read_handler_pid(name)
        status = f"running (pid {pid})" if pid is not None else "stopped"
        defn = info.get("definition") or str(default_definition_path("default"))
        definition = Path(defn).stem
        root = info.get("root", "?")
        ns = " ".join(sorted(bindings.get(name.lower(), [])))
        rows.append((name, status, definition, root, ns))

    if any(row[4] for row in rows):
        _print_table(["NAME", "STATUS", "DEFINITION", "ROOT", "NAMESPACES"], rows)
    else:
        _print_table(["NAME", "STATUS", "DEFINITION", "ROOT"], [r[:4] for r in rows])
    return 0


def cmd_discover(args: list[str]) -> int:
    """`a8s discover <path>` — read-only walk for marker files. Prints suggested
    `a8s add` / `a8s define` commands; never mutates the registry."""
    if len(args) != 1:
        print("usage: a8s discover <path>", file=sys.stderr)
        return 2
    root = Path(args[0]).expanduser()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1
    found = _scan_for_markers(root.resolve())
    if not found:
        print(f"no marker files (CLAUDE.md/GEMINI.md/CODEX.md with `# Name` line) found under {root}")
        return 0
    reg = load_registry()
    registered_names = {n.lower() for n in reg}
    registered_roots = {Path(v.get("root", "")).resolve() for v in reg.values() if v.get("root")}
    print(f"found {len(found)} candidate(s) under {root}:\n")
    for name, kind, dir_path in found:
        already = name.lower() in registered_names or dir_path in registered_roots
        marker = "  [already registered]" if already else ""
        print(f"# {name} ({kind}) at {dir_path}{marker}")
        if not already:
            print(f"a8s add {name} {dir_path}")
            print(f"a8s define {name} {default_definition_path(kind)}")
        print()
    return 0


def cmd_mcp(args: list[str]) -> int:
    """`a8s mcp serve` — the stdio MCP server harnesses spawn as a child."""
    if args[:1] == ["serve"] and len(args) == 1:
        import mcp_server

        return mcp_server.serve()
    print("usage: a8s mcp serve", file=sys.stderr)
    return 2


# ---------- alias commands ----------

def cmd_alias(args: list[str]) -> int:
    """`a8s alias` — manage aliases.

    Forms (mirror `a8s remote` / `a8s storage`):
      a8s alias                      list all
      a8s alias <name>               show one alias's members
      a8s alias <alias> <member>     add or create

    Names are canonicalized (lowercase) so `a8s alias Devs CLAUDE` and
    `a8s alias devs claude` are the same operation. Members
    may be agent names OR existing alias names (nesting OK, cycles
    rejected at resolve time). The alias name must not collide with an
    existing agent name."""
    if len(args) == 0:
        return cmd_aliases()
    if len(args) == 1:
        return _cmd_alias_show(args[0])
    if len(args) != 2:
        print("usage: a8s alias <alias> <member>     # add or create", file=sys.stderr)
        print("       a8s alias <name>               # show one", file=sys.stderr)
        print("       a8s alias                      # list", file=sys.stderr)
        return 2
    raw_alias, raw_member = args
    try:
        alias_name = canonical_name(raw_alias)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        member = canonical_name(raw_member)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    agents = load_registry()
    aliases = load_aliases()
    for k in agents:
        if k.lower() == alias_name:
            print(f"agent already exists with name: {k} — pick a different alias", file=sys.stderr)
            return 1
    for k in load_namespaces():
        if k.lower() == alias_name:
            print(f"namespace already exists with prefix: {k} — pick a different alias", file=sys.stderr)
            return 1
    member_resolved: str | None = None
    for k in agents:
        if k.lower() == member:
            member_resolved = k
            break
    if member_resolved is None:
        for k in aliases:
            if k.lower() == member:
                member_resolved = k
                break
    if member_resolved is None:
        print(f"unknown member {raw_member!r} (not an agent or alias)", file=sys.stderr)
        return 1
    if member_resolved.lower() == alias_name:
        print(f"cannot add alias {alias_name!r} to itself", file=sys.stderr)
        return 1
    canonical_alias = alias_name
    for k in aliases:
        if k.lower() == alias_name:
            canonical_alias = k
            break
    members = aliases.get(canonical_alias) or []
    if any(m.lower() == member_resolved.lower() for m in members):
        print(f"{canonical_alias} already includes {member_resolved}")
        return 0
    members.append(member_resolved)
    aliases[canonical_alias] = members
    save_aliases(aliases)
    # Cycle check via resolve_name; revert on failure.
    try:
        resolve_name(canonical_alias)
    except ValueError as e:
        members.remove(member_resolved)
        if not members:
            aliases.pop(canonical_alias, None)
        else:
            aliases[canonical_alias] = members
        save_aliases(aliases)
        print(f"refusing add: {e}", file=sys.stderr)
        return 1
    print(f"{canonical_alias} += {member_resolved}")
    return 0


def cmd_unalias(args: list[str]) -> int:
    """`a8s unalias <alias> [<member>]` — remove a single member, or the whole
    alias if no member given. Both names are case-insensitive."""
    if not args or len(args) > 2:
        print("usage: a8s unalias <alias> [<member>]", file=sys.stderr)
        return 2
    target = args[0].strip().lower()
    aliases = load_aliases()
    canonical: str | None = None
    for k in aliases:
        if k.lower() == target:
            canonical = k
            break
    if canonical is None:
        print(f"unknown alias: {args[0]!r}", file=sys.stderr)
        return 1
    if len(args) == 1:
        del aliases[canonical]
        save_aliases(aliases)
        print(f"removed alias {canonical}")
        return 0
    member = args[1]
    member_lc = member.strip().lower()
    members = aliases[canonical]
    new_members = [m for m in members if m.lower() != member_lc]
    if len(new_members) == len(members):
        print(f"{canonical}: not a member: {member!r}", file=sys.stderr)
        return 1
    if not new_members:
        del aliases[canonical]
    else:
        aliases[canonical] = new_members
    save_aliases(aliases)
    print(f"{canonical} -= {member}")
    return 0


def _cmd_alias_show(name: str) -> int:
    """`a8s alias <name>` — show one alias's members. Mirrors `remote <name>`
    and `storage <name>`."""
    try:
        target = canonical_name(name)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    aliases = load_aliases()
    canonical: str | None = None
    for k in aliases:
        if k.lower() == target:
            canonical = k
            break
    if canonical is None:
        print(f"no alias named {name!r}", file=sys.stderr)
        return 1
    members = aliases[canonical]
    try:
        _, resolved = resolve_name(canonical)
        tail = "" if len(members) == len(resolved) else f"  → {len(resolved)} agents"
    except (KeyError, ValueError) as e:
        tail = f"  [{e}]"
    print(f"{canonical}: [{', '.join(members)}]{tail}")
    return 0


def cmd_aliases() -> int:
    """`a8s aliases` — list every alias and its members."""
    aliases = load_aliases()
    if not aliases:
        print("(no aliases — use `a8s alias <alias> <member>` to create one)")
        return 0
    width = max(len(name) for name in aliases)
    for name in sorted(aliases, key=str.lower):
        members = aliases[name]
        try:
            _, resolved = resolve_name(name)
            tail = "" if len(members) == len(resolved) else f"  → {len(resolved)} agents"
        except (KeyError, ValueError) as e:
            tail = f"  [{e}]"
        print(f"  {name.ljust(width)}  [{', '.join(members)}]{tail}")
    return 0


# ---------- namespace commands ----------

def cmd_namespace(args: list[str]) -> int:
    """`a8s namespace` — bind address prefixes to node agents.

    Forms (mirror `a8s alias`):
      a8s namespace                      list all
      a8s namespace <prefix>             show one binding
      a8s namespace <prefix> <agent>     bind or rebind

    A bound prefix routes every `<prefix>:<sub-address>` recipient to the
    single bound agent; the full address stays in the message's `to` so the
    node's `$RECIPIENT` carries it verbatim and the node can self-route
    internally. The target must be a registered agent, not an alias —
    namespace delegation is single-delivery by design, the opposite of
    alias fan-out. Prefixes share the agent/alias name grammar (lowercase
    canonical form). A prefix may match the name of the agent it binds to (a
    node owning its own namespace) but must not collide with an alias or
    with any other agent."""
    opaque = "--opaque" in args
    args = [a for a in args if a != "--opaque"]
    if len(args) == 0 and not opaque:
        return cmd_namespaces()
    if len(args) == 1 and not opaque:
        return _cmd_namespace_show(args[0])
    if len(args) != 2:
        print("usage: a8s namespace <prefix> <agent> [--opaque]   # bind or rebind", file=sys.stderr)
        print("       a8s namespace <prefix>                      # show one", file=sys.stderr)
        print("       a8s namespace                               # list", file=sys.stderr)
        return 2
    raw_prefix, raw_target = args
    try:
        prefix = canonical_name(raw_prefix)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        target = canonical_name(raw_target)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    agents = load_registry()
    aliases = load_aliases()
    # A prefix may match the name of the agent it binds to — that's a node
    # owning its own namespace, so cross-wall traffic is attributed to
    # `s1l`, not `s1l-node`. It must not match any *other* agent's name, which
    # `tell <prefix>` would silently shadow (namespace beats agent in resolve).
    for k in agents:
        if k.lower() == prefix and k.lower() != target:
            print(f"agent already exists with name: {k} — pick a different prefix", file=sys.stderr)
            return 1
    for k in aliases:
        if k.lower() == prefix:
            print(f"alias already exists with name: {k} — pick a different prefix", file=sys.stderr)
            return 1
    if any(k.lower() == target for k in aliases):
        print(f"namespace target must be an agent, not an alias: {raw_target!r}", file=sys.stderr)
        return 1
    target_resolved: str | None = None
    for k in agents:
        if k.lower() == target:
            target_resolved = k
            break
    if target_resolved is None:
        print(f"unknown agent {raw_target!r}", file=sys.stderr)
        return 1
    namespaces = load_namespaces()
    previous = namespaces.get(prefix)
    namespaces[prefix] = target_resolved
    save_namespaces(namespaces)
    # Rebinding is how opacity flips: the flag's absence clears it, so the
    # stored option always mirrors the latest bind.
    options = load_namespace_options()
    if opaque:
        options[prefix] = {"opaque": True}
    else:
        options.pop(prefix, None)
    save_namespace_options(options)
    tail = " (opaque)" if opaque else ""
    if previous is not None and str(previous).lower() != target_resolved.lower():
        print(f"rebound {prefix}: -> {target_resolved}{tail} (was {previous})")
    else:
        print(f"bound {prefix}: -> {target_resolved}{tail}")
    return 0


def cmd_unnamespace(args: list[str]) -> int:
    """`a8s unnamespace <prefix>` — remove a namespace binding. Mirrors
    `unalias`'s shape so the surface stays uniform across registry
    primitives."""
    if len(args) != 1:
        print("usage: a8s unnamespace <prefix>", file=sys.stderr)
        return 2
    target = args[0].strip().lower()
    namespaces = load_namespaces()
    canonical = next((k for k in namespaces if k.lower() == target), None)
    if canonical is None:
        print(f"no namespace named {args[0]!r}", file=sys.stderr)
        return 1
    del namespaces[canonical]
    save_namespaces(namespaces)
    options = load_namespace_options()
    if options.pop(canonical, None) is not None:
        save_namespace_options(options)
    print(f"removed namespace {canonical}")
    return 0


def _namespace_binding_tail(target: str) -> str:
    known = {n.lower() for n in load_registry()}
    return "" if str(target).lower() in known else f"  [unknown agent {target!r}]"


def _cmd_namespace_show(name: str) -> int:
    """`a8s namespace <prefix>` — show one binding. Mirrors `alias <name>`."""
    try:
        target = canonical_name(name)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    namespaces = load_namespaces()
    canonical = next((k for k in namespaces if k.lower() == target), None)
    if canonical is None:
        print(f"no namespace named {name!r}", file=sys.stderr)
        return 1
    bound = namespaces[canonical]
    options = load_namespace_options()
    is_opaque = any(
        k.lower() == canonical.lower() and (o or {}).get("opaque")
        for k, o in options.items()
    )
    mark = "  [opaque]" if is_opaque else ""
    print(f"{canonical}: -> {bound}{mark}{_namespace_binding_tail(bound)}")
    return 0


def cmd_namespaces() -> int:
    """`a8s namespaces` — list every namespace prefix and its bound agent."""
    namespaces = load_namespaces()
    if not namespaces:
        print("(no namespaces — use `a8s namespace <prefix> <agent>` to bind one)")
        return 0
    width = max(len(p) for p in namespaces)
    options = load_namespace_options()
    opaque = {k.lower() for k, o in options.items() if (o or {}).get("opaque")}
    for prefix in sorted(namespaces, key=str.lower):
        bound = namespaces[prefix]
        mark = "  [opaque]" if prefix.lower() in opaque else ""
        print(f"  {prefix.ljust(width)}  -> {bound}{mark}{_namespace_binding_tail(bound)}")
    return 0


# ---------- process control commands ----------

def _expand_to_agents(name: str) -> list[str] | None:
    """Resolve `name` to a flat list of agent names. Returns None on error
    (already-printed usage)."""
    try:
        _, members = resolve_name(name)
    except KeyError:
        print(f"no agent or alias named {name!r}", file=sys.stderr)
        return None
    except ValueError as e:
        print(f"{e}", file=sys.stderr)
        return None
    if not members:
        print(f"{name!r} resolves to no agents", file=sys.stderr)
        return None
    return members


def cmd_run(args: list[str], interval: float) -> int:
    """`a8s run <name> [--drain <seconds>]` — foreground attached loop. <name>
    may be an agent or an alias; aliases produce ONE process that handles every
    member (each member's pid file points at this PID). Ctrl+C: graceful detach.
    2nd Ctrl+C: kills the wake subprocess group.

    --drain <seconds>: connect to MQTT remotes and trash incoming messages for
    the specified duration without invoking. Default 1s when given without a
    value."""
    drain_seconds = 0.0
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--drain":
            i += 1
            if i < len(args) and not args[i].startswith("-"):
                try:
                    drain_seconds = float(args[i])
                except ValueError:
                    print("--drain requires a number (seconds)", file=sys.stderr)
                    return 2
            else:
                drain_seconds = 1.0
                continue
        else:
            filtered.append(args[i])
        i += 1
    if len(filtered) != 1:
        print("usage: a8s run <name> [--drain <seconds>]", file=sys.stderr)
        return 2
    members = _expand_to_agents(filtered[0])
    if members is None:
        return 1
    return attached_loop(members, interval, drain_seconds=drain_seconds)


def cmd_start(args: list[str]) -> int:
    """`a8s start <name>` — spawn ONE detached background process. The child
    runs `a8s run <name>` and (if <name> is an alias) handles every member in
    a single process. Returns the child's PID."""
    if len(args) != 1:
        print("usage: a8s start <name>", file=sys.stderr)
        return 2
    name = args[0]
    # Validate (resolve_name raises if unknown / cycle).
    try:
        _, members = resolve_name(name)
    except KeyError:
        print(f"start: no agent or alias named {name!r}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"start: {e}", file=sys.stderr)
        return 1
    if not members:
        print(f"start: {name!r} resolves to no agents", file=sys.stderr)
        return 1
    if _refuse_bad_wake_shell(members):
        return 1
    _warn_unresolvable_harnesses(members)
    # NOTE: the child must launch the entrypoint script (a8s.py), NOT this
    # commands.py module. That's why core.ENTRYPOINT exists.
    cmd = [sys.executable, str(ENTRYPOINT), "run", name]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if len(members) == 1:
        print(f"started {members[0]} as PID {proc.pid}")
    else:
        print(f"started {name} (alias of {len(members)}) as PID {proc.pid}")
    return 0


def _warn_unresolvable_harnesses(members: list[str]) -> None:
    """Say so at `a8s start` when a node's harness is not on the PATH its wakes
    will get.

    The probe runs against the spawn environment as a wake will see it —
    `definition.env` and the machine-wide `wake_path` already applied — so a
    node that has been given a PATH stops warning, and a node with neither
    inherits the starting shell's PATH and is judged on that. That inheritance
    is the whole footgun: start from `ssh host -- 'a8s start x'`, cron or CI and
    the rc-managed entries are missing, so the harness is unresolvable hours
    later at the first wake while the operator's own shell resolves it fine.

    Probing here rather than at first wake is the point: this process can
    compute exactly the environment the node will get.

    A warning, never a refusal. The definition may name a harness this machine
    installs later, and a node that cannot wake is still worth having attached.
    """
    for member in members:
        try:
            definition = load_definition(member)
        except Exception:
            continue  # a broken definition has its own diagnostics
        try:
            if wake_shell(definition) is not None:
                continue  # the rc decides PATH; nothing here can predict it
            env = {**os.environ, **wake_env(definition)}
        except ValueError:
            continue  # `a8s start` reports a malformed knob separately
        for label, argv in (
            ("invoke", definition.get("invoke")),
            ("idle.invoke", (definition.get("idle") or {}).get("invoke")
             if isinstance(definition.get("idle"), dict) else None),
        ):
            if not isinstance(argv, list) or not argv:
                continue
            program = harness_program([str(a) for a in argv])
            if program is None or "$" in program:
                continue  # a shell string, or a var that expands per wake
            if harness_is_resolvable(program, env):
                continue
            print(
                f"warning: {member}: {program!r} ({label}) is not on the PATH "
                f"this node's wakes will get.\n"
                f"         Set `definition.env` `{{\"PATH\": ...}}` for this node, "
                f"or `a8s config set wake_path \"$PATH\"` from a shell that "
                f"resolves it. `ar3 doctor` lists what it can find.",
                file=sys.stderr,
            )


def _refuse_bad_wake_shell(members: list[str]) -> bool:
    """True when a member's `wake_shell` cannot run here. Printed and refused at
    `a8s start`, because a node started with a knob that silently does nothing
    is the failure this knob exists to end."""
    bad = False
    for member in members:
        try:
            definition = load_definition(member)
        except Exception:
            continue
        try:
            wrap_wake_argv(definition, [])  # the argv is irrelevant; the wrap validates
        except ValueError as e:
            print(f"start: {member}: {e}", file=sys.stderr)
            bad = True
    return bad


def cmd_step(args: list[str], interval: float) -> int:
    """`a8s step <name>` — attach as handler, one route+drain pass, release.
    Aliases handled in a single process: one acquire across all members, one
    pass, one release."""
    if len(args) != 1:
        print("usage: a8s step <name>", file=sys.stderr)
        return 2
    members = _expand_to_agents(args[0])
    if members is None:
        return 1
    return attached_loop(members, interval, single_pass=True)


STOP_WAIT_S = 600.0
STOP_FORCE_WAIT_S = 30.0
STOP_POLL_S = 0.1


def _split_force_flag(args: list[str]) -> tuple[list[str], bool]:
    force = False
    rest: list[str] = []
    for a in args:
        if a in ("--force", "-f"):
            force = True
        else:
            rest.append(a)
    return rest, force


def _handlers_still_holding(members: list[str], signaled_pids: set[int]) -> bool:
    """True while any member is still attached to one of the signaled PIDs,
    or any of those PIDs is still alive (shutting down after release)."""
    for name in members:
        pid = _read_handler_pid(name)
        if pid is not None and pid in signaled_pids and _pid_alive(pid):
            return True
    return any(_pid_alive(pid) for pid in signaled_pids)


def _wait_handlers_stopped(
    members: list[str],
    signaled_pids: set[int],
    timeout: float,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _handlers_still_holding(members, signaled_pids):
            return True
        time.sleep(STOP_POLL_S)
    return not _handlers_still_holding(members, signaled_pids)


def cmd_stop(args: list[str]) -> int:
    """`a8s stop <name> [--force]` — SIGTERM the handler(s), then wait until
    they have actually detached.

    Like Ctrl+C on `a8s run`: the first signal asks for a graceful detach
    after the current wake. Idle nodes stop immediately; a busy wake finishes
    first. ``--force`` / ``-f`` sends a second SIGTERM so the daemon kills the
    in-flight wake subprocess group (same as a second Ctrl+C), then waits.

    One handler may serve multiple alias members; we dedupe by PID so each
    unique handler is signaled once. Detaches the WHOLE handler.
    """
    rest, force = _split_force_flag(args)
    if len(rest) != 1:
        print("usage: a8s stop <name> [--force]", file=sys.stderr)
        return 2
    members = _expand_to_agents(rest[0])
    if members is None:
        return 1
    seen_pids: dict[int, str] = {}
    not_running: list[str] = []
    for name in members:
        pid = _read_handler_pid(name)
        if pid is None:
            not_running.append(name)
            continue
        if pid not in seen_pids:
            seen_pids[pid] = name
    if not seen_pids:
        for n in not_running:
            print(f"{n}: not running", file=sys.stderr)
        return 1
    for pid, label in seen_pids.items():
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"{label}: sent SIGTERM to PID {pid}")
        except OSError as e:
            print(f"{label}: could not signal PID {pid}: {e}", file=sys.stderr)
    if force:
        time.sleep(0.05)
        for pid, label in seen_pids.items():
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"{label}: sent second SIGTERM (force) to PID {pid}")
            except ProcessLookupError:
                pass
            except OSError as e:
                print(f"{label}: could not signal PID {pid}: {e}", file=sys.stderr)
    wait_s = STOP_FORCE_WAIT_S if force else STOP_WAIT_S
    print(f"waiting up to {wait_s:g}s for stop…")
    if not _wait_handlers_stopped(members, set(seen_pids), wait_s):
        print(
            f"still running after {wait_s:g}s"
            + ("" if force else " — try `a8s stop --force` or `a8s kill`"),
            file=sys.stderr,
        )
        return 1
    for n in members:
        if n not in not_running:
            print(f"{n}: stopped")
    for n in not_running:
        print(f"{n}: not running")
    return 0


def cmd_restart(args: list[str]) -> int:
    """`a8s restart <name> [--force]` — stop (wait until detached) then start.

    If the node is not running, skip straight to start. ``--force`` is passed
    through to stop so an in-flight wake is interrupted.
    """
    rest, force = _split_force_flag(args)
    if len(rest) != 1:
        print("usage: a8s restart <name> [--force]", file=sys.stderr)
        return 2
    name = rest[0]
    members = _expand_to_agents(name)
    if members is None:
        return 1
    any_running = any(_read_handler_pid(n) is not None for n in members)
    if any_running:
        stop_args = [name]
        if force:
            stop_args.append("--force")
        rc = cmd_stop(stop_args)
        if rc != 0:
            return rc
    return cmd_start([name])


def _running_nodes_by_pid() -> dict[int, list[str]]:
    """Map handler PID → agent names currently attached to that process."""
    reg = load_registry()
    by_pid: dict[int, list[str]] = {}
    for name in sorted(reg, key=str.lower):
        pid = _read_handler_pid(name)
        if pid is None:
            continue
        by_pid.setdefault(pid, []).append(name)
    return by_pid


def _update_restart_targets(by_pid: dict[int, list[str]]) -> list[str]:
    """One restart target per live handler.

    Prefer an alias whose resolved members exactly match the agents sharing a
    PID (so ``a8s start devs`` comes back as one process). Otherwise restart
    each agent name on its own.
    """
    aliases = load_aliases()
    targets: list[str] = []
    claimed: set[str] = set()
    for _pid, names in sorted(by_pid.items(), key=lambda kv: kv[1][0].lower()):
        name_set = {n.lower() for n in names}
        if name_set & claimed:
            continue
        alias_hit: str | None = None
        for alias in sorted(aliases, key=str.lower):
            try:
                _, members = resolve_name(alias)
            except (KeyError, ValueError):
                continue
            if {m.lower() for m in members} == name_set:
                alias_hit = alias
                break
        if alias_hit is not None:
            targets.append(alias_hit)
            claimed |= name_set
        else:
            for n in names:
                if n.lower() not in claimed:
                    targets.append(n)
                    claimed.add(n.lower())
    return targets


def cmd_update(args: list[str]) -> int:
    """`a8s update [--force]` — housekeep state and restart every running node.

    v1: no code pull — stop+start so background handlers re-exec the current
    on-disk ``a8s`` entrypoint. Use after ``git pull``. Later iterations may
    fetch standalone releases.

    Groups agents that share a handler PID: if they match an alias exactly,
    that alias is restarted as one process; otherwise each agent is restarted.
    ``--force`` / ``-f`` is passed through to stop.
    """
    rest, force = _split_force_flag(args)
    if rest:
        print("usage: a8s update [--force]", file=sys.stderr)
        return 2
    from convo import ConversationArchiveError, prune_conversations
    from settings import get_int
    from txlog import TransactionLogError, prune_transactions

    try:
        pruned = prune_conversations()
    except ConversationArchiveError as e:
        print(f"update: conversation housekeeping failed: {e}", file=sys.stderr)
        return 1
    max_rows = get_int("convo_max_rows")
    print(
        f"conversation housekeeping: retained up to {max_rows} row(s), "
        f"pruned {pruned}"
    )
    try:
        pruned = prune_transactions()
    except TransactionLogError as e:
        print(f"update: transaction housekeeping failed: {e}", file=sys.stderr)
        return 1
    max_rows = get_int("txlog_max_rows")
    print(
        f"transaction housekeeping: retained up to {max_rows} row(s), "
        f"pruned {pruned}"
    )
    by_pid = _running_nodes_by_pid()
    if not by_pid:
        print("no nodes running")
        return 0
    targets = _update_restart_targets(by_pid)
    n_agents = sum(len(v) for v in by_pid.values())
    print(f"updating {n_agents} node(s) via {len(targets)} restart(s)…")
    rc = 0
    for target in targets:
        restart_args = [target]
        if force:
            restart_args.append("--force")
        print(f"--- {target} ---")
        r = cmd_restart(restart_args)
        if r != 0:
            print(f"update: {target} failed (rc={r})", file=sys.stderr)
            rc = r
    if rc == 0:
        print("update complete")
    return rc


KILL_TIMEOUT_S = 10.0
KILL_POLL_S = 0.1


def cmd_kill(args: list[str]) -> int:
    """`a8s kill <name>` — per-agent force-detach. For each member, write
    a kill-request file and SIGUSR1 the holder; the holder's iteration top
    releases just that agent (and its SIGUSR1 handler kills any in-flight
    wake subprocess group iff the current wake target matches), so siblings
    keep running. Falls back to whole-process SIGTERM if the holder doesn't
    honor the request within KILL_TIMEOUT_S — that's the only path that
    still creates collateral, and it's the user's explicit force escalation."""
    if len(args) != 1:
        print("usage: a8s kill <name>", file=sys.stderr)
        return 2
    members = _expand_to_agents(args[0])
    if members is None:
        return 1
    rc = 0
    for name in members:
        holder = _read_handler_pid(name)
        if holder is None:
            print(f"{name}: not running")
            continue
        _write_kill_request(name, os.getpid())
        print(f"{name}: kill request → PID {holder}")
        try:
            os.kill(holder, signal.SIGUSR1)
        except ProcessLookupError:
            _clear_kill_request(name)
            continue
        deadline = time.time() + KILL_TIMEOUT_S
        released = False
        while time.time() < deadline:
            if not pid_path(name).is_file():
                released = True
                break
            time.sleep(KILL_POLL_S)
        if not released:
            print(
                f"{name}: holder PID {holder} did not honor kill within {KILL_TIMEOUT_S}s — "
                f"escalating to whole-process SIGTERM",
                file=sys.stderr,
            )
            try:
                os.kill(holder, signal.SIGTERM)
            except ProcessLookupError:
                pass
            rc = 1
        _clear_kill_request(name)
    return rc


def cmd_exit() -> int:
    """`a8s exit` — SIGTERM every running agent's handler. Each daemon
    detaches gracefully on its own."""
    parts = participants_from_registry()
    sent = 0
    for p in parts:
        pid = _read_handler_pid(p.name)
        if pid is None:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"{p.name}: SIGTERM PID {pid}")
            sent += 1
        except OSError as e:
            print(f"{p.name}: could not signal PID {pid}: {e}", file=sys.stderr)
    if sent == 0:
        print("no agents running")
    return 0


def cmd_ps(args: list[str] | None = None) -> int:
    """`a8s ps` — list only running node processes (docker/ollama style).
    Columns: NAME, PID, UPTIME, ROOT. `-q` prints just names, one per line."""
    args = args or []
    quiet = "-q" in args
    reg = load_registry()
    running: list[tuple[str, int, str]] = []
    for name in sorted(reg, key=str.lower):
        pid = _read_handler_pid(name)
        if pid is None:
            continue
        running.append((name, pid, reg[name].get("root", "?")))

    if not running:
        if not quiet:
            print("no nodes running (try: a8s ls)")
        return 0

    if quiet:
        for name, _, _ in running:
            print(name)
        return 0

    rows = [(name, str(pid), _pid_uptime(name), root) for name, pid, root in running]
    _print_table(["NAME", "PID", "UPTIME", "ROOT"], rows)
    return 0


# ---------- messaging commands ----------

def cmd_tell(args: list[str]) -> int:
    """`a8s tell <name> <msg>` — write a single outbox message; `name` may be
    an agent or alias. Fan-out to alias members happens at routing time and
    preserves the original `to` (alias name) — strict opacity, mailing-list
    style: the recipient knows it came via the list, not who else got it."""
    from tell import tell_main

    return tell_main(args)


def cmd_tells(args: list[str]) -> int:
    """`a8s tells [-f|--follow] [--timeout SEC]` — block until the next message
    lands in this node's inbox, print each new envelope, and exit 0; exit 1 on
    timeout. With `-f`, poll continuously until interrupted."""
    from tells import tells_main

    return tells_main(args)


# ---------- drain ----------

def cmd_drain(args: list[str]) -> int:
    """`a8s drain <name>` — move all inbox messages to trash without invoking.
    Prints a summary of each drained message."""
    if len(args) != 1:
        print("usage: a8s drain <name>", file=sys.stderr)
        return 2
    match = resolve_recipient(args[0])
    if match is None:
        print(f"no agent named {args[0]!r}", file=sys.stderr)
        return 1
    name = match[0]
    inbox = inbox_dir(name)
    trash = trash_dir(name)
    if not inbox.is_dir():
        print(f"no inbox for {name!r}", file=sys.stderr)
        return 1
    trash.mkdir(parents=True, exist_ok=True)

    files = sorted(f for f in inbox.iterdir() if f.is_file() and f.name.endswith(".json"))
    if not files:
        print(f"{name}: inbox empty")
        return 0

    count = 0
    for f in files:
        try:
            msg = json.loads(f.read_text())
            sender = msg.get("from", "?")
            content = msg.get("content", "")
            preview = content.replace("\n", " ")[:80]
            print(f"  {sender}: {preview}")
        except Exception:
            print(f"  (unreadable: {f.name})")
        dest = unique_path(trash / f.name)
        f.rename(dest)
        count += 1

    print(f"{name}: drained {count} message(s)")
    return 0


# ---------- config ----------

def cmd_config(args: list[str]) -> int:
    """`a8s config` — read or write `~/.config/a8s/settings.json`; list all knobs."""
    import settings as sm

    if not args:
        machine = {r[0]: r for r in sm.list_settings()}
        for group_label, knobs in sm.list_catalog():
            print(f"\n{group_label}")
            for knob in knobs:
                if knob.writable:
                    _key, _stored, effective, default, source = machine[knob.key]
                    print(f"  {knob.key}: {effective}  ({source}; default {default})")
                    if knob.note:
                        print(f"    {knob.note}")
                else:
                    default = knob.default if knob.default is not None else "—"
                    print(f"  {knob.key}: {default}")
                    if knob.note:
                        print(f"    {knob.note}")
        print()
        return 0

    sub = args[0]
    if sub == "get":
        if len(args) != 2:
            print("usage: a8s config get <key>", file=sys.stderr)
            return 2
        key = args[1]
        if sm.is_writable(key):
            print(sm.get_setting(key))
            return 0
        knob = sm.knob_by_key(key)
        if knob is not None:
            val = knob.default if knob.default is not None else ""
            print(val)
            if knob.note:
                print(f"({knob.note})", file=sys.stderr)
            return 0
        print(f"unknown setting {key!r}", file=sys.stderr)
        return 1

    if sub == "set":
        if len(args) != 3:
            print("usage: a8s config set <key> <value>", file=sys.stderr)
            return 2
        try:
            sm.set_setting(args[1], args[2])
        except KeyError:
            print(f"unknown setting {args[1]!r}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"{args[1]}={sm.get_setting(args[1])}")
        return 0

    if sub == "unset":
        if len(args) != 2:
            print("usage: a8s config unset <key>", file=sys.stderr)
            return 2
        try:
            removed = sm.unset_setting(args[1])
        except KeyError:
            print(f"unknown setting {args[1]!r}", file=sys.stderr)
            return 1
        if not removed:
            print(f"{args[1]}: not set in settings.json")
        else:
            print(f"{args[1]} unset (effective {sm.get_setting(args[1])})")
        return 0

    print(
        "usage: a8s config [get <key> | set <key> <value> | unset <key>]",
        file=sys.stderr,
    )
    return 2


# ---------- convo ----------

def cmd_convo(args: list[str]) -> int:
    """`a8s convo <name> [--limit N] [-f|--follow] [--from NAME] [--glow [theme]]
    [--heading-out T] [--heading-in T]` — markdown history of messages to or from
    an agent."""
    import argparse

    from convo import (
        DEFAULT_HEADING_IN,
        DEFAULT_HEADING_OUT,
        convo_help_epilog,
        decode_template,
        follow_conversation,
        format_conversation,
        load_agent_entries,
        open_glow_stdout,
        print_entries,
    )

    default_glow = os.environ.get("A8S_GLOW", "").strip() or None
    parser = argparse.ArgumentParser(
        prog="a8s convo",
        description="Show markdown conversation history for an agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=convo_help_epilog(),
    )
    parser.add_argument("name", help="registered agent name")
    parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="print backlog then follow new rows in conversations.sqlite3",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="number of recent messages to show (default: 10)",
    )
    parser.add_argument(
        "--from",
        dest="senders",
        action="append",
        metavar="NAME",
        help="only messages sent by NAME (repeat for several senders)",
    )
    parser.add_argument(
        "--glow",
        nargs="?",
        const="auto",
        default=default_glow,
        metavar="THEME",
        help="render via glow (theme: auto, dark, light, dracula, …; default from A8S_GLOW)",
    )
    parser.add_argument(
        "--heading-out",
        nargs="+",
        metavar="LINE",
        help="outbound heading template; multiple LINEs join with newlines",
    )
    parser.add_argument(
        "--heading-in",
        nargs="+",
        metavar="LINE",
        help="inbound heading template; multiple LINEs join with newlines",
    )
    try:
        parsed = parser.parse_args(args)
    except SystemExit as e:
        return int(e.code if e.code is not None else 0)
    if parsed.limit < 1:
        print("a8s convo: --limit must be a positive integer", file=sys.stderr)
        return 2

    heading_out = (
        decode_template("\n".join(parsed.heading_out))
        if parsed.heading_out is not None
        else DEFAULT_HEADING_OUT
    )
    heading_in = (
        decode_template("\n".join(parsed.heading_in))
        if parsed.heading_in is not None
        else DEFAULT_HEADING_IN
    )
    glow_theme = parsed.glow

    match = resolve_recipient(parsed.name)
    if match is None:
        print(f"no agent named {parsed.name!r}", file=sys.stderr)
        return 1
    agent_name = match[0]

    if parsed.follow:
        try:
            follow_conversation(
                agent_name,
                limit=parsed.limit,
                heading_out=heading_out,
                heading_in=heading_in,
                glow_theme=glow_theme,
                senders=parsed.senders,
            )
        except KeyboardInterrupt:
            pass
        return 0

    rows = load_agent_entries(agent_name, limit=parsed.limit, senders=parsed.senders)
    if glow_theme is not None:
        glow_stream = None
        try:
            glow_stream = open_glow_stdout(glow_theme)
        except FileNotFoundError:
            print("a8s convo: glow not found on PATH", file=sys.stderr)
        try:
            print_entries(
                agent_name,
                rows,
                glow_stream=glow_stream,
                heading_out=heading_out,
                heading_in=heading_in,
            )
        finally:
            if glow_stream is not None:
                glow_stream.close()
        return 0

    text = format_conversation(
        agent_name,
        limit=parsed.limit,
        heading_out=heading_out,
        heading_in=heading_in,
        senders=parsed.senders,
    )
    if text:
        print(text)
    return 0


# ---------- transactions / trace / logs ----------

def _format_tx(event: dict[str, str], *, show_id: bool = True) -> str:
    fields = [event["timestamp"], event["event"]]
    if show_id and event["msg_id"]:
        fields.append(event["msg_id"])
    for key in ("from", "to", "remote", "files", "detail"):
        if event[key]:
            fields.append(f"{key}={event[key]}")
    return " ".join(fields)


def cmd_transactions(args: list[str]) -> int:
    """`a8s transactions [--limit N] [-f] [--event E] [--from N] [--to N] [--msg ULID]`
    — recent routing events across every message."""
    import argparse
    import time

    from txlog import EVENTS, read_recent

    parser = argparse.ArgumentParser(
        prog="a8s transactions",
        description="Show recent routing events. Alias: a8s tx.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "events:\n  " + "\n  ".join(EVENTS) + "\n\n"
            "examples:\n"
            "  a8s tx --limit 40\n"
            "  a8s tx -f --event DISCARDED --event FILE_UPLOAD_FAILED\n"
            "  a8s tx --from neil-phone --to ares\n\n"
            "`a8s trace <ULID>` follows one envelope end to end; this is the view\n"
            "for when you do not have a ULID yet. Retention is `txlog_max_rows`.\n"
        ),
    )
    parser.add_argument(
        "-f", "--follow", action="store_true", help="print recent rows, then new ones as they land"
    )
    parser.add_argument(
        "--limit", type=int, default=20, metavar="N", help="rows to show (default: 20)"
    )
    parser.add_argument(
        "--event", dest="events", action="append", metavar="E",
        help="only this event type (repeat for several)",
    )
    parser.add_argument(
        "--from", dest="senders", action="append", metavar="NAME",
        help="only messages sent by NAME (repeat for several)",
    )
    parser.add_argument(
        "--to", dest="recipients", action="append", metavar="NAME",
        help="only messages addressed to NAME (repeat for several)",
    )
    parser.add_argument("--msg", default="", metavar="ULID", help="only this envelope")
    try:
        parsed = parser.parse_args(args)
    except SystemExit as e:
        return int(e.code if e.code is not None else 0)
    if parsed.limit < 1:
        print("a8s transactions: --limit must be a positive integer", file=sys.stderr)
        return 2
    known = {e.lower() for e in EVENTS}
    unknown = [e for e in (parsed.events or []) if e.strip().lower() not in known]
    if unknown:
        print(
            f"a8s transactions: unknown event {unknown[0]!r} "
            f"(known: {', '.join(EVENTS)})",
            file=sys.stderr,
        )
        return 2
    msg_id = parsed.msg.upper() if parsed.msg else ""

    filters = {
        "events": parsed.events,
        "senders": parsed.senders,
        "recipients": parsed.recipients,
        "msg_id": msg_id,
    }
    rows = read_recent(limit=parsed.limit, **filters)
    for _seq, event in rows:
        print(_format_tx(event), flush=True)
    if not parsed.follow:
        if not rows:
            narrowed = any(filters.values())
            print(
                "no matching transaction events" if narrowed else "no transaction events",
                file=sys.stderr,
            )
            return 1
        return 0

    # An empty filtered backlog must still start from the table's high-water
    # mark, or the first poll replays every row that predates the command.
    if rows:
        cursor = rows[-1][0]
    else:
        newest = read_recent(limit=1)
        cursor = newest[-1][0] if newest else 0
    try:
        while True:
            time.sleep(1.0)
            fresh = read_recent(after_seq=cursor, **filters)
            for seq, event in fresh:
                print(_format_tx(event), flush=True)
                cursor = seq
    except KeyboardInterrupt:
        pass
    return 0


def cmd_trace(args: list[str]) -> int:
    if len(args) != 1 or not is_ulid(args[0]):
        print("usage: a8s trace <ULID>", file=sys.stderr)
        return 2
    msg_id = args[0].upper()
    events = read_events(msg_id)
    if not events:
        print(f"no transaction events for {msg_id}", file=sys.stderr)
        return 1
    print(f"trace {msg_id}")
    for event in events:
        print("  " + _format_tx(event, show_id=False))
    return 0


# ---------- logs ----------

def _parse_log_line_ts(line: str) -> datetime | None:
    if not line:
        return None
    head = line.split(" ", 1)[0]
    if head.endswith("Z"):
        head = head[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(head)
    except ValueError:
        return None


def _read_agent_log(path: Path) -> list[str]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


def _merge_log_lines(paths: list[Path]) -> list[str]:
    tagged: list[tuple[tuple, str]] = []
    for fi, path in enumerate(paths):
        for li, line in enumerate(_read_agent_log(path)):
            ts = _parse_log_line_ts(line)
            key = (0, ts, fi, li) if ts is not None else (1, fi, li)
            tagged.append((key, line))
    tagged.sort(key=lambda item: item[0])
    return [line for _key, line in tagged]


def _dump_logs(paths: list[Path], tail_n: int | None) -> None:
    existing = [p for p in paths if p.is_file()]
    if not existing:
        return
    lines = _read_agent_log(existing[0]) if len(existing) == 1 else _merge_log_lines(existing)
    if tail_n is not None:
        lines = lines[-tail_n:]
    for line in lines:
        sys.stdout.write(line)
    sys.stdout.flush()


def cmd_logs(args: list[str]) -> int:
    """Read each named agent's log.txt. One agent: append order (file order).
    Multiple agents: merge by leading ISO timestamp. -f follows; multi-agent
    follow uses a short ordering buffer."""
    if not args:
        print("usage: a8s logs <name> [<name>...] [--tail N] [-f|--follow]", file=sys.stderr)
        return 2
    names: list[str] = []
    tail_n: int | None = None
    follow = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--follow"):
            follow = True
            i += 1
        elif a == "--tail" and i + 1 < len(args):
            try:
                tail_n = int(args[i + 1])
            except ValueError:
                print(f"--tail: not an integer: {args[i + 1]!r}", file=sys.stderr)
                return 2
            i += 2
        elif a.startswith("--tail="):
            try:
                tail_n = int(a.split("=", 1)[1])
            except ValueError:
                print(f"--tail: not an integer: {a!r}", file=sys.stderr)
                return 2
            i += 1
        elif a.startswith("-"):
            print(f"unknown logs arg: {a!r}", file=sys.stderr)
            return 2
        else:
            names.append(a)
            i += 1

    if not names:
        print("usage: a8s logs <name> [<name>...] [--tail N] [-f|--follow]", file=sys.stderr)
        return 2

    # Expand aliases. Names may include agents and aliases; dedupe agent names
    # (an agent listed twice via overlapping aliases shouldn't double up).
    expanded: list[str] = []
    seen: set[str] = set()
    for n in names:
        try:
            _, members = resolve_name(n)
        except KeyError:
            print(f"logs: no agent or alias named {n!r}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"logs: {e}", file=sys.stderr)
            return 1
        for m in members:
            if m.lower() not in seen:
                seen.add(m.lower())
                expanded.append(m)

    paths = [agent_log_path(n) for n in expanded]
    missing = [p for p in paths if not p.is_file()]
    if len(missing) == len(paths):
        for p in missing:
            print(f"no log yet at {p}", file=sys.stderr)
        return 1

    # Initial dump: one file in append order; multiple files merge by timestamp.
    _dump_logs(paths, tail_n)

    if not follow:
        return 0

    handles: list[tuple[int, Path, "os.IOBase"]] = []
    try:
        for fi, p in enumerate(paths):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch(exist_ok=True)
            f = p.open("r", encoding="utf-8", errors="replace")
            f.seek(0, 2)
            handles.append((fi, p, f))
        if len(handles) == 1:
            try:
                while True:
                    ln = handles[0][2].readline()
                    if not ln:
                        time.sleep(0.25)
                        continue
                    sys.stdout.write(ln)
                    sys.stdout.flush()
            except KeyboardInterrupt:
                return 0
        buf: list[tuple[tuple, str]] = []
        seq = 0
        last_emit = time.time()
        try:
            while True:
                progress = False
                for fi, _path, f in handles:
                    while True:
                        ln = f.readline()
                        if not ln:
                            break
                        ts = _parse_log_line_ts(ln)
                        key = (0, ts, fi, seq) if ts is not None else (1, fi, seq)
                        buf.append((key, ln))
                        seq += 1
                        progress = True
                now = time.time()
                if buf and (not progress or now - last_emit >= 1.0):
                    buf.sort(key=lambda item: item[0])
                    for _key, ln in buf:
                        sys.stdout.write(ln)
                    sys.stdout.flush()
                    buf.clear()
                    last_emit = now
                if not progress:
                    time.sleep(0.25)
        except KeyboardInterrupt:
            if buf:
                buf.sort(key=lambda item: item[0])
                for _key, ln in buf:
                    sys.stdout.write(ln)
                sys.stdout.flush()
            return 0
    finally:
        for _fi, _p, f in handles:
            try:
                f.close()
            except Exception:
                pass


# ---------- remotes ----------

_REMOTE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SECRET_KEYS = {"pass", "password"}


def _remote_usage() -> int:
    print(
        "usage: a8s remote                                         # list all\n"
        "       a8s remote <name>                                  # show one\n"
        "       a8s remote <name> <broker> <topic> [--<k> <v> ...]   # add or overwrite\n"
        "       a8s unremote <name>                                # remove\n"
        "\n"
        "Any option past the broker and topic is passed verbatim to the\n"
        "transport (e.g. --user / --pass for mqtt). Either spelling works,\n"
        "--user alice or --user=alice, and dashes and underscores in an option\n"
        "name are equivalent. --pass/--password are stored in secrets.json (not\n"
        "network.json). Unknown options are rejected by the transport at load\n"
        "time.",
        file=sys.stderr,
    )
    return 2


def _format_remote_summary(spec: dict) -> str:
    kind = spec.get("transport", "?")
    broker = spec.get("broker", "?")
    topic = spec.get("topic", "?")
    extras = " ".join(
        f"--{k}=***" if k in _SECRET_KEYS else f"--{k}={v}"
        for k, v in spec.items()
        if k not in {"transport", "broker", "topic"}
    )
    line = f"{kind} {broker} topic={topic}"
    if extras:
        line += f" {extras}"
    return line


def cmd_remote(args: list[str]) -> int:
    """`a8s remote` — manage cross-cluster remotes.

    Non-secret fields live in ``network.json``; ``pass`` / ``password`` go to
    ``secrets.json`` (mode 0600). One ``a8s remote … --pass …`` call writes
    both — ``--pass`` is optional.

    Forms (mirror `a8s alias`):
      a8s remote                                          list all
      a8s remote <name>                                   show one
      a8s remote <name> <broker> <topic> [--<k> <v> ...]  add or overwrite
      a8s unremote <name>                                 remove (see `cmd_unremote`)
    """
    if len(args) == 0:
        return _cmd_remote_list()
    if len(args) == 1:
        return _cmd_remote_show(args[0])
    if len(args) >= 3:
        return _cmd_remote_set(args[0], args[1], args[2], args[3:])
    return _remote_usage()


def _cmd_remote_list() -> int:
    cfg = load_network_config()
    remotes = cfg.get("remotes", {})
    if not remotes:
        print("(no remotes configured)")
        return 0
    name_w = max(len(n) for n in remotes)
    for name, spec in remotes.items():
        if not isinstance(spec, dict):
            continue
        print(f"  {name.ljust(name_w)}  {_format_remote_summary(merge_remote_secrets(name, spec))}")
    return 0


def _cmd_remote_show(name: str) -> int:
    cfg = load_network_config()
    if name not in cfg["remotes"]:
        print(f"no remote named {name!r}", file=sys.stderr)
        return 1
    spec = cfg["remotes"][name]
    if not isinstance(spec, dict):
        print(f"remote {name!r} config is not an object", file=sys.stderr)
        return 1
    print(f"{name}: {_format_remote_summary(merge_remote_secrets(name, spec))}")
    return 0


def _cmd_remote_set(name: str, broker: str, topic: str, opt_tokens: list[str]) -> int:
    if not _REMOTE_NAME_RE.match(name):
        print(f"remote name must be alphanumeric (with -, _, .): {name!r}", file=sys.stderr)
        return 2
    try:
        extras = parse_option_tokens(opt_tokens)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return _remote_usage()
    cfg = load_network_config()
    overwriting = name in cfg["remotes"]
    public, secrets = split_secret_keys(
        {"transport": "mqtt", "broker": broker, "topic": topic, **extras}
    )
    cfg["remotes"][name] = public
    save_network_config(cfg)
    put_remote_secrets(name, secrets)
    verb = "updated" if overwriting else "added"
    print(f"{verb} remote {name} ({_format_remote_summary(merge_remote_secrets(name, public))})")
    return 0


def cmd_unremote(args: list[str]) -> int:
    """`a8s unremote <name>` — remove a configured remote. Mirrors `unalias`'s
    shape so the surface stays uniform across registry primitives."""
    if len(args) != 1:
        print("usage: a8s unremote <name>", file=sys.stderr)
        return 2
    name = args[0]
    cfg = load_network_config()
    if name not in cfg["remotes"]:
        print(f"no remote named {name!r}", file=sys.stderr)
        return 1
    del cfg["remotes"][name]
    save_network_config(cfg)
    delete_remote_secrets(name)
    print(f"removed remote {name}")
    return 0


# ---------- storage services ----------


_STORAGE_HELP = """\
usage: a8s storage                                # list all
       a8s storage <name>                         # show one
       a8s storage <name> <url> [--<k> <v> ...]   # add or overwrite
       a8s unstorage <name>                       # remove

Storage services carry attachment bytes between machines. A file attached to a
message is uploaded to EVERY configured service, and every resulting URL rides
along in the envelope — the receiver tries them in turn, so a service blocked
on one network does not lose the file. Receivers fetch public URLs with a plain
HTTP GET and need no credentials or storage config of their own.

The kind is auto-dispatched from the URL scheme. Options take either spelling,
--user alice or --user=alice. Dashes and underscores in an option name are
equivalent (--base-url == --base_url), and --pass is accepted for --password. Give --prefix an empty value to put objects at the top of the
configured path when that path is already dedicated to a8s.

  KIND          URL                              OPTIONS
  tempfile_org  https://tempfile.org             --expiry_hours (1|6|24|48, default 24)
                                                 --timeout_s (30)
  s3            s3://<bucket>/<prefix>           --region --profile --endpoint_url
                                                 --prefix (a8s) --presign_hours (24)
                                                 --timeout_s (60)
  file_sync     file:///<abs-path>               --base-url (REQUIRED) --prefix (a8s)
  webdav        webdav://<host>/<path>           --base-url (REQUIRED) --user --password
                                                 --prefix (a8s) --timeout_s (60)
  rclone        rclone://<remote>/<path>         --prefix (a8s) --timeout_s (300)
                                                 --rclone_path (rclone)
  sync_folder   <a local folder path>            --prefix (none) --retain_days (off)

URLs must be https. A peer picks the URL your node downloads from, and these
links carry their own authorization in the query string. A download follows at
most 3 redirects, and each hop obeys the same rule. Set storage_allow_http only
for a store on your own network with no certificate.

  a8s storage scratch https://tempfile.org --expiry_hours 24
  a8s storage bucket s3://my-bucket/a8s --region us-west-2 --profile ops
  a8s storage drive file:///home/me/Drive/a8s --base-url https://cdn.example/a8s
  a8s storage fm webdav://webdav.fastmail.com/dav/fs/user@domain/a8s \\
      --base-url https://files.example.com/a8s --user user@domain --password ...
  a8s storage drive rclone://gdrive/A8S
  a8s storage onedrive "~/OneDrive - Contoso/A8S" --retain_days 30

sync_folder is the desktop and laptop answer: point it at a folder your sync
client already watches, point a second machine at the same folder, and the
bytes cross by themselves. Nothing is published — no host, no credential, and
no URL that resolves for anyone outside the folder. The marker that rides in
the envelope names neither the service nor the path, so configure two folders
and whichever syncs first delivers the file. Attachments are keyed by message
ULID, so one message's files stay together. Set --retain_days to sweep old
bundles; it is off by default because deleting from one machine deletes from
all of them. Use rclone instead on headless and VM machines, which have no
sync client to ride along with.

file_sync copies into a folder some other tool already syncs and hands out the
public URL the object lands at, so a8s does no syncing of its own. It requires
a store whose public URL is derivable from the path: a webserver or CDN over
the synced directory, `rclone serve`, a Nextcloud public folder. It does NOT
work with Google Drive, OneDrive or Dropbox, which mint an opaque per-file id
at upload time that no path can predict.

webdav PUTs directly and is for stores whose upload host and public host
differ. Both need --base-url: the public https prefix a receiver downloads
from, and `<base-url>/<prefix>/<token>/<filename>` must resolve.

rclone is the answer for Google Drive and anything else that mints an opaque
per-file id: it uploads with `rclone copyto` and asks for the public URL with
`rclone link`, using the remote you already configured. Both calls are
synchronous, so nothing waits on a sync daemon. The uploader needs rclone; the
receiver still needs nothing. a8s must be able to read the rclone config of the
user it runs as, and only backends with a known direct-download URL are
accepted — Drive today — because storing a backend's preview page as the
attachment would be silent corruption.

s3 needs boto3 (pip install -r requirements/a8s-s3.txt) on the uploader only;
uploads return presigned GET URLs.

--password is written to secrets.json (mode 0600), never network.json.
Config is validated here — a bad option fails now, not at daemon start.

See also: a8s health (probe every service), a8s config (attachment knobs)."""


def _storage_usage(*, explicit: bool = False) -> int:
    """Usage text. `explicit` means the user asked (`--help`): stdout, exit 0.
    Otherwise it is a usage error: stderr, exit 2."""
    print(_STORAGE_HELP, file=sys.stdout if explicit else sys.stderr)
    return 0 if explicit else 2


_HELP_FLAGS = {"-h", "--help", "help"}


def _format_storage_summary(spec: dict) -> str:
    kind = spec.get("service", "?")
    url = spec.get("url", "?")
    extras = " ".join(
        f"--{k}=***" if k in _SECRET_KEYS else f"--{k}={v}"
        for k, v in spec.items()
        if k not in {"service", "url"}
    )
    line = f"{kind} {url}"
    if extras:
        line += f" {extras}"
    return line


# `a8s remote` spells the credential `--pass`, so an operator who has typed
# one command reasonably types the other. Accept both here.
_STORAGE_OPT_ALIASES = {"pass": "password"}


def cmd_storage(args: list[str]) -> int:
    """`a8s storage` — manage cross-cluster file services declared in
    `~/.config/a8s/network.json` (services map).

    Forms (mirror `a8s remote`):
      a8s storage                                 list all
      a8s storage <name>                          show one
      a8s storage <name> <url> [--<k> <v> ...]    add or overwrite
      a8s unstorage <name>                        remove (see `cmd_unstorage`)
    """
    if args and args[0] in _HELP_FLAGS:
        return _storage_usage(explicit=True)
    if len(args) == 0:
        return _cmd_storage_list()
    if len(args) == 1:
        return _cmd_storage_show(args[0])
    return _cmd_storage_set(args[0], args[1], args[2:])


def _cmd_storage_list() -> int:
    cfg = load_network_config()
    services = cfg.get("services", {})
    if not services:
        print("(no storage services configured)")
        return 0
    name_w = max(len(n) for n in services)
    for name, spec in services.items():
        summary = _format_storage_summary(merge_spec_secrets("services", name, spec))
        print(f"  {name.ljust(name_w)}  {summary}")
    return 0


def _cmd_storage_show(name: str) -> int:
    cfg = load_network_config()
    if name not in cfg["services"]:
        print(f"no storage named {name!r}", file=sys.stderr)
        return 1
    spec = merge_spec_secrets("services", name, cfg["services"][name])
    print(f"{name}: {_format_storage_summary(spec)}")
    return 0


def _cmd_storage_set(name: str, url: str, opt_tokens: list[str]) -> int:
    if not _REMOTE_NAME_RE.match(name):
        print(f"storage name must be alphanumeric (with -, _, .): {name!r}", file=sys.stderr)
        return 2
    try:
        extras = parse_option_tokens(opt_tokens, aliases=_STORAGE_OPT_ALIASES)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return _storage_usage()
    kind = detect_service_kind(url)
    if kind is None:
        print(
            f"no storage service matches URL {url!r} (known kinds: tempfile_org, s3, "
            "file_sync, webdav, rclone, sync_folder)",
            file=sys.stderr,
        )
        return 2
    spec: dict = {"service": kind, "url": url, **extras}
    # Build it now so a typo'd option or a missing --base-url fails here rather
    # than as a skipped service at daemon start.
    try:
        build_service(name, spec)
    except (ValueError, TypeError) as e:
        print(f"invalid storage config: {e}", file=sys.stderr)
        return 2
    public, secrets = split_secret_keys(spec)
    cfg = load_network_config()
    overwriting = name in cfg["services"]
    cfg["services"][name] = public
    save_network_config(cfg)
    put_spec_secrets("services", name, secrets)
    verb = "updated" if overwriting else "added"
    print(f"{verb} storage {name} ({_format_storage_summary(spec)})")
    return 0


def cmd_unstorage(args: list[str]) -> int:
    """`a8s unstorage <name>` — remove a configured storage service. Mirrors
    `unremote`'s shape so the surface stays uniform across configurable
    cross-cluster primitives."""
    if args and args[0] in _HELP_FLAGS:
        return _storage_usage(explicit=True)
    if len(args) != 1:
        print("usage: a8s unstorage <name>", file=sys.stderr)
        return 2
    name = args[0]
    cfg = load_network_config()
    if name not in cfg["services"]:
        print(f"no storage named {name!r}", file=sys.stderr)
        return 1
    del cfg["services"][name]
    save_network_config(cfg)
    delete_spec_secrets("services", name)
    print(f"removed storage {name}")
    return 0


def cmd_health() -> int:
    """`a8s health` — test connectivity of all configured remotes and storage services."""
    import tempfile
    from network import load_remotes, load_services

    errors = 0

    remotes = load_remotes()
    if not remotes:
        print("remotes: (none configured)")
    for t in remotes:
        name = t.id
        try:
            t.start(lambda *_: None)
            connected = t.is_connected() if hasattr(t, "is_connected") else True
            t.stop()
            if connected:
                print(f"remote {name}: OK")
            else:
                print(f"remote {name}: FAIL (connected but is_connected=False)")
                errors += 1
        except Exception as e:
            print(f"remote {name}: FAIL ({e})")
            errors += 1

    services = load_services()
    if not services:
        print("storage: (none configured)")
    for svc in services:
        name = svc.id
        tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w")
        tmp.write("a8s health check")
        tmp.close()
        tmp_path = Path(tmp.name)
        used_public_get = False
        try:
            url = svc.store(tmp_path)
            dl_dir = Path(tempfile.mkdtemp())
            dl_dest = dl_dir / "health-check.txt"
            ok = svc.retrieve(url, dl_dest)
            if not ok:
                # A service may decline its own URL on purpose: `rclone`
                # hands back a public https link and leaves the fetch to the
                # receiver, which needs no rclone and no credentials. Follow
                # the same path a receiver would rather than calling that a
                # failure.
                from settings import get_int
                from services.http_get import http_get_url_to_path

                ok = http_get_url_to_path(url, dl_dest, max_bytes=get_int("max_file_bytes"))
                if ok:
                    used_public_get = True
            # Health runs often. Without this every run leaves a probe object
            # behind for good, and for a service with a public base_url that
            # litter is served on the open web.
            try:
                removed = svc.delete(url)
            except Exception:
                removed = False
            if ok and dl_dest.is_file() and dl_dest.read_text().strip() == "a8s health check":
                how = "public URL" if used_public_get else "service"
                left = "" if removed or svc.objects_expire else f"; probe left at {url}"
                print(f"storage {name}: OK (upload + download verified via {how}{left})")
            elif ok:
                print(f"storage {name}: WARN (download succeeded but content mismatch)")
                errors += 1
            else:
                print(f"storage {name}: FAIL (no service claimed the URL and a public GET did not fetch it)")
                errors += 1
            dl_dest.unlink(missing_ok=True)
            dl_dir.rmdir()
        except Exception as e:
            print(f"storage {name}: FAIL ({e})")
            errors += 1
        finally:
            tmp_path.unlink(missing_ok=True)

    agents = load_registry()
    print(f"agents: {len(agents)} registered")
    for name, info in agents.items():
        root = Path(info.get("root", ""))
        if not root.is_dir():
            print(f"  {name}: WARN (root missing: {root})")
            errors += 1
        else:
            print(f"  {name}: OK ({root})")

    return 1 if errors else 0
