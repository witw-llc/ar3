#!/usr/bin/env python3
"""r4t — the roster: rigs, dispatch, verdicts, isolation.

Turns a repo into a roster of lightweight AI agents on the a8s network: a
human-readable ROSTER.md declares the members, an out-of-repo rig config
decides what each symbolic rig is allowed to run, and r4t dispatches turns
through the roster.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import state
import tasks
import verdict
from dispatch import (
    DispatchContext,
    class_from_meta,
    handle_message,
    run_clear,
    run_flush,
    run_idle,
    split_recipient,
)
from rig import (
    DEFAULT_CONCURRENCY,
    RigError,
    HARNESS_PRESETS,
    add_preset_rig,
    build_preset_invoke,
    continue_collisions,
    default_config_path,
    default_config_payload,
    format_preset_invoke,
    load_rig_config,
    preset_names,
    remove_rig,
    resolve_config_path,
    rig_setting,
    rig_settings,
    set_rig_value,
    setting_label,
    swap_preset_rig,
    unset_rig_value,
)
from notify import resolve_tell_fn, simulate_enabled
from org import check_org, load_org
from roster import (
    Member,
    Roster,
    RosterError,
    load_roster,
    resolve_roster_path,
)

DEFAULT_TASK_TTL_SECONDS = 7 * 86400


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
            Command("init", "Set up this repo: a starter roster and rig config", "init"),
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
                "Where a roster stands: budgets, queues, open threads",
                "status",
            ),
            Command(
                "logs",
                "Everything the roster does, as it happens",
                "logs",
                "r4t logs -f",
            ),
            Command(
                "chat",
                "Your seat at the roster: speak, and watch the work land",
                "chat",
            ),
            Command(
                "seat",
                "Read messages waiting for you and answer them",
                "seat",
            ),
            Command(
                "flush <member>",
                "Save a member's state and start the conversation fresh",
                "flush",
            ),
            Command("task list", "The roster's conversation threads", "task"),
            Command("task show <id>", "One thread, in full"),
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
            Command(
                "task trace <id>",
                "Who told whom on one task, hop by hop",
                None,
                "r4t task list",
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

ROSTER_TEMPLATE = """\
# Roster

Members are `### <Name>` blocks. AI is the default and carries no marker.
`Human: yes` members are never dispatched: mail to them parks in the roster's
seat mailbox (`r4t seat`, `r4t chat`), and the optional `Address:` is a
doorbell — a copy forwarded over a8s when no seat session is attached.
`Rig:` names a SYMBOLIC rig defined in the out-of-repo rig config
(~/.config/r4t/rigs.json) and belongs only to AI members. Free prose in a
block becomes the member's persona.

### Owner
- **Human:** yes
- **Address:** YOUR-A8S-NAME
- **Role:** Product owner

### Lead
- **Rig:** leader
- **Leader:** yes
- **Role:** Roster lead — delegates work and answers the owner

Coordinates the roster. Delegates implementation, follows up on replies, and
synthesizes answers for whoever asked.

### Dev
- **Rig:** member
- **Role:** Developer

Implements what the Lead asks for and reports back.
"""


def _resolve_root(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


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


def _context(args: argparse.Namespace, node: str) -> DispatchContext:
    # The stamped node root IS the org dir — it is what dispatch resolved and
    # ran against. Observer surfaces (chat/status/seat) must read the roster,
    # mission and docs from there too, not from wherever the human happens to
    # stand: in a portable org the workplace repo is not the org dir, and a
    # member that wrote a shadow ROSTER.md/MISSION.md into the workplace must
    # not shadow the authoritative copy. So prefer the stamp over cwd whenever
    # no explicit --root overrides it.
    root = _resolve_root(args.root)
    if not getattr(args, "root", None):
        stamped = state.read_root(node)
        if stamped is not None and stamped.is_dir():
            root = stamped
    org = load_org(root)
    roster_path = resolve_roster_path(org.dir, getattr(args, "roster", None))
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
        doorbell_check=org.doorbell_check,
        isolation=org.isolation,
        definition_path=(
            Path(defn).expanduser() if (defn := getattr(args, "definition", None)) else None
        ),
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
    show_concurrency = any(r.concurrency != DEFAULT_CONCURRENCY for _, r in rigs)
    show_rig_budget = any(r.rig_budget_max is not None for _, r in rigs)

    headers = ["RIG", "PRESET", "MODEL"]
    if show_concurrency:
        headers.append("CONC")
    headers += ["TIMEOUT", "SENDS", "BUDGET"]
    if show_rig_budget:
        headers.append("RIG-BUDGET")
    if wide:
        headers.append("INVOKE")

    rows: list[tuple[str, ...]] = []
    for name, rig in rigs:
        row = [name, rig.preset or "-", _rig_model(rig)]
        if show_concurrency:
            row.append(str(rig.concurrency))
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
        if m.is_human:
            rows.append((m.name, "-", "human"))
        elif m.errors:
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
        print(f"{indent}(no rigs yet — try: r4t rig add <rig> <preset>, or r4t init)")
        return
    _print_rig_table(config, indent, wide)
    if config.pins:
        print()
        print(f"{indent}pins:")
        for agent in sorted(config.pins):
            print(f"{indent}  {agent} -> {config.pins[agent]}")
    print()
    print(
        f"{indent}throttle:   max_concurrent={config.throttle.max_concurrent} "
        f"min_seconds_between_turn_starts="
        f"{config.throttle.min_seconds_between_turn_starts:g}"
    )
    print(
        f"{indent}governance: cell_budget={config.cell_budget_max:g}/"
        f"+{config.cell_budget_earn_per_hour:g}per-h "
        f"quiet_task={config.quiet_task_seconds:g}s "
        f"breaker_cap={config.breaker_cap} "
        f"breaker_cooldown={config.breaker_cooldown_seconds:g}s"
    )
    if roster_path and roster_path.is_file():
        print()
        _print_roster_table(config, roster_path, indent)


def _print_roster_summaries() -> None:
    rosters = state.known_rosters()
    if not rosters:
        print("  (none — register a roster after `r4t init`; see printed a8s steps)")
        return
    for node in rosters:
        locks = state.live_locks(node)
        open_tasks = [
            t for t in tasks.list_tasks(node) if t.get("status") == tasks.STATUS_OPEN
        ]
        dead = len(state.list_dead_letters(node))
        queued = sum(state.queue_depth(node, m) for m in state.members_with_queue(node))
        parts = [
            f"{len(open_tasks)} open thread(s)",
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
        steps.append("`r4t init` — write ~/.config/r4t/rigs.json with default rigs")
    if not roster_path.is_file():
        steps.append("`r4t init` — write a starter ROSTER.md in the current repo")
    else:
        steps.append("`r4t roster check` — lint the roster and rig mapping")
        steps.append("`r4t rig presets` — named CLI rigs aligned with a8s definitions")
        steps.append("`r4t rig add <rig> <preset>` — add a rig to the rig config")
    if not rosters:
        steps.append("`r4t init` — prints the a8s add / namespace / start sequence")
    elif len(rosters) == 1:
        steps.append(f"`r4t status --node {rosters[0]}` — budgets, queues, threads")
        steps.append(f"`r4t chat --node {rosters[0]}` — take your seat at the roster")
    else:
        steps.append("`r4t status --node <roster>` — pick a roster from the list above")
        steps.append("`r4t chat --node <roster>` — take your seat at the roster")
    return steps


def cmd_default(_args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    config_path = default_config_path()
    roster_path = resolve_roster_path(root, None)
    rosters = state.known_rosters()

    print("r4t — the roster")
    print("Define agents in ROSTER.md; ~/.config/r4t/rigs.json maps roster rigs")
    print("to what actually runs. r4t dispatches governed turns on a8s.")
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
            leaders = [m for m in roster.members if m.leader and not m.is_human]
            leader = leaders[0].name if len(leaders) == 1 else "(unset)"
            print(
                f"  {roster_path}: {len(roster.members)} member(s), "
                f"leader {leader}"
            )
        except RosterError as e:
            print(f"  {roster_path}: {e}")
    else:
        print(f"  no ROSTER.md under {root}")
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
    node, _sub = split_recipient(args.to)
    if not node:
        print("dispatch: --to must carry the node name", file=sys.stderr)
        return 2
    ctx = _context(args, node.lower())
    state.stamp_root(ctx.node, ctx.root)
    return handle_message(
        ctx, args.from_agent, args.to, args.message,
        klass=class_from_meta(args.meta),
        drain_after=not args.no_drain,
    )


def cmd_clear(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    ctx = _context(args, node)
    summary = run_clear(ctx, args.older_than)
    expired = summary["tasks_expired"]
    print(
        f"pruned {summary['locks_pruned']} stale lock(s); "
        f"expired {len(expired)} thread(s)"
        + (f" ({', '.join(expired)})" if expired else "")
        + f"; drained {summary['drained']} queued turn(s)"
        + _retention_line(summary)
    )
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
        roster = load_roster(ctx.roster_path)
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
    ctx = _context(args, node)
    summary = run_idle(ctx)
    nudged = summary.get("quiet_nudged") or []
    print(
        f"drained {summary['drained']} queued turn(s); "
        f"nudged the leader on {len(nudged)} quiet thread(s)"
        + (f" ({', '.join(nudged)})" if nudged else "")
    )
    clear_summary = run_clear(ctx, args.older_than)
    expired = clear_summary["tasks_expired"]
    print(
        f"pruned {clear_summary['locks_pruned']} stale lock(s); "
        f"expired {len(expired)} thread(s); "
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
        if m.is_human:
            rows.append((
                None,
                m.name,
                f"Human  address={m.address or '(none)'}{suffix}",
                None if m.address else "add an **Address:** line so the roster can reach them",
            ))
            continue
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
        None, "throttle",
        f"max_concurrent={config.throttle.max_concurrent}  "
        f"cadence={config.throttle.min_seconds_between_turn_starts:g}s",
        None,
    ))
    rows.append((
        None, "governance",
        f"cell_budget={config.cell_budget_max:g}/"
        f"+{config.cell_budget_earn_per_hour:g}per-h  "
        f"quiet_task={config.quiet_task_seconds:g}s  "
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
    open_tasks = [
        t for t in tasks.list_tasks(node) if t.get("status") == tasks.STATUS_OPEN
    ]
    for task in open_tasks:
        rows.append((
            None, "thread",
            f"{task['id']}  creator={task.get('creator', '?')}  "
            f"status={task.get('status', '?')}"
            + ("  answered" if task.get("answered") else "")
            + ("  relay" if task.get("relay") else ""),
            None,
        ))
    if not open_tasks:
        rows.append((None, "threads", "none open", None))
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


def cmd_status(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    ctx = _context(args, node)
    print(f"roster: {node}")
    print(f"state: {state.roster_dir(node)}")
    iso = _isolation_tag(ctx.isolation)
    if iso:
        print(f"isolation: {iso}  (every member turn runs behind this boundary)")
    print()

    roster = None
    config = None
    roster_err = config_err = None
    try:
        roster = load_roster(ctx.roster_path)
    except RosterError as e:
        roster_err = str(e)
    try:
        config = load_rig_config(ctx.config_path)
    except RigError as e:
        config_err = str(e)

    print("Health")
    for v in verdict.roster_verdicts(node, roster, config):
        line = f"  {verdict.MARKS[v.level]} {v.text}"
        if v.hint:
            line += f"   (try: {v.hint})"
        print(line)
    print()

    print(f"Roster  (repo settings: {ctx.roster_path})")
    if roster_err:
        _print_rows([(False, "roster", roster_err, "r4t init")])
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


def _resolve_log_member(args: argparse.Namespace, node: str) -> str | None | bool:
    """Validate --agent against the roster. Returns the canonical member name,
    None when no --agent was given, or False on an unknown member (already
    reported)."""
    if not args.agent:
        return None
    ctx = _context(args, node)
    try:
        roster = load_roster(ctx.roster_path)
    except RosterError as e:
        print(f"logs --agent: cannot read roster: {e}", file=sys.stderr)
        return False
    member = roster.find(args.agent)
    if member is None:
        names = ", ".join(roster.names()) or "(none)"
        print(
            f"logs --agent: no roster member named {args.agent!r} — "
            f"(try: r4t logs --node {node} --agent <name>; members: {names})",
            file=sys.stderr,
        )
        return False
    return member.name


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


def cmd_logs(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    from chat import filter_log_line

    member = _resolve_log_member(args, node)
    if member is False:
        return 2
    if member and args.full:
        return _print_member_turns(node, member)

    mention = re.compile(rf"\b{re.escape(member)}\b", re.IGNORECASE) if member else None
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
    collected: list[str] = []
    offset = 0
    for path in files[-2:]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if path == files[-1]:
            offset = len(text.encode("utf-8"))
        for raw in text.splitlines():
            collected.extend(rendered(raw))
    for line in collected[-args.lines:] if args.lines else collected:
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
    """Directly-invoked seat/chat sessions have no a8s-injected outbox env;
    give `tell` subprocesses (the Address: doorbell, error notices) the same
    fallback dispatch itself uses for releases."""
    os.environ.setdefault("TELL_OUTBOX_DIR", str(ctx.root / ".outbox"))


def _adopt_root(ctx: DispatchContext) -> None:
    """A seat session is roster ingress just like dispatch — chat/seat sends
    call handle_message directly and never pass cmd_dispatch, the only place
    the root stamp was written. A roster driven entirely through the seat
    therefore had no stamp, and every observer command fell back to guessing
    the root from cwd (the live quill repro). First successful seat
    resolution writes the stamp; an existing stamp is never overridden here
    — dispatch owns that."""
    if state.read_root(ctx.node) is None and ctx.roster_path.is_file():
        state.stamp_root(ctx.node, ctx.root)


def cmd_chat(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    ctx = _context(args, node)
    _adopt_root(ctx)
    _ensure_tell_outbox(ctx)
    attach = getattr(args, "attach", None)
    if not args.plain and sys.stdout.isatty():
        try:
            from chat_tui import run_chat_tui
        except ImportError:
            print(
                "textual not installed — line UI instead"
                " (try: python3 -m pip install textual)",
                file=sys.stderr,
            )
        else:
            return run_chat_tui(ctx, attach=attach)
    from chat import run_chat

    return run_chat(ctx, attach=attach)


def _seat_human(roster: Roster) -> Member | None:
    return next((m for m in roster.members if m.is_human), None)


def cmd_seat(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    ctx = _context(args, node)
    _adopt_root(ctx)
    _ensure_tell_outbox(ctx)
    try:
        roster = load_roster(ctx.roster_path)
    except RosterError as e:
        print(f"seat: {e}", file=sys.stderr)
        return 2
    human = _seat_human(roster)
    if human is None:
        print(
            "seat: no human member in the roster — add one to ROSTER.md "
            "(Human: yes)",
            file=sys.stderr,
        )
        return 2

    if args.action == "send":
        text = " ".join(args.message).strip()
        if not text:
            print("seat send: message is required", file=sys.stderr)
            return 2
        if args.to:
            member = roster.find(args.to)
            if member is None or member.is_human or member.errors:
                print(f"seat send: no dispatchable member {args.to!r}", file=sys.stderr)
                return 2
            to = f"{node}:{member.name.lower()}"
        else:
            to = node
        sender = f"{node}:{human.name.lower()}"
        handle_message(ctx, sender, to, text)
        from dispatch import resting_note

        note = resting_note(ctx, to)
        if note:
            print(note)
        return 0

    if args.action == "inbox":
        paths = state.list_seat_messages(node, human.name)
        if not paths:
            if not args.as_json:
                print("(no unread messages)")
            return 0
        for path in paths:
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if args.as_json:
                print(json.dumps(envelope))
            else:
                print(
                    f"── from {envelope.get('from', '?')}"
                    f" ({envelope.get('parked_at', '?')})"
                )
                print(envelope.get("content", ""))
                for f in envelope.get("files") or []:
                    print(
                        f"(attachment: {f.get('filename', '?')} "
                        f"at {f.get('path', '?')})"
                    )
                print()
            if not args.peek:
                state.mark_seat_read(node, human.name, path)
        return 0

    unread = len(state.list_seat_messages(node, human.name))
    attached = state.seat_attached(node, human.name)
    print(f"seat: {human.name} on {node}")
    print(f"unread: {unread}  (try: r4t seat inbox)")
    print(f"attached: {'yes' if attached else 'no'}")
    if human.address:
        print(f"doorbell: {human.address} (rings when not attached)")
    return 0


RIG_COMMAND_HELP = [
    ("rig list", "Rigs, limits, and roster rig resolution (alias: ls; --wide)"),
    ("rig presets", "Named CLI presets aligned with a8s definitions"),
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
        print("  - `r4t init` — or write the full starter config + ROSTER.md instead")
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
            print(
                f"  {'':<{width}}  continue: {' '.join(entry['continue_argv'])} "
                "(roster `- **Continue:** on`)"
            )
    print()
    print("Add one: r4t rig add <rig-name> <preset>")
    print("Example: r4t rig add worker opencode")
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
    print(f"Reference it from ROSTER.md: `- **Rig:** {rig_key}`")
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


def cmd_task(args: argparse.Namespace) -> int:
    node = _resolve_node(args.node)
    if node is None:
        return 2
    if args.action == "list":
        listing = tasks.list_tasks(node)
        if not listing:
            print("no tasks")
            return 0
        for task in listing:
            print(
                f"{task['id']}  creator={task.get('creator', '?')}  "
                f"status={task.get('status', '?')}"
                + ("  answered" if task.get("answered") else "")
            )
        return 0
    if not args.id:
        print(f"task {args.action}: <id> is required", file=sys.stderr)
        return 2
    if args.action == "trace":
        import tasktrace

        return tasktrace.run(node, args.id.strip().upper(), json_mode=args.json)
    task = tasks.load_task(node, args.id.strip().upper())
    if task is None:
        print(f"task not found: {args.id}", file=sys.stderr)
        return 1
    import json

    print(json.dumps(task, indent=2))
    return 0


def cmd_roster_check(args: argparse.Namespace) -> int:
    org = load_org(_resolve_root(args.root))
    root = org.dir
    problems = 0
    for message in check_org(root):
        print(f"org: {message}")
        problems += 1
    roster_path = resolve_roster_path(root, args.roster)
    try:
        roster = load_roster(roster_path)
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
        if m.is_human:
            if not m.address:
                print(f"{m.name}: note — Human without an Address (roster cannot tell them)")
            continue
        if config is not None and not m.errors:
            rig, err, _pinned = config.rig_for(m)
            if rig is None:
                print(f"{m.name}: {err}")
                problems += 1
    leaders = [m for m in roster.members if m.leader and not m.is_human]
    if not leaders:
        print(
            "no leader: mark one AI member with `- **Leader:** yes` "
            "(bare messages to the node have no recipient)"
        )
        problems += 1
    elif len(leaders) > 1:
        print(
            f"multiple leaders: {', '.join(m.name for m in leaders)} "
            f"(first one wins: {leaders[0].name})"
        )
        problems += 1
    warnings = 0
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
    for severity, message in roster.tree_problems():
        if severity == "error":
            print(message)
            problems += 1
        else:
            print(f"warning: {message}")
            warnings += 1
    mission = root / "MISSION.md"
    if mission.is_file():
        n = sum(1 for line in mission.read_text(encoding="utf-8").splitlines() if line.strip())
        if n > 40:
            print(
                f"warning: MISSION.md is {n} lines — intent docs read best "
                "under one page"
            )
            warnings += 1
    if problems:
        print(f"{problems} problem(s)")
        return 1
    tail = f", {warnings} warning(s)" if warnings else ""
    print(
        f"{roster_path}: OK ({len(roster.members)} member(s), "
        f"leader {leaders[0].name}{tail})"
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    if not root.is_dir():
        print(f"init: not a directory: {root}", file=sys.stderr)
        return 1

    roster_path = root / "ROSTER.md"
    if roster_path.is_file():
        print(f"roster: {roster_path} exists, left unchanged")
    else:
        roster_path.write_text(ROSTER_TEMPLATE, encoding="utf-8")
        print(f"roster: wrote starter {roster_path}")

    config_path = default_config_path()
    if config_path.is_file():
        print(f"rig config: {config_path} exists, left unchanged")
    else:
        state.atomic_write_json(config_path, default_config_payload())
        print(f"rig config: wrote starter {config_path}")

    prefix = re.sub(r"[^a-z0-9_-]+", "-", root.name.lower()).strip("-") or "roster"
    node = f"{prefix}-node"
    print()
    print("Register and start the roster (a namespace prefix cannot share a")
    print("name with its agent, so the node is registered as <roster>-node):")
    print()
    print(f"  a8s add {node} {root} r4t")
    print(f"  a8s namespace {prefix} {node}")
    print(f"  a8s start {node}")
    print(f'  tell {prefix} "hello"            # bare namespace -> roster leader')
    print(f'  tell {prefix}:dev "hello"        # namespace:member -> specific member')
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
        help="Roster path, absolute or root-relative (default: <root>/ROSTER.md).",
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


def _add_older_than(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--older-than",
        type=float,
        default=DEFAULT_TASK_TTL_SECONDS,
        metavar="SECS",
        help=f"Expire tasks idle longer than SECS (default {DEFAULT_TASK_TTL_SECONDS}).",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="r4t",
        description="The roster — define agents in ROSTER.md; govern turns on a8s.",
    )
    sub = p.add_subparsers(dest="command", required=False, metavar="COMMAND")
    p.set_defaults(func=cmd_default)

    init_p = sub.add_parser(
        "init",
        help=_cmd_help("init"),
        description="Write a starter ROSTER.md and ~/.config/r4t/rigs.json; print "
        "the a8s registration sequence.",
    )
    init_p.add_argument("--root", help="Repo to initialize (default: cwd).")
    init_p.set_defaults(func=cmd_init)

    roster_p = sub.add_parser("roster", help=_cmd_help("roster"))
    roster_sub = roster_p.add_subparsers(dest="action", required=True)
    roster_check_p = roster_sub.add_parser("check", help="Lint the roster.")
    _add_common(roster_check_p)
    roster_check_p.set_defaults(func=cmd_roster_check)

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

    rig_add_p = rig_sub.add_parser(
        "add",
        help="Add a symbolic rig from a named CLI preset.",
    )
    rig_add_p.add_argument(
        "rig",
        help="Symbolic rig name (referenced from ROSTER.md Harness lines).",
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
        help="Model name for presets that need it (e.g. opencode-ollama).",
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
        "--agent", metavar="MEMBER",
        help="Only one member's activity; with --full, their captured turns.",
    )
    logs_p.set_defaults(func=cmd_logs)

    chat_p = sub.add_parser(
        "chat",
        help=_cmd_help("chat"),
        description="Interactive human seat: messages and roster activity in one window.",
    )
    _add_common(chat_p, with_node=True)
    _add_tell_flags(chat_p)
    chat_p.add_argument(
        "--plain", action="store_true",
        help="Line UI instead of the full-screen TUI.",
    )
    chat_p.add_argument(
        "--attach", metavar="MEMBER",
        help="Open watching a member read-only (messages in and turn output live).",
    )
    chat_p.set_defaults(func=cmd_chat)

    seat_p = sub.add_parser(
        "seat",
        help=_cmd_help("seat"),
        description="The roster human's mailbox and voice (bare: summary).",
    )
    seat_p.add_argument(
        "action", nargs="?", choices=["inbox", "send"],
        help="inbox: read parked messages; send: speak as the human.",
    )
    seat_p.add_argument("message", nargs="*", help="send: message text.")
    seat_p.add_argument("--to", help="send: member first name (default: the leader).")
    seat_p.add_argument(
        "--peek", action="store_true", help="inbox: leave messages unread."
    )
    seat_p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="inbox: one JSON object per message.",
    )
    _add_common(seat_p, with_node=True)
    _add_tell_flags(seat_p)
    seat_p.set_defaults(func=cmd_seat)

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

    task_p = sub.add_parser(
        "task",
        help=_cmd_help("task"),
        description="Conversation threads: list them, show one ledger, or trace "
        "one task's delegation tree from recorded state.",
    )
    task_p.add_argument("action", choices=["list", "show", "trace"])
    task_p.add_argument("id", nargs="?", help="Task ULID.")
    task_p.add_argument("--node", help="Roster node name (default: sole ~/.config/r4t roster).")
    task_p.add_argument(
        "--json", action="store_true",
        help="With trace: the reconstruction as JSON instead of the panel.",
    )
    task_p.set_defaults(func=cmd_task)

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
    dispatch_p.add_argument("--from", dest="from_agent", required=True)
    dispatch_p.add_argument(
        "--to",
        required=True,
        help="Full recipient as delivered ($RECIPIENT): <node> or <node>:<member>.",
    )
    dispatch_p.add_argument("--message", required=True)
    dispatch_p.add_argument(
        "--meta",
        default="",
        help="Envelope metadata as JSON ($META): the sending cluster's "
        "protocol fields, of which r4t reads `class`.",
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
            "Maintenance: prune stale locks, expire tasks, drain, and apply "
            "log retention."
        ),
    )
    _add_common(clear_p, with_node=True)
    _add_older_than(clear_p)
    _add_tell_flags(clear_p)
    clear_p.set_defaults(func=cmd_clear)

    idle_p = sub.add_parser(
        "idle",
        description="Idle pass: nudge active agents with unfinished business, then clear.",
    )
    _add_common(idle_p, with_node=True)
    _add_older_than(idle_p)
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
        help="Model name for presets that need it (e.g. opencode-ollama).",
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
