# Idle pass — what runs when the roster goes quiet

One invocation of `r4t idle` is an **idle pass**. a8s is only a clock: the
node definition's `idle.timeout` (60 in `apps/a8s/definitions/r4t.json`)
fires `r4t idle` after that many seconds without a wake, and a8s knows
nothing about what the pass does. `r4t idle` is also runnable by hand. The
mechanisms below live entirely in `dispatch.run_idle`.

**A pass that finds nothing prints nothing at all.** At a one-minute cadence a
heartbeat line would be over a thousand lines a day in the one stream the
roster is meant to be watchable in. "Is it alive?" is answered by
`r4t status`, which carries the idle time on its `Now` row; the ticker is for
events.

Rationale for the stack sits in
[r4t-governance.md](r4t-governance.md);
`Continue:` and retirement in [r4t-rigs.md](r4t-rigs.md#continuing-a-conversation);
the mission in [r4t-org.md](r4t-org.md#the-mission-review-idle-pass);
dreaming in [r4t-knowledge.md](r4t-knowledge.md#distill-on-the-way-out--dreaming-not-per-turn).

## Order

`run_idle` runs these in this order, every pass:

| # | Vernacular | Log tag | What it does |
|---|---|---|---|
| 1 | **recover** | `RECOVERED` | Return every `queue/.inflight/` batch whose member holds no live lock — a turn that was killed mid-flight |
| 2 | **prune** | (silent) | Drop lock files whose PID is gone |
| 3 | **probe** | `RESUME` | Check each parked member's command with `shutil.which`; one that resolves rejoins the rotation with its whole queue |
| 4 | **drain** | (ordinary turn logs) | Run every runnable queued turn until a pass runs nothing |
| 5 | **dream** | `DREAM` | Distill fresh turn captures into each knowledge-carrying member's k7e store |
| 6 | **heartbeat** | `MISSION-REVIEW` | If the org is structurally stalled, ask the top leader whether the mission is met |
| 7 | **flush** | `FLUSH` | Retire continuing conversations idle past their `Continue: <duration>` window |

Recovery is first because everything after it reads the queue and has to read a
true one. The probe is free — no subprocess, no tokens, no engine wake — which
is what earns it a place on every pass.

If the heartbeat fires a review turn that stages work, `run_idle` drains
again before reaching the flush. Retirement is last on purpose: a turn running
after the sweep would re-record the very conversation it just retired.

A load error (roster or rig config) logs `IDLE-SKIPPED` and runs nothing.

## Mechanisms

### 1. Drain

Not a sweep — queued turns simply run via `drain_until_quiet`. Members out of
budget, on an open breaker, or blocked by the cadence floor keep their queue;
refill or cooldown is the retry. Mentioned here because any work the heartbeat
stages rides this same drain.

### 2. Dream (`DREAM`)

For each member with `Knowledge:` on (any non-off form), distill fresh turn
captures into that member's private k7e store and drain the embedding backlog.
Bounded per member per pass; the `.dreamed` watermark advances only on
success. Runs through k7e with the member's distill rig (or the
`Knowledge:` line's rig override) — see
[knowledge](r4t-knowledge.md#distill-on-the-way-out--dreaming-not-per-turn).
Default off: no `Knowledge:` line means no dream work for that member.

### 3. Heartbeat (`MISSION-REVIEW`)

Nobody is waiting — nobody ever is, because a message demands no answer. The
org is **structurally stalled** when this pass's drain ran nothing, every queue
is empty, no turn is live, and no member has finished a turn since the last
tick. That last clause is what makes a stall durable rather than a property of
one pass: work driven straight through `handle_message` between two idle passes
leaves no queue and no lock behind, so without a memory of the newest turn
stamp every quiet moment would read as a stall.

Then the top leader gets a budget-gated review turn to reweigh the mission and
delegate the next step. This is the single mechanism that re-engages a stalled
org. See [org](r4t-org.md#the-mission-review-idle-pass).

**Backoff and dormancy** (from `dispatch._mission_review`):

- Each stalled idle pass increments a stall counter.
- A review fires only when stalls reach
  `min(2 << silent_reviews, 32)` — so with no prior silent reviews the first
  fire needs 2 stalled passes; after silent reviews the threshold widens
  2 → 4 → 8 → 16 → 32.
- A productive review (something queued afterward) resets the silent
  counter. A silent review (leader stages nothing) increments it.
- After **3** silent reviews the heartbeat goes **dormant**. It stays dormant
  until a real message (any non-stalled pass) or a `MISSION.md` mtime change
  re-arms it.
- Any non-stalled pass resets stalls, silent reviews, and dormancy.

### 4. Flush (`FLUSH`)

An idle continuing conversation is retired after a dump turn so the next real
message refounds cheap and small. For each member with
`Continue: <duration>` (so `flush_seconds` is set), a live (not already
retired) conversation, an empty queue, and a last-completed turn older than
the window: enqueue the dump prompt, run one budget-gated turn, and retire
only when that turn exits 0 without timing out. History stays in place on the
idle path (manual `r4t flush` archives it — see
[rigs](r4t-rigs.md#continuing-a-conversation)).

This is the graceful path: it spends a dump turn so the state reaches disk
before the conversation goes. A roster whose idle pass has not run still has
the backstop at dispatch — a turn that finds its conversation idle past the
window logs `CONTINUE-STALE` and refounds cold from whatever is already on
disk (see [rigs](r4t-rigs.md#the-three-refound-gates)).

**`Continue: on` never flushes.** Retirement is armed only by
`Continue: <duration>`. `_flush_sweep` skips every member whose
`flush_seconds is None`, and `on` parses to exactly that — there is no
default window. A reader who wants cache hygiene and writes `Continue: on`
gets none.

## What each costs

The flush dump and the heartbeat review are real harness turns: they charge
member / cell / (optional) rig budget like any other turn.

| Mechanism | Budget-gated? | Empty budget |
|---|---|---|
| Drain | Yes | Queued work holds; next pass retries |
| Dream | No r4t spend-bucket gate — k7e distill uses the distill rig directly | N/A to member/cell budget; a missing or failing distill rig logs `DREAM-SKIP` / `DREAM-FAIL` and captures wait |
| Heartbeat | Yes — checks `_runnable` on the leader | Logs `MISSION-REVIEW deferred`; holds the stall counter at the threshold so the review fires as soon as the bucket refills |
| Flush | Yes — checks `_runnable` before the dump turn | Logs `FLUSH deferred`; retries next pass when budget refills |

## Knobs

| Knob | Where | Default | Effect |
|---|---|---|---|
| `idle.timeout` | a8s node definition (`apps/a8s/definitions/r4t.json`) | `60` | Seconds of quiet before a8s invokes `r4t idle` |
| `MISSION_REVIEW_MIN_INTERVAL_SECONDS` | `dispatch.py` | `1800` | Wall-clock floor between two heartbeat reviews. The backoff ladder counts idle WAKES, so without this a shorter wake interval would multiply the spend without anyone editing a policy |
| `Continue: <duration>` | roster, per member | off (no continue); `on` continues with **no** flush window | Duration (`15m`, `4h`, bare seconds, …) arms flush; `on` never flushes |
| `Knowledge:` | roster, per member | off | Any non-off form enables the store and the dream sweep for that member |
