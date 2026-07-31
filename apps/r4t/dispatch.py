"""Dispatch — enqueue delivered mail, then drain member queues as batch turns.

Every inbound message to a member ENQUEUES, unconditionally, into that
member's durable queue (state.enqueue). No gate ever drops or dead-letters a
deliverable message; dead letters are for undeliverable mail only (unknown
recipient, disabled member, no rig). External mail always enters at the top:
the topmost leader IS the garden from outside, so outside senders cannot pick
a member — except the roster human's own Address, whose doorbell reply lands
in the seat path as the human speaking. A separate drain loop picks a runnable
member with a non-empty queue and runs ONE turn that drains the WHOLE queue:
the prompt renders every queued message at once, so an agent that sees
"members discussed X, then the lead overrode with Y" pivots in one reading
instead of burning a turn per message.

Runnability is governed autonomously — no human gates. A member runs when its
own spend bucket and the shared cell bucket both hold at least 1 unit (a turn
costs 1 of each), its failure breaker is closed, and the roster throttle
(max_concurrent, cadence) admits another start. An empty bucket means the
member is *resting*: its queue holds and it runs again when the bucket
refills. Nothing is lost.

The agent replies with the unmodified `tell`. Dispatch points the harness
subprocess's $TELL_OUTBOX_DIR at a per-turn staging dir and reads the staged
files as r4t-message DRAFTS (`to` + `body` + optional `files`), then releases
them: attribution (only this turn wrote there), the thread/hop/class stamped as
structured fields, per-turn send quota, then either the node's real outbox
(external — converted to an a8s envelope at the wall) or straight onto the
recipient member's queue (intra-roster, no header, no round-trip). A reply is
attributed to the thread of the message it answers.

A turn can also end an obligation by sending nothing at all: it proposes
`close_without_reply <thread>` in its output and `ack.py` validates eligibility
before the ledger moves. That runs ahead of every reply path here, because a
thread the member deliberately closed must not then be answered by the stdout
fallback.

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
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import ack
import isolate
import knowledge
import state
import tasks
from rig import (
    McpPlan,
    RigConfig,
    RigError,
    Rig,
    apply_mcp,
    load_rig_config,
    resolve_agy_model,
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
# `prompts` object (#190). Substitution fields: {name}, {node}, {workplace},
# {creator}, {thread}. Structural section headers stay in code (not doctrine).
PROMPT_DEFAULTS: dict[str, str] = {
    "intro": (
        "You are {name}, a member of the {node} roster. Your working directory "
        "is {workplace} — that absolute path is your root. Every file you "
        "create or reference belongs under it unless a message tells you "
        "otherwise: write it under that absolute path rather than trusting a "
        "bare relative path to land there. If your tools advertise a different "
        "\"workspace root\" or \"project root\", ignore it for file placement — "
        "yours is the directory named above."
    ),
    "echo_intro": "You are {name}, a member of the {node} roster.",
    "mission_header": "## The mission (MISSION.md — outranks every other document)",
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
    # while the named one was called 20/20 (#310).
    "work_tell_mcp": (
        "- Send messages by calling the `a8s_tell` tool (call the tool — "
        "printing text sends nothing). Pass `recipient` (the name) and `body` "
        "(your message). The body is delivered byte-exact; there is no shell. "
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
    # Offered only to a member whose `Ack:` is on. Disqualifiers are phrased as
    # OVERRIDES rather than positive triggers: every false close the #59
    # experiments produced came from an obligation hidden under informational
    # framing. The task layer allows a close only on a machine-originated
    # thread, so the bullet says so — a proposal on any other thread is
    # rejected and costs a nudge.
    "work_close_without_reply": (
        "- An automated notification that asks nothing of you gets closed, not "
        "answered: print a line of its own reading exactly\n"
        "        close_without_reply <thread>\n"
        "    using that message's thread id from its header above, and send "
        "nothing. Only machine-sent mail can be closed this way — a person or "
        "a member is owed an answer. An assignment is still an assignment when "
        "it is framed as an FYI, and a question is still a question — answer "
        "those with a message instead."
    ),
    "work_body_only": (
        "- Your tell's body is the only thing the recipient sees — anything you "
        "write around it (framing, notes, your reasoning) is lost."
    ),
    "work_commit": "- Repo work is not done until it is committed.",
    "quiet_nudge": (
        "Thread {thread} has gone quiet and {creator} has not heard back. "
        "Reply to them with where things stand — what is done and what remains. "
        "You do not have to finish the work, just report current state."
    ),
    "history_in_harness": (
        "This turn continues the session you are already in — your earlier "
        "messages and replies are above in it, so they are not repeated here."
    ),
    "reinforce": "Reinforcement from your operator: {text}",
    "flush_dump": "Save your current state and progress to STATUS.md.",
    "refound_preamble": "Check your STATUS.md to refresh your memory.",
    "mission_review": (
        "The roster's queues are empty and no thread is open, but the mission "
        "may not be met. Review MISSION.md against where things stand and "
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
    doorbell_check: str | None = None
    isolation: isolate.Isolation = field(default_factory=isolate.Isolation)
    definition_path: Path | None = None

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
    `node:name`. Bare roster names — humans included — canonicalize to
    internal form; anything else (external addresses, unknown names) passes
    through untouched. Human members are internal on purpose: their mail parks
    in the node's seat mailbox, and `Address:` is only a doorbell copy when no
    seat session is attached. A human's `Address:` is another name for the
    human: a tell to it parks in the seat too, and — since doorbell replies
    enter as the human speaking — closes the human's threads."""
    t = to.strip()
    if ":" in t:
        prefix, _, sub = t.partition(":")
        if prefix.strip().lower() != node.lower():
            return t
        name = sub
    else:
        name = t
    member = roster.find(name) or _human_by_address(roster, name)
    if member is None:
        return t
    return f"{node}:{member.name.lower()}"


def _same_recipient(node: str, roster: Roster, a: str, b: str) -> bool:
    return (
        _canonical_recipient(node, roster, a).strip().lower()
        == _canonical_recipient(node, roster, b).strip().lower()
    )


def _human_member(node: str, roster: Roster, addr: str) -> Member | None:
    """The roster human that `addr` (canonical or bare) names, if any."""
    t = (addr or "").strip()
    if ":" in t:
        prefix, _, sub = t.partition(":")
        if prefix.strip().lower() != node.lower():
            return None
        t = sub
    member = roster.find(t)
    return member if member is not None and member.is_human else None


def _human_by_address(roster: Roster, sender: str) -> Member | None:
    """The roster human whose a8s `Address:` equals `sender` — the doorbell
    reply path. Their reply is the human speaking, not an outside agent, so
    ingress re-stamps it to the seat instead of routing it as external mail."""
    s = (sender or "").strip().lower()
    if not s:
        return None
    for m in roster.members:
        if m.is_human and m.address and m.address.strip().lower() == s:
            return m
    return None


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
    if member is None or member.is_human or member.errors:
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
    internal `class=error` r4t-message carrying the ORIGINATING thread id (#160):
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


DOORBELL_CHECK_TIMEOUT = 120


def _run_doorbell_check(ctx: DispatchContext, command: str) -> tuple[bool, str, list[str]]:
    """Run the org's `doorbell_check` before a ring. Returns
    (ring_ok, sender_reason, log_lines). A nonzero exit reports the check's
    first stdout line to the sender and its stderr to the node log; a timeout or
    exec failure fails CLOSED — a broken gate must never become a silently
    broken doorbell."""
    env = dict(os.environ, R4T_NODE=ctx.node)
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(ctx.workplace),
            capture_output=True,
            text=True,
            timeout=DOORBELL_CHECK_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "check did not complete", [
            f"check timed out after {DOORBELL_CHECK_TIMEOUT}s: {command}"
        ]
    except OSError as e:
        return False, "check did not complete", [f"check failed to run: {e}"]
    if proc.returncode == 0:
        return True, "", []
    stdout_lines = proc.stdout.strip().splitlines()
    reason = stdout_lines[0] if stdout_lines else "check failed"
    return False, reason, [l for l in proc.stderr.splitlines() if l.strip()]


def _park_seat(
    ctx: DispatchContext,
    member: Member,
    sender: str,
    body: str,
    *,
    thread: str | None = None,
    roster: Roster | None = None,
    files: list[dict] | None = None,
    bundle: Path | None = None,
) -> None:
    """Deliver a message to a roster human: park it in the node's seat
    mailbox (attachments copied into seat storage alongside it), and ring the
    `Address:` doorbell (a forwarded copy over a8s) only when no seat session
    is attached to read it live. The doorbell forward is body-only —
    attachments wait in the seat mailbox. When the org sets a
    `doorbell_check`, that command gates the ring — the message is always parked
    first (seat mail is never lost), and a failing gate suppresses only the
    ring and replies to the sender with an error."""
    state.park_seat_message(
        ctx.node, member.name, sender, body, files=files, bundle=bundle
    )
    if not (member.address and not state.seat_attached(ctx.node, member.name)):
        state.append_log(
            ctx.node, f"r4t: SEAT {member.name.lower()} <- {sender} (parked)"
        )
        return
    command = (ctx.doorbell_check or "").strip()
    if command:
        ring_ok, reason, log_lines = _run_doorbell_check(ctx, command)
        if not ring_ok:
            for line in log_lines:
                state.append_log(ctx.node, f"r4t: GATE {ctx.node} {line}")
            state.append_log(
                ctx.node,
                f"r4t: GATE {ctx.node} doorbell BLOCKED for "
                f"{member.name.lower()}: {reason}",
            )
            _tell_error(
                ctx, sender, f"seat unreachable: {reason}",
                thread=thread, roster=roster,
            )
            state.append_log(
                ctx.node,
                f"r4t: SEAT {member.name.lower()} <- {sender} "
                "(parked, doorbell blocked by gate)",
            )
            return
        state.append_log(ctx.node, f"r4t: GATE {ctx.node} passed")
    ctx.tell_fn(member.address, body)
    state.append_log(
        ctx.node,
        f"r4t: SEAT {member.name.lower()} <- {sender} "
        f"(parked, doorbell -> {member.address})",
    )


def _load_roster(ctx: DispatchContext, sender: str) -> Roster | None:
    try:
        return load_roster(ctx.roster_path)
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
    return [m.name for m in roster.members if not m.is_human and not m.errors]


def _member_lines(ctx: DispatchContext, roster: Roster, member: Member) -> list[str]:
    # Information hiding: when the roster declares a tree, a member sees only
    # its tree-adjacent names (lead, reports, cell-mates) plus the human seat —
    # lateral contact becomes informationally unthinkable, not just rerouted.
    # A flat roster (no Lead lines) still lists the whole roster, as before.
    if roster.declares_tree:
        pool = roster.adjacent(member)
    else:
        pool = [m for m in roster.members if m.name.lower() != member.name.lower()]
    lines: list[str] = []
    for m in pool:
        if m.is_human:
            lines.append(
                f"    - {m.name} (Human, tell {m.name.lower()}) — {m.role}".rstrip(" —")
            )
        elif not m.errors:
            lines.append(
                f"    - {m.name} (tell {m.name.lower()}) — {m.role}".rstrip(" —")
            )
    return lines


def _mission_section(ctx: DispatchContext, roster: Roster, member: Member) -> list[str]:
    """MISSION.md is injected verbatim into a lead's turn prompt and no one
    else's. A member is a lead when it has direct reports (tree rosters); a
    flat roster with no tree declared treats the marked Leader as the only
    lead. ICs never see the file injected — their lead restates the intent at
    the resolution they can hold. Missing MISSION.md means no section, no error.
    """
    try:
        text = (ctx.root / "MISSION.md").read_text(encoding="utf-8").strip()
    except OSError:
        return []
    if not text:
        return []
    if roster.declares_tree:
        is_lead = bool(roster.reports_to(member))
    else:
        is_lead = member.leader and not member.is_human
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
    knowable only here, at build time, so this is where its shape is exposed
    (#39). Joining every section's parts with newlines yields the prompt
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
            ("intro", [ctx.prompt("echo_intro", name=member.name, node=ctx.node), ""]),
            ("mission", _mission_section(ctx, roster, member)),
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
            ),
            *workdir_lines,
            "",
        ]),
        ("mission", _mission_section(ctx, roster, member)),
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
            *([ctx.prompt("work_close_without_reply")] if member.ack else []),
            ctx.prompt("work_body_only"),
            ctx.prompt("work_commit"),
        ]),
        # Knowledge rides after the doctrine and before Reinforce, so the
        # closing line keeps its last-read primacy. Echo members never get it
        # (a reachability probe has no use for recall). Off (the default)
        # keeps the prompt byte-identical.
        ("knowledge", knowledge.knowledge_section(ctx, member, batch)),
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
    if rig.mcp_on and env is not None:
        mcp = apply_mcp(rig, argv, env, cwd, isolation)
        argv = mcp.argv

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
    # anchor them outside the member's workdir (#273).
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
    throttle = config.throttle
    if throttle.max_concurrent > 0:
        live = len(state.live_locks(ctx.node))
        if live >= throttle.max_concurrent:
            return (
                f"roster throttle: {live} live turn(s) >= max_concurrent "
                f"{throttle.max_concurrent}"
            )
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
    """The class an external peer stamped on the a8s envelope's `meta` (#167).

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
    dispatcher: bool = False,
) -> str:
    """Resolve the recipient and enqueue a structured r4t-message. Humans park
    in the seat; undeliverable mail dead-letters with an audit record; a
    deliverable message to an AI member enqueues unconditionally
    (duplicate-collapsed) and returns QUEUED. No text header is parsed or
    stamped — `thread`/`hop`/`class` travel as fields end to end.

    Routing turns on `internal`. Intra-roster and seat traffic honors
    `node:member` addressing — that is how the tree delivers between members and
    how the human seat reaches anyone — and carries the resolved `thread`/`hop`.
    External mail does NOT: the topmost leader IS the garden from outside, so
    every outside message enters at the top regardless of any sub-address and
    opens a fresh thread. The lone exception is the roster human's own
    `Address:` — their doorbell reply is the human speaking, re-stamped to the
    seat so it routes and closes threads exactly like a chat/seat send.

    `dispatcher` says r4t itself is opening this thread — the sweeps' own voice,
    and the ledger's trust anchor for that fact. Ingress never sets it, so no
    `--from` string can claim it (#83)."""
    if roster is None:
        roster = _load_roster(ctx, sender)
    if roster is None:
        return SKIPPED

    if internal:
        _, sub = split_recipient(to)
    else:
        human = _human_by_address(roster, sender)
        if human is not None:
            sender = f"{ctx.node}:{human.name.lower()}"
        to = ctx.node
        sub = ""
        thread = None  # external mail always opens a fresh thread

    if sub:
        member = roster.find(sub)
        if member is None:
            names = ", ".join(_dispatchable_names(roster)) or "(none)"
            _tell_error(
                ctx, sender,
                f"no roster member named {sub!r}. Dispatchable members: {names}.",
                thread=thread, roster=roster,
            )
            state.record_dead_letter(
                ctx.node, reason="unknown-recipient", sender=sender, to=to,
                thread=thread or "", content=body,
            )
            return DEAD
    else:
        member = roster.leader()
        if member is None:
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

    if member.is_human:
        _park_seat(
            ctx, member, sender, body, thread=thread, roster=roster,
            files=files, bundle=bundle,
        )
        return SKIPPED

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
        thread = tasks.new_thread_id()
        hop = 0
    tasks.ensure_task(
        ctx.node, thread, sender, relay=not internal and klass == "auto",
        dispatcher=dispatcher,
    )

    state.enqueue(
        ctx.node,
        member.name,
        {
            "from": sender,
            "to": _canonical_recipient(ctx.node, roster, to),
            "thread": thread,
            "hop": hop,
            "class": klass,
            "body": body,
        },
    )
    state.update_meta(ctx.node, member.name, last_inbound_at=state.utc_now())
    preview = " ".join(body.split())[:80]
    state.append_log(
        ctx.node,
        f"r4t: QUEUED {sender} -> {member.name.lower()} thread={thread} "
        f'hop={hop} "{preview}" (depth {state.queue_depth(ctx.node, member.name)})',
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
) -> None:
    to = str(envelope.get("to", "")).strip()
    if _is_internal(ctx.node, to):
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
            # A human recipient's park copied the files into seat storage;
            # AI-member queues still carry no attachments.
            if _human_member(ctx.node, roster, to) is None:
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
    msg_id = str(envelope.get("id", "")) or tasks.new_thread_id()
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
                temporary = outbox / f".{msg_id}.{tasks.new_thread_id()}.tmp"
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
    tree-adjacent members (lead, reports, cell-mates), every human seat, and
    whoever messaged it this turn (answering a batch sender never reroutes)."""
    names = {m.name.lower() for m in roster.adjacent(member)}
    for m in roster.members:
        if m.is_human:
            names.add(m.name.lower())
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
    """`leader_sees_lateral` (#185): land a read-only history copy of a lateral
    (peer) delivery on the sender's lead so the lead sees it on its next real
    turn — no turn is burned, and traffic UP to the lead is skipped (already
    visible)."""
    if not member.lead:
        return
    lead = roster.find(member.lead)
    if lead is None or lead.is_human or lead.errors:
        return
    recipient_name = _display_name(ctx.node, to).strip().lower()
    if recipient_name == lead.name.lower():
        return
    recipient = roster.find(recipient_name)
    if recipient is None or recipient.is_human:
        return
    clip = body if len(body) <= rig.history_body_max else body[:rig.history_body_max] + " [...]"
    state.append_history(
        ctx.node,
        lead.name,
        f"## {state.utc_now()} lateral {member.name} -> "
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
        newest = (tasks.new_thread_id(), 0)

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
        to = _canonical_recipient(ctx.node, roster, to)
        envelope["to"] = to
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
        # a typo — exclude it so legitimate egress isn't tagged anomalous.
        if not is_top and not _is_internal(ctx.node, to) and ":" not in to:
            names = ", ".join(_dispatchable_names(roster)) or "(none)"
            state.append_log(
                ctx.node,
                f"r4t: UNKNOWN-MEMBER {sender_addr} -> {to} names no roster "
                f"member; routing as external (members: {names})",
            )

        # Egress gate (#183): the org presents as a single a8s node, and only
        # the topmost leader may originate external mail. A non-top member's
        # external tell redirects to the top leader (the garden's voice),
        # regardless of comms mode. When egress is disabled, not even the top
        # leader may message out — its external tell dead-letters with an audit
        # note; a non-top member's still redirects up.
        redirected_to_top = False
        if not _is_internal(ctx.node, to) and top_leader is not None:
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
                state.append_log(
                    ctx.node,
                    f"r4t: EGRESS-REDIRECT {sender_addr} -> external redirected "
                    f"to top leader {top_leader.name.lower()}",
                )

        # Hard tree enforcement (comms=closed): an intra-roster tell to a member
        # who is not tree-adjacent (and did not message the sender this turn)
        # reroutes to the sender's lead. The human seat and batch senders are
        # always reachable — answering must never reroute. Unknown names fall
        # through to the normal unknown-recipient dead letter, not to the lead.
        if not redirected_to_top and reachable is not None and _is_internal(ctx.node, to):
            target = _display_name(ctx.node, to).strip().lower()
            recipient = roster.find(target)
            if (
                recipient is not None
                and not recipient.is_human
                and target not in reachable
            ):
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
            f"## {state.utc_now()} to {_display_name(ctx.node, to)}\n\n"
            + (body if len(body) <= rig.history_body_max else body[:rig.history_body_max] + " [...]"),
            max_bytes=rig.history_max_bytes,
        )
        _release_one(
            ctx, outbox, staging, envelope, sender_addr, thread_id, next_hop,
            body, roster, config,
        )
        if ctx.leader_sees_lateral and _is_internal(ctx.node, to):
            _copy_lateral_to_lead(ctx, roster, member, rig, to, body, thread_id)
        path.unlink(missing_ok=True)
        released += 1

        task = tasks.load_task(ctx.node, thread_id)
        if (
            task is not None
            and task.get("status") != tasks.STATUS_CLOSED
            and _same_recipient(ctx.node, roster, to, str(task.get("creator", "")))
        ):
            tasks.close_task(ctx.node, thread_id)
            state.append_log(
                ctx.node,
                f"r4t: ANSWERED thread={thread_id} {sender_addr} -> {to} "
                "(originator answered, thread closed)",
            )
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
    msg_id = tasks.new_thread_id()
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


def _refound_turn(ctx: DispatchContext, member: Member, rig: Rig) -> bool:
    """True when this turn must found the member's conversation instead of
    continuing it — no continue argv, a read-your-state preamble on the prompt,
    and the embedded history kept (a cold CLI carries nothing).

    Rig-swap retirement happens here: the conversation is keyed on the CLI, so
    a rig that now drives a different CLI cannot resume it — the old CLI may be
    quota-dead, so no dump turn; state on disk is whatever the last flush or
    the member's own writing left. A swap that keeps the CLI key (model-only,
    launcher variant) keeps the conversation."""
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
    return not convo or bool(convo.get("retired"))


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


def _reply_thread(batch: list[dict], reply_target: str) -> str:
    """The thread the stdout fallback would answer on — the newest message from
    the reply target. Which obligation the fallback belongs to is what makes
    ack suppression per-obligation instead of per-turn."""
    for env_msg in reversed(batch):
        if str(env_msg.get("from", "")) == reply_target:
            return str(env_msg.get("thread", ""))
    return ""


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

    refound = _refound_turn(ctx, member, rig)
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
    state.append_log(
        ctx.node,
        f"## {state.utc_now()} dispatch {len(batch)} message(s) -> {member.name} "
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

    workdir = resolve_workdir(ctx, member)
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
        shutil.rmtree(staging, ignore_errors=True)
        for env_msg in batch:
            state.enqueue(ctx.node, member.name, env_msg)
        state.append_log(
            ctx.node,
            f"r4t: RETRY {member.name.lower()} turn failed ({outcome}); "
            f"{len(batch)} message(s) returned to the queue",
        )
    else:
        for env_msg in batch:
            entry_body = str(env_msg.get("body", ""))
            if len(entry_body) > rig.history_body_max:
                entry_body = entry_body[:rig.history_body_max] + " [...]"
            state.append_history(
                ctx.node,
                member.name,
                f"## {state.utc_now()} from "
                f"{_display_name(ctx.node, str(env_msg.get('from', '?')))}\n\n{entry_body}",
                max_bytes=rig.history_max_bytes,
            )
        # Propose -> validate -> commit, ahead of every reply path: a thread
        # this turn deliberately closed must not then be answered by the
        # stdout fallback, which exists to rescue members that do not know the
        # protocol — and this one just spoke it. The suppression is per
        # obligation, not per turn: it applies only when the thread the
        # fallback would answer on is one of the threads just closed, so a
        # batch that closed an FYI and answered a different thread in prose
        # still delivers that answer.
        closed = ack.run(ctx, member, rig, batch, output, roster=roster)
        quiet = bool(closed) and _reply_thread(batch, reply_target) in closed
        if rig.echo:
            _stage_echo_reply(ctx, member, rig, reply_target, output)
        elif quiet and not state.staged_envelopes(ctx.node, member.name):
            state.append_log(
                ctx.node,
                f"r4t: ACK-QUIET {member.name.lower()} (rig {rig.name}) closed "
                f"{len(closed)} thread(s) without replying; its "
                f"{len(output.strip())} bytes of stdout stay transcript",
            )
        elif not state.staged_envelopes(ctx.node, member.name):
            # The classic weak-rig shape: the model answers on stdout instead
            # of running `tell`. `tell` always wins — a turn that staged
            # anything keeps its stdout as transcript — but a clean turn that
            # released nothing gets its cleaned stdout staged as ONE reply to
            # the newest message's sender, riding the normal release gates.
            # `Fallback: off` in the roster mutes that staging for a member
            # whose prose-only turns are noise, not answers; the quota signal
            # below still fires — a blank is a blank on any member.
            # A rejected proposal leaves its protocol line in the transcript;
            # the member's prose still deserves to reach whoever asked, the
            # line it fumbled does not.
            reply = clean_transcript(ack.strip_proposals(output))
            if len(reply) > STDOUT_REPLY_MIN_CHARS and not reply_target:
                _log_internal_only(ctx, member, rig, output)
            elif len(reply) > STDOUT_REPLY_MIN_CHARS and not member.fallback:
                state.append_log(
                    ctx.node,
                    f"r4t: SILENT {member.name.lower()} (rig {rig.name}) exit 0 "
                    f"with {len(reply)} bytes of stdout and no tell; the "
                    "stdout fallback is off for this member, nothing staged",
                )
            elif len(reply) > STDOUT_REPLY_MIN_CHARS:
                msg_id = tasks.new_thread_id()
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
    if failed and failures == config.breaker_cap:
        state.append_log(
            ctx.node,
            f"r4t: BREAKER {member.name.lower()} tripped ({failures} consecutive "
            f"failed turns, rig {rig.name}) — turns pause; one probe per "
            f"{config.breaker_cooldown_seconds:g}s until a turn succeeds",
        )
    if exit_code == 127 and reply_target:
        _tell_error(
            ctx, reply_target,
            f"{member.name}'s harness (rig {rig.name}) failed to start: "
            f"{output.strip()}",
            thread=newest_thread, roster=roster,
        )


def _runnable(
    ctx: DispatchContext, config: RigConfig, member: Member, rig: Rig
) -> tuple[bool, str]:
    """Can this member start a turn right now? Returns (runnable, reason).
    The queue and everything else is untouched either way."""
    blocked, failures = state.breaker_open(
        ctx.node, member.name, config.breaker_cap, config.breaker_cooldown_seconds
    )
    if blocked:
        return False, (
            f"breaker open ({failures} consecutive failed turns)"
        )
    m = state.budget_level(ctx.node, member.name, rig.budget_max, rig.budget_earn_per_hour)
    t = state.budget_level(
        ctx.node, state.CELL_BUDGET_KEY,
        config.cell_budget_max, config.cell_budget_earn_per_hour,
    )
    if m < 1.0:
        wait = state.budget_seconds_until(
            ctx.node, member.name, rig.budget_max, rig.budget_earn_per_hour
        )
        return False, f"resting (member budget {state.fmt_budget(m)}, ready in ~{wait / 60:.0f} min)"
    if t < 1.0:
        wait = state.budget_seconds_until(
            ctx.node, state.CELL_BUDGET_KEY,
            config.cell_budget_max, config.cell_budget_earn_per_hour,
        )
        return False, f"resting (cell budget {state.fmt_budget(t)}, ready in ~{wait / 60:.0f} min)"
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
    runnable, reason = _runnable(ctx, config, member, rig)
    if not runnable:
        state.append_log(
            ctx.node,
            f"r4t: {'BREAKER' if reason.startswith('breaker') else 'RESTING'} "
            f"{member.name.lower()} — {reason} "
            f"({state.queue_depth(ctx.node, member.name)} queued)",
        )
        return BREAKER if reason.startswith("breaker") else RESTING

    lock = state.AgentLock(ctx.node, member.name)
    admission = state.admission_lock(ctx.node)
    if not admission.acquire():
        return DEFERRED
    acquired = False
    try:
        throttle_reason = _throttle_block(ctx, config)
        if throttle_reason is None:
            acquired = lock.acquire(rig.name)
            if acquired and state.count_rig_locks(ctx.node, rig.name) > rig.concurrency:
                lock.release()
                acquired = False
        if acquired:
            # Re-read budgets under the admission lock so simultaneous
            # admissions cannot both spend the last unit.
            runnable, reason = _runnable(ctx, config, member, rig)
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
            state.append_log(
                ctx.node,
                f"r4t: DEFERRED ({throttle_reason}) {member.name.lower()} "
                f"({state.queue_depth(ctx.node, member.name)} queued)",
            )
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


def resting_note(ctx: DispatchContext, to: str) -> str | None:
    """A one-line note for the seat when a deliberate send lands on a resting
    member — the human is never blocked from sending, but should know the turn
    is waiting on the bucket. None when the recipient will run normally."""
    _, sub = split_recipient(to)
    try:
        roster = load_roster(ctx.roster_path)
        config = load_rig_config(ctx.config_path)
    except (RosterError, RigError):
        return None
    member = roster.find(sub) if sub else roster.leader()
    if member is None or member.is_human or member.errors:
        return None
    rig, _err, _pinned = config.rig_for(member)
    if rig is None:
        return None
    depth = state.queue_depth(ctx.node, member.name)
    if depth == 0:
        return None
    runnable, reason = _runnable(ctx, config, member, rig)
    if runnable:
        return None
    return f"queued — {member.name} is {reason}"


# ---------- queue drain ----------

def drain(ctx: DispatchContext, *, run_fn=run_harness) -> int:
    """One pass over every member with a non-empty queue: run a batch turn for
    each runnable one. Returns the number of turns that RAN. The agent lock is
    the only claim — two concurrent drainers race on it and exactly one runs a
    given member; the loser's message stays safely queued."""
    try:
        roster = load_roster(ctx.roster_path)
        config = load_rig_config(ctx.config_path)
    except (RosterError, RigError):
        return 0
    ran = 0
    for name in state.members_with_queue(ctx.node):
        member = roster.find(name)
        if member is None or member.is_human or member.errors:
            continue
        rig, _err, _pinned = config.rig_for(member)
        if rig is None:
            continue
        if _run_member_turn(ctx, config, roster, member, rig, run_fn) == RAN:
            ran += 1
    return ran


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


def run_clear(ctx: DispatchContext, older_than: float, *, run_fn=run_harness) -> dict:
    pruned = state.prune_stale_locks(ctx.node)
    expired = tasks.expire_tasks(ctx.node, older_than)
    drained = drain_until_quiet(ctx, run_fn=run_fn)
    days, months = _run_retention(ctx)
    return {
        "locks_pruned": pruned,
        "tasks_expired": expired,
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


# ---------- quiet-thread sweep (the termination backstop) ----------

def _quiet_task_sweep(
    ctx: DispatchContext, config: RigConfig, roster: Roster
) -> list[str]:
    """A thread can go quiet with its originator never having heard back — a
    member's turn succeeds while staging no reply, or a chain stalls. When an
    open thread with an unanswered originator sees no ledger activity for
    `quiet_task_seconds`, wake the leader with a nudge to report current state
    (NOT to force-finish the work). Returns the threads nudged.

    A relay thread is skipped: its originator is another cluster's machinery
    (#167), so a status report to it is not attention owed — it is one more
    inbound that peer must class and answer, which is how two rosters keep each
    other awake forever. The nudge exists for whoever is actually waiting."""
    if config.quiet_task_seconds <= 0:
        return []
    if state.live_locks(ctx.node):
        return []
    leader = roster.leader()
    if leader is None or leader.errors:
        return []
    cutoff = time.time() - config.quiet_task_seconds
    nudged: list[str] = []
    for task in tasks.list_tasks(ctx.node):
        if task.get("status") != tasks.STATUS_OPEN or task.get("answered"):
            continue
        if task.get("relay"):
            continue
        if tasks.last_activity(task) > cutoff:
            continue
        thread_id = str(task["id"])
        creator = str(task.get("creator", "?"))
        body = ctx.prompt("quiet_nudge", thread=thread_id, creator=creator)
        _ingest(
            ctx, f"r4t:{ctx.node}", f"{ctx.node}:{leader.name.lower()}", body,
            klass="auto", internal=True, thread=thread_id, hop=0,
            roster=roster, config=config, dispatcher=True,
        )
        tasks.save_task(ctx.node, task)  # bump updated_at; won't re-fire until quiet again
        state.append_log(
            ctx.node,
            f"r4t: QUIET thread={thread_id} quiet >{config.quiet_task_seconds:g}s "
            f"— nudged leader {leader.name.lower()} to update {creator}",
        )
        nudged.append(thread_id)
    return nudged


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
        if member.is_human or member.errors:
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
        runnable, reason = _runnable(ctx, config, member, rig)
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
                "thread": tasks.new_thread_id(),
                "hop": 0,
                "class": "auto",
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
    if member.is_human:
        result["skipped"] = "human member"
        return result
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
        runnable, reason = _runnable(ctx, config, member, rig)
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
                "thread": tasks.new_thread_id(),
                "hop": 0,
                "class": "auto",
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


def _has_open_threads(node: str) -> bool:
    return any(t.get("status") == tasks.STATUS_OPEN for t in tasks.list_tasks(node))


def _mission_mtime(ctx: DispatchContext) -> float:
    try:
        return (ctx.root / "MISSION.md").stat().st_mtime
    except OSError:
        return 0.0


def _mission_review(
    ctx: DispatchContext,
    config: RigConfig,
    roster: Roster,
    drained: int,
    run_fn,
) -> dict:
    """When the org is structurally stalled — every queue empty, no open thread,
    the drain ran nothing, no live turn — hand the top leader a budget-gated
    mission-review turn so a done-looking-but-unmet mission does not sleep
    forever (#189). r4t detects the STALL; the leader judges whether the mission
    is met (§5.3). A backoff widens the cadence (2->4->8... stalled ticks); K
    silent reviews (the leader stages nothing) go dormant until a real message
    or a MISSION.md change re-arms it. The nudge must not train leaders to
    doorbell the human every cycle (§5.6)."""
    stalled = (
        drained == 0
        and not state.members_with_queue(ctx.node)
        and not _has_open_threads(ctx.node)
        and not state.live_locks(ctx.node)
    )
    st = state.read_mission_review(ctx.node)
    mtime = _mission_mtime(ctx)
    if not stalled:
        # Real work is flowing — the furnace does not need a nudge; reset.
        if st.get("stalls") or st.get("silent_reviews") or st.get("dormant"):
            state.write_mission_review(
                ctx.node, {"stalls": 0, "silent_reviews": 0, "dormant": False, "mission_mtime": mtime}
            )
        return {"fired": False}

    if st.get("dormant"):
        if mtime == st.get("mission_mtime"):
            return {"fired": False, "dormant": True}
        st = {"stalls": 0, "silent_reviews": 0, "dormant": False}  # MISSION changed -> re-arm

    stalls = int(st.get("stalls", 0)) + 1
    silent = int(st.get("silent_reviews", 0))
    threshold = min(MISSION_REVIEW_BACKOFF_BASE << silent, MISSION_REVIEW_BACKOFF_CAP)
    if stalls < threshold:
        state.write_mission_review(
            ctx.node,
            {"stalls": stalls, "silent_reviews": silent, "dormant": False, "mission_mtime": mtime},
        )
        return {"fired": False, "stalls": stalls}

    leader = roster.leader()
    if leader is None or leader.errors:
        return {"fired": False}
    rig, _err, _pinned = config.rig_for(leader)
    if rig is None:
        return {"fired": False}
    runnable, reason = _runnable(ctx, config, leader, rig)
    if not runnable:
        # A broke leader is a non-issue by construction — hold the counter at the
        # threshold so the review fires the moment the bucket refills (#189).
        state.write_mission_review(
            ctx.node,
            {"stalls": stalls, "silent_reviews": silent, "dormant": False, "mission_mtime": mtime},
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
            "thread": tasks.new_thread_id(),
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
    # productive review opens threads / queues work; a silent one leaves the org
    # still stalled and widens the backoff toward dormancy.
    _run_member_turn(ctx, config, roster, leader, rig, run_fn)
    produced = bool(state.members_with_queue(ctx.node)) or _has_open_threads(ctx.node)
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
        {"stalls": 0, "silent_reviews": silent, "dormant": dormant, "mission_mtime": mtime},
    )
    return {"fired": True, "leader": leader.name, "silent_reviews": silent, "dormant": dormant}


def run_idle(ctx: DispatchContext, *, run_fn=run_harness) -> dict:
    """One idle pass: nudge the leader about quiet unanswered threads, drain
    every runnable member's queue, retire idle continuing conversations
    (_flush_sweep), then — if the org is structurally stalled —
    hand the top leader a budget-gated mission-review turn. Crash recovery needs
    no special path — a turn that never completed left its messages in the queue
    (they were claimed only at a turn that ran), so the next runnable turn picks
    them up."""
    try:
        roster = load_roster(ctx.roster_path)
        config = load_rig_config(ctx.config_path)
    except (RosterError, RigError) as e:
        state.append_log(ctx.node, f"r4t: IDLE-SKIPPED {e}")
        return {
            "quiet_nudged": [], "drained": 0, "flushed": [], "dreamed": [],
            "mission_review": {"fired": False}, "error": str(e),
        }
    nudged = _quiet_task_sweep(ctx, config, roster)
    drained = drain_until_quiet(ctx, run_fn=run_fn)
    flushed = _flush_sweep(ctx, config, roster, run_fn)
    dreamed = knowledge.dream_sweep(ctx, roster)
    review = _mission_review(ctx, config, roster, drained, run_fn)
    if review.get("fired"):
        drained += drain_until_quiet(ctx, run_fn=run_fn)
    return {
        "quiet_nudged": nudged, "drained": drained, "flushed": flushed,
        "dreamed": dreamed, "mission_review": review,
    }
