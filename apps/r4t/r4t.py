#!/usr/bin/env python3
"""r4t — the roster: rigs, dispatch, verdicts, isolation.

Turns a repo into a roster of lightweight AI agents on the a8s network: a
human-readable ROSTER.md declares the members, an out-of-repo rig config
decides what each symbolic rig is allowed to run, and r4t dispatches turns
through the roster.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# `ar3ver` and the `ar3` foundation package sit in `<repo>/lib` and carry the
# suite semver and the shared code. That directory goes to the FRONT of
# sys.path, never the end: appended, it loses to site-packages, and any
# unrelated distribution named `ar3` then answers these imports instead. A
# copy of this tree relocated away from the repo root (the isolation container
# copies apps/r4t alone to /opt/r4t) has no `lib` beside it; the version is a
# nicety, never a dependency, so a missing module degrades to "unknown"
# instead of killing the CLI on import.
_AR3_LIB = str(Path(__file__).resolve().parents[2] / "lib")
while _AR3_LIB in sys.path:
    sys.path.remove(_AR3_LIB)
sys.path.insert(0, _AR3_LIB)
try:
    from ar3ver import version_line  # noqa: E402
except ImportError:
    def version_line(app: str) -> str:
        import platform

        return f"{app} unknown (ar3, python {platform.python_version()})"
from typing import NamedTuple

import knowledge
import runbook
import schedule
import state
import verdict
from dispatch import (
    DispatchContext,
    class_from_meta,
    handle_batch,
    handle_message,
    local_stamp,
    local_zone,
    run_clear,
    run_flush,
    run_idle,
    split_recipient,
)
from rig import (
    A8S_PY,
    CONTINUE_POOR,
    DEFAULT_TIMEOUT_SECONDS,
    PERMISSION_MODES,
    RigError,
    HARNESS_PRESETS,
    add_preset_rig,
    build_preset_invoke,
    continue_collisions,
    continue_grade,
    default_config_path,
    format_preset_invoke,
    is_below_knowledge_floor,
    load_rig_config,
    machine_ceiling,
    mcp_home_refusals,
    permission_ceiling_note,
    preset_names,
    raise_machine_ceiling,
    remove_rig,
    resolve_config_path,
    resolve_override,
    rig_preset,
    rig_setting,
    rig_settings,
    set_rig_value,
    setting_label,
    swap_preset_rig,
    unset_rig_value,
)
from notify import resolve_tell_fn, simulate_enabled, visible_a8s_names
from org import Org, check_org, load_org
from roster import (
    Member,
    Roster,
    RosterError,
    load_roster,
    resolve_roster_path,
)



class Command(NamedTuple):
    """One row of the visible CLI surface.

    `parser` names the top-level subcommand whose argparse `help=` this row's
    blurb becomes, so the bare-`r4t` panel and `r4t --help` share one wording.
    """

    display: str
    blurb: str
    parser: str | None = None
    hint: str | None = None


COMMAND_HELP = [
    (
        "Getting started",
        [
            Command("init", "Write a starter r4t.md in this repo", "init"),
            Command(
                "add <dir> [<runbook>]",
                "Register a directory as a node — one name, one door",
                "add",
                'tell <name> "hello"',
            ),
            Command(
                "runbook show --resolved",
                "The one file that says what the team is, layers merged",
                "runbook",
            ),
            Command(
                "roster check",
                "Check the roster reads cleanly and every rig resolves",
                "roster",
            ),
            Command("rig", "Rigs: what each name on the roster actually runs", "rig"),
            Command("rig list", "Your rigs, and the member riding each one"),
            Command(
                "rig presets",
                "Ready-made rigs to start from",
                None,
                "r4t rig add <rig> <preset>",
            ),
            Command("rig add <rig> <preset>", "Add a rig from a preset"),
            Command("rig set <rig> <key> <val>", "Change one rig setting"),
        ],
    ),
    (
        "Every day",
        [
            Command(
                "status",
                "Where a roster stands: budgets, queues, dead letters",
                "status",
            ),
            Command(
                "logs",
                "Everything the roster does, as it happens",
                "logs",
                "r4t logs -f",
            ),
            Command(
                "tell",
                "Speak into the roster as any member — jumpstart or diagnose",
                "tell",
            ),
            Command(
                "flush <member>",
                "Save a member's state and start the conversation fresh",
                "flush",
            ),
            Command(
                "resume <member>",
                "Put a parked member back in the rotation",
                "resume",
            ),
            Command(
                "engine <id> quota",
                "How much subscription an engine has left, and when it resets",
                "engine",
                "r4t engine list",
            ),
            Command(
                "engine <id> run PROMPT",
                "One headless turn as a bare stateless agent, outside any roster",
            ),
            Command(
                "rig run <rig> PROMPT",
                "The same turn as a named rig: its model, its tuning, its budget",
            ),
            Command(
                "rig fuel <rig>",
                "One number, 0 to 1: how much tank this rig's model has left",
            ),
        ],
    ),
    (
        "Verification",
        [
            Command(
                "check",
                "Sweep a roster's work for the patterns you forbid",
                "check",
            ),
        ],
    ),
]

PARSER_HELP = {
    cmd.parser: cmd.blurb
    for _section, cmds in COMMAND_HELP
    for cmd in cmds
    if cmd.parser
}

HIDDEN_COMMANDS = ("clear", "dispatch", "idle", "judge", "lab", "sandbox")

# `engine <id> quota` served a snapshot because the live check failed. Distinct
# from 0 (live) and 1 (no answer at all) so a script can tell them apart.
QUOTA_EXIT_STALE = 3

RUNBOOK_TEMPLATE = """\
---
name: "{name}"
extends: "triforce"
---

# {title}

This one file is the team. It extends the built-in `triforce` runbook — a
lead who talks to you, a builder, and one who tries to break it — so the only
thing you have to write here is what makes this project different.

Six sections exist and no others: `## Mission`, `## Charter`, `## Roster`,
`## Cells`, `## Rigs`, `## Rituals`. A section written here REPLACES the
base's whole, so read the base before you edit one:

    r4t runbook show --resolved --sources

Then register the node — one name for the directory, the runbook and the
address you mail:

    r4t add {root}

## Mission

Say what this roster is for, and how anyone will know it is done. Delete this
section to keep triforce's.
"""


def _resolve_root(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def _runbook_node(args: argparse.Namespace, root: Path) -> str | None:
    """The node whose a8s vars a runbook's `${VAR}` references resolve
    against, inferred without complaining: an explicit `--node`, the sole
    registered roster, or the one stamped at this root. A runbook that names
    no variable needs no node, so a missing one is not worth a line of
    stderr — the interpolation itself says so, by name, if it matters."""
    raw = getattr(args, "node", None)
    if raw:
        return raw.strip().lower()
    rosters = state.known_rosters()
    if len(rosters) == 1:
        return rosters[0]
    return state.node_for_root(root)


def _resolve_node(raw: str | None) -> str | None:
    if raw:
        return raw.strip().lower()
    rosters = state.known_rosters()
    if len(rosters) == 1:
        return rosters[0]
    match = state.node_for_root(Path.cwd())
    if match:
        return match
    if not rosters:
        print("no rosters found under ~/.config/r4t/rosters — pass --node", file=sys.stderr)
    else:
        print(f"multiple rosters ({', '.join(rosters)}) — pass --node", file=sys.stderr)
    return None


def _warn_org_errors(org: Org) -> None:
    """A malformed `comms:`/`egress:`/other org setting used to vanish the
    moment `load_org` degraded to defaults — invisible until the operator
    happened to run `roster check`. Every caller that dispatches on the
    result prints it instead: loud, not fatal, because the turn still has to
    run on the defaults `load_org` already chose."""
    for message in org.errors:
        print(f"warning: {message}", file=sys.stderr)


def _context(
    args: argparse.Namespace, node: str, *, ticker: bool = False
) -> DispatchContext:
    # The stamped node root IS the org dir — it is what dispatch resolved and
    # ran against. Observer surfaces (status, logs) must read the roster,
    # mission and docs from there too, not from wherever the operator happens
    # to stand: in a portable org the workplace repo is not the org dir, and a
    # member that wrote a shadow ROSTER.md/MISSION.md into the workplace must
    # not shadow the authoritative copy. So prefer the stamp over cwd whenever
    # no explicit --root overrides it.
    root = _resolve_root(args.root)
    if not getattr(args, "root", None):
        stamped = state.read_root(node)
        if stamped is not None and stamped.is_dir():
            root = stamped
    org = load_org(root)
    _warn_org_errors(org)
    roster_path = resolve_roster_path(org.dir, getattr(args, "roster", None), node)
    return DispatchContext(
        root=org.dir,
        node=node,
        roster_path=roster_path,
        config_path=resolve_config_path(getattr(args, "rig_config", None)),
        tell_fn=resolve_tell_fn(
            notify=getattr(args, "notify", True),
            simulate=simulate_enabled(getattr(args, "simulate_tell", False)),
        ),
        workplace=org.workplace,
        comms=org.comms,
        leader_sees_lateral=org.leader_sees_lateral,
        egress=org.egress,
        priority_senders=org.priority_senders,
        isolation=org.isolation,
        definition_path=(
            Path(defn).expanduser() if (defn := getattr(args, "definition", None)) else None
        ),
        ticker=ticker,
    )


def _print_table(
    headers: list[str], rows: list[tuple[str, ...]], indent: str = ""
) -> None:
    """Aligned columns in the shape `a8s ls` uses: uppercase header, three-space
    gutters, no padding on the trailing column."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: tuple[str, ...]) -> str:
        last = len(cells) - 1
        line = "   ".join(
            cell.ljust(widths[i]) if i < last else cell
            for i, cell in enumerate(cells)
        )
        return f"{indent}{line}"

    print(fmt(tuple(headers)))
    for row in rows:
        print(fmt(row))


def _rig_model(rig) -> str:
    """What this rig actually runs. An explicit `model` setting wins; otherwise
    read the token after --model/-m, which is where a preset-built invoke
    carries it."""
    if rig.model:
        return rig.model
    argv = next(iter(rig.pool()), [])
    for flag in ("--model", "-m"):
        if flag in argv:
            at = argv.index(flag) + 1
            if at < len(argv):
                return argv[at]
    return "-"


def _print_rig_table(config, indent: str, wide: bool) -> None:
    rigs = [(n, config.rigs[n]) for n in sorted(config.rigs) if not config.rigs[n].error]
    show_rig_budget = any(r.rig_budget_max is not None for _, r in rigs)

    headers = ["RIG", "PRESET", "MODEL", "TIMEOUT", "SENDS", "BUDGET"]
    if show_rig_budget:
        headers.append("RIG-BUDGET")
    if wide:
        headers.append("INVOKE")

    rows: list[tuple[str, ...]] = []
    for name, rig in rigs:
        row = [name, rig.preset or "-", _rig_model(rig)]
        row += [
            f"{rig.timeout_seconds:g}s",
            str(rig.max_sends_per_turn),
            f"{rig.budget_max:g}/+{rig.budget_earn_per_hour:g}h",
        ]
        if show_rig_budget:
            row.append(
                "-"
                if rig.rig_budget_max is None
                else f"{rig.rig_budget_max:g}/+{rig.rig_budget_earn_per_hour:g}h"
            )
        if wide:
            pool = rig.pool()
            argv = " ".join(pool[0])
            if len(pool) > 1:
                argv += f"  [+{len(pool) - 1} pool variant(s)]"
            row.append(argv)
        rows.append(tuple(row))

    if rows:
        _print_table(headers, rows, indent)
    invalid = [(n, config.rigs[n]) for n in sorted(config.rigs) if config.rigs[n].error]
    if invalid:
        print()
        print(f"{indent}invalid:")
        for name, rig in invalid:
            print(f"{indent}  {name}: {rig.error}")
        print(f"{indent}  (try: edit {config.path})")


def _print_roster_table(config, roster_path: Path, indent: str) -> None:
    try:
        roster = load_roster(roster_path)
    except RosterError as e:
        print(f"{indent}roster ({roster_path.name}): {e}")
        return
    rows: list[tuple[str, ...]] = []
    for m in roster.members:
        if m.errors:
            rows.append((m.name, "-", f"DISABLED — {m.error}"))
        else:
            rig, err, pinned = config.rig_for(m)
            if rig is None:
                rows.append((m.name, "-", f"FAIL CLOSED — {err}"))
            else:
                rows.append((m.name, rig.name, "pinned" if pinned else ""))
    print(f"{indent}roster ({roster_path}):")
    _print_table(["MEMBER", "RIG", "NOTE"], rows, indent + "  ")


def _print_rig_summary(
    config_path: Path,
    roster_path: Path | None = None,
    *,
    indent: str = "  ",
    wide: bool = False,
) -> None:
    try:
        config = load_rig_config(config_path)
    except RigError as e:
        print(f"{indent}error: {e}")
        return
    if config.missing:
        print(f"{indent}(no rigs yet — try: r4t rig add <rig> <preset>)")
        return
    _print_rig_table(config, indent, wide)
    if config.pins:
        print()
        print(f"{indent}pins:")
        for agent in sorted(config.pins):
            print(f"{indent}  {agent} -> {config.pins[agent]}")
    print()
    print(
        f"{indent}contract:   one turn at a time  (cadence "
        f"{config.throttle.min_seconds_between_turn_starts:g}s)"
    )
    print(
        f"{indent}governance: cell_budget={config.cell_budget_max:g}/"
        f"+{config.cell_budget_earn_per_hour:g}per-h "
        f"breaker_cap={config.breaker_cap} "
        f"breaker_cooldown={config.breaker_cooldown_seconds:g}s"
    )
    if roster_path and roster_path.is_file():
        print()
        _print_roster_table(config, roster_path, indent)


def _print_roster_summaries() -> None:
    rosters = state.known_rosters()
    if not rosters:
        print("  (none — try: r4t add <dir> [<runbook>])")
        return
    for node in rosters:
        locks = state.live_locks(node)
        dead = len(state.list_dead_letters(node))
        queued = sum(state.queue_depth(node, m) for m in state.members_with_queue(node))
        parts = [
            f"{len(locks)} lock(s)",
            f"{queued} queued",
            f"{dead} dead letter(s)",
        ]
        print(f"  {node}: {', '.join(parts)}")
        for lock in locks:
            print(f"    locked: {lock.get('agent', '?')} pid={lock.get('pid', '?')}")


def _cmd_help(name: str) -> str:
    return PARSER_HELP[name] + "."


def _print_command_panel() -> None:
    width = max(len(cmd.display) for _section, cmds in COMMAND_HELP for cmd in cmds)
    for index, (section, cmds) in enumerate(COMMAND_HELP):
        if index:
            print()
        print(section)
        for cmd in cmds:
            line = f"  {cmd.display:<{width}}  {cmd.blurb}"
            if cmd.hint:
                line += f"   (try: {cmd.hint})"
            print(line)


def _next_steps(
    *,
    config_missing: bool,
    roster_path: Path,
    rosters: list[str],
) -> list[str]:
    steps: list[str] = []
    if config_missing:
        steps.append(
            "`r4t rig add <rig> <preset>` — write ~/.config/r4t/rigs.json "
            "(a runbook whose members carry `Engine:` lines needs none)"
        )
    if not roster_path.is_file():
        steps.append("`r4t init` — write a starter r4t.md in the current repo")
    else:
        steps.append("`r4t roster check` — lint the roster and rig mapping")
        steps.append("`r4t rig presets` — named CLI rigs aligned with a8s definitions")
    if not rosters:
        steps.append("`r4t add .` — register this directory as a node")
    elif len(rosters) == 1:
        steps.append(f"`r4t status --node {rosters[0]}` — budgets, queues, dead letters")
    else:
        steps.append("`r4t status --node <roster>` — pick a roster from the list above")
    return steps


def cmd_default(_args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    config_path = default_config_path()
    roster_path = resolve_roster_path(root, None)
    rosters = state.known_rosters()

    print("r4t — the roster")
    print("Define the team in one r4t.md at the node dir — mission, charter,")
    print("roster, rigs. r4t dispatches governed turns on a8s.")
    print()
    print("Environment")
    print(f"  R4T_HOME: {state.r4t_home()}")
    print(f"  cwd: {root}")
    print(f"  rig config: {config_path}")
    print()
    print("Rigs")
    _print_rig_summary(config_path, roster_path)
    print()
    print(f"Rosters ({state.rosters_dir()})")
    _print_roster_summaries()
    print()
    print("This repo")
    if roster_path.is_file():
        try:
            roster = load_roster(roster_path)
            print(
                f"  {roster_path}: {len(roster.members)} member(s), "
                f"leader {roster.leader().name}"
            )
        except RosterError as e:
            print(f"  {roster_path}: {e}")
    else:
        print(f"  no r4t.md or ROSTER.md under {root}")
    print()
    _print_command_panel()
    print()
    print("Next steps")
    for step in _next_steps(
        config_missing=not config_path.is_file(),
        roster_path=roster_path,
        rosters=rosters,
    ):
        print(f"  - {step}")
    print()
    print("More: docs/r4t.md and `r4t <command> --help`")
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    if getattr(args, "batch", None) is not None:
        if (
            args.from_agent is not None
            or args.to is not None
            or args.message is not None
            or args.meta
        ):
            print(
                "dispatch: --batch cannot be combined with "
                "--from/--to/--message/--meta",
                file=sys.stderr,
            )
            return 2
        try:
            parsed = json.loads(args.batch)
        except (TypeError, ValueError):
            print("dispatch: --batch must be a JSON array", file=sys.stderr)
            return 2
        if not isinstance(parsed, list):
            print("dispatch: --batch must be a JSON array", file=sys.stderr)
            return 2
        node = _node_from_batch(args.batch)
        if not node:
            node = _resolve_node(None)
        if not node:
            return 2
        # a8s runs `dispatch` and `idle` as wake subprocesses and pumps their
        # stdout into the node's log, so these two verbs — and only these two —
        # narrate the ticker (`ctx.event`) that `a8s logs <node> -f` follows.
        ctx = _context(args, node, ticker=True)
        state.stamp_root(ctx.node, ctx.root)
        return handle_batch(
            ctx, args.batch, drain_after=not args.no_drain,
        )
    if args.from_agent is None or args.to is None or args.message is None:
        print(
            "dispatch: --from, --to, and --message are required "
            "(or pass --batch)",
            file=sys.stderr,
        )
        return 2
    node, _sub = split_recipient(args.to)
    if not node:
        print("dispatch: --to must carry the node name", file=sys.stderr)
        return 2
    ctx = _context(args, node.lower(), ticker=True)
    state.stamp_root(ctx.node, ctx.root)
    return handle_message(
        ctx, args.from_agent, args.to, args.message,
        klass=class_from_meta(args.meta),
        drain_after=not args.no_drain,
    )


def _node_from_batch(raw_json: str) -> str | None:
    try:
        entries = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or "_unreadable" in entry:
            continue
        node, _ = split_recipient(entry.get("to") or "")
        if node:
            return node.lower()
    return None


def cmd_clear(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    ctx = _context(args, node)
    summary = run_clear(ctx)
    recovered = sum(count for _name, count in summary["recovered"])
    print(
        f"pruned {summary['locks_pruned']} stale lock(s)"
        + (f"; recovered {recovered} in-flight message(s)" if recovered else "")
        + f"; drained {summary['drained']} queued turn(s)"
        + _retention_line(summary)
    )
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Put a parked member back in the rotation — systemd's `reset-failed`.

    Parking is left automatically only when a probe that costs nothing says the
    structural cause is gone. Where there is no such probe — an engine refusing
    authentication has no cheap test that is not a paid call — the operator
    says so, here. A system that cannot cheaply tell whether a problem is fixed
    must not spend the user's money finding out on a timer."""
    node = _resolve_node(args.node)
    if node is None:
        return 2
    ctx = _context(args, node)
    try:
        roster = load_roster(ctx.roster_path, node=ctx.node)
    except RosterError as e:
        print(f"resume: cannot read roster: {e}", file=sys.stderr)
        return 2
    if args.all:
        targets = state.parked_members(node)
    elif not args.member:
        print("resume: name a member, or --all (try: r4t status)", file=sys.stderr)
        return 2
    else:
        member = roster.find(args.member)
        if member is None:
            known = ", ".join(roster.names()) or "(none)"
            print(
                f"resume: no roster member named {args.member!r} "
                f"(members: {known})",
                file=sys.stderr,
            )
            return 2
        targets = [member.name.lower()]
    if not targets:
        print("nothing parked")
        return 0
    for name in targets:
        record = state.unpark_member(node, name)
        if not record:
            print(f"{name} is not parked")
            continue
        depth = state.queue_depth(node, name)
        state.append_log(node, f"r4t: RESUME {name} — resumed by hand; {depth} queued")
        print(f"resumed {name} — {depth} message(s) waiting  ({record['reason']})")
    return 0


def _retention_line(summary: dict) -> str:
    days = summary["log_days_pruned"]
    months = summary["velocity_months_rotated"]
    parts = []
    if days:
        parts.append(f"pruned {len(days)} day log(s) ({days[0]}..{days[-1]})")
    if months:
        parts.append(f"rotated velocity for {', '.join(months)}")
    return ("; " + "; ".join(parts)) if parts else ""


def _flush_line(result: dict) -> str:
    name = result["member"].lower()
    if result["skipped"]:
        return f"skipped {name} — {result['skipped']}"
    if result["failed"]:
        return (
            f"failed {name} — {result['failed']}; conversation and history "
            "left as they are"
        )
    done = []
    if result["dumped"]:
        done.append("dumped state to disk")
    if result["retired"]:
        done.append("retired the conversation")
    if result["archived"] is not None:
        done.append(f"archived history as {result['archived'].name}")
    if not done:
        return f"nothing to flush for {name} — no conversation, no history"
    return f"flushed {name} — {', '.join(done)}"


def cmd_flush(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    if bool(args.members) == bool(args.all):
        print(
            "flush: name the members to flush, or pass --all — not both "
            f"(try: r4t flush --node {node} <name>)",
            file=sys.stderr,
        )
        return 2
    ctx = _context(args, node)
    try:
        roster = load_roster(ctx.roster_path, node=ctx.node)
        config = load_rig_config(ctx.config_path)
    except (RosterError, RigError) as e:
        print(f"flush: {e}", file=sys.stderr)
        return 2
    if args.all:
        members = list(roster.members)
    else:
        members = []
        for raw in args.members:
            member = roster.find(raw)
            if member is None:
                names = ", ".join(roster.names()) or "(none)"
                print(
                    f"flush: no roster member named {raw!r} — "
                    f"(try: r4t flush --node {node} <name>; members: {names})",
                    file=sys.stderr,
                )
                return 2
            members.append(member)
    results = run_flush(ctx, config, roster, members, dump=not args.no_dump)
    for result in results:
        print(_flush_line(result))
    return 1 if any(r["failed"] for r in results) else 0


def cmd_idle(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    ctx = _context(args, node, ticker=True)
    summary = run_idle(ctx)
    print(f"drained {summary['drained']} queued turn(s)")
    clear_summary = run_clear(ctx)
    print(
        f"pruned {clear_summary['locks_pruned']} stale lock(s); "
        f"drained {clear_summary['drained']} more queued turn(s)"
        + _retention_line(clear_summary)
    )
    return 0


def _mark(healthy: bool | None) -> str:
    return {True: "✓", False: "✗"}.get(healthy, " ")


def _print_rows(rows: list[tuple[bool | None, str, str, str | None]]) -> None:
    """Render (healthy, name, state, hint) rows: mark + aligned name + state,
    with an actionable `(try: ...)` when the row needs a hand."""
    if not rows:
        print("  (none)")
        return
    width = max(len(name) for _h, name, _s, _t in rows)
    for healthy, name, state_text, hint in rows:
        line = f"  {_mark(healthy)} {name:<{width}}  {state_text}"
        if hint:
            line += f"   (try: {hint})"
        print(line.rstrip())


def _roster_rows(
    ctx: DispatchContext, node: str, roster, config
) -> list[tuple[bool | None, str, str, str | None]]:
    locks = {lock["agent"]: lock for lock in state.live_locks(node)}
    rows: list[tuple[bool | None, str, str, str | None]] = []
    for m in roster.members:
        flags = []
        if m.leader:
            flags.append("leader")
        if m.name.lower() in locks:
            flags.append(f"turn running, pid {locks[m.name.lower()].get('pid')}")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        if m.errors:
            rows.append((
                False, m.name, f"disabled: {m.error}{suffix}",
                f"fix {ctx.roster_path.name}",
            ))
            continue
        if config is None:
            rows.append((None, m.name, f"rig={m.rig or '?'}{suffix}", None))
            continue
        rig, err, pinned = config.rig_for(m)
        if rig is None:
            state_text, _, hint = (err or "").partition(" — try: ")
            rows.append((False, m.name, f"{state_text}{suffix}", hint or None))
            continue
        detail = f"rig={rig.name}" + (" (pinned)" if pinned else "")
        if m.cell:
            detail += f"  cell={m.cell}"
        if m.lead:
            detail += f"  lead={m.lead}"
        level = state.budget_level(
            node, m.name, rig.budget_max, rig.budget_earn_per_hour
        )
        detail += f"  budget={state.fmt_budget(level)}/{state.fmt_budget(rig.budget_max)}"
        rig_level = None
        if rig.rig_budget_max is not None:
            rig_level = state.rig_budget_level(
                rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour
            )
            detail += (
                f"  rig={state.fmt_budget(rig_level)}/"
                f"{state.fmt_budget(rig.rig_budget_max)}"
            )
        depth = state.queue_depth(node, m.name)
        if depth:
            detail += f"  {depth} queued"
        healthy: bool | None = True
        hint = None
        if level < 1.0 and depth:
            wait = state.budget_seconds_until(
                node, m.name, rig.budget_max, rig.budget_earn_per_hour
            )
            detail += f"  RESTING (ready in ~{wait / 60:.0f} min)"
        elif rig_level is not None and rig_level < 1.0 and depth:
            wait = state.rig_budget_seconds_until(
                rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour
            )
            detail += f"  RESTING (rig {rig.name}, ready in ~{wait / 60:.0f} min)"
        blocked, failures = state.breaker_open(
            node, m.name, config.breaker_cap, config.breaker_cooldown_seconds
        )
        if failures:
            detail += f"  failures={failures}"
        if blocked:
            detail += "  BREAKER OPEN"
            healthy = False
            hint = f"fix the {rig.name} harness; turns retry when it closes"
        rows.append((healthy, m.name, f"{detail}{suffix}", hint))
    return rows


def _isolation_tag(isolation) -> str:
    """The org's OS-level boundary at a glance, or "" for a bare org. Isolation
    is per-org (org.py), so one badge covers the whole roster."""
    if isolation.run_as:
        return f"[user:{isolation.run_as}]"
    if isolation.container:
        return f"[container:{isolation.container}]"
    return ""


def _rig_rows(
    ctx: DispatchContext, config
) -> list[tuple[bool | None, str, str, str | None]]:
    rows: list[tuple[bool | None, str, str, str | None]] = []
    if config.missing:
        rows.append((
            None, "rigs", "none configured yet",
            "r4t rig add <rig> <preset>",
        ))
        return rows
    for name in sorted(config.rigs):
        rig = config.rigs[name]
        if rig.error:
            rows.append((False, name, f"invalid: {rig.error}", f"edit {ctx.config_path}"))
            continue
        pool = rig.pool()
        argv = " ".join(pool[0])
        if len(pool) > 1:
            argv += f"  [+{len(pool) - 1} pool variant(s)]"
        limits = (
            f"timeout={rig.timeout_seconds:g}s "
            f"budget={rig.budget_max:g}/+{rig.budget_earn_per_hour:g}per-h "
            f"sends={rig.max_sends_per_turn}"
        )
        if rig.rig_budget_max is not None:
            limits += (
                f" rig-budget={rig.rig_budget_max:g}/"
                f"+{rig.rig_budget_earn_per_hour:g}per-h"
            )
        rows.append((True, name, f"{argv}  ({limits})", None))
    for agent in sorted(config.pins):
        rows.append((None, "pin", f"{agent} -> {config.pins[agent]}", None))
    rows.append((
        None, "contract",
        "one turn at a time  (cadence "
        f"{config.throttle.min_seconds_between_turn_starts:g}s)",
        None,
    ))
    rows.append((
        None, "governance",
        f"cell_budget={config.cell_budget_max:g}/"
        f"+{config.cell_budget_earn_per_hour:g}per-h  "
        f"breaker={config.breaker_cap}/{config.breaker_cooldown_seconds:g}s",
        None,
    ))
    return rows


def _activity_rows(node: str) -> list[tuple[bool | None, str, str, str | None]]:
    rows: list[tuple[bool | None, str, str, str | None]] = []
    for name in state.members_with_queue(node):
        rows.append((
            None, "queued",
            f"{name}  {state.queue_depth(node, name)} message(s) waiting",
            None,
        ))
    roll = verdict.rollup_dead_letters(state.list_dead_letters(node))
    if not roll.routine_total and not roll.signal_total:
        rows.append((None, "dead letters", "0", None))
    if roll.routine_total:
        breakdown = ", ".join(f"{k} {v}" for k, v in sorted(roll.routine.items()))
        rows.append((
            None, "dead letters",
            f"{roll.routine_total} routine ({breakdown}) — governance debris, "
            "not failures",
            None,
        ))
    for reason in sorted(roll.signals):
        records = roll.signals[reason]
        pairs: dict[str, int] = {}
        for record in records:
            key = f"{record.get('from', '?')} -> {record.get('to', '?')}"
            pairs[key] = pairs.get(key, 0) + 1
        worst = max(pairs, key=pairs.get)  # type: ignore[arg-type]
        others = f" +{len(pairs) - 1} pair(s)" if len(pairs) > 1 else ""
        rows.append((
            False, "dead letters",
            f"{len(records)} {reason} ({worst}{others}) — "
            f"{verdict.REASON_GLOSS.get(reason, reason)}",
            f"ls {state.dead_letter_dir(node)}",
        ))
    return rows


def _last_finished(node: str, roster) -> tuple[str, str, dict] | None:
    """(member, completed_at, last_turn) for the most recently finished turn on
    the roster, or None when nobody has run."""
    best: tuple[str, str, dict] | None = None
    for m in roster.members:
        meta = state.read_meta(node, m.name)
        turn = meta.get("last_turn")
        stamp = str(meta.get("last_completed_at", ""))
        if not stamp or not isinstance(turn, dict):
            continue
        if best is None or stamp > best[1]:
            best = (m.name.lower(), stamp, turn)
    return best


def _since(stamp: str) -> float:
    try:
        started = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
    return max(0.0, time.time() - started)


def _now_row(ctx: DispatchContext, node: str, roster, config) -> tuple[str, str]:
    """The one `Now` row. Exactly one, always: it IS the contract, rendered.

    The running turn's elapsed time is printed against its timeout because
    under run-to-completion a single hung member stalls the whole roster, and
    that per-turn timeout is the only thing that ends it. The cost of the
    contract belongs on screen while it is being paid."""
    for lock in state.live_locks(node):
        name = lock["agent"]
        member = roster.find(name)
        turn = state.read_turn(node, name) or {}
        detail = f"running {schedule.fmt_age(_since(str(lock.get('started', ''))))}"
        rig = config.rig_for(member)[0] if member is not None else None
        if rig is not None:
            detail += f" of {schedule.fmt_age(rig.timeout_seconds)}"
        detail += f"   rig {lock.get('rig', '?')}"
        if turn.get("batch"):
            detail += f"   {turn['batch']} msg"
        return name, detail
    last = _last_finished(node, roster)
    if last is None:
        return "—", "idle — no turn has run yet"
    name, stamp, turn = last
    return "—", (
        f"idle {schedule.fmt_age(_since(stamp))}   "
        f"(last: {name}, exit {turn.get('exit', '?')})"
    )


def _rotation_rows(
    ctx: DispatchContext, node: str, roster, config
) -> list[tuple[str, str, str]]:
    """(slot, member, detail) for the Now / Next / Then / Held / Idle block.

    `Next` is the real selection — `schedule.next_up`, the same call the drain
    loop makes. Status never re-implements the ranking: the day the two drift
    is the day "why" stops being true, and nothing would say which day that
    was."""
    entries = schedule.snapshot(
        node, config, roster, priority_senders=ctx.priority_senders
    )
    running, detail = _now_row(ctx, node, roster, config)
    rows = [("Now", running, detail)]
    ready = [e for e in entries if e.state == schedule.READY and e.member.lower() != running]
    for slot, entry in zip(("Next", "Then"), ready):
        rows.append((
            slot, entry.member.lower(),
            f"{entry.why}   score {entry.score}   {entry.queue_note}",
        ))
    if not ready:
        rows.append(("Next", "—", "nothing ready to run"))
    for entry in entries:
        if entry.state == schedule.READY or entry.member.lower() == running:
            continue
        detail = f"{entry.state.upper()} — {entry.held_note}   {entry.depth} queued"
        if entry.state == schedule.PARKED:
            detail += f"   (try: r4t resume {entry.member.lower()})"
        rows.append(("Held", entry.member.lower(), detail))
    quiet = len(roster.members) - len({e.member for e in entries}) - (running != "—")
    if quiet > 0:
        rows.append(("Idle", "", f"{quiet} member(s) with nothing queued"))
    return rows


def _print_rotation(rows: list[tuple[str, str, str]]) -> None:
    slot_w = max(len(slot) for slot, _n, _d in rows)
    name_w = max(len(name) for _s, name, _d in rows)
    seen: set[str] = set()
    for slot, name, detail in rows:
        label = "" if slot == "Held" and slot in seen else slot
        seen.add(slot)
        print(f"  {label:<{slot_w}}  {name:<{name_w}}  {detail}".rstrip())


def cmd_status(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    ctx = _context(args, node)
    print(f"roster: {node}")
    print(f"state: {state.roster_dir(node)}")
    # The zone the roster speaks, stated once at the top: every prompt this
    # node composes says the same thing, and an operator reading a member's
    # "tomorrow" needs to know which midnight it meant.
    print(f"time: {local_stamp()}")
    iso = _isolation_tag(ctx.isolation)
    if iso:
        print(f"isolation: {iso}  (every member turn runs behind this boundary)")
    print()

    roster = None
    config = None
    roster_err = config_err = None
    try:
        roster = load_roster(ctx.roster_path, node=ctx.node)
    except RosterError as e:
        roster_err = str(e)
    try:
        config = load_rig_config(ctx.config_path)
    except RigError as e:
        config_err = str(e)

    print("Rotation  (one turn at a time)")
    if roster is None or config is None:
        print("  (unavailable until the roster and rig config load)")
    else:
        _print_rotation(_rotation_rows(ctx, node, roster, config))
    print()

    print("Health")
    for v in verdict.roster_verdicts(node, roster, config):
        line = f"  {verdict.MARKS[v.level]} {v.text}"
        if v.hint:
            line += f"   (try: {v.hint})"
        print(line)
    print()

    print(f"Roster  (repo settings: {ctx.roster_path})")
    if roster_err:
        _print_rows([(False, "roster", roster_err, "r4t roster check")])
    else:
        _print_rows(_roster_rows(ctx, node, roster, config))
    print()

    print(f"Rigs  (your configuration: {ctx.config_path})")
    if config_err:
        _print_rows([(False, "config", config_err, f"edit {ctx.config_path}")])
    else:
        _print_rows(_rig_rows(ctx, config))
    print()

    print("Activity")
    _print_rows(_activity_rows(node))
    return 0


def _resolve_log_members(args: argparse.Namespace, node: str) -> list[str] | None | bool:
    """Validate --agent (repeatable) and --cell against the roster. Returns
    the canonical member name(s) to scope the stream to, None when neither
    was given, or False on an unknown member or cell (already reported)."""
    agents = args.agent or []
    cell = getattr(args, "cell", None)
    if not agents and not cell:
        return None
    ctx = _context(args, node)
    try:
        roster = load_roster(ctx.roster_path, node=ctx.node)
    except RosterError as e:
        print(f"logs: cannot read roster: {e}", file=sys.stderr)
        return False
    names: list[str] = []
    for raw in agents:
        member = roster.find(raw)
        if member is None:
            known = ", ".join(roster.names()) or "(none)"
            print(
                f"logs --agent: no roster member named {raw!r} — "
                f"(try: r4t logs --node {node} --agent <name>; members: {known})",
                file=sys.stderr,
            )
            return False
        names.append(member.name)
    if cell:
        cell_members = [
            m.name for m in roster.members
            if m.cell.strip().lower() == cell.strip().lower()
        ]
        if not cell_members:
            cells = sorted({m.cell for m in roster.members if m.cell})
            print(
                f"logs --cell: no roster members in cell {cell!r} — "
                f"(cells: {', '.join(cells) or '(none)'})",
                file=sys.stderr,
            )
            return False
        names.extend(cell_members)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name.lower() not in seen:
            seen.add(name.lower())
            ordered.append(name)
    return ordered


def _print_member_turns(node: str, member: str) -> int:
    files = state.list_turn_captures(node, member)
    if not files:
        print(
            f"(no captured turns yet for {member.lower()} under "
            f"{state.turns_dir(node, member)})",
            file=sys.stderr,
        )
        return 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        print(f"===== {path.name} =====")
        print(text.rstrip())
        print()
    return 0


def filter_log_line(line: str) -> str | None:
    """Compact one roster-log line into an activity event, or None to skip.

    The daily log interleaves single-line events with full multi-line turn
    transcripts; the scoped log view shows the events and the turn boundaries,
    never the transcript bodies."""
    if line.startswith("r4t: "):
        return line
    if line.startswith("## ") and " dispatch " in line:
        _, _, rest = line.partition(" dispatch ")
        return f"turn: {rest}"
    if line.startswith("### Output ("):
        return f"done: {line[len('### Output ('):].rstrip(')')}"
    return None


def _log_day_header(day: str) -> str:
    """Name the day file's zone, and the reader's.

    The file is named in UTC because its name is a sort key: `r4t`'s retention
    pass string-compares those names, and two machines writing one portable
    org's log must agree on the order. Near midnight the two zones name
    different days, so the header says both rather than pretending.
    """
    return f"— log day {day} UTC (this machine reads {local_zone()})"


def cmd_logs(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    members = _resolve_log_members(args, node)
    if members is False:
        return 2
    if members and args.full:
        for member in members:
            _print_member_turns(node, member)
        return 0

    mention = None
    if members:
        alternation = "|".join(re.escape(name) for name in members)
        mention = re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)
    log_dir = state.roster_dir(node) / "log"

    def rendered(raw: str) -> list[str]:
        if args.full:
            return [raw]
        event = filter_log_line(raw)
        if not event:
            return []
        if mention and not mention.search(event):
            return []
        return [event]

    files = sorted(log_dir.glob("*.md")) if log_dir.is_dir() else []
    collected: list[tuple[str, str]] = []
    offset = 0
    for path in files[-2:]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if path == files[-1]:
            offset = len(text.encode("utf-8"))
        for raw in text.splitlines():
            collected.extend((path.stem, line) for line in rendered(raw))
    # The tail counts log lines, not headers, so it is applied first and the
    # day headers are emitted over whatever survived.
    day: str | None = None
    for stem, line in collected[-args.lines:] if args.lines else collected:
        if stem != day:
            day = stem
            print(_log_day_header(day))
        print(line)
    if not args.follow:
        if not files:
            print(f"(no log yet under {log_dir})", file=sys.stderr)
        return 0

    current = files[-1] if files else None
    try:
        while True:
            today = log_dir / (
                datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".md"
            )
            if today != current:
                current, offset = today, 0
            if current.is_file():
                size = current.stat().st_size
                if size > offset:
                    with current.open("r", encoding="utf-8") as f:
                        f.seek(offset)
                        chunk = f.read()
                        offset = f.tell()
                    for raw in chunk.splitlines():
                        for line in rendered(raw):
                            if current.stem != day:
                                day = current.stem
                                print(_log_day_header(day), flush=True)
                            print(line, flush=True)
                elif size < offset:
                    offset = size
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def cmd_check(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    root = _resolve_root(args.root)
    if not getattr(args, "root", None):
        stamped = state.read_root(node)
        if stamped is not None and stamped.is_dir():
            root = stamped
    org = load_org(root)
    _warn_org_errors(org)
    if not org.workplace.is_dir():
        print(
            f"check: workplace {org.workplace} does not exist "
            f"(try: r4t roster check --node {node})",
            file=sys.stderr,
        )
        return 2
    from check import run as run_check

    return run_check(node, org.workplace)


def cmd_judge(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    from judge import run as run_judge

    return run_judge(
        node,
        rig_name=args.rig,
        config_path=resolve_config_path(args.rig_config),
        json_mode=args.json,
    )


def _ensure_tell_outbox(ctx: DispatchContext) -> None:
    """A directly-invoked `r4t tell` has no a8s-injected outbox env; give
    `tell` subprocesses (error notices) the same fallback dispatch itself
    uses for releases."""
    os.environ.setdefault("TELL_OUTBOX_DIR", str(ctx.root / ".outbox"))


def _adopt_root(ctx: DispatchContext) -> None:
    """`r4t tell` is roster ingress just like dispatch — it calls
    handle_message directly and never passes cmd_dispatch, the only place the
    root stamp was written. A roster driven entirely through `tell` therefore
    had no stamp, and every observer command fell back to guessing the root
    from cwd (the live quill repro). First successful resolution writes the
    stamp; an existing stamp is never overridden here — dispatch owns that."""
    if state.read_root(ctx.node) is None and ctx.roster_path.is_file():
        state.stamp_root(ctx.node, ctx.root)


def cmd_tell(args: argparse.Namespace) -> int:
    """Send into the walls as another roster member — the owner's
    impersonation verb, for jumpstarting a member's queue or diagnosing how
    one lands without waiting for a real sender. Routes through the same
    ingest path a real member-to-member send takes (`handle_message` ->
    `_ingest`), stamped `from` the impersonated member, so it enqueues,
    threads, and narrates the ticker exactly like any other arrival."""
    node = _resolve_node(args.node)
    if node is None:
        return 2
    ctx = _context(args, node, ticker=True)
    _adopt_root(ctx)
    _ensure_tell_outbox(ctx)
    try:
        roster = load_roster(ctx.roster_path, node=ctx.node)
    except RosterError as e:
        print(f"tell: {e}", file=sys.stderr)
        return 2
    sender_member = roster.find(args.as_member)
    if sender_member is None:
        names = ", ".join(roster.names()) or "(none)"
        print(
            f"tell --as: no roster member named {args.as_member!r} "
            f"(members: {names})",
            file=sys.stderr,
        )
        return 2
    text = " ".join(args.message).strip()
    if not text:
        print("tell: message is required", file=sys.stderr)
        return 2
    if args.to:
        to_member = roster.find(args.to)
        if to_member is None or to_member.errors:
            print(f"tell --to: no dispatchable member {args.to!r}", file=sys.stderr)
            return 2
        to = f"{node}:{to_member.name.lower()}"
    else:
        to = node
    sender = f"{node}:{sender_member.name.lower()}"
    handle_message(ctx, sender, to, text)
    from dispatch import resting_note

    note = resting_note(ctx, to)
    if note:
        print(note)
    return 0


RIG_COMMAND_HELP = [
    ("rig list", "Rigs, limits, and roster rig resolution (alias: ls; --wide)"),
    ("rig presets", "Named CLI presets aligned with a8s definitions"),
    ("rig run <rig> PROMPT", "One headless turn as this rig (--wait / --now on budget)"),
    ("rig fuel <rig>", "How much subscription this rig's model has left, 0..1"),
    ("rig add <rig> <preset>", "Add a rig (creates the config if needed; --model M, --force)"),
    ("rig swap <rig> <preset>", "Switch an existing rig to a preset, keeping its settings"),
    ("rig remove <rig>...", "Remove one or more rigs from the config (alias: rm)"),
    ("rig configure <rig>", "Walk a rig's settings one prompt at a time"),
    ("rig set <rig> <key> <val>", "Write one explicit rig setting"),
    ("rig get <rig> [<key>]", "Read a rig's effective settings, source-annotated"),
    ("rig unset <rig> <key>...", "Drop explicit settings back to preset/built-in defaults"),
]


def cmd_rig_overview(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(getattr(args, "rig_config", None))
    roster_path = resolve_roster_path(
        _resolve_root(getattr(args, "root", None)), getattr(args, "roster", None)
    )
    print("r4t rig — map the roster's symbolic rigs to what actually runs")
    print(f"config: {config_path}" + (" (missing)" if not config_path.is_file() else ""))
    print()
    print("Rigs")
    _print_rig_summary(config_path, roster_path if roster_path.is_file() else None)
    print()
    print("Commands")
    width = max(len(name) for name, _ in RIG_COMMAND_HELP)
    for name, blurb in RIG_COMMAND_HELP:
        print(f"  {name:<{width}}  {blurb}")
    print()
    print("Next steps")
    if not config_path.is_file():
        print("  - `r4t rig presets` — see the available CLI presets")
        print("  - `r4t rig add leader <preset>` — create the config with your first rig")
    else:
        if roster_path.is_file():
            print("  - `r4t roster check` — lint roster ↔ rig mappings")
        print("  - `r4t rig add <rig> <preset>` — add another rig")
        print("  - `r4t rig get <rig>` — see a rig's effective settings")
    return 0


def cmd_rig_list(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.rig_config)
    print(f"rig config: {config_path}" + (" (missing)" if not config_path.is_file() else ""))
    print()
    roster_path = resolve_roster_path(_resolve_root(args.root), args.roster)
    _print_rig_summary(
        config_path,
        roster_path if roster_path.is_file() else None,
        indent="",
        wide=args.wide,
    )
    if config_path.is_file() and not args.wide:
        print()
        print("(full invoke lines: r4t rig ls --wide)")
    return 0


def cmd_rig_presets(_args: argparse.Namespace) -> int:
    print("Named harness-CLI presets (from apps/a8s/definitions/):")
    width = max(len(name) for name in preset_names())
    for name in preset_names():
        entry = HARNESS_PRESETS[name]
        print(f"  {name:<{width}}  {entry['description']}")
        print(f"  {'':<{width}}  headless: {entry['headless']}")
        print(f"  {'':<{width}}  invoke: {format_preset_invoke(name)}")
        if entry.get("continue_argv"):
            # The grade decides whether a ROSTER may continue on this preset,
            # so a reader picking one here must see it — not discover it when
            # `roster check` disables the member.
            graded = continue_grade(name)
            if graded is None:
                note = "(roster `- **Continue:** on`)"
            elif graded[0] == CONTINUE_POOR:
                note = f"(NOT for a roster — {graded[0]}: {graded[1]})"
            else:
                note = f"(roster `- **Continue:** on`; {graded[0]})"
            print(f"  {'':<{width}}  continue: {' '.join(entry['continue_argv'])} {note}")
    print()
    print("Add one: r4t rig add <rig-name> <preset>")
    print("Example: r4t rig add worker opencode")
    return 0


def cmd_engine(args: argparse.Namespace) -> int:
    import engines

    if args.target == "check":
        return _cmd_engine_check(args, None)
    if args.target == "list":
        width = max(len(name) for name in engines.MODULES)
        for name in sorted(engines.MODULES):
            presets = sorted(
                [p for p, e in engines.PRESET_ENGINES.items() if e == name]
                + ([name] if name in HARNESS_PRESETS else [])
            )
            verbs = ", ".join(engines.capabilities(name)) or "-"
            served = f"  presets: {', '.join(presets)}" if presets else ""
            print(f"  {name:<{width}}  [{verbs}]{served}")
        print()
        print("Ask one: r4t engine <id> quota — or run a turn: r4t engine <id> run")
        return 0
    if not args.action:
        print("r4t engine: expected an action (quota, run, check)", file=sys.stderr)
        return 2
    if args.action == "run":
        return _cmd_engine_run(args)
    if args.action == "check":
        return _cmd_engine_check(args, args.target.strip().lower())
    try:
        payload = engines.quota(args.target)
    except engines.QuotaError as exc:
        print(f"r4t engine: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(engines.format_text(payload))
    # Three states, three exits: a live answer, a snapshot served because the
    # live check failed, and (via QuotaError above) no answer at all. They used
    # to be two, so a caller could only tell a working engine from a broken one
    # by reading prose — which is how one engine failed for eleven days behind
    # a plausible-looking reading.
    if payload.get("origin") == "snapshot":
        return QUOTA_EXIT_STALE
    return 0


def _cmd_engine_check(args: argparse.Namespace, engine: str | None) -> int:
    """`r4t engine <id> check`, or `r4t engine check` for every run-capable
    engine. Exits 1 when any composed argv is rejected; a CLI that is not
    installed is unverifiable, not a failure."""
    from engines import check as engine_check
    from engines import run as engine_run

    if engine is not None and engine not in engine_run.RUN_ENGINES:
        supported = ", ".join(sorted(engine_run.RUN_ENGINES))
        print(
            f"r4t engine: {args.target!r} does not support check "
            f"(engines: {supported})",
            file=sys.stderr,
        )
        return 1
    options = dict(
        model=args.model,
        permissions=args.permissions,
        allowed_tools=args.allowed_tools,
        continue_conversation=args.continue_conversation,
        workdir=Path(args.dir).expanduser().resolve() if args.dir else Path.cwd(),
    )
    reports = (
        engine_check.check_all(**options)
        if engine is None
        else [engine_check.check_engine(engine, **options)]
    )
    if args.as_json:
        print(json.dumps([r.as_dict() for r in reports], indent=2))
    else:
        print(engine_check.format_text(reports))
        print()
        print("No turn is spent: a check drives each CLI's own --help/--version.")
    return 1 if any(r.verdict == engine_check.REJECTED for r in reports) else 0


def _turn_dir(args: argparse.Namespace) -> Path:
    return Path(args.dir).expanduser().resolve() if args.dir else Path.cwd()


def _turn_prompt(
    args: argparse.Namespace, dir_path: Path, where: str
) -> tuple[str | None, int]:
    """The routed input for one headless turn — the `--idle` latch, the `-`
    stdin read, and the required positional — shared by `engine run` and
    `rig run` so both spell the latch the same way. Returns (prompt, 0) with
    the latch armed, or (None, exit code) when no turn should run: 0 when the
    latch already fired, 2 when PROMPT is missing."""
    from engines import run as engine_run

    marker = dir_path / engine_run.IDLE_MARKER_NAME
    if args.idle:
        if marker.exists():
            return None, 0
        marker.touch()
        prompt = args.prompt if args.prompt else engine_run.DEFAULT_IDLE_PROMPT
    else:
        marker.unlink(missing_ok=True)
        if not args.prompt:
            print(f"{where}: PROMPT is required unless --idle", file=sys.stderr)
            return None, 2
        prompt = args.prompt
    if prompt == "-":
        prompt = sys.stdin.read()
    return prompt, 0


def _cmd_engine_run(args: argparse.Namespace) -> int:
    from engines import run as engine_run

    # RUN_ENGINES holds preset ids directly (`ollama-claude`, not the quota
    # engine `ollama` it shares with the other three launchers), so this
    # checks the target itself rather than collapsing it through
    # `engines.engine_for` the way `quota` does.
    engine = args.target.strip().lower()
    if engine not in engine_run.RUN_ENGINES:
        supported = ", ".join(sorted(engine_run.RUN_ENGINES))
        print(
            f"r4t engine: {args.target!r} does not support run "
            f"(engines: {supported})",
            file=sys.stderr,
        )
        return 1

    # #155 rule 4 — an idle wake is a cold wake by definition, so the pair is
    # refused here rather than left to every definition author to remember.
    if args.idle and args.continue_conversation:
        print(
            "r4t engine run: --idle and --continue contradict — an idle wake is "
            "a cold start and never continues",
            file=sys.stderr,
        )
        return 2

    dir_path = _turn_dir(args)
    prompt, code = _turn_prompt(args, dir_path, "r4t engine run")
    if prompt is None:
        return code

    try:
        return engine_run.execute(
            engine,
            prompt,
            dir_path=dir_path,
            model=args.model,
            agent=args.agent,
            timeout=args.timeout,
            scaffold=not args.no_scaffold,
            echo=args.echo,
            lessons_cap=args.lessons_cap,
            continue_conversation=args.continue_conversation,
            permissions=args.permissions,
            allowed_tools=args.allowed_tools,
        )
    except engine_run.RunError as exc:
        print(f"r4t engine: {exc}", file=sys.stderr)
        return 1


RIG_BUDGET_POLL_SECONDS = 5.0


def _rig_pinned_model(rig) -> str | None:
    """The model this rig runs, in the form `build_preset_invoke` takes. Only
    the live-resolver presets (agy) record `model` as a setting; every other
    preset bakes the value into the invoke at `rig add --model` time, so the
    argv is the second place to look and `-` means the CLI's own default."""
    found = _rig_model(rig)
    return None if found == "-" else found


def _rig_budget_status(rig) -> tuple[float, float]:
    """The rig's machine-global bucket right now: (level, seconds until it
    holds one turn again). Both keys are validated together at load, so a
    budgeted rig always earns and the wait is always finite."""
    return (
        state.rig_budget_level(
            rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour
        ),
        state.rig_budget_seconds_until(
            rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour
        ),
    )


def _wait_for_rig_budget(rig) -> float:
    """Block until the bucket holds one turn, and return the seconds spent.
    One stderr line states the wait up front; the poll after it is silent, so
    a `--wait` in a pipeline is one line of noise rather than a ticker. The
    bucket is machine-global, so another node finishing can end the wait early
    — hence a poll rather than a single sleep."""
    started = time.time()
    _level, seconds = _rig_budget_status(rig)
    if seconds <= 0:
        return 0.0
    print(
        f"r4t rig run: rig {rig.name} is resting — waiting {seconds:.0f}s "
        f"(~{seconds / 60:.0f} min) for one turn's budget",
        file=sys.stderr,
    )
    while True:
        _level, remaining = _rig_budget_status(rig)
        if remaining <= 0:
            return time.time() - started
        time.sleep(min(remaining, RIG_BUDGET_POLL_SECONDS))


def _rig_engine_preset(
    rig, config_path: Path, name: str
) -> tuple[str | None, str | None]:
    """The preset naming this rig's engine, or the reason it has none. Every
    verb that reaches past the config to the engine behind a rig fails closed
    on the same three: no such rig, an invalid one, one with no preset."""
    if rig is None:
        return None, (
            f"rig {name!r} not found in {config_path} (fail closed) — "
            f"try: r4t rig presets, then r4t rig add {name} <preset>"
        )
    if rig.error:
        return None, f"rig {rig.name!r} is invalid: {rig.error}"
    if not rig.preset:
        return None, (
            f"rig {rig.name!r} has no preset, so there is no engine behind it "
            f"— try: r4t rig swap {rig.name} <preset>"
        )
    return rig.preset, None


def _rig_run_engine(rig, config_path: Path, name: str) -> tuple[str | None, str | None]:
    """The run-capable engine this rig rides, or the reason it has none."""
    from engines import run as engine_run

    preset, problem = _rig_engine_preset(rig, config_path, name)
    if preset is None:
        return None, problem
    if preset not in engine_run.RUN_ENGINES:
        return None, (
            f"rig {rig.name!r} rides preset {preset!r}, which does not "
            f"support run (engines: {', '.join(sorted(engine_run.RUN_ENGINES))}) — "
            f"try: r4t rig swap {rig.name} <preset>"
        )
    return rig.preset, None


def cmd_rig_run(args: argparse.Namespace) -> int:
    """One headless turn as a named rig: `engine run`'s composition with the
    rig's own engine, model, stance and env already applied, gated on the
    rig's machine-global budget. The engine layer is bare metal; this layer is
    what the rig has been tuned to."""
    from engines import run as engine_run

    config_path = resolve_config_path(args.rig_config)
    try:
        config = load_rig_config(config_path)
    except RigError as exc:
        print(f"r4t rig run: {exc}", file=sys.stderr)
        return 1
    name = args.rig.strip().lower()
    rig = None if config.missing else config.rigs.get(name)
    engine, problem = _rig_run_engine(rig, config_path, args.rig.strip())
    if engine is None:
        print(f"r4t rig run: {problem}", file=sys.stderr)
        return 1

    # #155 rule 4, as `engine run` enforces it: an idle wake is a cold wake.
    if args.idle and args.continue_conversation:
        print(
            "r4t rig run: --idle and --continue contradict — an idle wake is "
            "a cold start and never continues",
            file=sys.stderr,
        )
        return 2
    if args.wait and args.now:
        print(
            "r4t rig run: --wait and --now contradict — one holds for the "
            "budget, the other spends past it",
            file=sys.stderr,
        )
        return 2

    dir_path = _turn_dir(args)
    report: dict = {
        "rig": rig.name,
        "engine": engine,
        "dir": str(dir_path),
        "ran": False,
        "reason": "ran",
        "exit_code": 0,
        "budget": None,
    }

    def finish(code: int, reason: str, ran: bool = False) -> int:
        report.update(exit_code=code, reason=reason, ran=ran)
        if args.as_json:
            # stderr, not stdout: `engine run` keeps stdout the engine's own
            # reply stream byte for byte, and a summary object printed into
            # the middle of it would corrupt whatever reads the reply.
            print(json.dumps(report), file=sys.stderr)
        return code

    # Peeked before the gate so `--wait` never blocks for a turn the latch
    # would skip anyway; `_turn_prompt` is what actually arms it.
    if args.idle and (dir_path / engine_run.IDLE_MARKER_NAME).exists():
        return finish(0, "idle-latched")

    budgeted = rig.rig_budget_max is not None
    if budgeted:
        level, seconds = _rig_budget_status(rig)
        report["budget"] = {
            "max": rig.rig_budget_max,
            "earn_per_hour": rig.rig_budget_earn_per_hour,
            "level_before": round(level, 4),
            "level_after": round(level, 4),
            "waited_seconds": 0.0,
            "forced": bool(args.now),
        }
        if seconds > 0 and not args.now:
            if not args.wait:
                report["budget"]["seconds_until"] = round(seconds, 3)
                print(
                    f"r4t rig run: rig {rig.name} is resting — budget "
                    f"{state.fmt_budget(level)}/"
                    f"{state.fmt_budget(rig.rig_budget_max)}, one turn back in "
                    f"~{seconds / 60:.0f} min ({seconds:.0f}s). Hold for it "
                    f"with --wait, or spend past it with --now.",
                    file=sys.stderr,
                )
                # Exit 1, not a code of its own: docs/ar3-foundation.md reserves exit-code
                # meanings to the foundation. `--json`'s `reason` is where a
                # caller tells a resting rig from a failed turn.
                return finish(1, "resting")
            waited = _wait_for_rig_budget(rig)
            report["budget"]["waited_seconds"] = round(waited, 3)
            report["budget"]["level_before"] = round(_rig_budget_status(rig)[0], 4)

    prompt, code = _turn_prompt(args, dir_path, "r4t rig run")
    if prompt is None:
        return finish(code, "idle-latched" if code == 0 else "usage")

    # Charged inside execute, immediately before the spawn: a turn refused at
    # composition (a bad per-run override) costs nothing, while a harness that
    # fails to start has already paid — dispatch's own boundary. The bucket
    # clamps at zero, so `--now` on an empty one spends to the floor and stops
    # there rather than running up a debt.
    def _charge() -> None:
        report["budget"]["level_after"] = round(
            state.rig_budget_charge(
                rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour
            ),
            4,
        )

    try:
        exit_code = engine_run.execute(
            engine,
            prompt,
            dir_path=dir_path,
            model=resolve_override(args.model, _rig_pinned_model(rig)),
            agent=args.agent,
            timeout=(
                args.timeout if args.timeout is not None else int(rig.timeout_seconds)
            ),
            scaffold=not args.no_scaffold,
            echo=args.echo,
            lessons_cap=args.lessons_cap,
            continue_conversation=args.continue_conversation,
            permissions=resolve_override(args.permissions, rig.permissions),
            allowed_tools=resolve_override(args.allowed_tools, rig.allowed_tools),
            env={**os.environ, **rig.env} if rig.env else None,
            charge_hook=_charge if budgeted else None,
        )
    except engine_run.RunError as exc:
        print(f"r4t rig run: {exc}", file=sys.stderr)
        return finish(1, "error")
    return finish(exit_code, "ran", ran=True)


def _format_fuel(report: dict) -> str:
    import engines

    level = report["fuel"]
    lines = [
        f"{report['rig']} — fuel "
        + (f"{level:.2f}" if level is not None else f"unknown ({report['state']})")
    ]
    model = report["model"] or "the preset's default"
    lines.append(f"  engine: {report['preset']} (model: {model})")
    if report["quota_engine"] != report["preset"]:
        lines.append(f"  quota engine: {report['quota_engine']}")
    if report.get("plan"):
        lines.append(f"  plan: {report['plan']}")
    if report.get("origin") == "snapshot":
        age = engines.format_age(report.get("age_seconds"))
        lines.append(f"  source: snapshot from {age} ago")
    # Above the numbers, for the same reason `engines.format_text` puts it there.
    if report.get("note"):
        lines.append(f"  note: {report['note']}")
    binding = engines.binding_index(report["buckets"])
    for position, bucket in enumerate(report["buckets"]):
        fraction = bucket.get("remaining_fraction")
        mark = "*" if position == binding else " "
        value = (
            f"{round(fraction * 100)}% remaining"
            if isinstance(fraction, (int, float))
            else "unknown"
        )
        reset = bucket.get("reset_time")
        lines.append(
            f" {mark}{bucket.get('label', 'Quota')}: {value}"
            + (f" · resets {reset}" if reset else "")
        )
    if not report["buckets"]:
        lines.append("  no bucket constrains this model")
    return "\n".join(lines)


def cmd_rig_fuel(args: argparse.Namespace) -> int:
    """How much of this rig's tank is left, as one number in 0..1: the
    engine's quota narrowed to the buckets its model burns, reduced to the
    binding one. Reads only — no turn runs and no budget moves."""
    import engines

    config_path = resolve_config_path(args.rig_config)
    try:
        config = load_rig_config(config_path)
    except RigError as exc:
        print(f"r4t rig fuel: {exc}", file=sys.stderr)
        return 1
    name = args.rig.strip().lower()
    rig = None if config.missing else config.rigs.get(name)
    preset, problem = _rig_engine_preset(rig, config_path, args.rig.strip())
    if preset is None:
        print(f"r4t rig fuel: {problem}", file=sys.stderr)
        return 1
    try:
        report = {
            "rig": rig.name,
            **engines.fuel(preset, _rig_pinned_model(rig)),
        }
    except engines.QuotaError as exc:
        print(f"r4t rig fuel: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if args.as_json else _format_fuel(report))
    return 0


def cmd_rig_add(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.rig_config)
    preset_key = args.preset.strip().lower()
    try:
        rig_key = add_preset_rig(
            config_path,
            args.rig,
            args.preset,
            model=args.model,
            force=args.force,
        )
        invoke = build_preset_invoke(preset_key, model=args.model)
    except RigError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"added rig {rig_key!r} ({args.preset}) to {config_path}")
    print(f"  invoke: {' '.join(invoke)}")
    _print_model_note(preset_key, args.model)
    print(f"Reference it from your runbook: `- **Rig:** {rig_key}`")
    return 0


def cmd_rig_swap(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.rig_config)
    preset_key = args.preset.strip().lower()
    try:
        rig_key = swap_preset_rig(
            config_path,
            args.rig,
            args.preset,
            model=args.model,
        )
        invoke = build_preset_invoke(preset_key, model=args.model)
    except RigError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"swapped rig {rig_key!r} to {args.preset} in {config_path}")
    print(f"  invoke: {' '.join(invoke)}")
    _print_model_note(preset_key, args.model)
    return 0


def _print_model_note(preset_key: str, model: str | None) -> None:
    if model and HARNESS_PRESETS.get(preset_key, {}).get("model_resolver") == "agy-live":
        print(
            f"  model: {model.strip()!r} — resolved live against `agy models` "
            f"before every turn"
        )


def _rig_usage(config, roster, rig_key: str) -> list[str]:
    """Members and pins still pointing at rig_key — used to refuse a remove that
    would strand a live roster."""
    users: list[str] = []
    for agent, pinned in config.pins.items():
        if pinned == rig_key:
            users.append(f"{agent} (pinned)")
    if roster is not None:
        for m in roster.members:
            if (m.rig or "").strip().lower() == rig_key:
                users.append(m.name)
    return users


def cmd_rig_remove(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.rig_config)
    try:
        config = load_rig_config(config_path)
    except RigError as e:
        print(str(e), file=sys.stderr)
        return 1
    roster = None
    if not args.force:
        roster_path = resolve_roster_path(
            _resolve_root(getattr(args, "root", None)), getattr(args, "roster", None)
        )
        if roster_path.is_file():
            try:
                roster = load_roster(roster_path)
            except RosterError:
                roster = None
    rc = 0
    for name in args.rigs:
        rig_key = name.strip().lower()
        users = [] if args.force else _rig_usage(config, roster, rig_key)
        if users:
            print(
                f"rig {rig_key!r} still used by {', '.join(users)}; not removed "
                f"(try: repoint them, or r4t rig remove {rig_key} --force)",
                file=sys.stderr,
            )
            rc = 1
            continue
        try:
            remove_rig(config_path, name)
        except RigError as e:
            print(str(e), file=sys.stderr)
            rc = 1
            continue
        print(f"removed rig {rig_key!r} from {config_path}")
    return rc


def _setting_bracket(s) -> str:
    return f"[{s.display()}]" if s.explicit else f"[{s.display()}, {s.source}]"


def cmd_rig_configure(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.rig_config)
    rig_key = args.rig.strip().lower()
    try:
        settings = rig_settings(config_path, args.rig)
    except RigError as e:
        print(str(e), file=sys.stderr)
        return 1
    interactive = sys.stdin.isatty()
    print(f"Configuring rig {rig_key!r} in {config_path} — Enter keeps the current value.")
    for s in settings:
        while True:
            try:
                typed = input(f"{s.key} {_setting_bracket(s)}: ").strip()
            except EOFError:
                print()
                return 0
            if typed == "":
                break
            try:
                set_rig_value(config_path, args.rig, s.key, typed)
                break
            except RigError as e:
                print(str(e), file=sys.stderr)
                if interactive:
                    continue
                return 1
    return 0


def cmd_rig_set(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.rig_config)
    rig_key = args.rig.strip().lower()
    try:
        s = set_rig_value(config_path, args.rig, args.key, args.value)
    except RigError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"set {rig_key} {s.key} = {s.display()} in {config_path}")
    if s.key == "permissions":
        # The rig is where the stance lives, so this is where the operator
        # hears what it actually buys — the engine-run flag says it per turn.
        note = permission_ceiling_note(rig_preset(config_path, rig_key), s.value)
        if note:
            print(f"r4t rig: {note}", file=sys.stderr)
    return 0


def cmd_rig_get(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.rig_config)
    if args.key:
        try:
            s = rig_setting(config_path, args.rig, args.key)
        except RigError as e:
            print(str(e), file=sys.stderr)
            return 1
        print("" if s.value is None else s.display())
        print(f"({s.source})", file=sys.stderr)
        return 0
    try:
        settings = rig_settings(config_path, args.rig)
    except RigError as e:
        print(str(e), file=sys.stderr)
        return 1
    width = max(len(s.key) for s in settings)
    for s in settings:
        print(f"{s.key:<{width}}  {s.display()}  ({s.source})")
    return 0


def cmd_rig_unset(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.rig_config)
    rig_key = args.rig.strip().lower()
    rc = 0
    for key in args.keys:
        try:
            removed = unset_rig_value(config_path, args.rig, key)
        except RigError as e:
            print(str(e), file=sys.stderr)
            rc = 1
            continue
        label = setting_label(key)
        if removed:
            print(f"unset {rig_key} {label} in {config_path}")
        else:
            print(f"{rig_key} {label} was not explicitly set; nothing to unset")
    return rc


def _name_shadow_warnings(roster) -> list[str]:
    """Where a member's name also names something outside the wall.

    The leader stands in the doorway: it addresses roster members and
    registered a8s nodes with the same verb. In-roster wins, so a shadowed
    outside name is simply unreachable from this leader — deliberate, and
    silent, which is the part worth saying out loud. Nothing is blocked;
    a single-owner network rarely collides, and when it does the operator
    may well mean it.
    """
    visible = visible_a8s_names()
    if not visible:
        return []
    out: list[str] = []
    for m in roster.members:
        kind = visible.get(m.name) or visible.get(m.name.lower())
        if kind is None:
            continue
        out.append(
            f"{m.name}: also names an a8s {kind} visible from this host — "
            f"inside the roster the member wins; reach the a8s {kind} as "
            f"`:{m.name.lower()}`"
        )
    return out


def _store_in_workplace_warnings(roster, root: Path, workplace: Path) -> list[str]:
    """Where a member's knowledge store sits inside the shared workplace.

    A container holds the stores by never mounting `R4T_HOME`; it mounts the
    workplace read-write at its real path. So `R4T_HOME` under the workplace
    is the one placement that hands every store back to the member — its own
    and its siblings'.
    """
    node = state.node_for_root(root)
    if node is None:
        rosters = state.known_rosters()
        node = rosters[0] if len(rosters) == 1 else None
    if node is None:
        return []
    work = workplace.expanduser().resolve()
    out: list[str] = []
    for m in roster.members:
        if m.errors or not m.knowledge_on:
            continue
        store = knowledge.store_home(node, m.name).expanduser().resolve()
        if not store.is_relative_to(work):
            continue
        out.append(
            f"{m.name}: knowledge store {store} is inside the workplace "
            f"{work}, which a container mounts read-write — the cage cannot "
            f"hold a store it mounts (put R4T_HOME outside the workplace)"
        )
    return out


def cmd_roster_check(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    node = _runbook_node(args, root)
    org = load_org(root)
    root = org.dir
    problems = 0
    warnings = 0
    conflict = runbook.legacy_conflict(root)
    if conflict:
        print(f"warning: {conflict}")
        warnings += 1
    for message in check_org(root):
        print(f"org: {message}")
        problems += 1
    roster_path = resolve_roster_path(root, args.roster, node)
    book: runbook.Runbook | None = None
    if runbook.is_runbook(roster_path):
        try:
            # The one caller that skips leader validation: `check` exists to
            # name what is wrong, so it has to be able to load a wrong one.
            book = runbook.load_runbook(roster_path, node=node, validate=False)
        except runbook.RunbookError as e:
            print(str(e), file=sys.stderr)
            return 1
        roster = book.roster
        print(f"runbook: {' -> '.join(book.chain)}")
    else:
        try:
            roster = load_roster(roster_path, validate=False, node=node)
        except RosterError as e:
            print(str(e), file=sys.stderr)
            return 1
    config_path = resolve_config_path(args.rig_config)
    try:
        config = load_rig_config(config_path)
    except RigError as e:
        print(f"warning: {e}", file=sys.stderr)
        config = None

    if not roster.members:
        print(f"{roster_path}: no `### <Name>` member blocks found")
        problems += 1
    for m in roster.members:
        for err in m.errors:
            print(f"{m.name}: {err}")
            problems += 1
        if config is not None and not m.errors:
            rig, err, _pinned = config.rig_for(m)
            if rig is None:
                print(f"{m.name}: {err}")
                problems += 1
    leader_problem = roster.leader_problem()
    if leader_problem is not None:
        print(f"roster {leader_problem}")
        problems += 1
    if book is not None:
        for cell in book.cells.values():
            for err in cell.errors:
                print(f"cell {cell.name}: {err}")
                problems += 1
        for ritual in book.rituals.values():
            for err in ritual.errors:
                print(f"ritual {ritual.name}: {err}")
                problems += 1
        for name, rig in sorted(book.rigs.items()):
            if rig.error:
                print(f"rig {name}: {rig.error}")
                problems += 1
        for message in book.warnings:
            print(f"warning: {message}")
            warnings += 1
    if config is not None:
        for m in roster.members:
            if m.errors or not m.knowledge_on:
                continue
            if m.knowledge_distill_rig:
                distill_rig, distill_err = knowledge.resolve_distill_rig(m, config)
                if distill_rig is None:
                    print(f"{m.name}: {distill_err}")
                    problems += 1
                    continue
            else:
                distill_rig, _err, _pinned = config.rig_for(m)
            if distill_rig is not None and is_below_knowledge_floor(distill_rig.preset):
                print(
                    f"warning: {m.name}: Knowledge is on with rig "
                    f"{distill_rig.name!r} — a small-model class that smooths "
                    "specifics out of distilled notes; consider a distill-rig "
                    "override, and note budgets are bytes, not tokens "
                    "(see docs/r4t-knowledge.md)"
                )
                warnings += 1
    for m in roster.members:
        if len(m.reinforce) > 200:
            print(
                f"warning: {m.name}: Reinforce is {len(m.reinforce)} characters — "
                "a paragraph is a mission, not a reinforcement "
                "(try: one line under 200)"
            )
            warnings += 1
    if config is not None:
        for message in continue_collisions(roster, config, org.workplace):
            print(f"warning: {message}")
            warnings += 1
        # A problem, not a warning: these are the turns run_harness refuses.
        for message in mcp_home_refusals(roster, config, org.isolation):
            print(message)
            problems += 1
    for message in _name_shadow_warnings(roster):
        print(f"warning: {message}")
        warnings += 1
    for message in _store_in_workplace_warnings(roster, root, org.workplace):
        print(f"warning: {message}")
        warnings += 1
    for severity, message in roster.tree_problems():
        if severity == "error":
            print(message)
            problems += 1
        else:
            print(f"warning: {message}")
            warnings += 1
    mission = runbook.mission_text(root, node)
    n = sum(1 for line in mission.splitlines() if line.strip())
    if n > 40:
        label = "the mission" if book is not None else "MISSION.md"
        print(f"warning: {label} is {n} lines — intent docs read best under one page")
        warnings += 1
    if problems:
        print(f"{problems} problem(s)")
        return 1
    tail = f", {warnings} warning(s)" if warnings else ""
    print(
        f"{roster_path}: OK ({len(roster.members)} member(s), "
        f"leader {roster.leader().name}{tail})"
    )
    return 0


def cmd_runbook_show(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    path = runbook.runbook_path(root)
    if not path.is_file():
        print(f"no {runbook.RUNBOOK_NAME} under {root}", file=sys.stderr)
        return 1
    if not (args.resolved or args.sources):
        sys.stdout.write(path.read_text(encoding="utf-8"))
        return 0
    try:
        book = runbook.load_runbook(path, node=_runbook_node(args, root), validate=False)
    except runbook.RunbookError as e:
        print(str(e), file=sys.stderr)
        return 1
    sys.stdout.write(runbook.render(book, sources=args.sources))
    return 0


def cmd_runbook_check(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    if not runbook.has_runbook(root):
        print(f"no {runbook.RUNBOOK_NAME} under {root}", file=sys.stderr)
        return 1
    # One linter, not two: a runbook IS the roster, so the checks that name a
    # broken member, a missing rig or a leaderless roster are the same checks.
    return cmd_roster_check(args)


def _node_name_for(root: Path) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", root.name.lower()).strip("-")


def cmd_init(args: argparse.Namespace) -> int:
    """Write the file. `r4t add` registers the node — two verbs, one each."""
    root = _resolve_root(args.root)
    if not root.is_dir():
        print(f"init: not a directory: {root}", file=sys.stderr)
        return 1

    path = runbook.runbook_path(root)
    if path.is_file():
        print(f"runbook: {path} exists, left unchanged")
    else:
        name = _node_name_for(root) or "roster"
        path.write_text(
            RUNBOOK_TEMPLATE.format(name=name, title=root.name, root=root),
            encoding="utf-8",
        )
        print(f"runbook: wrote starter {path}")
    print(f"next: r4t add {root}")
    return 0


def _resolve_add_runbook(root: Path, raw: str | None) -> tuple[Path, bool]:
    """The runbook this node runs, and whether it is the node dir's own.

    Named like an a8s definition: a built-in by bare name, or a path. Naming
    nothing takes the `r4t.md` already at the directory, which is where a
    runbook normally lives — and which wins over anything named here, so
    naming one for a directory that has its own is refused rather than
    silently ignored.
    """
    own = runbook.runbook_path(root)
    builtins = ", ".join(runbook.builtin_names())
    if not raw:
        if own.is_file():
            return own, True
        raise RosterError(
            f"add: {root} carries no {runbook.RUNBOOK_NAME} — name a runbook "
            f"(built-ins: {builtins}), or write one with `r4t init {root}`"
        )
    if own.is_file():
        raise RosterError(
            f"add: {own} is this directory's runbook, so {raw!r} would never "
            f"run — drop the argument (or delete the file to use {raw!r})"
        )
    if runbook.looks_like_path(raw):
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.is_file():
            raise RosterError(f"add: no runbook at {candidate}")
        return candidate.resolve(), False
    candidate = runbook.BUILTIN_DIR / f"{raw}.md"
    if not candidate.is_file():
        raise RosterError(
            f"add: {raw!r} names no built-in runbook — built-ins are: "
            f"{builtins} (a path must start with ./ or ../ or end in .md)"
        )
    return candidate.resolve(), False


def _a8s(*argv: str) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(A8S_PY), *argv], capture_output=True, text=True
    )
    detail = (result.stderr or result.stdout or "").strip()
    return result.returncode, detail


def cmd_add(args: argparse.Namespace) -> int:
    """One name registers everything.

    The directory, the a8s agent, the namespace prefix and the address a
    person types are all the same word. a8s permits a prefix that binds the
    agent of its own name, so nothing here needs a `-node` suffix, and
    `a8s ls` and `r4t status` say the same thing.
    """
    root = Path(args.dir).expanduser()
    if not root.is_dir():
        print(f"add: not a directory: {root}", file=sys.stderr)
        return 1
    root = root.resolve()

    name = (args.name or _node_name_for(root)).strip().lower()
    if not runbook.NAME_RE.fullmatch(name):
        print(
            f"add: {name!r} is not a node name — letters, digits, underscore "
            f"and hyphen only, starting with a letter or digit. A colon "
            f"separates a node from a member and can never be inside either "
            f"(try: r4t add {root} --name <name>)",
            file=sys.stderr,
        )
        return 2

    taken = visible_a8s_names()
    kind = taken.get(name) or taken.get(name.lower())
    if kind:
        print(
            f"add: {name} already names an a8s {kind} — a node is registered "
            f"once. The runbook is re-read every turn, so a changed roster "
            f"needs no re-add; to move or rebuild this one, "
            f"`a8s remove {name}` first.",
            file=sys.stderr,
        )
        return 1

    try:
        book_path, own = _resolve_add_runbook(root, args.runbook)
    except RosterError as e:
        print(str(e), file=sys.stderr)
        return 2

    # The node's own state has to exist before the runbook is read — a
    # `${VAR}` and a relative `Workdir:` both resolve against the node, not
    # against wherever the file happens to sit. A failure past here unwinds
    # it, so a refused `add` leaves nothing behind: half a registration is a
    # phantom roster in `r4t status`, and a raised ceiling waiting for a node
    # that never arrived is worse than that.
    existing = state.roster_dir(name).is_dir()
    if args.trust:
        raise_machine_ceiling(name)
    state.stamp_root(name, root)

    def refuse(message: str | None, code: int) -> int:
        if not existing:
            shutil.rmtree(state.roster_dir(name), ignore_errors=True)
        if message:
            print(message, file=sys.stderr)
        return code

    check = argparse.Namespace(
        root=str(root),
        roster=None if own else str(book_path),
        rig_config=getattr(args, "rig_config", None),
        node=name,
        definition=None,
    )
    if cmd_roster_check(check) != 0:
        return refuse(
            f"add: {book_path} does not check out — nothing registered", 2
        )
    try:
        roster = load_roster(book_path, validate=False, node=name)
    except RosterError as e:
        return refuse(str(e), 2)
    shadow = roster.find(name)
    if shadow is not None and not shadow.leader:
        return refuse(
            f"add: member {shadow.name!r} shares the node name but is not the "
            f"leader — `tell {name}` would reach the leader and "
            f"`tell {name}:{name}` would reach this member. Make it the "
            f"leader, or rename it.",
            2,
        )

    for argv in (
        ("add", name, str(root), "r4t"),
        ("namespace", name, name),
        ("start", name),
    ):
        code, detail = _a8s(*argv)
        if code != 0:
            return refuse(f"add: a8s {' '.join(argv)} failed: {detail}", 1)

    state.stamp_runbook(name, None if own else book_path)
    print()
    print(f"added {name} -> {root}")
    print(f"  runbook:   {book_path}")
    print(f"  address:   {name} (leader {roster.leader().name}), "
          f"{name}:<member> for a member with Ingress:")
    print(f"  ceiling:   permissions {machine_ceiling(name)}")
    print()
    print(f'  tell {name} "hello"')
    return 0


def cmd_sandbox(args: argparse.Namespace) -> int:
    from sandbox import run_sandbox

    return run_sandbox(
        fake=args.fake,
        timeout=args.timeout,
        preset=args.preset,
        model=args.model,
        break_member=args.break_member,
    )


def cmd_lab_overview(_args: argparse.Namespace) -> int:
    from lab import cmd_list

    return cmd_list()


def cmd_lab_list(_args: argparse.Namespace) -> int:
    from lab import cmd_list

    return cmd_list()


def cmd_lab_run(args: argparse.Namespace) -> int:
    from lab import cmd_run

    overrides: dict[str, str] = {}
    for item in args.rig or []:
        if "=" not in item:
            print(f"lab run: --rig expects ROLE=RIG, got {item!r}", file=sys.stderr)
            return 2
        role, rig = item.split("=", 1)
        overrides[role.strip()] = rig.strip()
    return cmd_run(
        args.name, arm=args.arm, n=args.trials, fake=args.fake,
        rig_overrides=overrides, rig_config=args.rig_config,
    )


def cmd_lab_report(args: argparse.Namespace) -> int:
    from lab import cmd_report

    return cmd_report(args.name)


def cmd_lab_ledger(args: argparse.Namespace) -> int:
    from lab import cmd_ledger

    return cmd_ledger(args.name, as_json=args.json)


def _add_common(p: argparse.ArgumentParser, *, with_node: bool = False) -> None:
    p.add_argument("--root", help="Roster repo root (default: cwd).")
    p.add_argument(
        "--roster",
        help="Roster path, absolute or root-relative (default: <root>/r4t.md, "
        "else <root>/ROSTER.md).",
    )
    p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    p.add_argument(
        "--definition",
        help="This node's a8s definition path ($DEFINITION_PATH); read for "
        "prompt overrides under its `prompts` key.",
    )
    if with_node:
        p.add_argument("--node", help="Roster node name (default: sole ~/.config/r4t roster).")


def _add_tell_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--simulate-tell",
        action="store_true",
        help="Print would-be tell calls to stderr instead of invoking tell "
        "(also R4T_SIMULATE_TELL=1).",
    )
    p.add_argument(
        "--no-notify",
        dest="notify",
        action="store_false",
        default=True,
        help="Drop tell output entirely (unit tests).",
    )


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {raw!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="r4t",
        description="The roster — define the team in one r4t.md; govern turns on a8s.",
    )
    p.add_argument("--version", action="version", version=version_line("r4t"))
    sub = p.add_subparsers(dest="command", required=False, metavar="COMMAND")
    p.set_defaults(func=cmd_default)

    init_p = sub.add_parser(
        "init",
        help=_cmd_help("init"),
        description="Write a starter r4t.md that extends the built-in "
        "`triforce` runbook. That is all it does: `r4t add` registers the "
        "node.",
    )
    init_p.add_argument("--root", help="Repo to initialize (default: cwd).")
    init_p.set_defaults(func=cmd_init)

    add_p = sub.add_parser(
        "add",
        help=_cmd_help("add"),
        description="Register a directory as a node: validate its runbook, "
        "then bind the a8s agent, the namespace prefix and the address you "
        "mail — all under one name, the directory's own.",
    )
    add_p.add_argument("dir", help="The node directory.")
    add_p.add_argument(
        "runbook",
        nargs="?",
        help="A built-in runbook by name (see `r4t runbook show`), or a path. "
        "Omit it when the directory already carries an r4t.md.",
    )
    add_p.add_argument(
        "--name",
        help="Node name (default: the directory's, lowercased). One name is "
        "the agent, the namespace and what you type.",
    )
    add_p.add_argument(
        "--trust",
        action="store_true",
        help="Raise this node's permission ceiling, so its runbook may name "
        "`--permissions bypass`. Recorded on this machine, never in the repo.",
    )
    add_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    add_p.set_defaults(func=cmd_add)

    runbook_p = sub.add_parser(
        "runbook",
        help=_cmd_help("runbook"),
        description="The one file that says what the team is: r4t.md at the "
        "node dir. `show --resolved` prints the merged, interpolated truth — "
        "with inheritance, the file you read is not the file that runs, and "
        "this is the command that closes the gap. `--sources` names the layer "
        "every section came from.",
    )
    runbook_sub = runbook_p.add_subparsers(dest="action", required=True)
    runbook_show_p = runbook_sub.add_parser(
        "show", help="Print the runbook, as written or as resolved."
    )
    runbook_show_p.add_argument("--root", help="Node directory (default: cwd).")
    runbook_show_p.add_argument(
        "--node",
        help="Node whose a8s vars ${VAR} resolves against (default: inferred).",
    )
    runbook_show_p.add_argument(
        "--resolved",
        action="store_true",
        help="Print the merged, interpolated result instead of the file.",
    )
    runbook_show_p.add_argument(
        "--sources",
        action="store_true",
        help="Annotate every section with the layer it came from (implies "
        "--resolved).",
    )
    runbook_show_p.set_defaults(func=cmd_runbook_show)
    runbook_check_p = runbook_sub.add_parser("check", help="Lint the runbook.")
    _add_common(runbook_check_p, with_node=True)
    runbook_check_p.set_defaults(func=cmd_runbook_check)

    roster_p = sub.add_parser("roster", help=_cmd_help("roster"))
    roster_sub = roster_p.add_subparsers(dest="action", required=True)
    roster_check_p = roster_sub.add_parser("check", help="Lint the roster.")
    _add_common(roster_check_p, with_node=True)
    roster_check_p.set_defaults(func=cmd_roster_check)

    from engines.run import LESSONS_CAP_LINES

    rig_p = sub.add_parser(
        "rig",
        aliases=["rigs"],
        help=_cmd_help("rig"),
        description="Harness config commands (bare: overview + next steps).",
    )
    rig_p.set_defaults(func=cmd_rig_overview)
    rig_sub = rig_p.add_subparsers(dest="action", required=False)
    rig_list_p = rig_sub.add_parser(
        "list",
        aliases=["ls"],
        help="Show configured rigs and resolved roster rigs.",
    )
    _add_common(rig_list_p)
    rig_list_p.add_argument(
        "--wide",
        action="store_true",
        help="Add each rig's full invoke line as a trailing column.",
    )
    rig_list_p.set_defaults(func=cmd_rig_list)

    rig_presets_p = rig_sub.add_parser(
        "presets",
        help="List named CLI presets aligned with a8s definitions.",
    )
    rig_presets_p.set_defaults(func=cmd_rig_presets)

    rig_run_p = rig_sub.add_parser(
        "run",
        help="One headless turn as this rig: its engine, model and budget.",
        description="One headless turn as a named rig — the same composition "
        "`r4t engine <id> run` makes, with the rig's own preset, model, "
        "permission stance, tool allowlist, timeout and env map already "
        "applied, and gated on the rig's machine-global budget. Per-invocation "
        "flags win over the rig, and the rig wins over the preset. Continuation "
        "is a per-invocation choice (`--continue`), never a rig key.",
    )
    rig_run_p.add_argument("rig", help="Symbolic rig name (see `r4t rig list`).")
    rig_run_p.add_argument(
        "prompt",
        nargs="?",
        metavar="PROMPT",
        help="The message text ('-' reads stdin); required unless --idle.",
    )
    rig_run_p.add_argument(
        "--wait",
        action="store_true",
        help="Hold until the rig's budget allows a turn, then run it.",
    )
    rig_run_p.add_argument(
        "--now",
        action="store_true",
        help="Run even with the budget empty; the bucket still pays, to its floor.",
    )
    rig_run_p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print one JSON object about the turn to stderr (stdout stays "
        "the engine's own reply stream).",
    )
    rig_run_p.add_argument(
        "--dir", metavar="DIR", help="Working directory for the turn (default: CWD)."
    )
    rig_run_p.add_argument(
        "--model", metavar="M", help="Model for this turn, overriding the rig's."
    )
    rig_run_p.add_argument(
        "--agent",
        metavar="NAME",
        help="Adds an `a8s convo NAME` reconcile step to the scaffold.",
    )
    rig_run_p.add_argument(
        "--timeout",
        type=int,
        help="Turn timeout in seconds (default: the rig's timeout_seconds).",
    )
    rig_run_p.add_argument(
        "--no-scaffold",
        action="store_true",
        dest="no_scaffold",
        help="Send PROMPT unchanged, without the cold-boot scaffold.",
    )
    rig_run_p.add_argument(
        "--idle",
        action="store_true",
        help="Skip if the last turn was also idle, else run one and re-arm.",
    )
    rig_run_p.add_argument(
        "--lessons-cap",
        type=_positive_int,
        default=LESSONS_CAP_LINES,
        dest="lessons_cap",
        help="Line cap before rotating oldest LESSONS.md lines to "
        f"LESSONS-ARCHIVE.md (default: {LESSONS_CAP_LINES}).",
    )
    rig_run_p.add_argument(
        "--echo",
        action="store_true",
        help="Print the composed argv and prompt to stderr before running.",
    )
    rig_run_p.add_argument(
        "--continue",
        action="store_true",
        dest="continue_conversation",
        help="Resume the conversation this CLI already has in --dir. The "
        "caller asserts this turn continues live work; an idle or independent "
        "wake must not pass it.",
    )
    rig_run_p.add_argument(
        "--permissions",
        metavar="MODE",
        choices=list(PERMISSION_MODES),
        help="Permission stance for this turn, overriding the rig's: ask, "
        "auto or bypass.",
    )
    rig_run_p.add_argument(
        "--allowed-tools",
        metavar="SPEC",
        dest="allowed_tools",
        help="Tool-allowlist string for this turn, overriding the rig's.",
    )
    rig_run_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    rig_run_p.set_defaults(func=cmd_rig_run)

    rig_fuel_p = rig_sub.add_parser(
        "fuel",
        help="How much subscription this rig's model has left, as 0..1.",
        description="The rig's tank as one number in 0..1. `r4t engine <id> "
        "quota` reports every dial an engine has; fuel keeps the ones the "
        "rig's own model burns and reports the binding one, since that is the "
        "constraint the next turn hits. Nothing runs and no budget moves.",
    )
    rig_fuel_p.add_argument("rig", help="Symbolic rig name (see `r4t rig list`).")
    rig_fuel_p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Machine-readable JSON instead of the text lines.",
    )
    rig_fuel_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    rig_fuel_p.set_defaults(func=cmd_rig_fuel)

    rig_add_p = rig_sub.add_parser(
        "add",
        help="Add a symbolic rig from a named CLI preset.",
    )
    rig_add_p.add_argument(
        "rig",
        help="Symbolic rig name (referenced from runbook Rig lines).",
    )
    rig_add_p.add_argument(
        "preset",
        choices=preset_names(),
        help="CLI preset name (see `r4t rig presets`).",
    )
    rig_add_p.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing rig with the same name.",
    )
    rig_add_p.add_argument(
        "--model",
        metavar="MODEL",
        help="Optional model for the preset (required for ollama; agy resolves it live).",
    )
    rig_add_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    rig_add_p.set_defaults(func=cmd_rig_add)

    rig_swap_p = rig_sub.add_parser(
        "swap",
        help="Switch an existing rig to a preset, keeping its other settings.",
    )
    rig_swap_p.add_argument(
        "rig",
        help="Symbolic rig name already present in the rig config.",
    )
    rig_swap_p.add_argument(
        "preset",
        choices=preset_names(),
        help="CLI preset name (see `r4t rig presets`).",
    )
    rig_swap_p.add_argument(
        "--model",
        metavar="MODEL",
        help="Model name for presets that need it (e.g. ollama-opencode).",
    )
    rig_swap_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    rig_swap_p.set_defaults(func=cmd_rig_swap)

    rig_remove_p = rig_sub.add_parser(
        "remove",
        aliases=["rm"],
        help="Remove one or more rigs from the rig config.",
    )
    rig_remove_p.add_argument(
        "rigs",
        nargs="+",
        help="Symbolic rig name(s) to remove.",
    )
    rig_remove_p.add_argument(
        "--force",
        action="store_true",
        help="Remove even if a roster member or pin still references the rig.",
    )
    rig_remove_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    rig_remove_p.set_defaults(func=cmd_rig_remove)

    rig_configure_p = rig_sub.add_parser(
        "configure",
        help="Walk a rig's settings one prompt at a time (Enter keeps each).",
    )
    rig_configure_p.add_argument("rig", help="Symbolic rig name to configure.")
    rig_configure_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    rig_configure_p.set_defaults(func=cmd_rig_configure)

    rig_set_p = rig_sub.add_parser(
        "set",
        help="Write one explicit rig setting.",
    )
    rig_set_p.add_argument("rig", help="Symbolic rig name.")
    rig_set_p.add_argument("key", help="Setting name (see `r4t rig get <rig>`).")
    rig_set_p.add_argument("value", help="New value.")
    rig_set_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    rig_set_p.set_defaults(func=cmd_rig_set)

    rig_get_p = rig_sub.add_parser(
        "get",
        help="Read a rig's effective settings (bare: all; with key: one value).",
    )
    rig_get_p.add_argument("rig", help="Symbolic rig name.")
    rig_get_p.add_argument("key", nargs="?", help="Setting name; omit to list all.")
    rig_get_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    rig_get_p.set_defaults(func=cmd_rig_get)

    rig_unset_p = rig_sub.add_parser(
        "unset",
        help="Drop explicit settings so they fall back to preset/built-in defaults.",
    )
    rig_unset_p.add_argument("rig", help="Symbolic rig name.")
    rig_unset_p.add_argument("keys", nargs="+", help="Setting name(s) to unset.")
    rig_unset_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    rig_unset_p.set_defaults(func=cmd_rig_unset)

    engine_p = sub.add_parser(
        "engine",
        help=_cmd_help("engine"),
        description="Talk to an engine directly. Actions: quota — remaining "
        "subscription and reset time, without spending a turn; run — one "
        "headless turn as a bare stateless agent (claude, codex, agy, "
        "copilot, cursor, opencode, muse, and the ollama-* local variants), no "
        "roster or dispatcher involved; check — ask the installed CLI whether "
        "the argv r4t composes for it still parses, spending no turn. "
        "Accepts an engine id or any rig preset id; `list` shows both, and "
        "bare `check` probes every run-capable engine.",
    )
    engine_p.add_argument(
        "target",
        help="Engine or preset id (see `r4t engine list`), `list`, or `check`.",
    )
    engine_p.add_argument(
        "action",
        nargs="?",
        choices=["quota", "run", "check"],
        help="What to ask the engine.",
    )
    engine_p.add_argument(
        "prompt",
        nargs="?",
        metavar="PROMPT",
        help="run: the message text ('-' reads stdin); required unless --idle.",
    )
    engine_p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="quota: machine-readable JSON instead of the text lines.",
    )
    engine_p.add_argument(
        "--dir",
        metavar="DIR",
        help="run: working directory for the turn (default: CWD).",
    )
    engine_p.add_argument(
        "--model",
        metavar="M",
        help="run: model to pass through, in the preset's own flag pattern.",
    )
    engine_p.add_argument(
        "--agent",
        metavar="NAME",
        help="run: adds an `a8s convo NAME` reconcile step to the scaffold.",
    )
    engine_p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"run: turn timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    engine_p.add_argument(
        "--no-scaffold",
        action="store_true",
        dest="no_scaffold",
        help="run: send PROMPT unchanged, without the cold-boot scaffold.",
    )
    engine_p.add_argument(
        "--idle",
        action="store_true",
        help="run: skip if the last turn was also idle, else run one and re-arm.",
    )
    engine_p.add_argument(
        "--lessons-cap",
        type=_positive_int,
        default=LESSONS_CAP_LINES,
        dest="lessons_cap",
        help="run: line cap before rotating oldest LESSONS.md lines to "
        f"LESSONS-ARCHIVE.md (default: {LESSONS_CAP_LINES}).",
    )
    engine_p.add_argument(
        "--echo",
        action="store_true",
        help="run: print the composed argv and prompt to stderr before running.",
    )
    engine_p.add_argument(
        "--continue",
        action="store_true",
        dest="continue_conversation",
        help="run: resume the conversation this CLI already has in --dir, in "
        "the preset's own idiom. The caller asserts this turn continues live "
        "work; an idle or independent wake must not pass it. An engine with no "
        "verified continuation errors, naming the engines that can.",
    )
    engine_p.add_argument(
        "--permissions",
        metavar="MODE",
        choices=list(PERMISSION_MODES),
        help="run/check: the engine's permission stance — ask (the CLI's own "
        "default, no auto-approval), auto (approve tool use without "
        "prompting; deny rules still apply), bypass (the engine's strongest "
        "auto-approval). Unset keeps the preset's own flags. A mode below the "
        "engine's floor errors; above its ceiling it proceeds with a note.",
    )
    engine_p.add_argument(
        "--allowed-tools",
        metavar="SPEC",
        dest="allowed_tools",
        help="run/check: the engine's own tool-allowlist string, replacing the "
        "preset's list (claude and ollama-claude only; other engines error).",
    )
    engine_p.set_defaults(func=cmd_engine)

    status_p = sub.add_parser("status", help=_cmd_help("status"))
    _add_common(status_p, with_node=True)
    _add_tell_flags(status_p)
    status_p.set_defaults(func=cmd_status)

    logs_p = sub.add_parser("logs", help=_cmd_help("logs"))
    _add_common(logs_p, with_node=True)
    logs_p.add_argument(
        "-f", "--follow", action="store_true", help="Keep streaming new events."
    )
    logs_p.add_argument(
        "-n", "--lines", type=int, default=40,
        help="Backfill this many lines first (0 = everything kept on disk).",
    )
    logs_p.add_argument(
        "--full", action="store_true",
        help="Raw daily log, prompts and transcripts included.",
    )
    logs_p.add_argument(
        "--agent", action="append", metavar="MEMBER",
        help="Only this member's activity; repeat for several. With --full, "
        "their captured turns.",
    )
    logs_p.add_argument(
        "--cell", metavar="CELL",
        help="Only members in this cell (a roster member's Cell: field).",
    )
    logs_p.set_defaults(func=cmd_logs)

    tell_p = sub.add_parser(
        "tell",
        help=_cmd_help("tell"),
        description="Send into the roster as another member — jumpstart or diagnose.",
    )
    tell_p.add_argument("message", nargs="*", help="Message text.")
    tell_p.add_argument(
        "--as", dest="as_member", required=True, metavar="MEMBER",
        help="Roster member to send as.",
    )
    tell_p.add_argument("--to", help="Recipient member first name (default: the leader).")
    _add_common(tell_p, with_node=True)
    _add_tell_flags(tell_p)
    tell_p.set_defaults(func=cmd_tell)

    flush_p = sub.add_parser("flush", help=_cmd_help("flush"))
    _add_common(flush_p, with_node=True)
    flush_p.add_argument(
        "members", nargs="*", metavar="MEMBER", help="Members to flush."
    )
    flush_p.add_argument(
        "--all", action="store_true", help="Every member on the roster."
    )
    flush_p.add_argument(
        "--no-dump",
        action="store_true",
        help="Skip the save turn — for a conversation that cannot or should "
        "not write its state down.",
    )
    _add_tell_flags(flush_p)
    flush_p.set_defaults(func=cmd_flush)

    resume_p = sub.add_parser(
        "resume",
        help=_cmd_help("resume"),
        description="Put a parked member back in the rotation with its queue "
        "intact. A member parks when its harness cannot start at all.",
    )
    _add_common(resume_p, with_node=True)
    resume_p.add_argument(
        "member", nargs="?", metavar="MEMBER", help="The parked member."
    )
    resume_p.add_argument(
        "--all", action="store_true", help="Every parked member on the roster."
    )
    resume_p.set_defaults(func=cmd_resume)

    check_p = sub.add_parser(
        "check",
        help=_cmd_help("check"),
        description="Forbidden-pattern sweep: opaque pass/fail on stdout, findings "
        "on stderr. Patterns live in ~/.config/r4t/checklists/.",
    )
    _add_common(check_p)
    check_p.add_argument(
        "node", nargs="?",
        help="Roster node name (default: sole ~/.config/r4t roster).",
    )
    check_p.set_defaults(func=cmd_check)

    # HIDDEN_COMMANDS from here down: machinery a8s and cron invoke, plus
    # maintainer tooling. Omitting `help=` keeps each command and its own
    # --help intact while dropping it from the top-level listing.
    dispatch_p = sub.add_parser(
        "dispatch", description="Handle one delivered message (the a8s invoke entry)."
    )
    _add_common(dispatch_p)
    dispatch_p.add_argument("--from", dest="from_agent", default=None)
    dispatch_p.add_argument(
        "--to",
        default=None,
        help="Full recipient as delivered ($RECIPIENT): <node> or <node>:<member>.",
    )
    dispatch_p.add_argument("--message", default=None)
    dispatch_p.add_argument(
        "--meta",
        default="",
        help="Envelope metadata as JSON ($META): the sending cluster's "
        "protocol fields, of which r4t reads `class`.",
    )
    dispatch_p.add_argument(
        "--batch",
        default=None,
        help="JSON array of a8s envelopes from a batch wake; mutually "
        "exclusive with --from/--to/--message/--meta.",
    )
    dispatch_p.add_argument(
        "--no-drain",
        action="store_true",
        help="Skip the deferred-message drain passes around this message.",
    )
    _add_tell_flags(dispatch_p)
    dispatch_p.set_defaults(func=cmd_dispatch)

    clear_p = sub.add_parser(
        "clear",
        description=(
            "Maintenance: prune stale locks, drain, and apply "
            "log retention."
        ),
    )
    _add_common(clear_p, with_node=True)
    _add_tell_flags(clear_p)
    clear_p.set_defaults(func=cmd_clear)

    idle_p = sub.add_parser(
        "idle",
        description="Idle pass: drain queues, dream, heartbeat a stalled org, retire idle conversations.",
    )
    _add_common(idle_p, with_node=True)
    _add_tell_flags(idle_p)
    idle_p.set_defaults(func=cmd_idle)

    judge_p = sub.add_parser(
        "judge",
        description="Grade a finished run against the MAST failure taxonomy "
        "(post-hoc; the report is for humans, not agents).",
    )
    judge_p.add_argument(
        "node", nargs="?",
        help="Roster node name (default: sole ~/.config/r4t roster).",
    )
    judge_p.add_argument(
        "--rig", required=True,
        help="Configured rig that runs the judge prompts.",
    )
    judge_p.add_argument(
        "--json", action="store_true",
        help="Machine-readable report on stdout.",
    )
    judge_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    judge_p.set_defaults(func=cmd_judge)

    sandbox_p = sub.add_parser(
        "sandbox",
        description="Disposable end-to-end roster run in a temp A8S_HOME/R4T_HOME; "
        "logs to stderr, report on stdout.",
    )
    sandbox_p.add_argument(
        "--fake",
        action="store_true",
        help="Use the bundled deterministic fake agents (no LLM calls).",
    )
    sandbox_p.add_argument(
        "--preset",
        default="opencode",
        metavar="NAME",
        help="Live-mode harness preset (default: opencode). See `r4t rig presets`.",
    )
    sandbox_p.add_argument(
        "--model",
        metavar="MODEL",
        help="Model name for presets that need it (e.g. ollama-opencode).",
    )
    sandbox_p.add_argument(
        "--break",
        dest="break_member",
        metavar="MEMBER[:SHAPE]",
        help="Break one member on purpose and check the recovery path. SHAPE is "
        "exit (default, always fails), hang (times out), silent (answers on "
        "stdout, never tells) or mute (one turn stages nothing).",
    )
    sandbox_p.add_argument("--timeout", type=float, default=1800, metavar="SECS")
    sandbox_p.set_defaults(func=cmd_sandbox)

    lab_p = sub.add_parser(
        "lab",
        description="Run repo-bundled repeatable experiments (see apps/r4t/experiments/).",
    )
    lab_p.set_defaults(func=cmd_lab_overview)
    lab_sub = lab_p.add_subparsers(dest="action", required=False)

    lab_list_p = lab_sub.add_parser(
        "list", help="Experiments bundled in this repo + rig/model prereq status."
    )
    lab_list_p.set_defaults(func=cmd_lab_list)

    lab_run_p = lab_sub.add_parser(
        "run", help="Run N trials of an experiment (arms alternate unless --arm)."
    )
    lab_run_p.add_argument("name", help="Experiment name (see `r4t lab list`).")
    lab_run_p.add_argument("--arm", help="Run only this arm (default: all arms).")
    lab_run_p.add_argument(
        "-n", "--trials", type=int, default=None, metavar="N",
        help="Trials per arm (default: the manifest's trials_per_arm).",
    )
    lab_run_p.add_argument(
        "--rig", action="append", metavar="ROLE=RIG",
        help="Rebind a role to a different symbolic rig (repeatable).",
    )
    lab_run_p.add_argument(
        "--rig-config",
        help="Harness config path (default: ~/.config/r4t/rigs.json).",
    )
    lab_run_p.add_argument(
        "--fake", action="store_true",
        help="Use the deterministic fake judge (no LLM calls).",
    )
    lab_run_p.set_defaults(func=cmd_lab_run)

    lab_report_p = lab_sub.add_parser(
        "report", help="Aggregate the ledger: pattern over N, prediction scoring."
    )
    lab_report_p.add_argument("name", help="Experiment name.")
    lab_report_p.set_defaults(func=cmd_lab_report)

    lab_ledger_p = lab_sub.add_parser(
        "ledger", help="Raw trial rows for an experiment."
    )
    lab_ledger_p.add_argument("name", help="Experiment name.")
    lab_ledger_p.add_argument(
        "--json", action="store_true", help="Emit the rows as JSON."
    )
    lab_ledger_p.set_defaults(func=cmd_lab_ledger)

    return p


def _adopt_stray_positionals(args: argparse.Namespace, extras: list[str]) -> list[str]:
    """argparse before 3.12 hands a positional chunk that follows optionals to
    nothing once earlier optional positionals matched empty: on the 3.10 that
    Ubuntu 22.04 ships, `engine codex run --agent amos "hi"` leaves "hi"
    unparsed and dies "unrecognized arguments". Adopt what old argparse
    abandoned, in declaration order; 3.12+ never produces these extras.

    Every parser whose optional positional can trail its flags is covered
    here, one branch per dest: `engine`'s action/prompt, `rig run`'s prompt,
    `rig get`'s key, and `tell`'s message — the last is a list and so keeps
    taking. `flush` needs no branch — its
    `members` is the parser's only positional, and 3.10 places it correctly."""
    remaining = []
    for tok in extras:
        if tok != "-" and tok.startswith("-"):
            remaining.append(tok)
        elif getattr(args, "action", "") is None and tok in ("quota", "run", "check"):
            args.action = tok
        elif (
            getattr(args, "prompt", "") is None
            and getattr(args, "action", "run") == "run"
        ):
            args.prompt = tok
        elif getattr(args, "key", "") is None:
            args.key = tok
        elif isinstance(getattr(args, "message", None), list):
            args.message.append(tok)
        else:
            remaining.append(tok)
    return remaining


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, extras = parser.parse_known_args(argv)
    if extras:
        extras = _adopt_stray_positionals(args, extras)
    if extras:
        parser.error(f"unrecognized arguments: {' '.join(extras)}")
    return int(args.func(args))


if __name__ == "__main__":
    # stderr already defaults to backslashreplace, so only stdout needs the
    # floor — an unencodable glyph (e.g. on a redirected Windows console)
    # gets a lossless, reversible escape instead of crashing the process.
    # The isinstance/errors=="strict" guard is mypy's own (PR 18292): it
    # never fires once a caller has set a deliberate error handler, and
    # skips a replaced sys.stdout (e.g. io.StringIO under embedding) cleanly
    # instead of raising AttributeError. Every --json path in the suite is
    # ensure_ascii, so machine-readable output is unaffected either way.
    if isinstance(sys.stdout, io.TextIOWrapper) and sys.stdout.errors == "strict":
        sys.stdout.reconfigure(errors="backslashreplace")
    raise SystemExit(main())
