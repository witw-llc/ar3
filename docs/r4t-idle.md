# Idle pass — what runs when the roster goes quiet

One invocation of `r4t idle` is an **idle pass**. a8s is only a clock: the
node definition's `idle.timeout` (300 in `apps/a8s/definitions/r4t.json`)
fires `r4t idle` after that many seconds without a wake, and a8s knows
nothing about what the pass does. `r4t idle` is also runnable by hand. The
five mechanisms below live entirely in `dispatch.run_idle`.

Rationale for the watchdog sits in
[r4t-governance.md](r4t-governance.md#7-quiet-thread-sweep-the-termination-backstop);
`Continue:` and retirement in [r4t-rigs.md](r4t-rigs.md#continuing-a-conversation);
the mission in [r4t-org.md](r4t-org.md#the-mission-review-idle-pass);
dreaming in [r4t-knowledge.md](r4t-knowledge.md#distill-on-the-way-out--dreaming-not-per-turn).

## Order

`run_idle` runs these in this order, every pass:

| # | Vernacular | Log tag | What it does |
|---|---|---|---|
| 1 | **watchdog** | `QUIET` | Open intra-roster threads whose originator is unanswered and quiet past `quiet_task_seconds` — nudge the leader to report state |
| 2 | **drain** | (ordinary turn logs) | Run every runnable queued turn until a pass runs nothing |
| 3 | **flush** | `FLUSH` | Retire continuing conversations idle past their `Continue: <duration>` window |
| 4 | **dream** | `DREAM` | Distill fresh turn captures into each knowledge-carrying member's k7e store |
| 5 | **heartbeat** | `MISSION-REVIEW` | If the org is structurally stalled, ask the top leader whether the mission is met |

If the heartbeat fires a review turn that stages work, `run_idle` drains
again before returning.

A load error (roster or rig config) logs `IDLE-SKIPPED` and runs nothing.

## Mechanisms

### 1. Watchdog (`QUIET`)

Someone is waiting. An open thread whose originator has not been answered,
that is not ingress (outside-the-wall mail is owed nothing — see
[governance](r4t-governance.md#7-quiet-thread-sweep-the-termination-backstop)),
and whose ledger activity is older than `quiet_task_seconds`, gets one nudge
to the leader: report current state, do not force-finish the work. The nudge
is ingested as an `auto` message; the following drain runs it as a normal
turn.

Skipped entirely when `quiet_task_seconds <= 0`, when any turn is live, or
when there is no healthy leader. After a nudge the thread's `updated_at` is
bumped so the same silence does not re-fire until it goes quiet again.

### 2. Drain

Not a sweep — queued turns simply run via `drain_until_quiet`. Members out of
budget, on an open breaker, or blocked by the cadence floor keep their queue;
refill or cooldown is the retry. Mentioned here because the watchdog's nudge
and any work the heartbeat stages ride this same drain.

### 3. Flush (`FLUSH`)

An idle continuing conversation is retired after a dump turn so the next real
message refounds cheap and small. For each non-human member with
`Continue: <duration>` (so `flush_seconds` is set), a live (not already
retired) conversation, an empty queue, and a last-completed turn older than
the window: enqueue the dump prompt, run one budget-gated turn, and retire
only when that turn exits 0 without timing out. History stays in place on the
idle path (manual `r4t flush` archives it — see
[rigs](r4t-rigs.md#continuing-a-conversation)).

**`Continue: on` never flushes.** Retirement is armed only by
`Continue: <duration>`. `_flush_sweep` skips every member whose
`flush_seconds is None`, and `on` parses to exactly that — there is no
default window. A reader who wants cache hygiene and writes `Continue: on`
gets none.

### 4. Dream (`DREAM`)

For each member with `Knowledge:` on (any non-off form), distill fresh turn
captures into that member's private k7e store and drain the embedding backlog.
Bounded per member per pass; the `.dreamed` watermark advances only on
success. Runs through k7e with the member's distill rig (or the
`Knowledge:` line's rig override) — see
[knowledge](r4t-knowledge.md#distill-on-the-way-out--dreaming-not-per-turn).
Default off: no `Knowledge:` line means no dream work for that member.

### 5. Heartbeat (`MISSION-REVIEW`)

Nobody is waiting. The org is **structurally stalled** when this pass's drain
ran nothing, every queue is empty, no thread is open, and no turn is live.
Then the top leader gets a budget-gated review turn to reweigh the mission and
delegate the next step — never to doorbell the human. See
[org](r4t-org.md#the-mission-review-idle-pass).

**Backoff and dormancy** (from `dispatch._mission_review`):

- Each stalled idle pass increments a stall counter.
- A review fires only when stalls reach
  `min(2 << silent_reviews, 32)` — so with no prior silent reviews the first
  fire needs 2 stalled passes; after silent reviews the threshold widens
  2 → 4 → 8 → 16 → 32.
- A productive review (queue or open thread afterward) resets the silent
  counter. A silent review (leader stages nothing) increments it.
- After **3** silent reviews the heartbeat goes **dormant**. It stays dormant
  until a real message (any non-stalled pass) or a `MISSION.md` mtime change
  re-arms it.
- Any non-stalled pass resets stalls, silent reviews, and dormancy.

## What each costs

Watchdog nudge, flush dump, and heartbeat review are real harness turns: they
charge member / cell / (optional) rig budget like any other turn.

| Mechanism | Budget-gated? | Empty budget |
|---|---|---|
| Watchdog | Yes — nudge enqueues, drain's `_runnable` gate applies | Queue holds; runs when the bucket refills |
| Drain | Yes | Queued work holds; next pass retries |
| Flush | Yes — checks `_runnable` before the dump turn | Logs `FLUSH deferred`; retries next pass when budget refills |
| Dream | No r4t spend-bucket gate — k7e distill uses the distill rig directly | N/A to member/cell budget; a missing or failing distill rig logs `DREAM-SKIP` / `DREAM-FAIL` and captures wait |
| Heartbeat | Yes — checks `_runnable` on the leader | Logs `MISSION-REVIEW deferred`; holds the stall counter at the threshold so the review fires as soon as the bucket refills |

## Knobs

| Knob | Where | Default | Effect |
|---|---|---|---|
| `idle.timeout` | a8s node definition (`apps/a8s/definitions/r4t.json`) | `300` | Seconds of quiet before a8s invokes `r4t idle` |
| `quiet_task_seconds` | rig config | `1800` | Watchdog silence threshold in seconds; `0` disables |
| `Continue: <duration>` | roster, per member | off (no continue); `on` continues with **no** flush window | Duration (`15m`, `4h`, bare seconds, …) arms flush; `on` never flushes |
| `Knowledge:` | roster, per member | off | Any non-off form enables the store and the dream sweep for that member |
