# Closing a thread without a reply

Silence is a valid answer, and the task layer cannot hear it. An open thread
has no way to tell deliberate silence from a dropped ball, so the
[quiet-thread sweep](r4t-governance.md) must assume the worst forever and keep
nudging. `close_without_reply` is the terminal disposition that ends that: a
member closes the obligation and sends nothing.

```
close_without_reply 01K3QSJ7Z0F9M4V2N8XBQC7RTD
```

One line in the turn's output, one obligation, zero egress. The thread's
ledger closes, the sweep stops seeing it, and no message reaches anyone.

## Propose, validate, commit

The member never closes anything. It **proposes**; the task layer **validates**
eligibility deterministically and only then **commits**. That split is not
caution for its own sake — no model class cleared the precision bar the #59
experiments set. A Sonnet-class member's one false close was a direct
assignment wrapped in FYI framing, which is the most expensive mistake
available: an owner's assignment silently discarded. A 4B-class member invented
the verb `CLOSE_WITHOUT_RETRY` and dropped two of three batch messages.

So the gate is machinery, not prompting:

| Stage | Who | Rule |
|---|---|---|
| Propose | member | one line per obligation, verb echoed exactly, thread named |
| Validate | task layer | knob, batch membership, open ledger, allow-list, overrides, responsible member |
| Commit | task layer | ledger closed, own reason recorded, `ACK` logged |

Every rejection fails in the same direction: nothing closes, the thread stays
open, and the sweep nudges once more. A missed close costs one nudge; a wrong
one discards an answer someone is waiting for.

## The emission contract

A proposal is a line whose first token is exactly `close_without_reply`,
followed by a thread id from a message header in the same prompt. Anything
after the thread is the member's stated reason and is kept as color only.

- **The verb is not repaired.** `CLOSE_WITHOUT_RETRY`, `close-without-reply`,
  a backticked verb, a capitalized one — each, named a thread, is logged as a
  malformed proposal and rejected. Guessing at a corrupted verb would mean
  closing a thread on a word the model never wrote.
- **The thread must be one this turn actually holds.** A proposal naming a
  thread outside the batch is rejected, so a member that hallucinates an id
  closes nothing.
- **One proposal per line.** A batch of three messages needs three lines;
  a member that emits one closes one and gets nudged about the rest.
- **Writing about the protocol is not speaking it.** A corrupted verb counts as
  an attempt only when a thread-shaped token follows it, so
  `Close_without_reply ends an obligation...` in a sentence is prose: nothing
  closes, nothing is logged, and the sentence reaches whoever asked. Lines
  inside a fenced code block are quotation and are skipped entirely — a member
  asked to document the syntax must not close the thread it was asked on.
- **Protocol lines never become message bodies.** Whether the proposal is
  accepted or rejected, its line is stripped from the prose the
  [stdout fallback](r4t-message-flow.md) stages. That stripping is the
  fallback's; the `echo` rig ships its raw transcript as always, because echo
  is a diagnostics rig whose whole job is showing exactly what a harness
  printed.

## Eligibility is an allow-list

Eligibility is read off the **ledger**, never off the wording of the messages
and never off the member's account of them. A thread may close in silence only
when it is structurally machine-originated:

- a **relay** thread — machine-classed external mail, another cluster's
  machinery or a filedrop node (#167, #58);
- a thread the **dispatcher** opened — creator `r4t:<node>`, r4t's own voice.

Nobody is waiting on prose in either case, and a reply would only be one more
inbound that peer has to class and answer. Every other thread was opened by a
person or by a peer member and is owed an answer, so **no phrasing can make it
eligible**. That is the lesson of the experiments' most expensive cell: a
keyword gate reads an assignment written in plain English — "the key rotates
tonight; swap it before the nightly run" — as an FYI, while the ledger already
knows the owner opened the thread.

On top of the allow-list, the content **overrides** still apply — a
machine-originated thread is ineligible when any message in it carries:

- a **direct question** — an interrogative the member did not answer;
- a **direct assignment** — a request, an approval, a report-back, a deadline;
- an **operational error** — a `class: error` message, an unresolved failure.

A relay that asks something is a relay that gets an answer. An assignment is
still an assignment when it is framed as an FYI, and the machinery treats it
that way regardless of how convincingly the model explains otherwise.

## Only the responsible member commits

A thread id travels down the delegation chain: an intra-roster tell inherits
the inbound thread, so one conversation label covers several obligations. The
member the creator is waiting on is the only one that can end it — the same
`answer-the-originator` predicate that decides when a reply closes a thread.

A valid proposal from any other member on the chain is **noted, not
committed**: it lands in the task's `ack_notes` and is logged as `ACK-NOTED`,
the ledger stays open, and the sweep keeps chasing the member that actually
owes the creator. Nothing about that member's own turn is suppressed either —
its stdout fallback still runs, because it did not close anything.

## The reason is the layer's own

Stated reasons were about 72% accurate even on closes that were otherwise
correct, collapsing toward "informational_only" whatever the message actually
was. So the task layer re-derives its own:

| Reason | When |
|---|---|
| `automated_notification` | a relay thread, or one the dispatcher opened — the eligible shape |
| `informational_only` | anything else, which is exactly what makes it ineligible |

The derived reason IS the allow-list: a thread that reads `informational_only`
is one the ledger cannot vouch for as machine-originated, so it is refused
rather than closed, and only `automated_notification` is ever recorded.

The ledger records that reason, the member, and the timestamp under the task's
`ack` key, plus the model's stated reason as an unverified note. `r4t task
<id>` shows the record; `r4t task trace <id>` shows the `ACK` event in the
thread's timeline.

## An ack is never prospective

A close ends the obligations the thread was carrying — not the ones it has not
carried yet. A new inbound on an ack-closed thread therefore **reopens** the
ledger: status back to open, the spent `ack` record moved into `ack_notes`, and
the sweep can see the thread again. Without that, one silent close would blind
the backstop to everything that thread ever carries afterwards. A thread closed
by a real answer stays closed; only an ack-closed one reopens.

## The knob

```markdown
### Wren
- **Rig:** claude
- **Ack:** off          # default on
```

`Ack:` is a per-member roster field, default **on**: the doctrine bullet rides
every wake prompt and proposals are honored. `Ack: off` drops the bullet and
rejects any proposal the member emits anyway (`ACK-REJECT` in the log) — for a
member whose silence you would rather see as an open thread. Any other value
disables the member with a roster error.

## In the day log

```
r4t: ACK thread=01K3Q… gerry reason=automated_notification (closed without a reply)
r4t: ACK-QUIET gerry (rig leader) closed 1 thread(s) without replying; its 340 bytes of stdout stay transcript
r4t: ACK-NOTED thread=01K3Q… phil proposed a close on a thread it does not owe boss — noted, the obligation stays open
r4t: ACK-REJECT gerry thread=01K3Q… not-machine-originated: opened by boss — only a relay or a thread r4t opened may close without a reply
r4t: ACK-REJECT phil thread=01K3Q… content-override: the thread carries a direct question — it is owed an answer
r4t: ACK-REJECT phil malformed proposal 'CLOSE_WITHOUT_RETRY 01K3Q…' — the verb must be echoed exactly as close_without_reply <thread>
```

`ACK-QUIET` is the fallback's counterpart: a member that spoke the protocol
gets its prose kept as transcript rather than staged as a reply, because the
[stdout fallback](r4t-message-flow.md) exists to rescue members that do not
know the protocol — and this one just used it. That suppression is per
obligation too: it applies only when the thread the fallback would answer on is
one of the threads just closed. A turn that closes an FYI and writes a real
answer for another open thread still delivers that answer.

Scope is per obligation, never per sender and never per turn. Closing one FYI
from a member does nothing to that member's next direct question.
