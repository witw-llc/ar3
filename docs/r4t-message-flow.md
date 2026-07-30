# How a message flows

The full life of a message through an r4t roster, from the a8s wall to the
member's queue and back out. For governance rationale see
[r4t-governance.md](r4t-governance.md); for the knob table see
[r4t-rigs.md](r4t-rigs.md#governance-knobs).

## The five steps

1. `tell acme "..."` routes through a8s to the roster node; the node's
   definition invokes `r4t dispatch`. **The topmost leader IS the garden
   from outside** — every external message enters at the top, no matter how
   it is addressed. A `node:member` sub-address from an outside sender is
   ignored (the namespace is the garden's outside address, not a way in to a
   specific member); the leader is the one who decides what to relay inward.
   The lone exception is the roster human's own `Address:` — a reply from it
   is the human speaking, so it lands in the seat path and routes exactly
   like a chat/seat send (see [r4t-operations.md](r4t-operations.md)).
2. r4t opens or continues a thread (a conversation label; a fresh thread
   opens for external mail) and ENQUEUES the message into the leader's
   durable queue — unconditionally. When the leader is runnable (both its
   spend bucket and the cell bucket hold ≥1, its breaker is closed, the
   throttle admits a start), ONE turn drains its whole queue: the prompt
   carries its persona, rolling history, and every waiting message at once.
   Intra-roster routing (below) is what delivers to a named member — from
   *inside* the garden, addressing is honored.
3. The harness's `$TELL_OUTBOX_DIR` points at a per-turn staging dir, so
   a member replies with the ordinary `tell` — unmodified. Inside the walls a
   message is a structured r4t-message, not a text header: dispatch reads the
   staged files as drafts (`to` + `body` + optional files) and stamps the rest
   as fields — `from` (from the staging dir, unforgeable), the thread/hop, and
   a `class` (`human` deliberate · `auto` relay/nudge · `error` feedback). Each
   reply is attributed to the thread of the message it answers, the send quota
   applies, outbound messages land in the sender's history, and the message goes
   straight onto the recipient member's queue (intra-roster, no header, no
   round-trip) or is converted to an a8s envelope at the wall (external — the
   only place a wire header exists, carrying `class` in the envelope's `meta`).
   Inside the roster, agents address each other by bare first name
   (`tell gerry`) — the namespace prefix is the *outside* address of the
   walled garden, and roster agents never see it. Release canonicalizes
   recipients: bare roster names become intra-roster routes, human members
   resolve to their real a8s address, and anything else (`chatroom`,
   external addresses) passes through untouched.
4. Agents never wait for replies in a turn (actor doctrine): delegate, end
   the turn, get woken when replies arrive, answer the originator when
   there is enough. `tell --sync` to members is prohibited by prompt and
   pointless by design.
5. Stdout fallback — `tell` always wins. A turn that staged even one
   envelope keeps its stdout as transcript. But a turn that exits 0,
   releases nothing, and printed a non-trivial answer gets its cleaned
   stdout — ANSI and harness chrome stripped — staged as one reply to the
   inbound sender, riding every gate in step 3. On by default, and no
   rig is above it: small local models reliably answer in prose and never
   run `tell`, and strong models fall into the same shape — a frontier
   Gemini model on the agy preset reasoned itself into prose-only replies
   in a live org (see [r4t-harness-agy.md](r4t-harness-agy.md) for one incident the
   fallback absorbed). Stdout-only turns participate without knowing the
   protocol exists; they are just downgraded to a single reply. A member
   whose stray prose is noise rather than answers opts out with
   `- **Fallback:** off` in the roster: its no-tell turns log `SILENT`
   instead of staging a reply, and the blank-output quota detection is
   untouched.

## The durable queue

Every inbound message to a member enqueues unconditionally — no gate ever
drops or dead-letters a deliverable message. Dead letters are for
*undeliverable* mail only (unknown recipient, disabled member, a rig that
will not resolve) plus a per-turn send-quota overflow: each becomes one JSON
record (reason, count, from, to, thread, time) in
`~/.config/r4t/rosters/<node>/dead-letter/`. Duplicate collapse replaces pair
suppression: when the newest queued entry has the same sender and identical
normalized body, the arrival collapses into it with a `repeats` count rather
than adding noise. A turn drains the WHOLE queue at once (batch invoke): one
prompt shows every waiting message, so an agent pivots on the current state
instead of burning a turn per message.

## No wire header inside the walls

Inside the walls there is no text header: a message is a structured
r4t-message whose fields (`thread` label, telemetry `hop` that never cuts a
message, and a `class` of `human`/`auto`/`error`) travel end to end, stamped by
dispatch and never written or parsed as prose. The only wire header is at
egress, where an external release is converted to an a8s envelope carrying the
bare body and `class` in the envelope's `meta` object — other a8s nodes must not
need to know whether a name is one agent, a human, a device, or a whole roster.
Symmetrically, external ingress is untrusted: a sub-address can't pick a member
and nothing is parsed out of the body — everything from outside enters at the
top lead on a fresh thread. One ingress point means one thing to reason about.

## Class across the wall

`meta.class` is the one protocol field that crosses the wall in both
directions. Releases carry `auto`, because everything a member sends is machine
traffic; inbound mail is deliberate attention unless the sender marked it
`auto`, so a human, a phone, or a peer that says nothing is heard as a person.

Metadata is advisory for governance and never for identity. A peer can only
downgrade its own traffic, an unknown word means deliberate, and thread and hop
stay garden-internal — nothing on the wire can claim a thread. What a relayed
inbound changes is what the garden owes it: the thread it opens is a label, not
a report someone is waiting on, so the quiet-thread sweep leaves it alone. That
is what keeps two federated rosters from reading each other's polite
status updates as fresh human attention and nudging each other awake forever.

What the outside sees is the org owner's choice. Dispatch releases with
`from: <node>:<member>`, and by default that attribution stands — external
mail arrives as `acme:lead`. Bind the namespace with `a8s namespace acme
<node> --opaque` and the a8s router — which owns `from`, because the outbox is
agent-writable — presents egress as the bound prefix instead: mail from the
org arrives as `acme`, never `acme:lead` or `acme-node`, so the org speaks
with one mouth under one name and a reply to `acme` re-enters at the top lead.
Inside the walls attribution stays member-level either way; opacity is
presentation at the wall, not a loss of who said what.
