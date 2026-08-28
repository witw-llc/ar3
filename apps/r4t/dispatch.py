"""Dispatch — enqueue delivered mail, then drain member queues as batch turns.

Every inbound message to a member ENQUEUES, unconditionally, into that
member's durable queue (state.enqueue). No gate ever drops or dead-letters a
deliverable message; dead letters are for undeliverable mail only (unknown
recipient, disabled member, no rig). External mail always enters at the top:
the topmost leader IS the garden from outside, so outside senders cannot pick
a member. A separate drain loop picks a runnable
member with a non-empty queue and runs ONE turn that drains the WHOLE queue:
the prompt renders every queued message at once, so an agent that sees
"members discussed X, then the lead overrode with Y" pivots in one reading
instead of burning a turn per message.

ONE TURN AT A TIME is the contract, not a setting: the drain loop picks one
member, runs its turn to completion, and only then asks who is next. Which
member that is comes from schedule.py, which both this loop and `r4t status`
consult so the printed answer and the taken one cannot drift. Parallelism is a
second node, not a second turn.

Runnability is governed autonomously — no human gates. A member runs when its
own spend bucket and the shared cell bucket both hold at least 1 unit (a turn
costs 1 of each), its failure breaker is closed, it is not parked, and the
cadence throttle admits another start. An empty bucket means the member is
*resting*: its queue holds and it runs again when the bucket refills. Nothing is
lost.

The agent replies with the unmodified `tell`. Dispatch points the harness
subprocess's $TELL_OUTBOX_DIR at a per-turn staging dir and reads the staged
files as r4t-message DRAFTS (`to` + `body` + optional `files`), then releases
them: attribution (only this turn wrote there), the thread/hop/class stamped as
structured fields, per-turn send quota, then either the node's real outbox
(external — converted to an a8s envelope at the wall) or straight onto the
recipient member's queue (intra-roster, no header, no round-trip). A reply is
attributed to the thread of the message it answers.

Requeueing note: a8s treats exit 0 as the only delivery ack and redelivers the
envelope (with backoff) when a wake exits nonzero. `handle_message` therefore
acks early — it enqueues durably, then returns 0 whatever the turn does — so a
failed turn is retried by r4t's own quota-aware machinery rather than by a8s
handing the same message to the queue again. Redelivery only happens when
dispatch itself dies, and `state.enqueue`'s duplicate collapse absorbs it as
long as the queue hasn't drained yet.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# The isolation test (apps/r4t/tests/docker/run-as.sh) copies apps/r4t alone
# into a container with no repo root, so `ark` is not always reachable there —
# and a caged turn is exactly the case that needs the zone stated, because a
# container boots UTC until `rig.env` sets `TZ`. So the fallback reimplements
# the display contract rather than degrading to a stub: local time, always
# carrying its zone, with `ark.clock`'s abbreviation rule.
try:
    from ark.clock import (
        local_now,
        stamp as local_stamp,
        zone_label as local_zone,
    )
except ImportError:
    def local_now() -> datetime:
        return datetime.now().astimezone()

    def local_zone(when: datetime | None = None) -> str:
        dt = when or local_now()
        abbr = dt.strftime("%Z")
        if abbr.isalpha() and len(abbr) <= 5:
            return abbr
        off = dt.strftime("%z") or "+0000"
        return f"UTC{off[:3]}:{off[3:5]}"

    def _local_dt(ts: str | datetime) -> datetime | None:
        if isinstance(ts, datetime):
            dt = ts
        else:
            text = str(ts).strip()
            if not text:
                return None
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()

    def local_stamp(ts: str | datetime | None = None, *, seconds: bool = False) -> str:
        dt = local_now() if ts is None else _local_dt(ts)
        if dt is None:
            return str(ts)
        fmt = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
        return f"{dt.strftime(fmt)} {local_zone(dt)}"

def zoned_stamp() -> str:
    """History and day-log heading stamp: local display plus the UTC offset,
    because a zone abbreviation alone is not a reversible instant when the
    reader's machine or zone differs from the writer's."""
    dt = local_now()
    base = local_stamp(dt, seconds=True)
    off = dt.strftime("%z") or "+0000"
    tag = f"UTC{off[:3]}:{off[3:5]}"
    return base if base.endswith(tag) else f"{base} ({tag})"


import isolate
import knowledge
import schedule
import state
import transcript
from rig import (
    AGY_HOME_IDIOM,
    ENV_MCP_HOME_PEERS,
    McpPlan,
    RigConfig,
    RigError,
    Rig,
    apply_mcp,
    load_rig_config,
    mcp_home_peers,
    resolve_agy_model,
    revoke_mcp,
)
from notify import TellFn
from roster import Member, Roster, RosterError, load_roster

DRAIN_MAX_PASSES = 20

# Twin of ATTACHED_FILE_PREFIX in apps/a8s/definitions.py — the marker a8s
# injects into a wake message for each delivered file. Importing it would drag
# a8s's registry/core modules into dispatch (and collide with r4t's own flat
# module names), so the string is pinned here; test_isolate.py asserts the
# twins stay identical.
ATTACHED_FILE_PREFIX = "ATTACHED FILE: "

# Default prompt text, overridable sparsely by key via the a8s node definition's
# `prompts` object. Substitution fields: {name}, {node}, {workplace}, {now},
# {creator}, {thread}. Structural section headers stay in code (not doctrine).
#
# The time sentence lives in the intro rather than the doctrine block: the
# intro is read first and this is a framing statement, and `reinforce` keeps
# its last-read primacy for the operator's own words. An override written
# before {now} existed still renders — `str.format` ignores extra fields.
PROMPT_DEFAULTS: dict[str, str] = {
    "intro": (
        "You are {name}, a member of the {node} roster. Your working directory "
        "is {workplace} — that absolute path is your root. Every file you "
        "create or reference belongs under it unless a message tells you "
        "otherwise: write it under that absolute path rather than trusting a "
        "bare relative path to land there. If your tools advertise a different "
        "\"workspace root\" or \"project root\", ignore it for file placement — "
        "yours is the directory named above. Local time is {now}. Every "
        "relative time you read or write — today, tomorrow, this morning — "
        "resolves in that zone, not UTC."
    ),
    "echo_intro": (
        "You are {name}, a member of the {node} roster. Local time is {now}. "
        "Every relative time you read or write — today, tomorrow, this "
        "morning — resolves in that zone, not UTC."
    ),
    "mission_header": "## The mission (outranks every other document)",
    "charter_header": "## The charter (how this team works, whatever it is working on)",
    "workdir_note": (
        "The roster repo (org workplace) is at {workplace} — use that absolute "
        "path to reach shared roster files."
    ),
    "work_batch": (
        "- This is one turn: you were woken with every message above at once. "
        "Read them together and act on the current state, not each message in "
        "sequence. Your process ends when you finish; you are woken again when "
        "more messages arrive."
    ),
    "work_never_wait": (
        "- Never wait for a reply inside a turn. If you need work from "
        "members, message them and END your turn without answering the "
        "original request; when their replies wake you later, answer the "
        "person who asked once you have enough."
    ),
    "work_tell": (
        "- Send messages with the `tell` shell command (run it via your shell "
        "tool — printing it as text sends nothing). Body on stdin, delimiter "
        "quoted so nothing expands ($ ` \\ arrive byte-exact):\n"
        "        tell <name> - <<'EOF'\n"
        "        <your message>\n"
        "        EOF\n"
        "    Or write the body with your file tool: tell <name> - < msg.md. "
        "<name> is whoever asked, or a member. Members:"
    ),
    # Used in place of `work_tell` on a rig with the `mcp` knob on. It names the
    # tool verbatim: a tool described generically goes unused on small models,
    # while the named one was called 20/20.
    "work_tell_mcp": (
        "- Send messages by calling the `a8s_tell` tool (call the tool — "
        "printing text sends nothing). Pass `recipient` (the name) and `body` "
        "(your message), and `attachments` (a list of absolute paths) to send "
        "files with it. The body is delivered byte-exact; there is no shell. "
        "`recipient` is whoever asked, or a member. Members:"
    ),
    "work_direct": (
        "- Speak to members directly and one at a time — do not post to "
        "chat rooms or broadcast channels."
    ),
    "work_no_ack": (
        "- Do not send acknowledgment-only messages. If you have nothing "
        "substantive to add, send nothing — silence is fine."
    ),
    "work_body_only": (
        "- Your tell's body is the only thing the recipient sees — anything you "
        "write around it (framing, notes, your reasoning) is lost."
    ),
    "work_commit": "- Repo work is not done until it is committed.",
    "history_in_harness": (
        "This turn continues the session you are already in — your earlier "
        "messages and replies are above in it, so they are not repeated here."
    ),
    "reinforce": "Reinforcement from your operator: {text}",
    "flush_dump": "Save your current state and progress to STATUS.md.",
    "refound_preamble": "Check your STATUS.md to refresh your memory.",
    "mission_review": (
        "The roster's queues are empty and no thread is open, but the mission "
        "may not be met. Review the mission against where things stand and "
        "decide the next move — delegate the next step down the tree if there "
        "is one. No communication to the human NEEDS to happen: this is a "
        "working review, not a status report, so do not message the human "
        "unless you genuinely have something they must act on."
    ),
}


def _load_prompt_overrides(definition_path: Path | None) -> dict[str, str]:
    """Read the `prompts` object from the a8s node definition (sparse, by key).
    Tolerates absence at every step → returns {} and all defaults apply."""
    if not definition_path:
        return {}
    try:
        data = json.loads(Path(definition_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, dict):
        return {}
    return {
        k: v for k, v in prompts.items()
        if isinstance(v, str) and not k.startswith("_")
    }

RAN = "ran"
REROUTED = "rerouted"
DEFERRED = "deferred"
RESTING = "resting"
DEAD = "dead-letter"
QUEUED = "queued"
SKIPPED = "skipped"
BREAKER = "breaker"
PARKED = "parked"

# `run_harness` writes this when the OS refuses to start the harness at all —
# ENOENT and its kin. It is the one failure r4t can be sure will recur
# identically, so it is the one that parks a member instead of retrying it.
SPAWN_FAILURE_PREFIX = "failed to spawn harness "


@dataclass
class DispatchContext:
    root: Path
    node: str
    roster_path: Path
    config_path: Path
    tell_fn: TellFn
    workplace: Path | None = None
    comms: str = "open"
    leader_sees_lateral: bool = False
    egress: bool = True
    priority_senders: list[str] = field(
        default_factory=lambda: list(schedule.DEFAULT_PRIORITY_SENDERS)
    )
    isolation: isolate.Isolation = field(default_factory=isolate.Isolation)
    definition_path: Path | None = None
    ticker: bool = False

    def __post_init__(self) -> None:
        # `root` is where the roster's documents live (ROSTER.md, MISSION.md, the
        # a8s node's outbox); `workplace` is the repo where turns run and commits
        # land. A portable org splits them (see org.py); the in-repo default has
        # them equal.
        if self.workplace is None:
            self.workplace = self.root
        self._prompts = _load_prompt_overrides(self.definition_path)

    def prompt(self, key: str, **fields: object) -> str:
        """Resolve a prompt bullet: the definition's override for `key`, else the
        built-in default, with any substitution fields filled in."""
        template = self._prompts.get(key) or PROMPT_DEFAULTS[key]
        return template.format(**fields) if fields else template

    def event(self, name: str, subject: str, rest: str = "") -> None:
        """One lifecycle line on the node ticker.

        a8s pumps a wake's stdout into that node's log as the lines arrive, so
        a flushed line here is a line in `a8s logs <node> -f`. The two logs
        have separate jobs: the day log (`state.append_log`) is the archive —
        whole prompts, whole transcripts, scoped after the fact by `r4t logs` —
        and this is the ticker, the roster running, one event at a time.

        The vocabulary, `r4t: <EVENT> <member> <rest>`:

            QUEUED    a message joined a member's queue
            REFUSED   an address named this member or cell and did not deliver
            TURN      a turn started
            DONE      a turn ended
            RESTING   a member with mail did not run: budget
            BREAKER   ... its breaker is open
            DEFERRED  ... the cadence throttle held the start
            PARKED    a member left the rotation: its harness cannot start
            RESUME    ... and a free probe says it can again
            RECOVERED a killed turn's batch went back to the queue

        `subject` is the member the line is about and is always the second
        field, so a reader — and a later `r4t logs <member>` — matches on the
        subject rather than on any name the line happens to mention.

        **Never a message body, and never transcript text.** Names, counts,
        outcomes, durations, reasons. A member's own harness output is
        megabytes per turn and would drown the one stream the roster is meant
        to be watchable in; it lives in `r4t logs <member> --full` and the
        live tail.

        Off unless an entry point that owns its stdout turns it on. The a8s
        wake verbs do. The chat UIs draw their own terminal from a thread that
        runs turns, so they leave it off.
        """
        if not self.ticker:
            return
        print(f"r4t: {name} {subject}" + (f" {rest}" if rest else ""), flush=True)


def split_recipient(to: str) -> tuple[str, str]:
    """`acme:phil` -> (`acme`, `phil`); bare `acme` -> (`acme`, `""`).
    The sub-address is everything after the FIRST colon, verbatim."""
    to = (to or "").strip()
    if ":" in to:
        node, sub = to.split(":", 1)
        return node.strip(), sub.strip()
    return to, ""


def _is_internal(node: str, to: str) -> bool:
    t = (to or "").strip().lower()
    return t == node.lower() or t.startswith(node.lower() + ":")


def _display_name(node: str, addr: str) -> str:
    prefix = node.lower() + ":"
    a = (addr or "").strip()
    return a[len(prefix):] if a.lower().startswith(prefix) else a


def _canonical_recipient(node: str, roster: Roster, to: str) -> str:
    """Agents address the walled garden by bare first name; the wire uses
    `node:name`. Bare roster names canonicalize to internal form; anything
    else (external addresses, unknown names) passes through untouched."""
    t = to.strip()
    if ":" in t:
        prefix, _, sub = t.partition(":")
        if prefix.strip().lower() != node.lower():
            return t
        name = sub
    else:
        name = t
    member = roster.find(name)
    if member is None:
        return t
    return f"{node}:{member.name.lower()}"


def _same_recipient(node: str, roster: Roster, a: str, b: str) -> bool:
    return (
        _canonical_recipient(node, roster, a).strip().lower()
        == _canonical_recipient(node, roster, b).strip().lower()
    )


def _internal_ai_member(
    ctx: DispatchContext, roster: Roster | None, recipient: str
) -> Member | None:
    """The runnable AI member `recipient` (canonical or bare) names, if any —
    the guard for routing operational feedback back in-band."""
    if roster is None:
        return None
    if ":" in recipient and not _is_internal(ctx.node, recipient):
        return None
    member = roster.find(_display_name(ctx.node, recipient))
    if member is None or member.errors:
        return None
    return member


def _tell_error(
    ctx: DispatchContext,
    recipient: str,
    text: str,
    *,
    thread: str | None = None,
    roster: Roster | None = None,
) -> None:
    """Operational feedback to a sender. For an INTRA-roster sender it is an
    internal `class=error` r4t-message carrying the ORIGINATING thread id:
    because it already has a thread it can never mint a fresh one, so it cannot
    spawn a headerless new-task turn — it dies at the normal budget/answer gates
    like any other message. External senders keep the direct a8s tell."""
    state.append_log(ctx.node, f"r4t: ERROR -> {recipient}: {text}")
    member = _internal_ai_member(ctx, roster, recipient)
    if member is not None and thread:
        state.enqueue(
            ctx.node,
            member.name,
            {
                "from": f"r4t:{ctx.node}",
                "to": f"{ctx.node}:{member.name.lower()}",
                "thread": thread,
                "hop": 0,
                "class": "error",
                "body": text,
            },
        )
        return
    ctx.tell_fn(recipient, f"[r4t {ctx.node}] {text}")


def _load_roster(ctx: DispatchContext, sender: str) -> Roster | None:
    try:
        return load_roster(ctx.roster_path, node=ctx.node)
    except RosterError as e:
        _tell_error(ctx, sender, f"cannot dispatch: {e}")
        return None


def _load_config(ctx: DispatchContext, sender: str) -> RigConfig | None:
    try:
        return load_rig_config(ctx.config_path)
    except RigError as e:
        _tell_error(ctx, sender, f"cannot dispatch: {e}")
        return None


def _dispatchable_names(roster: Roster) -> list[str]:
    return [m.name for m in roster.members if not m.errors]


def _find_cell(roster: Roster, name: str) -> str | None:
    key = name.strip().lower()
    for cell in roster.cells:
        if cell.lower() == key:
            return cell
    return None


def _member_lines(ctx: DispatchContext, roster: Roster, member: Member) -> list[str]:
    # Information hiding: when the roster declares a tree, a member sees only
    # its tree-adjacent names (lead, reports, cell-mates) — lateral contact
    # becomes informationally unthinkable, not just rerouted.
    # A flat roster (no Lead lines) still lists the whole roster, as before.
    if roster.declares_tree:
        pool = roster.adjacent(member)
    else:
        pool = [m for m in roster.members if m.name.lower() != member.name.lower()]
    lines: list[str] = []
    for m in pool:
        if not m.errors:
            lines.append(
                f"    - {m.name} (tell {m.name.lower()}) — {m.role}".rstrip(" —")
            )
    return lines


def _charter_section(ctx: DispatchContext) -> list[str]:
    """The runbook's `## Charter` — how the team operates regardless of what
    it is working on, so unlike the mission it reaches EVERY member."""
    from runbook import charter_text

    text = charter_text(ctx.root, ctx.node)
    if not text:
        return []
    return [ctx.prompt("charter_header"), text, ""]


def _mission_section(ctx: DispatchContext, roster: Roster, member: Member) -> list[str]:
    """The mission is injected verbatim into a lead's turn prompt and no one
    else's. A member is a lead when it has direct reports (tree rosters); a
    flat roster with no tree declared treats the marked Leader as the only
    lead. ICs never see it injected — their lead restates the intent at
    the resolution they can hold.
    """
    from runbook import mission_text

    text = mission_text(ctx.root, ctx.node)
    if not text:
        return []
    if roster.declares_tree:
        is_lead = bool(roster.reports_to(member))
    else:
        is_lead = member.leader
    if not is_lead:
        return []
    return [
        ctx.prompt("mission_header"),
        text,
        "",
    ]


def resolve_workdir(ctx: DispatchContext, member: Member) -> Path:
    """The directory a member's turn runs from: its `Workdir:` when set
    (relative paths against the workplace; absolute and `~` paths as given),
    else the workplace itself."""
    if not member.workdir:
        return ctx.workplace
    p = Path(member.workdir).expanduser()
    if p.is_absolute():
        return p
    return ctx.workplace / p


def prompt_sections(
    ctx: DispatchContext,
    roster: Roster,
    member: Member,
    batch: list[dict],
    rig: Rig,
    *,
    continues: bool = False,
    refound: bool = False,
) -> list[tuple[str, list[str]]]:
    """The wake prompt as ordered, labeled sections — the composition is
    knowable only here, at build time, so this is where its shape is
    exposed. Joining every section's parts with newlines yields the prompt
    byte-for-byte; `prompt_stats` measures the same structure.

    `continues` means this turn runs inside the CLI's own conversation
    (`Continue: on`, a rig that resumes, and not a refound) — the harness
    already carries the whole conversation, so embedding r4t's transcript of it
    would send the same context twice. A one-line note stands in its place so
    the missing section never reads as amnesia. Echo members always get the
    transcript: they have no CLI conversation at all.

    A member's `Reinforce:` line closes every variant of the prompt — echo and
    continue included — because on a small model the last thing read wins, and
    winning there is the field's whole job. Absent, the prompt is byte-identical
    to a roster without the field."""
    preamble: list[tuple[str, list[str]]] = (
        [("preamble", [ctx.prompt("refound_preamble"), ""])] if refound else []
    )
    history = state.read_history(ctx.node, member.name)
    members = _member_lines(ctx, roster, member)
    message_lines: list[str] = []
    for env in batch:
        sender = _display_name(ctx.node, str(env.get("from", "?")))
        thread = str(env.get("thread", "")) or "?"
        repeats = int(env.get("repeats", 1) or 1)
        body = str(env.get("body", "")).strip() or "(empty message)"
        if len(body) > rig.prompt_body_max:
            body = body[:rig.prompt_body_max] + "\n[... message truncated by r4t ...]"
        if str(env.get("class", "")) == "error":
            header = f"From: {sender} (operational error, thread {thread})"
        else:
            header = f"From: {sender} (thread {thread})"
        if repeats > 1:
            header += f" (sent {repeats} times)"
        message_lines.append(header)
        message_lines.append("")
        message_lines.append(body)
        message_lines.append("")
    if rig.echo:
        # An echo member has no tools and no concept of messages: no tell
        # instructions, no member list, no how-to-work doctrine. It gets who
        # it is, what has been said, and the new messages — its stdout IS the
        # reply (_stage_echo_reply).
        sections = preamble + [
            ("intro", [
                ctx.prompt(
                    "echo_intro",
                    name=member.name,
                    node=ctx.node,
                    now=local_stamp(),
                ),
                "",
            ]),
            ("mission", _mission_section(ctx, roster, member)),
            ("charter", _charter_section(ctx)),
            ("persona", [
                "## Who you are (from the roster)",
                member.persona or f"### {member.name}",
                "",
            ]),
            ("history", [
                "## Your conversation so far (messages you received and sent)",
                history.strip() or "(no prior messages — this is your first recorded turn)",
                "",
            ]),
            ("messages", [
                "## Messages since your last turn",
                *(message_lines or ["(none)"]),
            ]),
        ]
        if member.reinforce:
            sections.append(
                ("reinforce", ["", ctx.prompt("reinforce", text=member.reinforce)])
            )
        return [(label, parts) for label, parts in sections if parts]
    workdir = resolve_workdir(ctx, member)
    workdir_lines: list[str] = []
    if workdir.resolve() != ctx.workplace.resolve():
        workdir_lines = [
            ctx.prompt("workdir_note", workplace=ctx.workplace.resolve())
        ]
    history_section = (
        ctx.prompt("history_in_harness") if continues
        else history.strip() or "(no prior messages — this is your first recorded turn)"
    )
    sections = preamble + [
        ("intro", [
            ctx.prompt(
                "intro",
                name=member.name,
                node=ctx.node,
                workplace=workdir.resolve(),
                now=local_stamp(),
            ),
            *workdir_lines,
            "",
        ]),
        ("mission", _mission_section(ctx, roster, member)),
        ("charter", _charter_section(ctx)),
        ("persona", [
            "## Who you are (from the roster)",
            member.persona or f"### {member.name}",
            "",
        ]),
        ("history", [
            "## Your conversation so far (messages you received and sent)",
            history_section,
            "",
        ]),
        ("messages", [
            "## Messages since your last turn",
            *(message_lines or ["(none)"]),
        ]),
        ("doctrine", [
            "## How to work",
            ctx.prompt("work_batch"),
            ctx.prompt("work_never_wait"),
            ctx.prompt("work_tell_mcp" if rig.mcp_on else "work_tell"),
            *(members or ["    - (none)"]),
            ctx.prompt("work_direct"),
            ctx.prompt("work_no_ack"),
            ctx.prompt("work_body_only"),
            ctx.prompt("work_commit"),
        ]),
        # Knowledge rides after the doctrine and before Reinforce, so the
        # closing line keeps its last-read primacy. Echo members never get it
        # (a reachability probe has no use for recall). Off (the default)
        # keeps the prompt byte-identical.
        ("knowledge", knowledge.knowledge_section(ctx, member, batch, rig)),
    ]
    if member.reinforce:
        sections.append(
            ("reinforce", ["", ctx.prompt("reinforce", text=member.reinforce)])
        )
    return [(label, parts) for label, parts in sections if parts]


def build_prompt(
    ctx: DispatchContext,
    roster: Roster,
    member: Member,
    batch: list[dict],
    rig: Rig,
    *,
    continues: bool = False,
    refound: bool = False,
) -> str:
    """The joined form of `prompt_sections` — see there for the shape rules."""
    return "\n".join(
        p
        for _label, parts in prompt_sections(
            ctx, roster, member, batch, rig, continues=continues, refound=refound
        )
        for p in parts
    )


def prompt_stats(sections: list[tuple[str, list[str]]]) -> list[tuple[str, int]]:
    """UTF-8 byte size per section, in prompt order. The single newline joining
    adjacent sections is counted in the total (`len(prompt.encode())`), not in
    any section — so total == sum(sizes) + len(sections) - 1."""
    return [
        (label, len("\n".join(parts).encode("utf-8")))
        for label, parts in sections
    ]


def _kb(n: int) -> str:
    return f"{n / 1000:.1f}k"


def run_harness(
    rig: Rig,
    prompt: str,
    cwd: Path,
    *,
    env: dict | None = None,
    variant: int = 0,
) -> tuple[int, str, float, bool]:
    """Run the rig's argv (pool variant `variant`) with {prompt} substituted
    as a single argv element — never a shell — and {workdir} with `cwd`, for a
    harness that takes its working directory as an argument. Returns
    (exit_code, output, duration_seconds, timed_out). `PWD` in the turn env is
    pinned to `cwd`, because a spawned process otherwise inherits the caller's.
    When the env carries `R4T_LIVE_LOG`, the harness output is teed there line
    by line as it arrives, so a gemba attach can tail the turn live; the full
    output is still returned for staging.

    When the org sets `run_as` or `container` (org.py)
    the choice rides in via the turn env and the argv is wrapped in the OS-level
    boundary — every member turn of that org, whatever rig. An isolation prereq
    that fails closed returns a nonzero exit like any other failed turn — the
    batch stays queued and the breaker counts it.

    The rig's `env` map rides the turn environment on top of r4t's own controls,
    and is named to the isolation wrapper so it survives the boundary.

    `R4T_CONTINUE` in the env (set for a member with `Continue: on`) appends the
    rig's continue tokens so the CLI resumes the conversation it already has in
    `cwd`. It rides the env for the same reason isolation does: the run_fn
    contract stays narrow."""
    argv = rig.argv(
        prompt,
        variant,
        continue_conversation=(env or {}).get("R4T_CONTINUE") == "1",
        workdir=cwd,
    )
    if rig.model_resolver == "agy-live":
        # Resolve the friendly --model against the live `agy models` list before
        # every turn — the display names drift as agy ships versions, and agy
        # silently ignores an unrecognized string, so a stale/bad value must
        # fail the turn loudly rather than run the account default.
        try:
            resolved = resolve_agy_model(rig.model or "")
        except RigError as e:
            return 127, f"agy --model {rig.model!r} did not resolve: {e}", 0.0, False
        argv = [resolved if a == "{model}" else a for a in argv]

    # The rig's `env` map (docs/r4t-rigs.md): static harness knobs on every turn.
    # It goes on before r4t's own per-turn injections below (the mcp idiom's
    # variables, the PWD pin) so those still win, and a name the turn owns fails
    # the rig closed at parse time, so this cannot shadow one.
    if env is not None:
        env.update(rig.env)

    staging = (env or {}).get("TELL_OUTBOX_DIR", "")
    delivered = (env or {}).get("R4T_DELIVERED_DIR", "")
    isolation = isolate.isolation_from_env(env)

    # The `mcp` knob: splice the a8s MCP server in with this harness's own
    # idiom, before any isolation wrapper takes the argv over. The idioms ride
    # different channels — argv survives a boundary, environment and files only
    # cross when the wrapper is told to carry them — so the injection states its
    # needs and the wrapper below honours them.
    mcp = McpPlan(argv=argv)
    if env is not None:
        if rig.mcp_on:
            try:
                mcp = apply_mcp(rig, argv, env, cwd, isolation)
            except (RigError, isolate.IsolationError) as e:
                # Same verdict as the read probe below: a prompt that teaches
                # `a8s_tell` against a server that cannot start leaves the member
                # with no way to send at all, so the turn fails instead.
                return 126, str(e), 0.0, False
            argv = mcp.argv
        else:
            # Off has to UN-write what on wrote, in the same place and the same
            # breath — one idiom's channel is a file the harness reads whether
            # r4t injected anything this turn or not. A member holding a tool
            # its prompt no longer teaches is degraded, not broken, so a removal
            # that cannot happen is logged and the turn runs anyway.
            stale = revoke_mcp(rig, env, isolation)
            node = env.get("R4T_NODE", "")
            if stale and node:
                state.append_log(node, f"r4t: MCP-STALE {stale}")

    # An `env_reset`/container keeps only what the wrapper is told to carry, so
    # the rig map has to be named to it the same way the mcp idiom's env is.
    # The idiom wins a collision — it is r4t's own per-turn injection.
    boundary_env = {**rig.env, **mcp.env_pass}

    kill_container_name: str | None = None
    if isolation.run_as:
        probe_error = isolate.probe_run_as(isolation.run_as, cwd)
        if probe_error:
            return 126, f"run_as {isolation.run_as!r} isolation failed: {probe_error}", 0.0, False
        # A prompt that teaches `a8s_tell` against a server the boundary's user
        # cannot start leaves the member with no way to send at all, so an
        # unreadable server fails the turn rather than degrading it.
        mcp_error = isolate.probe_readable_as(isolation.run_as, mcp.read_paths)
        if mcp_error:
            return 126, f"rig {rig.name!r} has mcp on but {mcp_error}", 0.0, False
        if staging:
            isolate.assert_writable_shared_dir(staging, isolate.agent_gid(isolation.run_as))
        if delivered:
            # The bundle AND its parent delivered/ dir, so the agent user can
            # traverse to the copies — 2750, the read-only counterpart of the
            # 2770 staging channel.
            gid = isolate.agent_gid(isolation.run_as)
            isolate.assert_readonly_shared_dir(Path(delivered).parent, gid)
            isolate.assert_readonly_shared_dir(delivered, gid)
        argv = isolate.wrap_run_as(
            argv, isolation.run_as, staging, cwd, env_pass=boundary_env
        )
    elif isolation.container:
        kill_container_name = isolate.container_name(
            (env or {}).get("R4T_NODE", ""), (env or {}).get("R4T_MEMBER", "")
        )
        argv = isolate.build_container_argv(
            argv,
            isolation.container,
            name=kill_container_name,
            staging_dir=staging,
            workplace=cwd,
            tell_outbox=staging,
            container_args=isolation.container_args,
            delivered_dir=delivered or None,
            extra_env=boundary_env,
            extra_ro_dirs=mcp.mount_dirs,
        )

    live_log = (env or {}).get("R4T_LIVE_LOG")
    # PWD is a shell convention no kernel maintains, so a spawned harness
    # inherits the PWD of whoever started r4t however `cwd` is set. A harness
    # that resolves its own paths against it (opencode does, for --dir) would
    # anchor them outside the member's workdir.
    turn_env = dict(env if env is not None else os.environ, PWD=str(cwd))
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=turn_env,
            start_new_session=True,
        )
    except OSError as e:
        return 127, f"failed to spawn harness {argv[0]!r}: {e}", 0.0, False

    chunks: list[str] = []

    def _pump() -> None:
        sink = None
        if live_log:
            try:
                sink = open(live_log, "a", encoding="utf-8")
            except OSError:
                sink = None
        try:
            for line in proc.stdout:
                chunks.append(line)
                if sink is not None:
                    sink.write(line)
                    sink.flush()
        finally:
            if sink is not None:
                sink.close()

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=rig.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        # A container runs detached from the `docker run` client's process
        # group, so killpg alone leaks it — kill it by its deterministic name,
        # then `--rm` reaps it.
        if kill_container_name:
            isolate.kill_container(kill_container_name)
        if os.name != "posix":
            proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
        proc.wait()
    reader.join()
    duration = time.monotonic() - start
    return proc.returncode, "".join(chunks), duration, timed_out


def _throttle_block(ctx: DispatchContext, config: RigConfig) -> str | None:
    """The cadence gate, and nothing else. Concurrency is not a setting: the
    admission lock plus the per-member lock hold the node to one live turn by
    contract, so there is no number left to compare against. Cadence survives
    because it is orthogonal — deliberate slow motion for watching a rotation,
    off by default."""
    throttle = config.throttle
    if throttle.min_seconds_between_turn_starts > 0:
        last = state.read_last_turn_start(ctx.node)
        if last is not None:
            elapsed = time.time() - last
            if elapsed < throttle.min_seconds_between_turn_starts:
                return (
                    f"roster throttle: last turn started {elapsed:.0f}s ago < "
                    f"min_seconds_between_turn_starts "
                    f"{throttle.min_seconds_between_turn_starts:g}"
                )
    return None


# ---------- ingress (enqueue only; never runs a turn) ----------

def class_from_meta(raw: str) -> str:
    """The class an external peer stamped on the a8s envelope's `meta`.

    Wire metadata is advisory for governance and never for identity: a peer may
    downgrade its OWN traffic to machine relay (`auto`), and anything else —
    absent, unparseable, a word this version does not know — is deliberate
    attention. Thread and hop stay garden-internal whatever the wire says."""
    if not raw.strip():
        return "human"
    try:
        meta = json.loads(raw)
    except ValueError:
        return "human"
    if not isinstance(meta, dict):
        return "human"
    return "auto" if str(meta.get("class", "")).strip().lower() == "auto" else "human"


def _ingest(
    ctx: DispatchContext,
    sender: str,
    to: str,
    body: str,
    *,
    klass: str,
    internal: bool,
    thread: str | None = None,
    hop: int = 0,
    roster: Roster | None = None,
    config: RigConfig | None = None,
    files: list[dict] | None = None,
    bundle: Path | None = None,
) -> str:
    """Resolve the recipient and enqueue a structured r4t-message.
    Undeliverable mail dead-letters with an audit record; a deliverable message
    to a member enqueues unconditionally (duplicate-collapsed) and returns
    QUEUED. No text header is parsed or stamped — `thread`/`hop`/`class` travel
    as fields end to end.

    Routing turns on `internal` in one place only — the thread. Intra-roster
    traffic carries the resolved `thread`/`hop`; external mail always opens a
    fresh one. Both honor `node:member` addressing, and from OUTSIDE the wall
    that address is gated: a member reachable directly is one that says so with
    `Ingress:`. Everyone else's mail enters at the leader, and an address that
    named a walled member is refused rather than quietly redirected — the
    leader would otherwise answer for a member who never saw it, and the sender
    would never learn its address was ignored."""
    if roster is None:
        roster = _load_roster(ctx, sender)
    if roster is None:
        return SKIPPED

    prefix, sub = split_recipient(to)
    if not internal:
        thread = None  # external mail always opens a fresh thread
        if prefix.strip().lower() != ctx.node.lower():
            # Delivered under an alias or the bare agent name: nothing here
            # named a member, so this is the node's own mail.
            sub = ""
        to = f"{ctx.node}:{sub}" if sub else ctx.node

    if sub:
        member = roster.find(sub)
        cell = _find_cell(roster, sub)
        if member is None and cell is not None:
            _tell_error(
                ctx, sender,
                f"{cell} names a cell, and one post forked to a whole cell is "
                f"deferred (#183) — address a member, or send to {ctx.node} to "
                f"reach the leader.",
                thread=thread, roster=roster,
            )
            state.record_dead_letter(
                ctx.node, reason="cell-deferred", sender=sender, to=to,
                thread=thread or "", content=body,
            )
            ctx.event("REFUSED", cell.lower(), "cell fan-out is deferred (#183)")
            return DEAD
        if member is None:
            names = ", ".join(_dispatchable_names(roster)) or "(none)"
            _tell_error(
                ctx, sender,
                f"{ctx.node} has no member or cell named {sub!r} — send to "
                f"{ctx.node} to reach the leader. Dispatchable members: {names}.",
                thread=thread, roster=roster,
            )
            state.record_dead_letter(
                ctx.node, reason="unknown-recipient", sender=sender, to=to,
                thread=thread or "", content=body,
            )
            return DEAD
        if not internal and not member.ingress:
            _tell_error(
                ctx, sender,
                f"{member.name} does not accept ingress; external mail enters "
                f"at the leader — send to {ctx.node}, or set "
                f"`- **Ingress:** yes` on {member.name} in the runbook.",
                thread=thread, roster=roster,
            )
            state.record_dead_letter(
                ctx.node, reason="no-ingress", sender=sender, to=to,
                thread=thread or "", content=body,
            )
            ctx.event(
                "REFUSED", member.name.lower(), f"no ingress; from {sender}"
            )
            return DEAD
    else:
        # Nothing past the colon: the node's own mail is the leader's mail.
        member = roster.leader()
        if member is None:
            # `load_roster` refuses a leaderless roster, so this is reachable
            # only for a roster handed in by a caller that parsed it itself.
            names = ", ".join(_dispatchable_names(roster)) or "(none)"
            _tell_error(
                ctx, sender,
                "no leader is marked in the roster, so bare messages to "
                f"{ctx.node} have no recipient. Address a member directly "
                f"(members: {names}).",
                thread=thread, roster=roster,
            )
            state.record_dead_letter(
                ctx.node, reason="no-leader", sender=sender, to=to,
                thread=thread or "", content=body,
            )
            return DEAD

    if member.errors:
        _tell_error(
            ctx, sender,
            f"{member.name} is disabled by a roster problem: {member.error}. "
            f"Fix {ctx.roster_path.name} and resend.",
            thread=thread, roster=roster,
        )
        state.record_dead_letter(
            ctx.node, reason="member-disabled", sender=sender, to=to,
            thread=thread or "", content=body,
        )
        return DEAD

    if config is None:
        config = _load_config(ctx, sender)
    if config is None:
        return SKIPPED
    rig, err, _pinned = config.rig_for(member)
    if rig is None:
        _tell_error(
            ctx, sender, f"{member.name} cannot run: {err}",
            thread=thread, roster=roster,
        )
        state.record_dead_letter(
            ctx.node, reason="no-rig", sender=sender, to=to,
            thread=thread or "", content=body,
        )
        return DEAD

    if thread is None:
        thread = state.new_ulid()
        hop = 0

    state.enqueue(
        ctx.node,
        member.name,
        {
            "from": sender,
            "to": _canonical_recipient(ctx.node, roster, to),
            "thread": thread,
            "hop": hop,
            "class": klass,
            # Where the message came from, stamped once at the wall. The
            # rotation scores mail from outside the roster above one member
            # talking to another (schedule.py), and this is the only moment
            # that distinction is knowable — by the time an envelope is sitting
            # in a queue, `from` alone cannot tell an outside human named for a
            # member apart from the member.
            "origin": (
                schedule.ORIGIN_INTRA if internal else schedule.ORIGIN_INGRESS
            ),
            "body": body,
        },
    )
    state.update_meta(ctx.node, member.name, last_inbound_at=state.utc_now())
    preview = " ".join(body.split())[:80]
    depth = state.queue_depth(ctx.node, member.name)
    state.append_log(
        ctx.node,
        f"r4t: QUEUED {sender} -> {member.name.lower()} thread={thread} "
        f'hop={hop} "{preview}" (depth {depth})',
    )
    # The archive line above carries a preview of the body; the ticker does not.
    ctx.event(
        "QUEUED", member.name.lower(),
        f"from {sender} thread={thread} hop={hop} depth={depth}",
    )
    return QUEUED


# ---------- staging release ----------

def _real_outbox(ctx: DispatchContext) -> Path:
    raw = os.environ.get("TELL_OUTBOX_DIR", "").strip()
    if raw:
        return Path(raw)
    return ctx.root / ".outbox"


def _release_one(
    ctx: DispatchContext,
    outbox: Path,
    staging: Path,
    envelope: dict,
    sender_addr: str,
    thread_id: str,
    next_hop: int,
    body: str,
    roster: Roster,
    config: RigConfig,
    *,
    inside: bool,
) -> None:
    to = str(envelope.get("to", "")).strip()
    if inside:
        bundle = staging / str(envelope.get("id", ""))
        _ingest(
            ctx, sender_addr, to, body,
            klass="auto", internal=True, thread=thread_id, hop=next_hop,
            roster=roster, config=config,
            files=envelope.get("files") or [],
            bundle=bundle if bundle.is_dir() else None,
        )
        if bundle.is_dir():
            shutil.rmtree(bundle, ignore_errors=True)
            state.append_log(
                ctx.node,
                f"r4t: WARN attachments dropped on intra-roster route "
                f"{sender_addr} -> {to}",
            )
        state.append_log(
            ctx.node,
            f"r4t: RELEASED-internal {sender_addr} -> {to} thread={thread_id} "
            f"hop={next_hop}",
        )
        return
    # Egress protocol: the r4t header never leaves the garden. Other a8s nodes
    # must not need to know whether a name is one agent, a human, a device, or
    # a whole roster — class marking survives as envelope metadata only.
    envelope["content"] = body
    envelope["meta"] = {"class": "auto"}
    envelope["from"] = sender_addr
    outbox.mkdir(parents=True, exist_ok=True)
    msg_id = str(envelope.get("id", "")) or state.new_ulid()
    envelope["id"] = msg_id
    bundle = staging / msg_id
    if bundle.is_dir():
        destination = outbox / msg_id
        if destination.exists():
            shutil.rmtree(bundle, ignore_errors=True)
        else:
            try:
                os.replace(bundle, destination)
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise
                temporary = outbox / f".{msg_id}.{state.new_ulid()}.tmp"
                try:
                    shutil.copytree(bundle, temporary)
                    if not destination.exists():
                        os.replace(temporary, destination)
                finally:
                    shutil.rmtree(temporary, ignore_errors=True)
                shutil.rmtree(bundle, ignore_errors=True)
    state.atomic_write_json(outbox / f"{msg_id}.json", envelope)
    state.append_log(
        ctx.node,
        f"r4t: RELEASED {sender_addr} -> {to} thread={thread_id} hop={next_hop}",
    )


def _reachable_names(
    ctx: DispatchContext, roster: Roster, member: Member, batch: list[dict]
) -> set[str]:
    """Names this member may address intra-roster without rerouting: its
    tree-adjacent members (lead, reports, cell-mates) and whoever messaged it
    this turn (answering a batch sender never reroutes)."""
    names = {m.name.lower() for m in roster.adjacent(member)}
    for env in batch:
        names.add(_display_name(ctx.node, str(env.get("from", ""))).strip().lower())
    return names


def _copy_lateral_to_lead(
    ctx: DispatchContext,
    roster: Roster,
    member: Member,
    rig: Rig,
    to: str,
    body: str,
    thread_id: str,
) -> None:
    """`leader_sees_lateral`: land a read-only history copy of a lateral
    (peer) delivery on the sender's lead so the lead sees it on its next real
    turn — no turn is burned, and traffic UP to the lead is skipped (already
    visible)."""
    if not member.lead:
        return
    lead = roster.find(member.lead)
    if lead is None or lead.errors:
        return
    recipient_name = _display_name(ctx.node, to).strip().lower()
    if recipient_name == lead.name.lower():
        return
    recipient = roster.find(recipient_name)
    if recipient is None:
        return
    clip = body if len(body) <= rig.history_body_max else body[:rig.history_body_max] + " [...]"
    state.append_history(
        ctx.node,
        lead.name,
        f"## {zoned_stamp()} lateral {member.name} -> "
        f"{_display_name(ctx.node, to)} (thread {thread_id})\n\n{clip}",
        max_bytes=rig.history_max_bytes,
    )
    state.append_log(
        ctx.node,
        f"r4t: LATERAL-COPY {member.name.lower()} -> {recipient_name} "
        f"visible to lead {lead.name.lower()}",
    )


def release_staging(
    ctx: DispatchContext,
    config: RigConfig,
    roster: Roster,
    member: Member,
    rig: Rig,
    batch: list[dict],
) -> dict:
    """Process the turn's staged envelopes in send order: per-turn send quota,
    thread attribution, outbound history, then release (real outbox or the
    recipient member's queue). A reply is attributed to the thread of the
    message it answers — the newest queued message from that recipient in this
    batch; a message to someone the batch did not include rides the batch's
    newest thread. A substantive reply to a thread's originator closes it.
    Returns {"released": n, "violations": n}."""
    staging = state.staging_dir(ctx.node, member.name)
    sender_addr = f"{ctx.node}:{member.name.lower()}"
    outbox = _real_outbox(ctx)

    consumed: dict[str, tuple[str, int]] = {}
    newest: tuple[str, int] | None = None
    for env in batch:
        key = _display_name(ctx.node, str(env.get("from", ""))).strip().lower()
        pair = (str(env.get("thread", "")), int(env.get("hop", 0) or 0))
        consumed[key] = pair
        newest = pair
    if newest is None:
        newest = (state.new_ulid(), 0)

    # `closed` comms keeps the hard reroute-through-lead; `open` (the default)
    # delivers to any valid member and computes no reachability set.
    reachable = (
        _reachable_names(ctx, roster, member, batch)
        if roster.declares_tree and ctx.comms == "closed"
        else None
    )
    top_leader = roster.leader()
    is_top = top_leader is not None and member.name.lower() == top_leader.name.lower()

    released = 0
    violations = 0
    for i, path in enumerate(state.staged_envelopes(ctx.node, member.name)):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            continue
        if not isinstance(envelope, dict):
            path.unlink(missing_ok=True)
            continue
        to = str(envelope.get("to", "")).strip()
        body = str(envelope.get("content", "")).strip()
        if not to or not body.strip():
            path.unlink(missing_ok=True)
            continue
        global_form = to.startswith(":")
        if global_form:
            to = to[1:].strip()
            if not to or to.startswith(":"):
                path.unlink(missing_ok=True)
                violations += 1
                bad = str(envelope.get("to", "")).strip()
                state.record_dead_letter(
                    ctx.node, reason="bad-address", sender=sender_addr, to=bad,
                    thread=newest[0], content=body,
                )
                state.append_log(
                    ctx.node,
                    f"r4t: BAD-ADDRESS {sender_addr} -> {bad} (one leading "
                    f"colon means global, and it takes a name)",
                )
                continue
        else:
            to = _canonical_recipient(ctx.node, roster, to)
        envelope["to"] = to
        # A leading colon means the address leaves the walls, whatever it
        # names. That is the whole escape hatch: `:bob` is the a8s node bob
        # even when this roster has a member called bob, and `:acme:bob` on
        # this own node goes out and comes back at the ingress gate rather
        # than short-circuiting past it.
        inside = not global_form and _is_internal(ctx.node, to)
        if i >= rig.max_sends_per_turn:
            path.unlink(missing_ok=True)
            violations += 1
            state.record_dead_letter(
                ctx.node, reason="quota", sender=sender_addr, to=to,
                thread=newest[0], content=body,
            )
            state.append_log(
                ctx.node,
                f"r4t: QUOTA {sender_addr} -> {to} "
                f"(> max_sends_per_turn {rig.max_sends_per_turn})",
            )
            continue

        # `tell` inside the cage writes staging and validates nothing — this
        # loop is the router. A bare name (no `node:` prefix) that matched no
        # roster member is a misaddressed delegation far more often than a real
        # outside agent, and the egress gate below would swallow it into the
        # lead's queue without a word. Name it in the log; routing is unchanged,
        # and a genuinely external recipient is still rejected by a8s. The top
        # leader's bare external recipient is the garden's sanctioned voice, not
        # a typo — exclude it so legitimate egress isn't tagged anomalous, and
        # exclude `:name` too, which is a sender saying outside on purpose.
        if not is_top and not inside and not global_form and ":" not in to:
            names = ", ".join(_dispatchable_names(roster)) or "(none)"
            state.append_log(
                ctx.node,
                f"r4t: UNKNOWN-MEMBER {sender_addr} -> {to} names no roster "
                f"member; routing as external (members: {names})",
            )

        # Egress gate: the org presents as a single a8s node, and only
        # the topmost leader may originate external mail. A non-top member's
        # external tell redirects to the top leader (the garden's voice),
        # regardless of comms mode. When egress is disabled, not even the top
        # leader may message out — its external tell dead-letters with an audit
        # note; a non-top member's still redirects up.
        redirected_to_top = False
        if not inside and top_leader is not None:
            if is_top and not ctx.egress:
                path.unlink(missing_ok=True)
                violations += 1
                state.record_dead_letter(
                    ctx.node, reason="egress-disabled", sender=sender_addr, to=to,
                    thread=newest[0], content=body,
                )
                state.append_log(
                    ctx.node,
                    f"r4t: EGRESS-BLOCKED {sender_addr} -> {to} "
                    "(egress disabled; the org does not message outside)",
                )
                continue
            if not is_top:
                to = _canonical_recipient(ctx.node, roster, top_leader.name)
                envelope["to"] = to
                redirected_to_top = True
                inside = True
                state.append_log(
                    ctx.node,
                    f"r4t: EGRESS-REDIRECT {sender_addr} -> external redirected "
                    f"to top leader {top_leader.name.lower()}",
                )

        # Hard tree enforcement (comms=closed): an intra-roster tell to a member
        # who is not tree-adjacent (and did not message the sender this turn)
        # reroutes to the sender's lead. Batch senders are always reachable —
        # answering must never reroute. Unknown names fall through to the
        # normal unknown-recipient dead letter, not to the lead.
        if not redirected_to_top and reachable is not None and inside:
            target = _display_name(ctx.node, to).strip().lower()
            recipient = roster.find(target)
            if recipient is not None and target not in reachable:
                lead = (roster.find(member.lead) if member.lead else None) or roster.leader()
                if lead is not None and lead.name.lower() != member.name.lower():
                    original = recipient.name
                    body = f"[r4t rerouted: {member.name} -> {original}] {body}"
                    to = _canonical_recipient(ctx.node, roster, lead.name)
                    envelope["to"] = to
                    envelope["content"] = body
                    state.append_log(
                        ctx.node,
                        f"r4t: REROUTED {sender_addr} -> {original} "
                        f"(not tree-adjacent) redirected to lead {lead.name.lower()}",
                    )

        key = _display_name(ctx.node, to).strip().lower()
        thread_id, in_hop = consumed.get(key, newest)
        next_hop = in_hop + 1

        state.append_history(
            ctx.node,
            member.name,
            f"## {zoned_stamp()} to {_display_name(ctx.node, to)}\n\n"
            + (body if len(body) <= rig.history_body_max else body[:rig.history_body_max] + " [...]"),
            max_bytes=rig.history_max_bytes,
        )
        _release_one(
            ctx, outbox, staging, envelope, sender_addr, thread_id, next_hop,
            body, roster, config, inside=inside,
        )
        if ctx.leader_sees_lateral and inside:
            _copy_lateral_to_lead(ctx, roster, member, rig, to, body, thread_id)
        path.unlink(missing_ok=True)
        released += 1
    shutil.rmtree(staging, ignore_errors=True)
    return {"released": released, "violations": violations}


# ---------- the turn ----------

STDOUT_REPLY_MIN_CHARS = 80

_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?)")
_HARNESS_NOISE_RE = re.compile(
    r"^(?:"
    r">\s+\S+\s+·\s"          # opencode banner: "> build · qwen3.6:latest"
    r"|[→✱✳✻●⏺✓✔✖|]\s"  # tool-trace glyphs
    r"|Shell cwd was reset\b"
    r")"
)


def clean_transcript(output: str) -> str:
    """Reduce a harness transcript to what the model actually said: strip
    ANSI escapes, then drop harness chrome — the rig banner, tool-trace lines,
    cwd-reset notices. Heuristic by design."""
    text = _ANSI_RE.sub("", output)
    kept = [
        line for line in text.splitlines()
        if not _HARNESS_NOISE_RE.match(line.strip())
    ]
    return "\n".join(kept).strip()


def _stage_echo_reply(
    ctx: DispatchContext,
    member: Member,
    rig: Rig,
    to: str,
    output: str,
) -> None:
    """The echo rig's ONLY reply path: the turn's cleaned stdout becomes one
    envelope to the newest inbound sender, staged pre-release so every gate
    (quota, egress, attribution, hop) applies unchanged. Anything the model
    somehow staged is noise — an echo member was never offered `tell` — and is
    discarded. Output past `echo_max_chars` is truncated in the body with the
    full text riding the same envelope as a markdown attachment (the a8s
    bundle-dir mechanism `tell --attach` uses). Empty or chrome-only output
    stays silent, SILENT logged; so does an empty `to`, the turn that answered
    nobody but r4t."""
    staging = state.staging_dir(ctx.node, member.name)
    for stale in state.staged_envelopes(ctx.node, member.name):
        stale.unlink(missing_ok=True)
    if not to:
        _log_internal_only(ctx, member, rig, output)
        return
    reply = clean_transcript(output)
    if not reply:
        state.append_log(
            ctx.node,
            f"r4t: SILENT {member.name.lower()} (rig {rig.name}) exit 0 with "
            f"{len(output.strip())} bytes of stdout but nothing worth "
            "relaying survived transcript cleaning",
        )
        return
    msg_id = state.new_ulid()
    body = reply
    files: list[dict] = []
    if len(reply) > rig.echo_max_chars:
        name = "reply.md"
        bundle = staging / msg_id
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / name).write_text(reply + "\n", encoding="utf-8")
        body = (
            reply[: rig.echo_max_chars]
            + f"\n[truncated by r4t at {rig.echo_max_chars} chars — "
            f"full reply attached as {name}]"
        )
        files = [{"filename": name}]
    state.atomic_write_json(
        staging / f"{msg_id}.json",
        {"id": msg_id, "to": to, "content": body, "files": files},
    )
    state.append_log(
        ctx.node,
        f"r4t: ECHO-REPLY {member.name.lower()} (rig {rig.name}) "
        f"{len(reply)} bytes of cleaned stdout staged as the reply to {to}"
        + (" (full text attached)" if files else ""),
    )


def _log_internal_only(
    ctx: DispatchContext,
    member: Member,
    rig: Rig,
    output: str,
) -> None:
    state.append_log(
        ctx.node,
        f"r4t: SILENT {member.name.lower()} (rig {rig.name}) answered only "
        f"r4t-internal senders; its {len(output.strip())} bytes of stdout stay "
        "transcript, nothing staged",
    )


def _capture_turn(
    ctx: DispatchContext,
    member: Member,
    *,
    threads: list[str],
    exit_code: int,
    duration: float,
    timed_out: bool,
    rig_name: str,
    prompt: str,
    output: str,
    prompt_note: str = "",
) -> None:
    """Persist one turn's full assembled prompt and full raw harness output to
    agents/<member>/turns/. Wrapped so a write failure only warns — observability
    must never take down a turn. Captures every dispatched turn, timeouts
    included: an empty/partial output is exactly the evidence a hang needs."""
    stamp = state.turn_capture_stamp()
    meta = "\n".join(
        [
            f"- stamp: {stamp}",
            f"- local: {local_stamp(seconds=True)}",
            f"- threads: {', '.join(threads) or '(none)'}",
            f"- exit: {exit_code}",
            f"- duration_seconds: {duration:.2f}",
            f"- timed_out: {str(timed_out).lower()}",
            f"- rig: {rig_name}",
        ]
        + ([f"- prompt: {prompt_note}"] if prompt_note else [])
    )
    content = (
        f"# turn {stamp} ({member.name})\n\n{meta}\n\n"
        f"## Prompt\n\n{prompt}\n\n"
        f"## Output\n\n{output.strip() or '(no output)'}\n"
    )
    try:
        state.write_turn_capture(
            ctx.node, member.name, stamp, threads[0] if threads else "batch", content
        )
    except OSError as e:
        state.append_log(
            ctx.node,
            f"r4t: WARN turn capture for {member.name.lower()} failed: {e}",
        )


def _refound_turn(
    ctx: DispatchContext, member: Member, rig: Rig, batch: list[dict]
) -> bool:
    """True when this turn must found the member's conversation instead of
    continuing it — no continue argv, a read-your-state preamble on the prompt,
    and the embedded history kept (a cold CLI carries nothing).

    `Continue:` is the operator's opt-in, and it is not a blank cheque. Three
    things send a turn back to a cold start:

    - **the rig swapped CLIs.** The conversation is keyed on the CLI, so a rig
      that now drives a different one cannot resume it — the old CLI may be
      quota-dead, so no dump turn; state on disk is whatever the last flush or
      the member's own writing left. A swap that keeps the CLI key (model-only,
      launcher variant) keeps the conversation.
    - **the previous turn did not exit clean.** A crashed or timed-out turn
      leaves the CLI's conversation in a state r4t never saw the end of.
      Resuming into it carries that wreckage into every later turn, so the
      next turn founds fresh and reads its state off disk instead.
    - **the window elapsed.** `Continue: 15m` bounds how long a conversation
      may sit idle and still be resumed. `_flush_sweep` is the graceful path —
      it spends a dump turn writing state to disk, then retires — and this is
      the backstop for a roster whose idle pass has not run. The sweep's own
      dump turn is exempt: it is the turn that still needs the conversation.
    """
    if not member.continue_conversation:
        return False
    convo = state.read_conversation(ctx.node, member.name)
    if convo and not convo.get("retired") and convo.get("cli") != rig.cli:
        state.retire_conversation(ctx.node, member.name)
        state.append_log(
            ctx.node,
            f"r4t: CONTINUE-SWAP {member.name.lower()} (rig {rig.name}) "
            f"drives {rig.cli!r} but the conversation lives on "
            f"{convo.get('cli')!r} — retired; this turn refounds from "
            "state on disk",
        )
        convo = {}
    if not convo or convo.get("retired"):
        return True

    last_turn = state.read_meta(ctx.node, member.name).get("last_turn") or {}
    if last_turn.get("exit") not in (0, None) or last_turn.get("timed_out"):
        state.append_log(
            ctx.node,
            f"r4t: CONTINUE-DIRTY {member.name.lower()} (rig {rig.name}) "
            f"last turn exited {last_turn.get('exit')}"
            + (" (timed out)" if last_turn.get("timed_out") else "")
            + " — refounding rather than resuming a conversation r4t never "
            "saw finish",
        )
        return True

    if member.flush_seconds is not None and not _is_dump_batch(batch):
        last = _last_completed(ctx.node, member.name)
        if last is not None and time.time() - last >= member.flush_seconds:
            state.append_log(
                ctx.node,
                f"r4t: CONTINUE-STALE {member.name.lower()} (rig {rig.name}) "
                f"idle past its {member.flush_seconds:g}s window — refounding "
                "from state on disk",
            )
            return True
    return False


def _is_dump_batch(batch: list[dict]) -> bool:
    """The flush sweep's own dump turn, which must resume the conversation it
    is about to retire — that is the whole point of spending the turn."""
    return bool(batch) and all(env.get("dump") for env in batch)


def _log_cache_usage(
    ctx: DispatchContext,
    member: Member,
    rig: Rig,
    workdir: Path,
    continued: bool,
) -> None:
    """Record what the turn just did to the provider's cache.

    Reads are charged at a fraction of the input rate and writes at a premium,
    so the ratio between them is the whole economics of a continuing member.
    This is measurement, not gating: whether a member continues at all is the
    roster's `Continue:` flag, an explicit operator acceptance of the miss
    risk.

    The failure signature worth shouting about is a continued turn that read
    only a stable prefix while re-creating most of its own history — the
    process-boundary breakpoint miss that makes automatic continuation
    uneconomic. That gets a CACHE-MISS line with the same numbers.
    """
    convo = transcript.probe(rig.preset, workdir)
    if convo is None or not convo.measured:
        return
    missed = (
        continued
        and convo.cache_creation_tokens > 10_000
        and convo.cache_creation_tokens > convo.cache_read_tokens
    )
    state.append_log(
        ctx.node,
        f"r4t: {'CACHE-MISS' if missed else 'CACHE'} "
        f"{member.name.lower()} (rig {rig.name}) "
        f"read {convo.cache_read_tokens} wrote {convo.cache_creation_tokens} "
        f"(1h {convo.ephemeral_1h_tokens}, 5m {convo.ephemeral_5m_tokens}) — "
        f"context {convo.context_tokens} tokens in {_kb(convo.size_bytes)}"
        + (
            " — the continued conversation re-wrote itself instead of reading "
            "its cache"
            if missed
            else ""
        ),
    )


def _marshal_attachments(
    ctx: DispatchContext, member: Member, batch: list[dict]
) -> Path | None:
    """Copy a8s-attached files into a fresh delivered bundle for this member
    and rewrite the batch's ATTACHED FILE lines to the copies. a8s hands paths
    inside the router's own files dir — behind run_as/container that is another
    user's sealed home — so the bundle (asserted 2750 by run_harness, mounted
    read-only by the container wrapper) is the readable form. Returns the
    bundle, or None when the batch carries no attachments; only called when the
    org's isolation is active, so a bare org's prompts stay byte-identical.
    A source that cannot be read is logged and its line left untouched — the
    turn still runs and the member sees the original path."""
    if not any(
        line.startswith(ATTACHED_FILE_PREFIX)
        for env_msg in batch
        for line in str(env_msg.get("body", "")).splitlines()
    ):
        return None
    bundle = state.new_delivered_bundle(ctx.node, member.name)
    used: set[str] = set()
    for env_msg in batch:
        lines = str(env_msg.get("body", "")).splitlines()
        rewritten = False
        for i, line in enumerate(lines):
            if not line.startswith(ATTACHED_FILE_PREFIX):
                continue
            src = Path(line[len(ATTACHED_FILE_PREFIX):].strip())
            name = src.name
            n = 1
            while name in used:
                name = f"{n}-{src.name}"
                n += 1
            dest = bundle / name
            try:
                shutil.copyfile(src, dest)
                os.chmod(dest, 0o644)
            except OSError as e:
                state.append_log(
                    ctx.node,
                    f"r4t: ATTACH-SKIP {member.name.lower()} cannot copy {src} "
                    f"into the delivered bundle ({e}); the original path rides "
                    "the prompt and may be unreadable behind the boundary",
                )
                continue
            used.add(name)
            lines[i] = f"{ATTACHED_FILE_PREFIX}{dest}"
            rewritten = True
        if rewritten:
            env_msg["body"] = "\n".join(lines)
    return bundle


def _run_turn(
    ctx: DispatchContext,
    config: RigConfig,
    roster: Roster,
    member: Member,
    rig: Rig,
    run_fn,
) -> None:
    batch = state.claim_queue(ctx.node, member.name)
    if not batch:
        return
    newest_thread = str(batch[-1].get("thread", "")) or "?"
    newest_hop = int(batch[-1].get("hop", 0) or 0)
    newest_sender = str(batch[-1].get("from", "")) or f"{ctx.node}"
    # `r4t:<node>` is a synthetic sender — the dispatcher's own voice on dump,
    # error and review turns — not a mailbox, so no stdout-derived reply may be
    # addressed to it. The reply target is the newest sender that is a real one;
    # a batch carrying nothing but r4t's own prompts has no target at all.
    internal_sender = f"r4t:{ctx.node}"
    reply_target = next(
        (
            str(env_msg.get("from", "")) or f"{ctx.node}"
            for env_msg in reversed(batch)
            if str(env_msg.get("from", "")) != internal_sender
        ),
        "",
    )

    refound = _refound_turn(ctx, member, rig, batch)
    workdir = resolve_workdir(ctx, member)
    # The one fact the rest of the turn keys on: this turn really does run
    # inside the CLI's existing conversation. It drives the continue argv AND
    # the prompt (which then omits the history the CLI is already carrying), so
    # it is decided once, here, before the prompt is built.
    continuing = member.continue_conversation and rig.supports_continue and not refound

    variant = state.take_rotation(ctx.node, rig.name, rig.pool_size)
    staging = state.prepare_staging(ctx.node, member.name)
    state.write_turn(
        ctx.node,
        member.name,
        {
            "batch": len(batch),
            "threads": sorted({str(b.get("thread", "")) for b in batch}),
            "newest_sender": newest_sender,
            "rig": rig.name,
            "started": state.utc_now(),
        },
    )
    delivered = _marshal_attachments(ctx, member, batch) if ctx.isolation.active else None
    sections = prompt_sections(
        ctx, roster, member, batch, rig, continues=continuing, refound=refound
    )
    prompt = "\n".join(p for _label, parts in sections for p in parts)
    if rig.echo:
        prompt_path = "echo"
    elif continuing:
        prompt_path = "continue"
    elif refound:
        prompt_path = "refound"
    else:
        prompt_path = "founding"
    stats = prompt_stats(sections)
    prompt_total = len(prompt.encode("utf-8"))

    env = dict(os.environ)
    env["TELL_OUTBOX_DIR"] = str(staging)
    if delivered is not None:
        env["R4T_DELIVERED_DIR"] = str(delivered)
    env["R4T_LIVE_LOG"] = str(state.reset_live_log(ctx.node, member.name))
    # Carried so run_harness can name a container's container deterministically
    # (r4t-<node>-<member>-<ts>) without widening the run_fn contract.
    env["R4T_NODE"] = ctx.node
    env["R4T_MEMBER"] = member.name
    # `Continue: on` — the member's own CLI conversation carries its recent work
    # forward and the provider cache prices the wake as a continuation rather
    # than a fresh full prompt.
    if continuing:
        env["R4T_CONTINUE"] = "1"
    # The org's OS-level boundary (org.py) rides in the same way — one setting
    # wraps every member turn regardless of rig (machinery outside, hands inside).
    env.update(ctx.isolation.to_env())
    # agy takes MCP config only from the member's own `~/.gemini`, and the
    # roster is the only place a second agy member sharing that home shows up.
    if rig.mcp_idiom == AGY_HOME_IDIOM:
        peers = mcp_home_peers(roster, config, member)
        if peers:
            env[ENV_MCP_HOME_PEERS] = ",".join(peers)
    state.append_log(
        ctx.node,
        f"## {zoned_stamp()} dispatch {len(batch)} message(s) -> {member.name} "
        f"(threads {', '.join(sorted({str(b.get('thread', '')) for b in batch}))}, "
        f"rig {rig.name}"
        + (f" variant {variant}" if rig.pool_size > 1 else "")
        + f")\n\n### Prompt\n\n{prompt}",
    )
    state.append_log(
        ctx.node,
        f"r4t: PROMPT {member.name.lower()} {prompt_path} {_kb(prompt_total)} — "
        + " ".join(f"{label} {_kb(size)}" for label, size in stats),
    )
    ctx.event(
        "TURN", member.name.lower(),
        f"{len(batch)} msg rig={rig.name} {prompt_path} prompt={_kb(prompt_total)}",
    )

    workdir.mkdir(parents=True, exist_ok=True)
    exit_code, output, duration, timed_out = run_fn(
        rig, prompt, workdir, env=env, variant=variant
    )

    # Some CLIs (cursor) refuse to launch at all when asked to continue a
    # conversation that does not exist yet — the state every continuing member
    # starts in. Retry that ONE failure cold: the turn founds the conversation
    # every later turn continues. Any other failure falls through to the normal
    # requeue-and-breaker path.
    if (
        "R4T_CONTINUE" in env
        and (timed_out or exit_code != 0)
        and rig.had_no_prior_conversation(output)
    ):
        state.append_log(
            ctx.node,
            f"r4t: CONTINUE-COLD {member.name.lower()} (rig {rig.name}) exit "
            f"{exit_code}: no conversation to continue in {workdir} — "
            "retrying once without it to found one",
        )
        del env["R4T_CONTINUE"]
        exit_code, output, duration, timed_out = run_fn(
            rig, prompt, workdir, env=env, variant=variant
        )

    outcome = f"exit {exit_code} in {duration:.1f}s"
    if timed_out:
        outcome += f" (killed at timeout {rig.timeout_seconds:g}s)"
    state.append_log(
        ctx.node,
        f"### Output ({member.name}, {outcome})\n\n{output.strip() or '(no output)'}",
    )
    # The CONTINUE-COLD retry deletes the marker, so this reads what actually
    # ran: only a turn that resumed a prior conversation can be a MISS.
    _log_cache_usage(ctx, member, rig, workdir, continued="R4T_CONTINUE" in env)

    _capture_turn(
        ctx,
        member,
        threads=sorted({str(b.get("thread", "")) for b in batch}),
        exit_code=exit_code,
        duration=duration,
        timed_out=timed_out,
        rig_name=rig.name,
        prompt=prompt,
        output=output,
        prompt_note=f"{prompt_path} {prompt_total} bytes — "
        + ", ".join(f"{label} {size}" for label, size in stats),
    )

    failed = timed_out or exit_code != 0
    if not failed and member.continue_conversation:
        # One recording path for every way a conversation comes to exist —
        # a refound, the CONTINUE-COLD founding retry, and a normal continue
        # all land here: the current CLI key, retired cleared.
        state.record_conversation(ctx.node, member.name, rig.cli)
    if failed:
        # A failed turn releases nothing and returns its whole batch to the
        # queue: the messages are never lost, and the breaker accumulates
        # against repeated failures until it trips and the queue simply holds.
        # The batch moves back from `.inflight/` under its original filenames,
        # so it keeps its ids and its place in arrival order rather than being
        # minted afresh behind whatever arrived while the turn ran.
        shutil.rmtree(staging, ignore_errors=True)
        returned = state.return_claim(ctx.node, member.name)
        state.append_log(
            ctx.node,
            f"r4t: RETRY {member.name.lower()} turn failed ({outcome}); "
            f"{returned} message(s) returned to the queue",
        )
    else:
        for env_msg in batch:
            entry_body = str(env_msg.get("body", ""))
            if len(entry_body) > rig.history_body_max:
                entry_body = entry_body[:rig.history_body_max] + " [...]"
            state.append_history(
                ctx.node,
                member.name,
                f"## {zoned_stamp()} from "
                f"{_display_name(ctx.node, str(env_msg.get('from', '?')))}\n\n{entry_body}",
                max_bytes=rig.history_max_bytes,
            )
        if rig.echo:
            _stage_echo_reply(ctx, member, rig, reply_target, output)
        elif not state.staged_envelopes(ctx.node, member.name):
            # The classic weak-rig shape: the model answers on stdout instead
            # of running `tell`. `tell` always wins — a turn that staged
            # anything keeps its stdout as transcript — but a clean turn that
            # released nothing gets its cleaned stdout staged as ONE reply to
            # the newest message's sender, riding the normal release gates.
            # `ProseReply: off` in the roster mutes that staging for a member
            # whose prose-only turns are noise, not answers; the quota signal
            # below still fires — a blank is a blank on any member.
            reply = clean_transcript(output)
            if len(reply) > STDOUT_REPLY_MIN_CHARS and not reply_target:
                _log_internal_only(ctx, member, rig, output)
            elif len(reply) > STDOUT_REPLY_MIN_CHARS and not member.prose_reply:
                state.append_log(
                    ctx.node,
                    f"r4t: SILENT {member.name.lower()} (rig {rig.name}) exit 0 "
                    f"with {len(reply)} bytes of stdout and no tell; the "
                    "stdout fallback is off for this member, nothing staged",
                )
            elif len(reply) > STDOUT_REPLY_MIN_CHARS:
                msg_id = state.new_ulid()
                state.atomic_write_json(
                    state.staging_dir(ctx.node, member.name) / f"{msg_id}.json",
                    {"id": msg_id, "to": reply_target, "content": reply, "files": []},
                )
                state.append_log(
                    ctx.node,
                    f"r4t: STDOUT-REPLY {member.name.lower()} (rig {rig.name}) "
                    f"released nothing; {len(reply)} bytes of cleaned stdout "
                    f"staged as a reply to {reply_target}",
                )
            elif not output.strip():
                # The blank-response quota signal (Neil's field observation):
                # an out-of-quota model on agy/claude/opencode exits 0 with a
                # BLANK — the only reliable cross-harness signal. Conservatively
                # we treat ONLY a truly empty transcript as quota-suspect, never
                # chrome-only output (a quiet-but-alive member still prints tool
                # traces). Draining the rig bucket rests the whole rig; queued
                # messages catch up once it refills — r4t is deliberately the
                # retry system, so a8s can stay dumb delivery.
                note = ""
                if rig.rig_budget_max is not None:
                    state.rig_budget_drain(rig.name)
                    note = (
                        f"; rig {rig.name} bucket drained to 0 — the rig rests "
                        "until it refills, then the queue catches up"
                    )
                state.append_log(
                    ctx.node,
                    f"r4t: QUOTA-SUSPECT {member.name.lower()} (rig {rig.name}) "
                    f"exit 0 with empty output{note}",
                )
            elif len(output.strip()) > STDOUT_REPLY_MIN_CHARS:
                state.append_log(
                    ctx.node,
                    f"r4t: SILENT {member.name.lower()} (rig {rig.name}) exit 0 "
                    f"with {len(output.strip())} bytes of stdout but nothing "
                    "worth relaying survived transcript cleaning",
                )
        release_staging(ctx, config, roster, member, rig, batch)
        # Last, so a kill anywhere above leaves the batch recoverable. Replaying
        # a turn is survivable; losing the mail that asked for it is not.
        state.release_claim(ctx.node, member.name)

    state.record_velocity(
        ctx.node,
        agent=member.name.lower(),
        rig=rig.name,
        thread=newest_thread,
        hop=newest_hop,
        duration_seconds=duration,
        exit_code=exit_code,
    )
    completed = state.utc_now()
    failures = int(
        state.read_meta(ctx.node, member.name).get("consecutive_failures", 0) or 0
    )
    failures = failures + 1 if failed else 0
    meta_fields = {
        "last_completed_at": completed,
        "consecutive_failures": failures,
        "last_turn": {
            "threads": sorted({str(b.get("thread", "")) for b in batch}),
            "messages": len(batch),
            "exit": exit_code,
            "timed_out": timed_out,
            "completed_at": completed,
        },
    }
    if failed:
        meta_fields["last_failure_at"] = completed
    state.update_meta(ctx.node, member.name, **meta_fields)
    state.clear_turn(ctx.node, member.name)
    ctx.event(
        "DONE", member.name.lower(),
        outcome + (f" — {len(batch)} msg requeued" if failed else ""),
    )
    structural = _structural_reason(exit_code, output)
    if structural:
        _park(ctx, member, rig, structural)
    elif failed and failures == config.breaker_cap:
        state.append_log(
            ctx.node,
            f"r4t: BREAKER {member.name.lower()} tripped ({failures} consecutive "
            f"failed turns, rig {rig.name}) — turns pause; one probe per "
            f"{config.breaker_cooldown_seconds:g}s until a turn succeeds",
        )


# ---------- park: the failures that will recur identically ----------
#
# The transient breaker probes forever, which is right for a timeout and wrong
# for a harness binary that is not installed. Nothing changes between probes, so
# every probe fails the same way and — before this — every failure told the
# sender about it: one tell in, an error every ten minutes out, forever. A
# structural failure parks the member on its FIRST occurrence instead. One
# ticker line, one day-log line, then silence. The queue holds untouched, which
# is what makes the silence safe, and only a probe that costs nothing —
# never a paid turn run to see what happens — brings the member back.

def _structural_reason(exit_code: int, output: str) -> str | None:
    """The one-line reason a turn failed structurally, or None when the failure
    may yet resolve itself. Only an exec that never started counts: a timeout, a
    nonzero exit with output, a network error and an exhausted quota can all
    look different on the next try."""
    if exit_code != 127:
        return None
    first = output.strip().splitlines()[0] if output.strip() else ""
    return first if first.startswith(SPAWN_FAILURE_PREFIX) else None


def _rig_probe(rig: Rig) -> str:
    """The command whose existence decides whether this rig can start — argv[0]
    of its first pool variant."""
    pool = rig.pool()
    return pool[0][0] if pool and pool[0] else ""


def _probe_resolves(command: str) -> str | None:
    """Where `command` resolves today, or None. `shutil.which` for a bare name,
    an executable-file test for a path — no subprocess, no tokens, no wake."""
    if not command:
        return None
    if os.sep in command or (os.altsep and os.altsep in command):
        path = Path(command).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(command)


def _park(ctx: DispatchContext, member: Member, rig: Rig, reason: str) -> None:
    probe = _rig_probe(rig)
    record = state.park_member(
        ctx.node, member.name, reason=reason, rig=rig.name, probe=probe
    )
    if not record:
        return
    depth = state.queue_depth(ctx.node, member.name)
    state.append_log(
        ctx.node,
        f"r4t: PARKED {member.name.lower()} (rig {rig.name}) {reason} — out of "
        f"the rotation with {depth} message(s) held; a free probe of {probe!r} "
        f"each idle wake un-parks it, or: r4t resume {member.name.lower()}",
    )
    ctx.event("PARKED", member.name.lower(), f"rig={rig.name} {reason}")


def _park_probe_sweep(ctx: DispatchContext, roster: Roster) -> list[str]:
    """Un-park every member whose structural precondition now holds. Free by
    construction, so it runs on every idle wake. Returns members resumed."""
    resumed: list[str] = []
    for member in roster.members:
        parked = state.read_parked(ctx.node, member.name)
        if not parked:
            continue
        probe = str(parked.get("probe", ""))
        where = _probe_resolves(probe)
        if where is None:
            continue
        state.unpark_member(ctx.node, member.name)
        depth = state.queue_depth(ctx.node, member.name)
        state.append_log(
            ctx.node,
            f"r4t: RESUME {member.name.lower()} — {probe!r} now resolves at "
            f"{where}; {depth} queued",
        )
        ctx.event("RESUME", member.name.lower(), f"{probe} -> {where}; {depth} queued")
        resumed.append(member.name)
    return resumed


def _run_member_turn(
    ctx: DispatchContext,
    config: RigConfig,
    roster: Roster,
    member: Member,
    rig: Rig,
    run_fn,
) -> str:
    if state.queue_depth(ctx.node, member.name) == 0:
        return SKIPPED
    runnable, reason = schedule.runnable(ctx.node, config, member, rig)
    if not runnable:
        if reason.startswith("parked"):
            # A parked member said its piece once, when it parked. Every
            # message after that enqueues in total silence — the silence is
            # the point, and `r4t status` is where it is paid for.
            return PARKED
        blocked = "BREAKER" if reason.startswith("breaker") else "RESTING"
        queued = f"({state.queue_depth(ctx.node, member.name)} queued)"
        state.append_log(
            ctx.node,
            f"r4t: {blocked} {member.name.lower()} — {reason} {queued}",
        )
        ctx.event(blocked, member.name.lower(), f"{reason} {queued}")
        return BREAKER if reason.startswith("breaker") else RESTING

    lock = state.AgentLock(ctx.node, member.name)
    admission = state.admission_lock(ctx.node)
    if not admission.acquire():
        return DEFERRED
    acquired = False
    try:
        # ONE TURN AT A TIME, and this is where it is true. The admission lock
        # makes the check-and-claim atomic across processes; the live member
        # locks are the answer. This is a contract, not a setting: there is no
        # number to raise, because raising it is what a second node is for.
        throttle_reason = _throttle_block(ctx, config)
        if throttle_reason is None:
            live = state.live_locks(ctx.node)
            if live:
                throttle_reason = (
                    f"one turn at a time: {live[0]['agent']} is already running"
                )
        if throttle_reason is None:
            acquired = lock.acquire(rig.name)
        if acquired:
            # Re-read budgets under the admission lock so simultaneous
            # admissions cannot both spend the last unit.
            runnable, reason = schedule.runnable(ctx.node, config, member, rig)
            if not runnable:
                lock.release()
                acquired = False
            else:
                state.budget_charge(
                    ctx.node, member.name, rig.budget_max, rig.budget_earn_per_hour
                )
                state.budget_charge(
                    ctx.node, state.CELL_BUDGET_KEY,
                    config.cell_budget_max, config.cell_budget_earn_per_hour,
                )
                if rig.rig_budget_max is not None:
                    state.rig_budget_charge(
                        rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour
                    )
                state.stamp_last_turn_start(ctx.node)
    finally:
        admission.release()

    if not acquired:
        if throttle_reason:
            queued = f"({state.queue_depth(ctx.node, member.name)} queued)"
            state.append_log(
                ctx.node,
                f"r4t: DEFERRED ({throttle_reason}) {member.name.lower()} {queued}",
            )
            ctx.event("DEFERRED", member.name.lower(), f"{throttle_reason} {queued}")
        return RESTING if not runnable else DEFERRED

    try:
        _run_turn(ctx, config, roster, member, rig, run_fn)
        return RAN
    finally:
        lock.release()


# ---------- dispatch entry points ----------

def handle_message(
    ctx: DispatchContext,
    sender: str,
    to: str,
    message: str,
    *,
    klass: str = "human",
    run_fn=run_harness,
    drain_after: bool = True,
) -> int:
    _ingest(
        ctx, sender, to, (message or "").strip(),
        klass=klass, internal=_is_internal(ctx.node, sender),
    )
    if drain_after:
        drain_until_quiet(ctx, run_fn=run_fn)
    return 0


def handle_batch(
    ctx: DispatchContext,
    raw_json: str,
    *,
    run_fn=run_harness,
    drain_after: bool = True,
) -> int:
    """Ingest a JSON array of a8s envelopes from one batch wake, then drain
    once. A malformed array is a hard error; per-entry failures (unreadable
    envelopes, dead-letters, skips) do not abort the rest."""
    try:
        entries = json.loads(raw_json)
    except (TypeError, ValueError):
        print("dispatch: --batch must be a JSON array", file=sys.stderr)
        return 2
    if not isinstance(entries, list):
        print("dispatch: --batch must be a JSON array", file=sys.stderr)
        return 2

    def unreadable(detail: str) -> None:
        state.record_dead_letter(
            ctx.node, reason="unreadable-envelope", sender="", to="",
            thread="", content=detail[:2000],
        )

    senders: set[str] = set()
    ingested = 0
    for entry in entries:
        if not isinstance(entry, dict):
            unreadable(repr(entry))
            continue
        if "_unreadable" in entry:
            unreadable(f"{entry.get('_unreadable', '')}: {entry.get('error', '')}")
            continue
        # The wire is a boundary: a8s copies `content` through verbatim and
        # never requires it to be a string, so one non-string field must
        # dead-letter its own envelope rather than take the batch down.
        fields = {k: entry.get(k) or "" for k in ("from", "to", "content")}
        if any(not isinstance(v, str) for v in fields.values()):
            unreadable(f"non-string envelope field: {entry!r}")
            continue
        sender = fields["from"]
        if sender:
            senders.add(sender)
        _ingest(
            ctx, sender, fields["to"], fields["content"].strip(),
            klass=class_from_meta(json.dumps(entry.get("meta") or {})),
            internal=_is_internal(ctx.node, sender),
        )
        ingested += 1

    # Logged before the drain: the turn's own lines belong after the arrival
    # that caused them, and a drain that dies must not take the record of
    # what arrived with it.
    state.append_log(
        ctx.node,
        f"r4t: BATCH ingested {ingested} of {len(entries)} message(s) from "
        f"{', '.join(sorted(senders)) or '(none)'}",
    )
    if drain_after:
        drain_until_quiet(ctx, run_fn=run_fn)
    return 0


def resting_note(ctx: DispatchContext, to: str) -> str | None:
    """A one-line note for the operator when a deliberate send lands on a
    resting member — sending is never blocked, but they should know the turn
    is waiting on the bucket. None when the recipient will run normally."""
    _, sub = split_recipient(to)
    try:
        roster = load_roster(ctx.roster_path, node=ctx.node)
        config = load_rig_config(ctx.config_path)
    except (RosterError, RigError):
        return None
    member = roster.find(sub) if sub else roster.leader()
    if member is None or member.errors:
        return None
    rig, _err, _pinned = config.rig_for(member)
    if rig is None:
        return None
    depth = state.queue_depth(ctx.node, member.name)
    if depth == 0:
        return None
    runnable, reason = schedule.runnable(ctx.node, config, member, rig)
    if runnable:
        return None
    return f"queued — {member.name} is {reason}"


# ---------- queue drain ----------

def drain(ctx: DispatchContext, *, run_fn=run_harness) -> int:
    """One pass over the members holding mail when the pass began: run their
    turns one at a time, re-asking who goes next after every one.

    Selection is `schedule.next_up` — the same call `r4t status` prints — so
    what the drain does and what status says will happen are one fact, not two.
    The rotation is recomputed between turns rather than fixed at the top of
    the pass, because a turn changes the answer: it spends a budget, empties a
    queue, and ages every member it went ahead of.

    The pass is still a pass: mail that arrives DURING it belongs to the next
    one, which `drain_until_quiet` starts immediately. Returns the number of
    turns that RAN. The agent lock is the only claim — two concurrent drainers
    race on it and exactly one runs a given member; the loser's message stays
    safely queued."""
    try:
        roster = load_roster(ctx.roster_path, node=ctx.node)
        config = load_rig_config(ctx.config_path)
    except (RosterError, RigError) as e:
        # A roster that will not load stops every queued turn at once, so say
        # so. Silence here reads as an empty queue and hides the real cause.
        state.append_log(ctx.node, f"r4t: DRAIN-SKIPPED {e}")
        return 0

    def look() -> list:
        return schedule.snapshot(
            ctx.node, config, roster, priority_senders=ctx.priority_senders
        )

    entries = look()
    cohort = {e.member.lower() for e in entries}
    _narrate_held(ctx, entries)
    ran = 0
    done: set[str] = set()
    while len(done) < len(cohort):
        entry = schedule.next_up(
            [e for e in entries if e.member.lower() in cohort], skip=done
        )
        if entry is None:
            break
        member = roster.find(entry.member)
        rig, _err, _pinned = config.rig_for(member)
        done.add(entry.member.lower())
        if _run_member_turn(ctx, config, roster, member, rig, run_fn) != RAN:
            # Ready a moment ago and not now: the cadence window closed, or a
            # concurrent drainer took the lock or the last budget unit.
            entries = look()
            continue
        schedule.record_selection(ctx.node, entries, entry.member)
        ran += 1
        entries = look()
    return ran


def _narrate_held(ctx: DispatchContext, entries: list) -> None:
    """Say why each member with mail is not running. The selection never hands
    a held member to `_run_member_turn`, so without this the queue would just
    sit there with nothing on the ticker to explain it. A parked member is the
    deliberate exception: it spoke once, when it parked."""
    for entry in entries:
        if entry.state in (schedule.READY, schedule.PARKED) or not entry.depth:
            continue
        name = entry.member.lower()
        blocked = "BREAKER" if entry.state == schedule.BREAKER else "RESTING"
        queued = f"({entry.depth} queued)"
        state.append_log(ctx.node, f"r4t: {blocked} {name} — {entry.reason} {queued}")
        ctx.event(blocked, name, f"{entry.reason} {queued}")


def _cadence_wait(ctx: DispatchContext) -> float:
    """Seconds until the cadence throttle admits another turn start (0 when the
    window is already open or the config is unreadable)."""
    try:
        config = load_rig_config(ctx.config_path)
    except RigError:
        return 0.0
    interval = config.throttle.min_seconds_between_turn_starts
    if interval <= 0:
        return 0.0
    last = state.read_last_turn_start(ctx.node)
    if last is None:
        return 0.0
    return max(0.0, interval - (time.time() - last))


def drain_until_quiet(ctx: DispatchContext, *, run_fn=run_harness) -> int:
    """Drain repeatedly until a pass runs nothing — a released intra-roster
    message enqueues the next member and can enable another turn in the same
    invocation. A pass that runs nothing while queued work remains and no turn
    is live means either the cadence window is the only thing in the way (sleep
    it out and retry) or every queued member is resting/broken (return; the
    queue holds until the bucket refills or the breaker closes)."""
    total = 0
    for _ in range(DRAIN_MAX_PASSES):
        ran = drain(ctx, run_fn=run_fn)
        total += ran
        if ran:
            continue
        if not state.members_with_queue(ctx.node) or state.live_locks(ctx.node):
            break
        wait = _cadence_wait(ctx)
        if wait <= 0:
            break
        time.sleep(wait + 0.05)
    return total


def run_clear(ctx: DispatchContext, *, run_fn=run_harness) -> dict:
    pruned = state.prune_stale_locks(ctx.node)
    recovered = _recover_inflight(ctx)
    drained = drain_until_quiet(ctx, run_fn=run_fn)
    days, months = _run_retention(ctx)
    return {
        "locks_pruned": pruned,
        "recovered": recovered,
        "drained": drained,
        "log_days_pruned": days,
        "velocity_months_rotated": months,
    }


def _run_retention(ctx: DispatchContext) -> tuple[list[str], list[str]]:
    """Bound the two files that grow per turn forever: drop day logs past
    `log_retention_days`, and rotate finished months out of velocity.csv.
    Both are announced in the log — nothing is silently dropped — and
    dead-letter records are deliberately untouched (they wait for a human).
    An unreadable rig config skips retention rather than guessing a policy
    that deletes."""
    try:
        retention = load_rig_config(ctx.config_path).log_retention_days
    except RigError:
        return [], []
    days = state.prune_day_logs(ctx.node, retention)
    if days:
        state.append_log(
            ctx.node,
            f"r4t: PRUNED {len(days)} day log(s) past {retention}-day retention "
            f"({days[0]}..{days[-1]})",
        )
    months = [] if state.live_locks(ctx.node) else state.rotate_velocity(ctx.node)
    if months:
        state.append_log(
            ctx.node,
            "r4t: ROTATED velocity rows for finished month(s) "
            f"{', '.join(months)} into velocity-<month>.csv",
        )
    return days, months


# ---------- idle conversation flush (retire, dump, refound) ----------
#
# A continuing conversation left idle eventually goes stale or falls out of
# the provider cache — re-caching a huge context at frontier prices costs real
# money for one message. `Continue: <duration>` bounds that: once the member's
# last turn is older than the duration, a DUMP TURN (a normal continuing turn
# whose prompt asks the member to write its state to disk) runs and the
# conversation is retired; the next real message refounds cold from that state.


def _last_completed(node: str, name: str) -> float | None:
    raw = str(state.read_meta(node, name).get("last_completed_at", ""))
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _flush_sweep(
    ctx: DispatchContext, config: RigConfig, roster: Roster, run_fn
) -> list[str]:
    """Retire idle continuing conversations. Budget-gated exactly like
    mission-review: no budget skips the member this sweep and retries when it
    refills. The dump turn is a real turn — logged, captured, counted — and
    retirement is marked only when it succeeds. Returns members retired."""
    flushed: list[str] = []
    now = time.time()
    for member in roster.members:
        if member.errors:
            continue
        if not member.continue_conversation or member.flush_seconds is None:
            continue
        convo = state.read_conversation(ctx.node, member.name)
        if not convo or convo.get("retired"):
            continue
        if state.queue_depth(ctx.node, member.name):
            continue  # queued work will run a real continuing turn anyway
        last = _last_completed(ctx.node, member.name)
        if last is None or now - last < member.flush_seconds:
            continue
        rig, _err, _pinned = config.rig_for(member)
        if rig is None:
            continue
        runnable, reason = schedule.runnable(ctx.node, config, member, rig)
        if not runnable:
            state.append_log(
                ctx.node,
                f"r4t: FLUSH deferred — {member.name.lower()} {reason}",
            )
            continue
        state.enqueue(
            ctx.node,
            member.name,
            {
                "from": f"r4t:{ctx.node}",
                "to": f"{ctx.node}:{member.name.lower()}",
                "thread": state.new_ulid(),
                "hop": 0,
                "class": "auto",
                "dump": True,
                "body": ctx.prompt("flush_dump"),
            },
        )
        state.append_log(
            ctx.node,
            f"r4t: FLUSH dump turn -> {member.name.lower()} (conversation idle "
            f"> {member.flush_seconds:g}s)",
        )
        if _run_member_turn(ctx, config, roster, member, rig, run_fn) != RAN:
            continue
        last_turn = state.read_meta(ctx.node, member.name).get("last_turn") or {}
        if last_turn.get("exit") == 0 and not last_turn.get("timed_out"):
            state.retire_conversation(ctx.node, member.name)
            state.append_log(
                ctx.node,
                f"r4t: FLUSH retired {member.name.lower()}'s conversation — "
                "the next real message refounds it from state on disk",
            )
            flushed.append(member.name)
    return flushed


# ---------- manual flush (the on-demand verb) ----------
#
# The sweep waits out the `Continue:` window; the verb runs on the operator's
# word, so it checks neither. It goes one step further than the sweep and
# archives the member's history log: the refound reads STATUS.md and nothing else,
# which is what makes a memory test provable and what cures a conversation
# whose recent transcript is the problem.


def _flush_member(
    ctx: DispatchContext,
    config: RigConfig,
    roster: Roster,
    member: Member,
    dump: bool,
    run_fn,
) -> dict:
    result = {
        "member": member.name,
        "dumped": False,
        "retired": False,
        "archived": None,
        "skipped": "",
        "failed": "",
    }
    if member.errors:
        result["skipped"] = member.error
        return result
    convo = state.read_conversation(ctx.node, member.name)
    live = bool(convo) and not convo.get("retired")
    if dump and live:
        rig, err, _pinned = config.rig_for(member)
        if rig is None:
            result["skipped"] = err
            return result
        runnable, reason = schedule.runnable(ctx.node, config, member, rig)
        if not runnable:
            state.append_log(
                ctx.node, f"r4t: FLUSH deferred — {member.name.lower()} {reason}"
            )
            result["failed"] = reason
            return result
        state.enqueue(
            ctx.node,
            member.name,
            {
                "from": f"r4t:{ctx.node}",
                "to": f"{ctx.node}:{member.name.lower()}",
                "thread": state.new_ulid(),
                "hop": 0,
                "class": "auto",
                "dump": True,
                "body": ctx.prompt("flush_dump"),
            },
        )
        state.append_log(
            ctx.node, f"r4t: FLUSH dump turn -> {member.name.lower()} (r4t flush)"
        )
        ran = _run_member_turn(ctx, config, roster, member, rig, run_fn)
        last_turn = state.read_meta(ctx.node, member.name).get("last_turn") or {}
        if ran != RAN or last_turn.get("exit") != 0 or last_turn.get("timed_out"):
            # Nothing was banked, so nothing is thrown away: the conversation
            # still holds the state the dump failed to write. --no-dump is the
            # way past a conversation that cannot dump at all.
            result["failed"] = "the dump turn did not complete"
            return result
        result["dumped"] = True
    if live:
        state.retire_conversation(ctx.node, member.name)
        state.append_log(
            ctx.node,
            f"r4t: FLUSH retired {member.name.lower()}'s conversation — "
            "the next real message refounds it from state on disk",
        )
        result["retired"] = True
    archived = state.archive_history(ctx.node, member.name)
    if archived is not None:
        state.append_log(
            ctx.node,
            f"r4t: FLUSH archived {member.name.lower()}'s history log as "
            f"{archived.name} — a fresh one starts at the next turn",
        )
        result["archived"] = archived
    return result


def run_flush(
    ctx: DispatchContext,
    config: RigConfig,
    roster: Roster,
    members: list[Member],
    *,
    dump: bool = True,
    run_fn=run_harness,
) -> list[dict]:
    """Flush each member on demand: dump turn, retire the conversation, archive
    the history log. `dump=False` skips the turn — a poisoned or quota-dead
    conversation must not be asked to write its state down. Returns one result
    per member, in the order asked."""
    return [
        _flush_member(ctx, config, roster, member, dump, run_fn) for member in members
    ]


# ---------- mission-review idle turn (the furnace burns on its own) ----------

MISSION_REVIEW_BACKOFF_BASE = 2
MISSION_REVIEW_BACKOFF_CAP = 32
MISSION_REVIEW_SILENT_CAP = 3
# The backoff ladder above counts IDLE WAKES, so shortening the wake interval
# would multiply the review rate without anyone editing a policy. A review is a
# real, paid leader turn, so the ladder is floored by wall time as well: however
# fast the wakes come, two reviews are never closer together than this.
MISSION_REVIEW_MIN_INTERVAL_SECONDS = 1800.0


def _newest_turn(ctx: DispatchContext, roster: Roster) -> str:
    """The newest turn-completion stamp on the roster ("" when nobody has run).
    Comparing it between idle passes is how a stall stays distinguishable from
    a lull: an ISO stamp sorts lexically, so max() is newest."""
    return max(
        (
            str(state.read_meta(ctx.node, m.name).get("last_completed_at", ""))
            for m in roster.members
        ),
        default="",
    )


def _mission_mtime(ctx: DispatchContext) -> float:
    """Whatever file states the mission — the runbook, or `MISSION.md`. Its
    mtime is what re-arms mission review, so editing the runbook's `## Mission`
    counts exactly as editing the old file did."""
    from runbook import mission_source

    try:
        return mission_source(ctx.root).stat().st_mtime
    except OSError:
        return 0.0


def _mission_review(
    ctx: DispatchContext,
    config: RigConfig,
    roster: Roster,
    drained: int,
    run_fn,
) -> dict:
    """When the org is structurally stalled — every queue empty, the drain ran
    nothing, no live turn, and no member has finished a turn since the last
    tick — hand the top leader a budget-gated mission-review turn so a
    done-looking-but-unmet mission does not sleep forever. r4t detects the
    STALL; the leader judges whether the mission is met (§5.3). A backoff
    widens the cadence (2->4->8... stalled ticks); K silent reviews (the leader
    stages nothing) go dormant until a real message or a MISSION.md change
    re-arms it (§5.6).

    The turn-completion stamp is what makes a stall durable rather than a
    property of one pass: work that flowed between two idle passes (a turn
    driven straight through `handle_message`, say) leaves no queue and no lock
    behind, and without a memory of it every quiet moment would read as a
    stall."""
    st = state.read_mission_review(ctx.node)
    newest_turn = _newest_turn(ctx, roster)
    stalled = (
        drained == 0
        and not state.members_with_queue(ctx.node)
        and not state.live_locks(ctx.node)
        and newest_turn == str(st.get("last_turn_seen", ""))
    )
    mtime = _mission_mtime(ctx)
    last_review = float(st.get("last_review_at", 0.0) or 0.0)
    if not stalled:
        # Real work is flowing — the furnace does not need a nudge; reset.
        state.write_mission_review(
            ctx.node,
            {"stalls": 0, "silent_reviews": 0, "dormant": False,
             "mission_mtime": mtime, "last_turn_seen": newest_turn,
             "last_review_at": last_review},
        )
        return {"fired": False}

    if st.get("dormant"):
        if mtime == st.get("mission_mtime"):
            return {"fired": False, "dormant": True}
        st = {"stalls": 0, "silent_reviews": 0, "dormant": False}  # MISSION changed -> re-arm

    stalls = int(st.get("stalls", 0)) + 1
    silent = int(st.get("silent_reviews", 0))
    threshold = min(MISSION_REVIEW_BACKOFF_BASE << silent, MISSION_REVIEW_BACKOFF_CAP)
    too_soon = time.time() - last_review < MISSION_REVIEW_MIN_INTERVAL_SECONDS
    if stalls < threshold or too_soon:
        state.write_mission_review(
            ctx.node,
            {"stalls": stalls, "silent_reviews": silent, "dormant": False,
             "mission_mtime": mtime, "last_turn_seen": newest_turn,
             "last_review_at": last_review},
        )
        return {"fired": False, "stalls": stalls}

    leader = roster.leader()
    if leader is None or leader.errors:
        return {"fired": False}
    rig, _err, _pinned = config.rig_for(leader)
    if rig is None:
        return {"fired": False}
    runnable, reason = schedule.runnable(ctx.node, config, leader, rig)
    if not runnable:
        # A broke leader is a non-issue by construction — hold the counter at the
        # threshold so the review fires the moment the bucket refills.
        state.write_mission_review(
            ctx.node,
            {"stalls": stalls, "silent_reviews": silent, "dormant": False,
             "mission_mtime": mtime, "last_review_at": last_review},
        )
        state.append_log(
            ctx.node,
            f"r4t: MISSION-REVIEW deferred — leader {leader.name.lower()} {reason}",
        )
        return {"fired": False, "resting": True}

    state.enqueue(
        ctx.node,
        leader.name,
        {
            "from": f"r4t:{ctx.node}",
            "to": f"{ctx.node}:{leader.name.lower()}",
            "thread": state.new_ulid(),
            "hop": 0,
            "class": "auto",
            "body": ctx.prompt("mission_review"),
        },
    )
    state.append_log(
        ctx.node,
        f"r4t: MISSION-REVIEW fired -> {leader.name.lower()} "
        f"(stall {stalls}, review {silent + 1})",
    )
    # Run just the leader's review turn to observe whether it delegates: a
    # productive review queues work; a silent one leaves the org still stalled
    # and widens the backoff toward dormancy.
    _run_member_turn(ctx, config, roster, leader, rig, run_fn)
    produced = bool(state.members_with_queue(ctx.node))
    if produced:
        silent = 0
        dormant = False
    else:
        silent += 1
        dormant = silent >= MISSION_REVIEW_SILENT_CAP
        if dormant:
            state.append_log(
                ctx.node,
                f"r4t: MISSION-REVIEW dormant after {silent} silent review(s) — "
                "leader judged the mission met; a real message or MISSION.md "
                "change re-arms it",
            )
    state.write_mission_review(
        ctx.node,
        {"stalls": 0, "silent_reviews": silent, "dormant": dormant,
         "mission_mtime": mtime, "last_turn_seen": _newest_turn(ctx, roster),
         "last_review_at": time.time()},
    )
    return {"fired": True, "leader": leader.name, "silent_reviews": silent, "dormant": dormant}


def _recover_inflight(ctx: DispatchContext) -> list[tuple[str, int]]:
    """Return every batch a killed turn left in `queue/.inflight/`. The live
    PID lock is the liveness test, so a turn genuinely running is left alone.
    First in the idle pass, because every step after it reads the queue and has
    to read a true one."""
    recovered = state.recover_inflight(ctx.node)
    for name, count in recovered:
        state.append_log(
            ctx.node,
            f"r4t: RECOVERED {name} — {count} message(s) from a turn that never "
            "finished went back to the queue",
        )
        ctx.event("RECOVERED", name, f"{count} msg from an unfinished turn")
    return recovered


def run_idle(ctx: DispatchContext, *, run_fn=run_harness) -> dict:
    """One idle pass, in a strict order, silent when nothing happened.

    1. Recover in-flight batches a killed turn left behind — everything below
       reads the queue, so the queue has to be true first.
    2. Prune stale locks.
    3. Probe parked members; a free probe that now resolves un-parks one and it
       rejoins the rotation with its whole queue.
    4. Drain: whatever arrived between wakes, was recovered in 1, or was
       released in 3.
    5. Dream, then — if the org is stalled — a budget-gated mission-review turn.
    6. Retire idle continuing conversations, last: a turn running after the
       sweep would re-record the conversation the sweep just retired.

    An idle wake that finds nothing prints nothing at all. At a one-minute
    cadence a heartbeat line would be over a thousand lines a day in the one
    stream the roster is meant to be watchable in."""
    try:
        roster = load_roster(ctx.roster_path, node=ctx.node)
        config = load_rig_config(ctx.config_path)
    except (RosterError, RigError) as e:
        state.append_log(ctx.node, f"r4t: IDLE-SKIPPED {e}")
        return {
            "drained": 0, "flushed": [], "dreamed": [], "recovered": [],
            "resumed": [], "mission_review": {"fired": False}, "error": str(e),
        }
    recovered = _recover_inflight(ctx)
    state.prune_stale_locks(ctx.node)
    resumed = _park_probe_sweep(ctx, roster)
    drained = drain_until_quiet(ctx, run_fn=run_fn)
    dreamed = knowledge.dream_sweep(ctx, roster, config)
    review = _mission_review(ctx, config, roster, drained, run_fn)
    if review.get("fired"):
        drained += drain_until_quiet(ctx, run_fn=run_fn)
    flushed = _flush_sweep(ctx, config, roster, run_fn)
    return {
        "drained": drained, "flushed": flushed, "dreamed": dreamed,
        "recovered": recovered, "resumed": resumed, "mission_review": review,
    }
