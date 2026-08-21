# Operating a roster: status, logs, and tell

The surfaces for watching and talking to a running roster — one per way of
looking. Setup is in the [tutorial](r4t-tutorial.md); this page assumes a
registered roster.

You are not a member of the roster. Your way IN is `r4t tell --as <member>`
and your way to WATCH is `r4t logs`.

## The three surfaces

- `r4t status` — the snapshot. Leads with the rotation (who is running, who is
  next and why, who is held and why), then plain-English health verdicts
  (runaway? member broken or resting with work queued? cell budget spent? a
  queue backing up?), then member budgets, queue depths, and dead letters
  rolled up by meaning.
- `r4t logs -f` — the stream. The roster's own event log: every governance
  decision and turn boundary, including walled-garden traffic that never
  reaches a8s. `--full` includes prompts and transcripts. `--agent <member>`
  (repeatable) or `--cell <cell>` narrows the stream to one or more members;
  with `--full` it prints their captured turns (each turn's full prompt and
  raw output, newest last).
- `r4t tell --as <member>` — speak into the roster as another member:
  jumpstart a queue or diagnose a route, without waiting for a real sender
  (below).

The first dispatch stamps the repo root into roster state, so `--node` works
from any directory — and from inside a roster repo the `--node` flag itself
is optional.

`a8s logs <node> -f` shows the cross-wall view, and the roster's own ticker
with it: dispatch narrates each lifecycle event to stdout as it happens, and
a8s pumps a wake's stdout into that node's log. One follow on one node is the
whole roster running, one line per event:

```
r4t: QUEUED gerry from neil thread=01M06DTTFGQ5K18ZAP1J5JCWP0 hop=0 depth=2
r4t: TURN gerry 2 msg rig=ark-lead continue prompt=18.4k
r4t: DONE gerry exit 0 in 192.4s
r4t: RESTING phil resting (member budget 0.4, ready in ~7 min) (1 queued)
```

`BREAKER` and `DEFERRED` join `RESTING` for a member that had mail and did not
run — the reason is on the line. `PARKED` and `RESUME` bracket a member leaving
and rejoining the rotation, and `RECOVERED` reports a killed turn's batch going
back to its queue. The member is always the second field, so a name greps
cleanly. Message bodies and transcripts never reach the ticker; they are what
`r4t logs --full` is for.

## The rotation

One node runs one turn at a time, start to finish, and picks the next member
only when that turn ends. `r4t status` prints the whole answer:

```
Rotation  (one turn at a time)
  Now   —       idle 35s   (last: mira, exit 0)
  Next  dana    intra + passed over 4   score 4   1 queued, oldest 22m
  Then  carlos  ingress + passed over 2   score 3   2 queued, oldest 9m
  Held  finn    PARKED — failed to spawn harness 'codex'   1 queued   (try: r4t resume finn)
        gus     RESTING — member budget 0.4, ready in ~8 min   1 queued
  Idle          1 member(s) with nothing queued
```

There is exactly one `Now` row, always: it is the contract, rendered. While a
turn runs it reads `running 1m12s of 45m` — the elapsed time against the rig's
per-turn timeout, because under run-to-completion one hung member stalls the
whole roster and that timeout is the only thing that ends it.

`Next` is the real selection, from the same call the drain loop makes. It is
never a re-implementation, so the printed answer and the taken one cannot
drift.

### Which member is queued

A member is in the run queue when its inbox holds at least one message. That is
the whole of it — one bit per member, and the messages are payload, not
scheduling units. A member with ten queued messages gets one turn that renders
all ten, not ten turns. Nothing is stored anywhere else; the queue directories
are the queue.

### Tier 1: priority

A member holding mail from a **priority sender** goes next, always. Among
several, the oldest message goes first.

| | |
|---|---|
| Where it is set | `priority_senders` in [`r4t-org.json`](r4t-org.md) |
| Default | `[]` — no priority sender ships |
| Matching | `fnmatch` globs against the envelope's `from`, case-insensitive |
| Empty list | the tier is never populated; a pure tier-2 rotation |

**Priority never preempts.** A running turn always finishes, so the promise is
*next*, not *now*.

### Tier 2: the score

```
score = 2*ask + 1*ingress + passes
```

| Term | Value | Meaning |
|---|---|---|
| `ask` | 2 | reserved for a future r4t-only verb. **Always 0 today** — the term is kept visible so the ladder does not change shape when it lands. |
| `ingress` | 1 | the queue holds mail from outside the roster, stamped `origin: ingress` when it entered at the wall. Member-to-member mail is `intra` and scores 0. |
| `passes` | 0..n | how many consecutive selections this member was **ready and not chosen**. |

Ties break by oldest message, then by member name, so the order is
deterministic and reproducible without a clock.

### Starvation

`passes` rises by one for every ready member a selection skips and resets to 0
when the member runs. A member the *budget* held back does not age — the
scheduler did not pass it over, the budget did, and conflating the two makes
"why won't it run" unanswerable.

The classes can contribute at most `2 + 1 = 3`. So:

> **A member passed over 4 times outranks any class combination on freshly
> arrived mail.** After four passes, the only members that can still go first
> are ones that have waited at least as long as you.

Wall-clock wait is printed, because people think in minutes, and is
deliberately not in the score.

### Held, and what clears it

| State | Meaning | Clears when |
|---|---|---|
| `RESTING` | a spend bucket is empty (member, cell, or rig) | the bucket refills — refill is the retry |
| `BREAKER` | `breaker_cap` consecutive failed turns | one probe turn per `breaker_cooldown_seconds` succeeds |
| `PARKED` | the harness could not start at all | a free probe finds the command, or `r4t resume <member>` |

### Parking: the failures that will recur

A structural failure — the harness binary is not on `PATH`, the exec never
started — fails identically every time it is retried. Retrying it forever is a
stream of identical errors about a fact that has not changed. So the **first**
one parks the member instead:

- one `r4t: PARKED <member> <reason>` line, one day-log line, then silence.
  Every later message to that member enqueues without a word.
- **the queue holds.** Nothing is dead-lettered, nothing is dropped. That is
  what makes the silence safe.
- the member leaves the rotation: not scored, not selected, no `passes`.

It comes back one of two ways. Every idle wake probes the rig's command with
`shutil.which` — free, no subprocess, no tokens, no engine wake — and the
instant it resolves the member un-parks with one `RESUME` line and its whole
queue. Where there is no free probe, `r4t resume <member>` (or `r4t resume
--all`) says so by hand. A system that cannot cheaply tell whether a problem is
fixed must not spend your money finding out on a timer.

A timeout, a nonzero exit with output, a network error and an exhausted quota
are all *transient* and keep the ordinary breaker.

### Crash safety

A turn claims its whole batch by MOVING the envelopes to
`agents/<member>/queue/.inflight/`, not by deleting them. On a clean end they
are dropped; on a failed turn they move back under their original names, so
they keep their ids and their place in arrival order. On a `SIGKILL`, an OOM or
a closed lid they simply stay there, and the next idle wake returns every
in-flight batch whose member holds no live lock. The PID lock is the liveness
test; a turn genuinely running is left alone.

### Parallelism

Two nodes, not two turns. Run the same structure under a second node name — on
another machine, or on this one. The rig spend buckets are machine-global, so
two nodes on one machine share them and cannot double your burn behind your
back. The cost is two rotations, two log streams, and no ordering guarantee
*between* them: the never-interleaved promise is per node.

## What the roster keeps, and for how long

Everything above reads out of `~/.config/r4t/rosters/<node>/`, which grows with
every turn. Maintenance passes (`r4t idle` — one
[idle pass](r4t-idle.md) — and `r4t clear` on its own) hold
it to a shape a roster can run for months on. `r4t clear` does three things
and no more: prune stale locks, drain what is runnable, apply log retention.

- **Day logs** (`log/<date>.md`, the stream behind `r4t logs`) are kept for
  `log_retention_days` UTC days — 14 by default, `0` to keep every day
  forever. Older days are deleted whole, so a surviving day is exactly what
  was written, and the pass records one `r4t: PRUNED ...` line naming what
  went.
- **Turn economics** (`velocity.csv`, one row per turn) is never pruned:
  finished months rotate out into `velocity-<month>.csv` beside it, which is
  what keeps the live file small.
- **Captured turns** (`agents/<member>/turns/`) keep the most recent 50 per
  member; the newest turn always displaces the oldest.
- **Dead letters** wait for you. Nothing prunes them.

The retention window is a governance knob in the rig config, alongside the
budgets and the breaker: see the table in [r4t-rigs.md](r4t-rigs.md#governance-knobs).

## What zone the roster speaks

r4t stores UTC and shows local. `r4t status` opens with a `time:` line naming
this machine's wall clock and its zone, and every member prompt's intro says
the same thing in a sentence — *Local time is 2026-08-16 13:22 PDT. Every
relative time you read or write — today, tomorrow, this morning — resolves in
that zone, not UTC.* That sentence is the point of the arrangement: a member
handed nothing but UTC concludes it lives in UTC, and then every *tomorrow* it
writes lands a day off.

Headings a model reads carry the same local reading: a history entry's
`## 2026-08-16 13:22:04 PDT (UTC-07:00) from acme:gerry`, the day log's
`## <local> dispatch N message(s) -> member` line, the turn-capture meta
block, and the sandbox report header all stamp with the machine's zone. The
parenthetical offset is the reversible instant — an abbreviation alone cannot
be turned back into UTC on a machine in another zone, and the sandbox's
conversation table sorts by that instant. What sorts,
names a file, or otherwise round-trips through machinery stays UTC instead:
the day log's filename, the retention window, and a turn capture's `stamp`
identifier are all UTC sort keys. `prune_day_logs` compares day-log filenames
as strings, and a portable org whose log directory is shared between a Mac, a
Windows seat and a Linux VM needs every machine to name the same wall moment
the same file. So `r4t logs` prints a day header saying both — `— log day
2026-08-16 UTC (this machine reads PDT)` — rather than letting one stand for
the other near midnight.

The zone is the machine's, which means `TZ` is the only knob. A caged member is
the case that needs it: `isolate.py` runs the turn in a container, and a
container boots UTC. Put `"env": {"TZ": "America/Los_Angeles"}` on the rig and
the turn's whole environment is corrected, the member's own tools included —
see [r4t-rigs.md](r4t-rigs.md). There is no r4t-specific zone field, and there
will not be one until `TZ` demonstrably fails.

## Speaking as another member: `r4t tell --as`

```bash
r4t tell --as gerry "go check the build" # as gerry, to the leader (self)
r4t tell --as gerry --to phil "ship it"  # as gerry, to phil
```

Routes through the same ingest path a real member-to-member send takes —
enqueues, threads, and narrates the ticker (`r4t: QUEUED ...`) exactly like
any other arrival, stamped `from` the impersonated member. Use it to jumpstart
a member's queue without waiting on a real sender, or to diagnose where a
message lands. `--as` and `--to` must each name a roster member; an unknown
name is refused.

It is the scriptable surface too: an orchestrating agent speaks into the
roster with it, watches with `r4t logs -f`, and speaks again. Mail that has to
cross the wall — to you, to a phone, to another node — is a8s's job, and a
member sends it with the ordinary `tell` under the org's `egress` setting.
