# Experiment protocol — template

*Every r4t experiment fills this in **before** launch. The point (issue
[#187](https://github.com/neilobremski/bin/issues/187)) is that a filled-in
copy is enough for a non-Fable harness — a subagent, a cheaper model, or a
different runner — to execute the whole experiment mechanically, escalating
to the human only for the things the table below says to escalate.*

*Copy this file into the experiment's own directory (e.g.
`experiments/<name>/PROTOCOL.md`), fill every section, and do not launch
until every `<...>` placeholder is replaced. The
n5a retrospective (private wiki archive) is the worked example of why each section
exists.*

---

## 0. Identity

- **Name:** `<slug>`
- **Owner:** `<who blesses / makes off-table calls>` (default: Neil)
- **Runner:** `<harness or model actually executing the schedule>`
- **Launch time (declared before start):** `<YYYY-MM-DD HH:MM TZ>`

## 1. Hypothesis

- **The one question:** `<a single sentence the experiment answers>`
- **The variable:** `<the ONE thing that differs between conditions>`
- **Held constant:** `<everything else — roster, mission, rigs, repo, seat>`
- **What confirms it:** `<observable result that supports the hypothesis>`
- **What falsifies it:** `<observable result that kills it>` — state this
  concretely enough that the runner can recognize it without judgment.
- **Primary measurement:** `<the one thing the owner will read/judge>`
- **Secondary measurements:** `<cost, wall-clock, turns — from r4t logs>`

If you cannot write the falsifier as something the runner can *see*, the
experiment is not ready to launch.

## 2. Setup runbook

Exact, copy-pasteable commands. No judgment calls — if a step needs a
decision, that decision belongs in Section 1, not here.

```bash
# Rigs (per-rig model where the preset supports it — see #186)
r4t rig <rigname> <preset> [--model <name>] ...

# Org(s) — one block per condition. Portable orgs: config points at a repo.
# Seed the identical MISSION.md and the condition-specific ROSTER.md.
cp experiments/<name>/MISSION.md   ~/.config/r4t/orgs/<node>/MISSION.md
cp experiments/<name>/<cond>/ROSTER.md ~/.config/r4t/orgs/<node>/ROSTER.md
# ... repo pointer config (r4t-org.json), definition.json, register node ...

# Genesis: each condition gets its own clone of the SAME empty repo, so the
# runs never share state. Record the genesis commit hash here: <hash>

# Kickoff — as simultaneous as the seat allows; record the wall-clock stamp.
r4t seat send <node> <lead> "<kickoff text, identical across conditions>"
```

- **Isolation check:** confirm no two conditions share a process, queue, or
  budget bucket before kickoff.
- **Hawthorne check:** MISSION.md and every ROSTER.md read as a real brief;
  nothing names the experiment, the other condition, or the variable under
  test.

## 3. Observation schedule

What to check, how often, with **exact read-only commands**. All observation
is non-invasive.

**Non-invasive rules (hard):**

- **Never `git checkout` / `git switch` / write in a live team repo.** Read
  committed state only, via `git -C <repo> show <ref>:<path>` and
  `git -C <repo> log --oneline`. A checkout mutates the working tree a live
  member may be writing into.
- Never message a member except via the intervention table (Section 4).
- Never edit dispatch code, org files, or budgets mid-run (the freeze rule).
- Reading the org's own logs is fine; observation must not open a channel.

**Cadence:** every `<N>` minutes, record one row in the metrics ledger and
run this sweep:

```bash
# Health verdict + queues + budgets, per node
r4t status --node <node>

# Progress on the artifact (committed state only — never checkout)
git -C <repo> log --oneline
git -C <repo> ls-tree -r --name-only <branch>
git -C <repo> show <branch>:<artifact-path> | wc -w   # size vs any cap

# Routing / storm signals from the org's own log
grep -c "REROUTED" ~/.config/r4t/teams/<node>/log/<UTC-date>.md
r4t logs <node>            # compact event stream

# Cost
r4t logs <node> --full     # turns per member, budget exhaustions
```

## 4. Intervention decision table

Pre-written: condition → exact action or message text. If a situation is not
on this table, **do nothing and escalate to the owner.** The runner never
improvises a nudge.

| Condition (observed) | Exact action |
| --- | --- |
| A milestone gate is reached and the artifact meets the gate's written criteria | Escalate to owner for blessing — **the runner never blesses.** |
| A member consumed messages and ended its turn without replying or committing (stall) | Seat-send that member exactly: `Waiting is not a plan. State what you are doing next and commit it, or say what you are blocked on.` |
| An org is idle > `<T>` min with the mission unmet and all threads closed | Seat-send the top lead exactly: `The mission is not met and nothing is in flight. Re-read the mission, decide the next step, and delegate it.` |
| A member self-certifies a blessing (claims the owner blessed something the owner did not) | Seat-send that member exactly: `Only the owner blesses. Retract that claim; the gate is not passed until the owner says so.` and log it. |
| The artifact crosses a hard written constraint (e.g. size cap) | See Section 5 — this is a **stop condition**, not a live cut directive. |
| Anything not listed above | Do nothing. Record it in the ledger and escalate to the owner. |

Fill `<T>` and any thresholds with concrete numbers before launch.

## 5. Stop conditions

**The wall-clock ceiling is mandatory and comes first.** Every experiment
declares a wall-clock ceiling before launch; when it expires, the experiment
stops and the retro happens with whatever exists. It is the simplest guard
against overrun and it **outranks the budget and output-based stops below** —
if the clock runs out, stop, regardless of any other state.

- **Wall-clock ceiling (required):** `<duration, e.g. 3h from launch>`. On
  expiry: stop all nodes (`a8s stop <node>`), leave state intact, write the
  retro. (n5a's lesson: it had *no* time box and ran a full day and evening;
  a "one sitting" mission implies a box of a few hours.)
- **Budget ceiling:** stop if the run exceeds `<X>` of the weekly/team budget
  (n5a burned ~15% with a frontier model driving free-form — cap this).
- **Out-of-control criteria:** stop if the artifact overshoots a hard written
  constraint beyond `<factor>` (n5a hit ~2.4x the word cap and should have
  stopped there, not been handed a live cut directive).
- **Falsifier reached:** if the Section 1 falsifier is observed, stop — the
  question is answered.

On any stop: `a8s stop <node>` for every node, leave queues/repos/org dirs
intact (restartable if the retro wants more data), then write the retro.

## 6. Metrics ledger

Recorded in the experiment's `notebook.md` as the run happens, so the retro
writes itself. One row per observation sweep (Section 3), plus event rows for
anything on the intervention table.

**Schema (per sweep):**

| field | meaning |
| --- | --- |
| `t` | wall-clock stamp of the observation |
| `node` | which condition |
| `verdict` | `r4t status` health verdict (resting / running / stalled / ...) |
| `commits` | commit count on the tracked branch |
| `artifact` | primary size metric (e.g. words) vs. cap |
| `reroutes` | REROUTED event count so far (routing/tree-tax signal) |
| `turns` | turns consumed (cost) |
| `budget` | budget burn / exhaustions since last row |
| `note` | anything notable, interventions, escalations |

**Also record, once:** genesis commit hash per condition, exact kickoff
stamps, the declared wall-clock ceiling and budget ceiling, and every
off-table escalation with the owner's ruling.

At stop time, the ledger plus the committed artifacts are the whole retro
input — the findings ledger and verdict come straight out of these rows.
