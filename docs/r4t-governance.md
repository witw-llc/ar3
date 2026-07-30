# Governance: keeping agent rosters from running amok

r4t is the single execution chokepoint for a roster's turns. Every governance
mechanism below is enforced at dispatch or at outbox release — never inside
the LLM. Prompt etiquette ("don't send ack-only messages") is kept as a
courtesy, but no mechanism depends on an agent obeying it.

Everything here runs autonomously. There are knobs (config) and lenses
(`r4t status`, the dead-letter dir, a8s logs); the only gate is on
escalation to an absent human (the doorbell — see
[r4t-verification.md](r4t-verification.md)). Member work never waits, and a
deliverable message is never dropped.
The economics are *budgets, not cuts* — an agent that is out of budget does
not run (its mail queues), rather than having its mail thrown away. The point
is not only that no roster overspends the plan, but that the plan you already
pay for keeps earning: held queues mean refill is the retry, so capacity is
spent on work rather than left idle.

## The failure modes

These are not hypothetical. An early in-repo agent dispatcher produced all
of them within days:

1. **Ack storms.** Agents acknowledging each other's acknowledgments —
   107 files matching `*reciprocal*`, turns burned restating the same
   facts, a real-money quota jump from ~10% to ~40% of a monthly plan.
   The CAMEL paper (NeurIPS 2023) documents the same phenomenon in
   role-playing agent pairs: "repeatedly thanking each other or saying
   goodbye without progressing the task," sometimes aware they are stuck
   but unable to break out. LLM self-awareness does not stop the loop;
   external enforcement does.
2. **Ping-pong through a re-broadcaster.** A chat-room node re-emits
   posts as fresh notifications, stripping any per-conversation metadata —
   so anything keyed on conversation state is defeated on every bounce. The
   defense has to be cost (budgets) and idempotent arrival (duplicate
   collapse), not chain-length bookkeeping.
3. **Runaway fan-out.** One turn messages N members; each of those
   messages N more. Unbounded width, multiplied by depth.
4. **Stalled fan-in.** A leader delegates, a subordinate's process dies,
   and the leader never answers the human because nothing re-engages
   either side.
5. **Quota burn without work.** All of the above cost real tokens/credits
   while producing nothing.

## Why the transport can't help

All messages — including member-to-member — round-trip through a8s.
a8s is deliberately a dumb pipe (recipient opacity is its core invariant),
messages can originate from unmanaged senders, and rooms re-broadcast. So
the transport layer can neither attribute nor police traffic. r4t's answer
is to make *cost*, not message-dropping, the throttle: a durable per-member
queue absorbs every arrival, and spend budgets decide when a member is
allowed to spend a turn draining it.

## The stack

Ordered from most to least load-bearing. Each layer names its prior art;
parameters were verified against primary sources (references at bottom).

### 1. The durable member queue (nothing is ever dropped)

Every inbound message to a member enqueues, unconditionally, into
`agents/<member>/queue/`. No gate drops or dead-letters a deliverable
message — dead letters are reserved for genuinely *undeliverable* mail
(unknown recipient, disabled member, a rig that will not resolve). Work
waits; messages never die. This is the keystone the rest of the stack
hangs off: budgets, no-cutting, and batch invoke are all one data structure.

### 2. Duplicate collapse (the only "suppression")

When a message enqueues, if the NEWEST queued entry has the same sender and
identical normalized body, the arrival collapses into it — one entry, a
bumped `repeats` count — instead of adding a file. Collapsing loses no
information (the prompt notes "sent N times"), so it needs no window and
no dead letter. It replaces content-keyed pair suppression outright.

Prior art: RFC 3834 §2 (do not issue the same response to the same sender
repeatedly); syslogd's "last message repeated N times"; gemini-cli's
`LoopDetectionService`. The mechanism transfers; the difference is that
collapse *keeps* the count rather than discarding the repeats.

### 3. Batch invoke

A turn drains the WHOLE queue at pick time: one prompt renders every waiting
message chronologically ("Messages since your last turn"). An agent that
sees "members discussed X, then the lead overrode with Y" in one reading
pivots to the current state instead of burning a turn reacting to each
message in sequence. It is both a quota saver and a storm damper — a burst
of N arrivals costs one turn, not N.

### 4. Spend budgets (member + cell)

A bifurcated token bucket, refilled lazily by elapsed wall-clock time. Each
member has its own bucket (`budget_max` / `budget_earn_per_hour`) and the
whole cell shares one (`cell_budget_max` / `cell_budget_earn_per_hour`). A
turn costs 1 member unit AND 1 cell unit, regardless of how many messages it
consumes. Both must hold ≥1 for a member to run; an empty bucket means the
member is *resting* — its queue holds and it runs again when the bucket
refills. Put frontier rigs on a low budget (they run slowly and smartly) and
local rigs on a high one (near-free) so nothing goes to waste.

Prior art: real AI subscription plans (a rolling window plus a shared cap);
gRPC retry throttling (gRFC A6 — a token bucket that disables spend below a
floor); IRC flood control's burst-credit model (RFC 1459 §8.10). The design
lesson is graduated degradation: slow first, queue second, never drop.

### 5. Cadence throttle and concurrency cap

Roster-wide floor on burn rate: a minimum interval between turn starts and a
cap on concurrent turns. Content- and topology-blind, so nothing evades it;
a perfectly evasive storm degrades into a slow, visible drip. A member that
can't start yet keeps its queue and runs on a later pass.

Prior art: IRC flood control — RFC 1459 §8.10 (burst credit, per-message
penalty; excess queues rather than drops), UnrealIRCd fake lag.

### 6. Per-member failure breaker

A member whose turns keep failing outright (nonzero exit or timeout — a bad
flag after a CLI update, a revoked key, a dead local model) would otherwise
burn a full turn on every arrival. After `breaker_cap` consecutive failed
turns the member's turns pause; its queue simply holds (nothing is dropped).
One probe turn is let through per `breaker_cooldown_seconds`; the first
clean turn closes the breaker.

Prior art: systemd start rate limiting (`StartLimitBurst`/
`StartLimitIntervalSec`) and the circuit-breaker pattern's
closed/open/half-open probe cycle.

### 7. Quiet-thread sweep (the termination backstop)

A thread (conversation label) can go quiet with its originator never having
heard back — a turn succeeds while staging no reply, or a chain stalls. When
an open thread whose originator is unanswered sees no activity for
`quiet_task_seconds`, the leader is woken with a nudge to report current
state — NOT to force-finish the work. The human, or the leader, decides what
"done" means; r4t only makes sure the originator is not left in silence.

A thread opened by relayed mail — an inbound the sending cluster marked
`meta.class: auto` (see [r4t-message-flow.md](r4t-message-flow.md#class-across-the-wall))
— is skipped. Its originator is another cluster's machinery, so a status report
to it is not attention owed; it is one more inbound that peer must answer, which
is how two rosters keep each other awake forever. The nudge exists for whoever
is actually waiting.

Prior art: Erlang/OTP supervision — a bounded, rate-limited recovery action
rather than an unbounded retry loop.

### 8. The tree (information hiding + hard rerouting)

A roster is not a flat pool of peers; it is a tree of small **cells**, each
with one lead, composing up to a single top lead. The roster declares it: an
AI member's `Cell:` line names its cell and its `Lead:` line names the member
it reports to (the top lead's `Lead:` is the human). Two mechanisms make the
tree structural rather than merely advisory, and both switch on only when the
roster declares `Lead:` lines — a flat roster (no `Lead:` lines anywhere) is
treated as one cell under the leader and behaves exactly as before.

- **Information hiding.** A member's turn prompt lists only its
  tree-adjacent names — its lead, its direct reports, its cell-mates — plus
  the human seat, which is always reachable. It never sees the whole roster.
  Lateral contact becomes informationally *unthinkable*, not just structurally
  blocked: an IC in the design cell has no idea the build cell's members exist
  by name. Cross-cell contact is created by introduction — a lead mentioning
  a name in a message is the grant — which keeps the channel social, relayed
  through a lead, rather than a standing back-channel.
- **Hard rerouting.** If a member does address someone outside its adjacency
  (a stale name from history, say), the release path reroutes that tell to the
  member's lead with a mechanical prefix (`[r4t rerouted: Ann -> Cal] …`) and
  logs a `REROUTED` event. Replies to whoever messaged the member this turn,
  and any message to the human seat, are never rerouted — answering must
  always get through.

Why this shape: span-of-control research converges on cells of ~4–6 (soft
warning past 6, hard cap 10) and trees no deeper than ~2–3 levels; `roster
check` lints those bounds. And the live evidence is direct — in the first d5n
run the turn prompt advertised the full roster, and a build-cell lead (Rook)
messaged a design-cell lead (Cass) laterally *because the name was in front of
him*. The tree held voluntarily otherwise, but "voluntarily" is not a control.
Information hiding removes the temptation; rerouting removes the option.

Prior art: Roster Topologies (Skelton/Pais) — explicit, bounded interaction
modes rather than ad-hoc cross-roster chatter; the parametric bounds trace to
Hackman's roster-size work and the US Army's fire-roster/squad structure.

### 9. The mission file (nested intent)

A `MISSION.md` at the repo root is the roster's highest-ranking document — a
short, human-owned page of *why* the repo exists and what "done" looks like
(purpose, end state, current milestone), never the *how*. It outranks every
other document; where anything conflicts with it, it wins.

Injection is **leads-only**: a member with direct reports gets the file
verbatim at the top of every turn prompt; an IC never does. The IC receives
its portion as ordinary messages from its lead, restated at the resolution it
can hold — a dumb-rig member's whole world is its lead's message, by design.
Intent thus flows edge-by-edge down the tree, restated at every hop, and "who
gets the mission" has exactly the same answer as "who reports to whom". A flat
roster treats the marked leader as the only lead. This is commander's-intent
doctrine (US Army ADP 6-0): intent is the purpose and desired end state, not a
plan, pushed down the chain so each level can act coherently when the plan
meets reality — over-specifying the *how* is the failure mode, not the goal.

Two things stay deliberately social, not mechanical. The **briefback**: when
the file changes (only at milestone boundaries), the top lead's next turn
restates the intent in its own words to the human and waits for correction
before work resumes — the loop that catches a wrong reading cheaply. And the
milestones themselves stay prose the human interprets; r4t builds no status
fields or staleness verdicts. The only machinery is injection plus a length
lint: `roster check` warns when `MISSION.md` exceeds ~40 non-blank lines,
because intent that outgrows a page has usually drifted into planning. The
mission-command evidence lives in the wiki's Plans archive (ORG-LESSONS).

## Disposal and observability

Undeliverable mail and per-turn send-quota overflow are never silently
dropped: they move to a dead-letter directory under the roster's state dir
with an x-death-style record (reason, count, sender, recipient, thread,
time) — RabbitMQ's audit pattern. That is a lens and a replay source, not a
queue anyone waits on. Deliverable messages never land here; they queue.

Observability rides on a8s rather than duplicating it:

- Traffic: every message, with full `roster:member` addresses, is already
  in a8s's transaction log and convo history — r4t adds no transport.
- Decisions: r4t's dispatch stdout is captured into the a8s node log by
  the wake machinery, so every governance action is one structured line
  (`r4t: RESTING bob — resting (member budget 0.0, ready in ~14 min) (3 queued)`) in the stream `a8s logs` already
  tails.
- State only r4t can have (locks, buckets, ledgers, dead letters):
  `r4t status` and the state dir.

## References

- RFC 3834, Recommendations for Automatic Responses to Electronic Mail —
  https://www.rfc-editor.org/rfc/rfc3834
- RFC 5230, Sieve Vacation Extension — https://www.rfc-editor.org/rfc/rfc5230
- RFC 5321 §6.3, SMTP loop detection — https://www.rfc-editor.org/rfc/rfc5321
- Postfix `hopcount_limit` — https://www.postfix.org/postconf.5.html
- procmail X-Loop convention — https://linux.die.net/man/5/procmailex
- RFC 1459 §8.10, IRC flood control — https://www.rfc-editor.org/rfc/rfc1459
- UnrealIRCd anti-flood — https://www.unrealircd.org/docs/Anti-flood_settings
- RabbitMQ federation max-hops — https://www.rabbitmq.com/docs/federation-reference
- RabbitMQ dead-letter exchanges — https://www.rabbitmq.com/docs/dlx
- Erlang/OTP supervision principles — https://www.erlang.org/doc/system/sup_princ.html
- systemd unit start rate limiting — https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#StartLimitIntervalSec=interval
- AWS SQS dead-letter queue redrive (`maxReceiveCount`) — https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html
- gRFC A6, gRPC client retries — https://github.com/grpc/proposal/blob/master/A6-client-retries.md
- RFC 5681, TCP congestion control (AIMD) — https://www.rfc-editor.org/rfc/rfc5681
- AWS, Exponential Backoff and Jitter —
  https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
- gemini-cli LoopDetectionService —
  https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/loopDetectionService.ts
- CAMEL: Communicative Agents (NeurIPS 2023) — https://arxiv.org/abs/2303.17760
- Anthropic, How we built our multi-agent research system —
  https://www.anthropic.com/engineering/multi-agent-research-system
- OpenAI Agents SDK `max_turns` —
  https://openai.github.io/openai-agents-python/running_agents/
- LangGraph `recursion_limit` —
  https://docs.langchain.com/oss/python/langgraph/errors/GRAPH_RECURSION_LIMIT
- CrewAI agent attributes — https://docs.crewai.com/en/concepts/agents
- AutoGen `max_consecutive_auto_reply` —
  https://microsoft.github.io/autogen/0.2/docs/reference/agentchat/conversable_agent/
