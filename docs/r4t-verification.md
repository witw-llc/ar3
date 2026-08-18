# The verification round

An agent cannot be the judge of its own deliverable, so the judge is
machinery it cannot see into. Design history and the incident that drove it
live in the wiki's Plans archive (VERIFY-SPEC).

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

## The post-hoc judge

How best to ask a judge is an open question the field is actively
arguing — published work reports pairwise comparison more reliable than
absolute scores, and persona-anchored evaluation of uncertain benefit —
so the judging shape here (yes/no flags, persona anchoring proposed) is
held as a hypothesis: the experiment ladder's E5 rung tests it before it
hardens into doctrine.

`r4t check` acts on a live run; the judge is the other leg — it grades a
finished run. `r4t judge <node> --rig <rig>` reads a
completed run's recorded transcripts and scores them against the MAST
multi-agent failure taxonomy ("Why Do Multi-Agent LLM Systems Fail?",
arXiv:2503.13657), plus one r4t extension mode for mutual-wait deadlock, a
failure MAST has no single mode for.

It is post-hoc and out-of-band by design: a graded org changes behavior, and
an agent that could read its own grade would learn to game it. Reports land
under the roster dir's `judge/` — a surface no roster agent ever reads — never
inside the workplace repo. Pass `--json` instead of the sectioned panel to
derive an experiment-ledger column.

## Reading a run back

The day log is the spine of everything after the fact: append-only, kept for
`log_retention_days`, with every delivery and turn boundary carrying the
thread id that labels the message lineage. `r4t logs --agent <member>` narrows
it to one member and `--full` prints the captured turns; the dead-letter dir
holds what never got delivered. Grepping one thread id across the day log is
how a single chain of messages reads back, hop by hop.
