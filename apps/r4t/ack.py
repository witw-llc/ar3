"""close_without_reply — a member closes an obligation without sending anything.

"Silence is a valid answer" is member doctrine the task layer cannot hear: an
open thread never learns the difference between deliberate silence and a
dropped ball, so the quiet sweep must assume the worst forever and nudge. This
module gives that decision a machine-observable form — and gives the task
layer, not the model, the last word on it.

The shape is propose -> validate -> commit. A member PROPOSES by printing one
line per obligation in its turn output:

    close_without_reply <thread>

Everything after the thread id is the member's stated reason, kept as color.
The task layer then validates eligibility deterministically and only then
commits the ledger close. Four rules carry the safety, each one paid for by
the #59 experiments:

1. **Parse strictly, repair nothing.** One proposal per line, the verb echoed
   exactly, a thread-shaped token after it. A 4B-class member invented
   `CLOSE_WITHOUT_RETRY` and dropped two of three batch messages; a parser that
   guessed at near-misses would have closed a thread on that. A malformed verb
   is logged and rejected, and the failure mode of every rejection is one extra
   nudge — never a discarded answer. Prose that merely names the verb is not
   protocol at all, and neither is a verb line inside a fenced code block: a
   member asked to document the syntax must not close the thread it was asked
   on. Fences exempt a line from PARSING only — every protocol-shaped line is
   stripped from a delivered body wherever it sits, so no unbalanced fence can
   ship the verb to someone else's parser.

2. **Eligibility is an allow-list read off the ledger.** Only a
   machine-originated thread — a relay from another cluster's machinery, or one
   the dispatcher itself opened — may end in silence. Both facts are recorded
   on the ledger when the thread is opened, by the code that opens it, never
   re-derived later from a sender string an outside caller can choose. A thread
   a person or a peer member opened is owed an answer whatever its wording, so
   no phrasing can talk the gate into a close. Content disqualifiers (a direct
   question, a direct assignment, an operational error) then ride on top as
   overrides: a relay that still asks something stays ineligible.

3. **A close is committed only by the member that owes the creator.** Threads
   are shared down a delegation chain — an intra-roster tell inherits the
   inbound thread id — so a downstream member's close would otherwise flip the
   originator's ledger and erase an obligation nobody answered. A valid
   proposal from any other member is recorded on the task and the thread stays
   open.

4. **The model's reason is never trusted.** Stated reasons were ~72% accurate
   even on moves that were otherwise correct, collapsing toward
   `informational_only`. The layer re-derives its own reason from the ledger
   and records that; the stated one rides along as an audit note.

Scope is per-obligation — one thread this member owes this turn — never per
sender and never per turn: acking one FYI from a member must not suppress that
member's next direct question, nor the answer written for another thread in the
same batch.
"""
from __future__ import annotations

import re

import state
import tasks

VERB = "close_without_reply"

# The task layer's own reasons. Derived from the ledger, so the vocabulary is
# exactly what the ledger can answer for.
REASON_AUTOMATED = "automated_notification"
REASON_INFORMATIONAL = "informational_only"

_THREAD_RE = re.compile(r"^[0-9A-Z]{26}$")
# Near-miss verbs are matched only so the miss can be LOGGED. Nothing in this
# family is ever repaired into a proposal.
_FAMILY_RE = re.compile(r"close[_-]?without", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

_QUESTION_RE = re.compile(r"\?")
_ASSIGNMENT_RE = re.compile(
    r"\b(?:please|can you|could you|would you|need you to|"
    r"you (?:must|should|need to)|make sure|take care of|"
    r"reply (?:to|with)|respond to|report back|let me know|get back to me|"
    r"update me|send me|confirm|approve|sign off|deadline|due by|asap)\b",
    re.IGNORECASE,
)

MAX_LOG_LINE = 120
MAX_STATED = 80


PROPOSAL = "proposal"
MALFORMED = "malformed"


def _scan(output: str):
    """(line, in_fence) for every line of a turn's output, for PARSING only —
    stripping reads the raw lines. A fenced block is quotation, not protocol: a
    member showing the syntax it was asked about must not thereby close the
    thread it was asked on. The fence delimiters themselves are never protocol
    either."""
    in_fence = False
    for line in output.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            yield line, True
            continue
        yield line, in_fence


def _classify(line: str) -> tuple[str | None, str, str]:
    """(kind, thread, stated) for one line: a PROPOSAL, a MALFORMED protocol
    attempt, or None for prose. A corrupted verb counts as an attempt only when
    a thread-shaped token follows it — otherwise `Close_without_reply ends an
    obligation...` in a sentence would be stripped out of a member's answer."""
    head, _, rest = line.strip().partition(" ")
    if not head:
        return None, "", ""
    thread, _, stated = rest.strip().partition(" ")
    thread = thread.strip().upper()
    shaped = bool(_THREAD_RE.match(thread))
    if head == VERB:
        return (PROPOSAL, thread, stated.strip()) if shaped else (MALFORMED, "", "")
    if shaped and _FAMILY_RE.search(head):
        return MALFORMED, "", ""
    return None, "", ""


def is_protocol_line(line: str) -> bool:
    """True for a line the member meant as a proposal — the verb, or a
    corruption of it naming a thread. Both are protocol, not prose, so neither
    may survive into a reply body."""
    return _classify(line)[0] is not None


def strip_proposals(output: str) -> str:
    """The turn's output with every protocol line removed — what the stdout
    fallback reads when a proposal was rejected and the member's prose still
    has to reach someone.

    Stripping ignores fences on purpose, though parsing honors them. A fence is
    a claim the model makes about its own output, and an unbalanced one is a
    claim it got wrong: honoring it here would let `close_without_reply <thread>`
    ride out in a delivered body, into the prompt of whichever member reads that
    message next. Quotation that cannot be delivered intact costs one garbled
    syntax example; a leaked verb line costs a thread nobody meant to close."""
    return "\n".join(
        line for line in output.splitlines() if not is_protocol_line(line)
    )


def parse(output: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(proposals, malformed) from a turn's raw output, where a proposal is
    (thread, stated reason) and a malformed entry is the offending line."""
    proposals: list[tuple[str, str]] = []
    malformed: list[str] = []
    for line, in_fence in _scan(output):
        if in_fence:
            continue
        kind, thread, stated = _classify(line)
        if kind == PROPOSAL:
            proposals.append((thread, stated))
        elif kind == MALFORMED:
            malformed.append(line.strip()[:MAX_LOG_LINE])
    return proposals, malformed


def disqualifier(messages: list[dict]) -> str | None:
    """Why an otherwise eligible obligation may NOT be closed silently, or
    None — the content overrides that ride on top of the allow-list. Structural
    and deterministic: the check reads the inbound messages, never the member's
    account of them."""
    for env in messages:
        if str(env.get("class", "")) == "error":
            return "an operational-error message is on the thread"
        body = str(env.get("body", ""))
        if _QUESTION_RE.search(body):
            return "the thread carries a direct question"
        if _ASSIGNMENT_RE.search(body):
            return "the thread carries a direct assignment"
    return None


def machine_originated(task: dict) -> bool:
    """The allow-list, read off two flags the ledger was born with. A relay
    thread's originator is another cluster's machinery (#167) and a thread the
    dispatcher itself opened has no one waiting on prose — the #58 filedrop
    shape, where a reply is only one more inbound somebody has to class. Every
    other thread was opened by a person or by a peer member and is owed an
    answer, however it was worded.

    Both flags are stamped at `ensure_task` time by the code that opens the
    thread. Neither is re-derived from the creator string: `r4t dispatch --from`
    takes any sender a caller cares to type, so a creator that merely LOOKS like
    the dispatcher's voice proves nothing (#83)."""
    return bool(task.get("relay")) or task.get("origin") == tasks.ORIGIN_DISPATCHER


def derive_reason(task: dict) -> str:
    """The reason the task layer will stand behind — and, since only
    `REASON_AUTOMATED` is eligible, the classification the allow-list gates on.
    A thread that reads `informational_only` is one the ledger cannot vouch for
    as machine-originated, which is exactly what makes it ineligible."""
    return REASON_AUTOMATED if machine_originated(task) else REASON_INFORMATIONAL


def owes_creator(node: str, roster, task: dict, messages: list[dict]) -> bool:
    """True when this turn's member is the one that owes the thread's creator —
    it received the thread's message from the creator itself, not from a member
    that forwarded it down the tree. Same predicate the answer-the-originator
    close uses (`dispatch._same_recipient`), so a chain that shares one thread
    id still has exactly one member able to end it.

    No roster means the predicate cannot be evaluated, and an unevaluable safety
    check denies: a close that never happens costs one nudge."""
    if roster is None:
        return False
    # Local import: dispatch imports this module, so the address-canonicalizing
    # predicate can only be borrowed at call time.
    from dispatch import _same_recipient

    creator = str(task.get("creator", ""))
    return any(
        _same_recipient(node, roster, str(env.get("from", "")), creator)
        for env in messages
    )


def _reject(ctx, member, thread: str, why: str) -> None:
    state.append_log(
        ctx.node,
        f"r4t: ACK-REJECT {member.name.lower()} thread={thread} {why}",
    )


def run(ctx, member, rig, batch: list[dict], output: str, roster) -> list[str]:
    """Harvest, validate and commit this turn's proposals. Returns the threads
    actually closed — empty whenever anything at all was off, which is the
    designed failure direction: a missed close costs one nudge, a wrong one
    silently discards an answer someone is waiting for. `roster` is required
    because it is what makes the commit per-obligation: a safety property that
    can be switched off by omitting an argument is a safety property waiting to
    be omitted."""
    if rig.echo:
        return []
    proposals, malformed = parse(output)
    for line in malformed:
        state.append_log(
            ctx.node,
            f"r4t: ACK-REJECT {member.name.lower()} malformed proposal "
            f"{line!r} — the verb must be echoed exactly as {VERB} <thread>",
        )
    if not proposals:
        return []
    if not member.ack:
        for thread, _stated in proposals:
            _reject(ctx, member, thread, "Ack is off for this member")
        return []

    by_thread: dict[str, list[dict]] = {}
    for env in batch:
        by_thread.setdefault(str(env.get("thread", "")), []).append(env)

    closed: list[str] = []
    for thread, stated in proposals:
        messages = by_thread.get(thread)
        if not messages:
            _reject(ctx, member, thread, "no such obligation in this turn's batch")
            continue
        task = tasks.load_task(ctx.node, thread)
        if task is None:
            _reject(ctx, member, thread, "no ledger for this thread")
            continue
        if task.get("status") != tasks.STATUS_OPEN or task.get("answered"):
            _reject(ctx, member, thread, "the thread is already closed")
            continue
        reason = derive_reason(task)
        if reason != REASON_AUTOMATED:
            _reject(
                ctx, member, thread,
                f"not-machine-originated: opened by {task.get('creator', '?')} "
                "— only a relay or a thread r4t opened may close without a reply",
            )
            continue
        why = disqualifier(messages)
        if why is not None:
            _reject(
                ctx, member, thread,
                f"content-override: {why} — it is owed an answer",
            )
            continue
        if not owes_creator(ctx.node, roster, task, messages):
            tasks.note_ack(
                ctx.node, thread, member=member.name.lower(),
                reason=reason, stated=stated,
            )
            state.append_log(
                ctx.node,
                f"r4t: ACK-NOTED thread={thread} {member.name.lower()} proposed a "
                f"close on a thread it does not owe {task.get('creator', '?')} "
                "— noted, the obligation stays open",
            )
            continue
        tasks.close_without_reply(
            ctx.node, thread, member=member.name.lower(),
            reason=reason, stated=stated,
        )
        state.append_log(
            ctx.node,
            f"r4t: ACK thread={thread} {member.name.lower()} reason={reason} "
            "(closed without a reply)"
            + (f' stated="{stated[:MAX_STATED]}"' if stated else ""),
        )
        closed.append(thread)
    return closed
