# The verification round

An agent cannot be the judge of its own deliverable, so the judge is
machinery it cannot see into. Design history and the incident that drove it:
[../plans/history/VERIFY-SPEC.md](../plans/history/VERIFY-SPEC.md).

## `r4t check`

`r4t check <node>` sweeps the tracked files in the node's workplace for
forbidden patterns and prints exactly `check passed` or
`check failed: N finding(s)` — nothing else.

The findings (which file, which line, which pattern) go to stderr, the
surface only the human reads; the agent gets an opaque verdict it cannot
game.

## Checklists

Patterns are one Python regex per line in
`~/.config/r4t/checklists/default.txt` (every node) and
`~/.config/r4t/checklists/<node>.txt` (per-node additions); `#` comments and
blank lines are ignored, and no checklist at all is a pass.

These files live outside every repo, uncommitted, because they may carry
private strings like a codename (e.g. `secret-codename`) or a name.

## Gating the doorbell

Set `doorbell_check` in `r4t-org.json` (see [org.md](org.md#org-settings)) to
run any command — the sweep or a test suite — before the org may ring an
absent human, and a failing check parks the message without ringing rather
than losing it.

## The post-hoc judge

How best to ask a judge is an open question the field is actively
arguing — published work reports pairwise comparison more reliable than
absolute scores, and persona-anchored evaluation of uncertain benefit —
so the judging shape here (yes/no flags, persona anchoring proposed) is
held as a hypothesis: the experiment ladder's E5 rung tests it before it
hardens into doctrine.

`r4t check` and the doorbell gate act on a live run; the judge is the third
leg — it grades a finished run. `r4t judge <node> --rig <rig>` reads a
completed run's recorded transcripts and scores them against the MAST
multi-agent failure taxonomy ("Why Do Multi-Agent LLM Systems Fail?",
arXiv:2503.13657), plus one r4t extension mode for mutual-wait deadlock, a
failure MAST has no single mode for.

It is post-hoc and out-of-band by design: a graded org changes behavior, and
an agent that could read its own grade would learn to game it. Reports land
under the team dir's `judge/` — a surface no roster agent ever reads — never
inside the workplace repo. Pass `--json` instead of the sectioned panel to
derive an experiment-ledger column.

## Tracing one task

The judge grades a run; `r4t task trace <id>` reconstructs a single task. It
answers "what actually happened here?" in one screen: the delegation tree, hop
by hop — who received the task, who they passed it to, what came back — plus
every turn the thread cost with its exit code and duration, dead letters
inline, and whatever is still in flight.

```
Delegation
  boss -> gerry        hop 0  "ship the parser by friday"
    gerry -> phil      hop 1  "take the tokenizer and land it behind the flag"
      phil -> neil     hop 2
      phil -> gerry    hop 2  "tokenizer landed, PR is up"
        gerry -> boss  hop 3  (out of the walls)  (closes the thread)
```

Nothing new is written for it, and no new transport is involved: the whole
trace is read back out of state the team already keeps. The day log is the
spine — append-only, never pruned, and every delivery and turn boundary lands
in it carrying the thread id — while the thread ledger, the dead-letter dir,
the members' queues and any in-flight turn supply the originator, what never
got delivered, and what is still moving. A thread whose ledger has already
expired still traces: the panel says so, and reads the originator and the
closure back out of the log.

`--json` gives the same reconstruction as a structure, so an acceptance check
can assert on trace shape (`delegation`, `turns`, `dead_letters`) instead of
grepping logs.
