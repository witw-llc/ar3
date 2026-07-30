"""Team health verdicts and dead-letter rollup — the shared brain behind
`r4t status` and the chat header.

Everything here is read-only over team state. Callers render the marks;
levels are `ok`/`warn`/`bad`. Thresholds are heuristics tuned for an
operator glancing at a team, not alerting SLOs.
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from datetime import datetime

import state
import tasks as taskmod

OK = "ok"
WARN = "warn"
BAD = "bad"
MARKS = {OK: "✓", WARN: "⚠", BAD: "✗"}

RECENT_WINDOW_SECONDS = 600.0
RUNAWAY_TURNS_PER_WINDOW = 20
SIGNAL_RECENT_SECONDS = 3600.0
QUEUE_DEPTH_WARN = 10

ROUTINE_REASONS = {"quota"}

REASON_GLOSS = {
    "quota": "one turn tried to send past max_sends_per_turn",
    "unknown-recipient": "mail to a name that is not on the roster",
    "no-leader": "a bare-node message with no leader marked to receive it",
    "member-disabled": "mail to a member disabled by a roster problem",
    "no-rig": "mail to a member whose rig will not resolve",
}


@dataclass
class Verdict:
    level: str
    text: str
    hint: str | None = None


@dataclass
class Rollup:
    routine: dict[str, int]
    signals: dict[str, list[dict]]

    @property
    def routine_total(self) -> int:
        return sum(self.routine.values())

    @property
    def signal_total(self) -> int:
        return sum(len(v) for v in self.signals.values())


def _ts(iso: object) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def rollup_dead_letters(records: list[dict]) -> Rollup:
    routine: dict[str, int] = {}
    signals: dict[str, list[dict]] = {}
    for record in records:
        reason = str(record.get("reason", "?"))
        if reason in ROUTINE_REASONS:
            routine[reason] = routine.get(reason, 0) + 1
        else:
            signals.setdefault(reason, []).append(record)
    return Rollup(routine, signals)


def recent_turns(
    node: str, now: float, window: float = RECENT_WINDOW_SECONDS
) -> tuple[int, set[str]]:
    """(turn count, distinct task ids) from velocity.csv within the window."""
    path = state.team_dir(node) / "velocity.csv"
    if not path.is_file():
        return 0, set()
    count, seen = 0, set()
    try:
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if now - _ts(row.get("timestamp", "")) <= window:
                    count += 1
                    seen.add(row.get("thread", "?"))
    except OSError:
        return 0, set()
    return count, seen


def team_verdicts(
    node: str, roster=None, config=None, *, now: float | None = None
) -> list[Verdict]:
    """One plain-English line per operator concern: is anything waiting on
    the human, is the team runaway, is a member broken or resting with work
    queued, is the shared cell budget spent, is any queue backing up.
    Roster/config are optional; concerns that need them are skipped when they
    are unavailable."""
    now = time.time() if now is None else now
    out: list[Verdict] = []
    open_tasks = [
        t for t in taskmod.list_tasks(node)
        if t.get("status") == taskmod.STATUS_OPEN
    ]
    dead = state.list_dead_letters(node)

    human = None
    if roster is not None:
        human = next((m for m in roster.members if m.is_human), None)
    if human is not None:
        unread = len(state.list_seat_messages(node, human.name))
        if unread:
            out.append(Verdict(
                BAD, f"{unread} message(s) waiting on YOU",
                f"r4t seat inbox --node {node}",
            ))
        else:
            out.append(Verdict(OK, "nothing waiting on you"))

    turns, active_tasks = recent_turns(node, now)
    live = state.live_locks(node, prune=False)
    if turns >= RUNAWAY_TURNS_PER_WINDOW:
        out.append(Verdict(
            WARN,
            f"hot: {turns} turns in the last 10m across "
            f"{len(active_tasks)} thread(s)",
            "watch it live: r4t chat",
        ))
    else:
        detail = f"{turns} turn(s) last 10m"
        if live:
            detail += f", {len(live)} live now"
        out.append(Verdict(OK, f"no runaway signs ({detail})"))

    if config is not None:
        team_level = state.budget_level(
            node, state.CELL_BUDGET_KEY,
            config.cell_budget_max, config.cell_budget_earn_per_hour, now=now,
        )
        if team_level < 1.0:
            wait = state.budget_seconds_until(
                node, state.CELL_BUDGET_KEY,
                config.cell_budget_max, config.cell_budget_earn_per_hour, now=now,
            )
            out.append(Verdict(
                WARN,
                f"cell budget spent ({state.fmt_budget(team_level)}/{state.fmt_budget(config.cell_budget_max)}) "
                f"— everyone rests, ready in ~{wait / 60:.0f} min",
                "raise cell_budget_max/cell_budget_earn_per_hour to run faster",
            ))

    if roster is not None and config is not None:
        flagged = 0
        members = [m for m in roster.members if not m.is_human and not m.errors]
        for m in members:
            rig, _err, _pinned = config.rig_for(m)
            depth = state.queue_depth(node, m.name)
            blocked, failures = state.breaker_open(
                node, m.name, config.breaker_cap, config.breaker_cooldown_seconds
            )
            if blocked:
                out.append(Verdict(
                    BAD,
                    f"{m.name} broken — {failures} straight harness failures; "
                    f"turns paused ({depth} queued)",
                    f"fix the {rig.name if rig else '?'} rig; "
                    "turns retry when the breaker closes",
                ))
                flagged += 1
                continue
            if rig is not None and depth:
                level = state.budget_level(
                    node, m.name, rig.budget_max, rig.budget_earn_per_hour, now=now
                )
                if level < 1.0:
                    wait = state.budget_seconds_until(
                        node, m.name, rig.budget_max, rig.budget_earn_per_hour, now=now
                    )
                    out.append(Verdict(
                        WARN,
                        f"{m.name} resting — {depth} queued, ready in "
                        f"~{wait / 60:.0f} min",
                    ))
                    flagged += 1
                    continue
                if rig.rig_budget_max is not None:
                    rig_level = state.rig_budget_level(
                        rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour,
                        now=now,
                    )
                    if rig_level < 1.0:
                        wait = state.rig_budget_seconds_until(
                            rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour,
                            now=now,
                        )
                        out.append(Verdict(
                            WARN,
                            f"{m.name} resting — rig {rig.name} exhausted, "
                            f"{depth} queued, ready in ~{wait / 60:.0f} min",
                            "raise rig_budget_max/rig_budget_earn_per_hour, or "
                            "the subscription is out of quota",
                        ))
                        flagged += 1
                        continue
            if depth >= QUEUE_DEPTH_WARN:
                out.append(Verdict(
                    WARN,
                    f"{m.name}'s queue is backing up — {depth} message(s) waiting",
                ))
                flagged += 1
        if members and not flagged:
            out.append(Verdict(OK, f"all {len(members)} member(s) healthy"))

    roll = rollup_dead_letters(dead)
    for reason in sorted(roll.signals):
        recent = [
            r for r in roll.signals[reason]
            if now - _ts(r.get("time", "")) <= SIGNAL_RECENT_SECONDS
        ]
        if recent:
            out.append(Verdict(
                WARN,
                f"{len(recent)} {reason} dead letter(s) in the last hour — "
                f"{REASON_GLOSS.get(reason, reason)}",
                f"ls {state.dead_letter_dir(node)}",
            ))
    return out


def worst_level(verdicts: list[Verdict]) -> str:
    levels = {v.level for v in verdicts}
    if BAD in levels:
        return BAD
    if WARN in levels:
        return WARN
    return OK
