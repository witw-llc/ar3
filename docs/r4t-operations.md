# Operating a roster: status, logs, chat, and the seat

The surfaces for watching and talking to a running roster — one per way of
looking. Setup is in the [tutorial](r4t-tutorial.md); this page assumes a
registered roster.

## The four surfaces

- `r4t status` — the snapshot. Leads with plain-English health verdicts
  (waiting on you? runaway? member broken or resting with work queued? roster
  budget spent? a queue backing up?), then member budgets, queue depths,
  open threads, and dead letters rolled up by meaning.
- `r4t logs -f` — the stream. The roster's own event log: every governance
  decision and turn boundary, including walled-garden traffic that never
  reaches a8s. `--full` includes prompts and transcripts. `--agent <member>`
  narrows the stream to one member; with `--full` it prints that member's
  captured turns (each turn's full prompt and raw output, newest last).
- `r4t chat` — the human, interactively (below).
- `r4t seat` — an orchestrating agent, programmatically (below).

These four watch a roster as it works. For one task after the fact — who told
whom, and what each hop cost — see
[`r4t task trace`](r4t-verification.md#tracing-one-task).

The first dispatch stamps the repo root into roster state, so `--node` works
from any directory — and from inside a roster repo the `--node` flag itself
is optional. (`a8s logs <node> -f` still shows the cross-wall view.)

## What the roster keeps, and for how long

Everything above reads out of `~/.config/r4t/rosters/<node>/`, which grows with
every turn. Maintenance passes (`r4t idle` — one
[idle pass](r4t-idle.md) — and `r4t clear` on its own) hold
it to a shape a roster can run for months on:

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
- **Dead letters** wait for a human. Nothing prunes them.

The retention window is a governance knob in the rig config, alongside the
budgets and the breaker: see the table in [r4t-rigs.md](r4t-rigs.md#governance-knobs).

## The seat: being the human in the roster

A human roster member is a first-class roster address: members just
`tell neil`, and messages park in the node's seat mailbox under
`~/.config/r4t/rosters/<node>/seat/` — no handler, no router, nothing to
disconnect. When the seat is unattended dispatch rings the `Address:`
doorbell (a copy forwarded over a8s to the human's phone); a reply from that
Address is the human speaking, so it re-enters through the seat path and
routes like a chat send. The doorbell forward is body-only — attachments wait
in the seat mailbox, where `r4t seat inbox` prints their stored paths. Outsiders other than the human do not reach the
seat directly — like all external traffic they enter through the top lead.
Two surfaces read and speak for it:

```bash
r4t seat                    # summary: unread count, attached, doorbell
r4t seat inbox              # read parked messages (marks them read; --peek, --json)
r4t seat send "message"     # speak as the human — to the leader
r4t seat send --to phil "…" # or to a member (runs their turn synchronously)
```

`r4t seat` is the scriptable surface — an orchestrating agent impersonates
the human with it directly. `r4t chat` is the human view over the same
mailbox, and the roster's **control plane**: a full-screen TUI with a health
header fed by the same verdict engine as `r4t status`, a clickable member
status panel (who is active, resting, or broken and how deep each queue is),
a conversation pane beside a fly-on-the-wall activity pane, and an input line
(`/to`, `/attach`, `/detach`, `/who`, `/threads`, `/help`, `/quit`). The TUI
needs [textual](https://textual.textualize.io/) (`python3 -m pip install
textual`); without it — or with `--plain`, or piped — chat falls back to
a line UI over the same feed. While chat (or anything touching the
presence file) is attached, dispatch skips the `Address:` doorbell;
detach and the doorbell rings again.

## Gemba attach — walk the floor, read-only

Click a member or type `/attach vela` (`r4t chat --attach vela` to open
straight into it) for a live view of one member: every message it receives as
it is enqueued, and its turn output streaming as it comes out (teed to
`agents/<member>/live.log`, truncated at each turn start). For the record
after the fact, every turn is also captured whole under
`agents/<member>/turns/` — one markdown file per turn (prompt + raw output,
successes and timeouts alike, most recent 50 kept), surfaced by
`r4t logs --agent <member> --full`. What each wake cost is measured at
composition: every dispatched turn writes a `r4t: PROMPT <member> <path>
<total> — <section> <bytes> …` line to the day log (path is founding, continue,
refound, or echo) and the same breakdown onto the capture's `- prompt:` meta
line, so a mission that rides every calendar reminder shows up as a number, not
an archaeology dig. Attaching is observation only — it never
sends to that member; the composer keeps talking to the seat's usual
counterparties. `/detach` steps back out. Training wheels, not a replacement
for autonomy — everything still flows through normal dispatch and governance.
