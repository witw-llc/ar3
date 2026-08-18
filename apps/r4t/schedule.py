"""The rotation — who goes next, and the sentence that says why.

One turn at a time is the contract, so the only scheduling question left is
which member takes it. That question is answered here, once, by a pure function
over the inbox directories, and both callers that need the answer — the drain
loop and `r4t status` — ask this module rather than working it out again. A
status screen that re-derived the ranking would be right until the day it
drifted, and there would be no way to tell which day that was.

THE QUEUE IS DERIVED, NEVER STORED. A member is in the run queue when its inbox
holds at least one message, and that is the whole of it. There is no scheduler
state file: a durable run queue would be a second source of truth that can
disagree with the inboxes, and the reconciliation is exactly the class of state
this design deletes. Everything the rotation needs is already durable — the
envelopes themselves, whose filenames carry arrival order, and two fields in
`meta.json`.

THE RANKING IS ARITHMETIC AND STAYS ARITHMETIC. Never a model call. Frameworks
that hand speaker selection to an LLM trade away the ability to explain the
queue, and the whole point of this rotation is that a human can verify it by
hand from one status line.

Two tiers:

    Tier 1  PRIORITY   a member holding mail from a priority sender goes next,
                       always. Oldest such message first. Priority never
                       preempts: a running turn always finishes, so the promise
                       is "next", not "now".
    Tier 2  score = 2*ask + 1*ingress + passes, ties broken by oldest message
                       and then by name, so the order is deterministic and
                       testable without mocking a clock.

`ask` is the future r4t-only verb and contributes 0 by construction today; the
term stays visible so the ladder does not change shape when it lands.

Aging is counted in TURNS PASSED OVER, not seconds. `passes` rises by one for
every ready member a selection skips and resets when the member runs. The
maximum a class can contribute is ASK_WEIGHT + INGRESS_WEIGHT = 3, so a member
passed over STARVATION_BOUND = 4 times outranks any possible class combination
on freshly arrived mail: after four turns, only members that have waited at
least as long as you can still go first. Wall-clock wait is printed, because
humans think in minutes, and is deliberately not in the score — a member that
waited forty minutes because the whole node was resting on budget was not passed
over by the scheduler, and conflating the two makes "why won't it run"
unanswerable.
"""
from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import dataclass

import state

# The class weights. `ask` does not exist yet and every entry carries has_ask
# False, so its term is 0 in every score this version can produce.
ASK_WEIGHT = 2
INGRESS_WEIGHT = 1

# Passing a member over this many times makes its score exceed anything the
# classes alone can reach (ASK_WEIGHT + INGRESS_WEIGHT), so nothing that just
# arrived can outrank it.
STARVATION_BOUND = ASK_WEIGHT + INGRESS_WEIGHT + 1

# The envelope stamp `_ingest` writes for mail that entered from outside the
# roster, as opposed to one member speaking to another.
ORIGIN_INGRESS = "ingress"
ORIGIN_INTRA = "intra"

# No name shipped in the public mirror by default. An org that wants a Tier-1
# sender states it in its own config (`priority_senders` in `r4t-org.json` or
# a runbook's frontmatter) — see org.py.
DEFAULT_PRIORITY_SENDERS: tuple[str, ...] = ()

READY = "ready"
RESTING = "resting"
BREAKER = "breaker"
PARKED = "parked"


@dataclass(frozen=True)
class RunEntry:
    """One member's standing in the rotation, computed from its inbox."""

    member: str
    depth: int
    oldest_ns: int
    repeats: int
    has_ingress: bool
    has_ask: bool
    priority_from: str
    passes: int
    state: str
    reason: str

    @property
    def priority(self) -> bool:
        return bool(self.priority_from)

    @property
    def score(self) -> int:
        return (
            ASK_WEIGHT * int(self.has_ask)
            + INGRESS_WEIGHT * int(self.has_ingress)
            + self.passes
        )

    @property
    def why(self) -> str:
        """The decomposition, words first — a bare number is a symptom with no
        cause, which is the one thing a status line must never be."""
        if self.priority:
            return f"PRIORITY ({self.priority_from}) — always next"
        parts = []
        if self.has_ask:
            parts.append("ask")
        parts.append("ingress" if self.has_ingress else "intra")
        if self.passes:
            parts.append(f"passed over {self.passes}")
        return " + ".join(parts)

    @property
    def held_note(self) -> str:
        """The blocker, said once. `reason` leads with the state word because
        the ticker prints it as a sentence; a status row already has the state
        in its own column, so the lead comes off there."""
        text = self.reason
        if text.lower().startswith(self.state + " "):
            text = text[len(self.state) + 1:]
        return text.lstrip("— ").strip().strip("()")

    @property
    def queue_note(self) -> str:
        """`2 queued (1 repeated x3), oldest 9m` — repeats surface because
        duplicate collapse is the one place r4t drops a file, and a count that
        is never printed is a count nobody can audit."""
        note = f"{self.depth} queued"
        if self.repeats > 1:
            note += f" (1 repeated x{self.repeats})"
        return f"{note}, oldest {fmt_age(self.age_seconds())}"

    def age_seconds(self, *, now: float | None = None) -> float:
        if not self.oldest_ns:
            return 0.0
        now = time.time() if now is None else now
        return max(0.0, now - self.oldest_ns / 1e9)

    def rank(self) -> tuple:
        """Descending priority order. Tier first so a configured priority
        sender can never be overtaken by aging; then score; then FIFO; then
        name, so two members that tie on everything still order the same way
        every time."""
        return (
            0 if self.priority else 1,
            -self.score,
            self.oldest_ns,
            self.member,
        )


def fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def runnable(node: str, config, member, rig) -> tuple[bool, str]:
    """Can this member start a turn right now? Returns (runnable, reason).
    The queue and everything else is untouched either way. Park is checked
    first: a member whose harness cannot start is out of the rotation whatever
    its budget says."""
    parked = state.read_parked(node, member.name)
    if parked:
        return False, f"parked — {parked.get('reason', 'structural failure')}"
    blocked, failures = state.breaker_open(
        node, member.name, config.breaker_cap, config.breaker_cooldown_seconds
    )
    if blocked:
        return False, f"breaker open ({failures} consecutive failed turns)"
    m = state.budget_level(node, member.name, rig.budget_max, rig.budget_earn_per_hour)
    t = state.budget_level(
        node, state.CELL_BUDGET_KEY,
        config.cell_budget_max, config.cell_budget_earn_per_hour,
    )
    if m < 1.0:
        wait = state.budget_seconds_until(
            node, member.name, rig.budget_max, rig.budget_earn_per_hour
        )
        return False, (
            f"resting (member budget {state.fmt_budget(m)}, "
            f"ready in ~{wait / 60:.0f} min)"
        )
    if t < 1.0:
        wait = state.budget_seconds_until(
            node, state.CELL_BUDGET_KEY,
            config.cell_budget_max, config.cell_budget_earn_per_hour,
        )
        return False, (
            f"resting (cell budget {state.fmt_budget(t)}, "
            f"ready in ~{wait / 60:.0f} min)"
        )
    if rig.rig_budget_max is not None:
        r = state.rig_budget_level(
            rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour
        )
        if r < 1.0:
            wait = state.rig_budget_seconds_until(
                rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour
            )
            return False, (
                f"resting — rig {rig.name} exhausted "
                f"({state.fmt_budget(r)}), ready in ~{wait / 60:.0f} min"
            )
    return True, ""


def _blocked_state(reason: str) -> str:
    if reason.startswith("parked"):
        return PARKED
    if reason.startswith("breaker"):
        return BREAKER
    return RESTING


def _oldest_ns(paths) -> int:
    """The nanosecond stamp `state.enqueue` put at the head of the oldest queue
    filename. Free — the name is already the arrival order, so nothing has to be
    written, parsed or clock-corrected to read it."""
    if not paths:
        return 0
    head = paths[0].name.split("-", 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


def _matches(sender: str, patterns) -> bool:
    s = (sender or "").strip().lower()
    return any(fnmatch.fnmatch(s, p.strip().lower()) for p in patterns if p.strip())


def snapshot(node: str, config, roster, *, priority_senders=()) -> list[RunEntry]:
    """Every member the rotation has an opinion about — one with mail waiting,
    or one parked and owed a clearing — scored and explained, in rank order.

    A handful of stat calls per member per selection. Do not cache it: a cache
    is a second source of truth, which is the thing this design does not have.
    """
    entries: list[RunEntry] = []
    for member in roster.members:
        paths = state.list_queue(node, member.name)
        parked = state.read_parked(node, member.name)
        if not paths and not parked:
            continue
        if member.errors:
            continue
        rig, _err, _pinned = config.rig_for(member)
        if rig is None:
            continue
        has_ingress = False
        repeats = 1
        priority_from = ""
        for path in paths:
            try:
                env = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(env, dict):
                continue
            sender = str(env.get("from", ""))
            if str(env.get("origin", "")) == ORIGIN_INGRESS:
                has_ingress = True
            repeats = max(repeats, int(env.get("repeats", 1) or 1))
            if not priority_from and _matches(sender, priority_senders):
                priority_from = sender
        ok, reason = runnable(node, config, member, rig)
        entries.append(
            RunEntry(
                member=member.name,
                depth=len(paths),
                oldest_ns=_oldest_ns(paths),
                repeats=repeats,
                has_ingress=has_ingress,
                has_ask=False,
                priority_from=priority_from,
                passes=int(state.read_meta(node, member.name).get("passes", 0) or 0),
                state=READY if ok else _blocked_state(reason),
                reason=reason,
            )
        )
    return sorted(entries, key=RunEntry.rank)


def next_up(entries, *, skip=()) -> RunEntry | None:
    """The member that goes next, or None when nothing can run. `skip` names
    members a caller has already tried and had held back this pass, so the loop
    moves on instead of re-picking the same one."""
    held = {s.lower() for s in skip}
    for entry in entries:
        if entry.state == READY and entry.member.lower() not in held:
            return entry
    return None


def record_selection(node: str, entries, chosen: str) -> None:
    """Age the rotation by one turn: every member that was ready and did not go
    has been passed over once more, and the one that went starts again from
    zero. Only READY members age — a member the budget held back was not passed
    over by the scheduler, and saying it was would let a resting member jump the
    queue the moment it can afford a turn."""
    picked = chosen.lower()
    for entry in entries:
        if entry.state != READY:
            continue
        if entry.member.lower() == picked:
            if entry.passes:
                state.update_meta(node, entry.member, passes=0)
        else:
            state.update_meta(node, entry.member, passes=entry.passes + 1)
