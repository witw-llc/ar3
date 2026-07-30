# Org design: the tree, the mission, and portable orgs

How a roster grows structure: cells and leads, the mission file, org
directories that live outside the repo, and the org-level settings that
travel with them. Rationale and prior art: [r4t-governance.md](r4t-governance.md)
§8–9.

## The tree: cells, leads, and information hiding

A roster is a tree of small **cells**, not a flat pool of peers. Give each AI
member a `Cell:` line (its cell) and a `Lead:` line (the member it reports
to); the top lead reports to the human:

```markdown
### Cass
- **Rig:** specialist
- **Cell:** design
- **Lead:** Vela
```

Once any `Lead:` line is present the tree becomes structural, not advisory:

- **Information hiding.** A member's turn prompt lists only its tree-adjacent
  names — its lead, its direct reports, its cell-mates — plus the human seat.
  It never sees the whole roster, so lateral contact is not advertised.
- **Delivery follows the `comms` setting** (org-level, default `open`). In
  `open` a tell to any valid roster member delivers — a learned address works
  even though the prompt did not list it (info hiding stays at the prompt
  level, softening the tree tax). In `closed` a tell outside a member's
  adjacency is rerouted to its lead (`[r4t rerouted: Ann -> Cal] …`, logged
  `REROUTED`) — the military model. In both, replies to whoever messaged you
  this turn and anything to the human seat always get through. Set it in
  `r4t-org.json` (below).

`r4t roster check` lints the shape: every `Lead:` must name a real member,
exactly one member is the leader, a cell warns past 6 AI members and errors
past 10, and a tree deeper than 2 levels below the top lead warns (the
span-of-control bounds from the org research). A roster with **no** `Lead:`
lines is a flat roster — one cell under the leader — and none of this applies.

Why: in the first live run a full-roster prompt let a build-cell lead message
a design-cell lead laterally *because the name was in front of him*. The tree
held voluntarily, but voluntary is not a control — information hiding removes
the temptation, rerouting removes the option. See
[r4t-governance.md](r4t-governance.md) §8 for the evidence.

## The mission file

Drop a `MISSION.md` at the repo root and it becomes the roster's north star: a
short, **human-owned** page stating *why* the repo exists and what "done"
looks like — purpose, end state, and the current milestone, never the *how*.
It outranks every other document in the repo; where anything conflicts with
it, it wins.

Injection is **leads-only**. Every member with direct reports gets the file
verbatim at the top of each turn prompt, under a section labelled *"The mission
(MISSION.md — outranks every other document)"*. ICs never see it injected —
their lead restates the relevant intent as ordinary messages, at the
resolution the receiver can hold. Intent flows edge-by-edge down the tree,
restated at every hop: "who gets the mission" has the same answer as "who
reports to whom". (A flat roster with no `Lead:` lines treats the marked
leader as the only lead. Any member with tools can of course open the file
itself; there is no machinery for that.)

This follows commander's-intent doctrine (US Army ADP 6-0): intent is the
purpose and desired end state, not a plan, restated down the chain so each
level can act on its own when the plan meets reality. When the file changes —
which should happen only at milestone boundaries — the briefback ritual
applies: the top lead's next turn restates the intent in their own words to
the human and waits for correction before work resumes. That ritual is social
convention, not machinery: r4t injects the file and lints its length, nothing
more. `r4t roster check` warns when `MISSION.md` runs past ~40 lines, because
intent that no longer fits a page has usually gone stale into planning.

## The mission-review idle pass

When an org goes fully quiet — every queue empty, no open thread — but the
mission may not be met, the idle pass hands the topmost leader a budget-gated
**mission-review** turn to reweigh the mission and delegate the next step
(cadence is the a8s `idle.timeout` with a widening backoff; three silent
reviews go dormant until a real message or a `MISSION.md` change re-arms it).
The nudge never asks the leader to report to the human. Prompt text — the turn
framing, doctrine bullets, and both nudges — is overridable per key under a
`prompts` object in the a8s node definition (defaults live in `dispatch.py`);
the definition reaches r4t via `--definition $DEFINITION_PATH`.

## Portable orgs

By default `ROSTER.md` and `MISSION.md` live in the repo — the slow furnace a
proven structure graduates into. When you want to keep the org OUT of the repo
(to A/B two casts against the same project, or to iterate on a roster without
touching the codebase), make an **org directory**: put `ROSTER.md` +
`MISSION.md` there alongside an `r4t-org.json` naming the workplace repo.

```json
{ "repo": "/path/to/the/repo" }
```

Register the a8s node at the org dir; turns run and commit in `repo`, while the
roster, mission, and mission injection read from the org dir. Org-to-repo is
many-to-one: two org dirs (same `MISSION.md`, different `ROSTER.md`) can point
at two clones of one project without their roster state colliding — state is
per-a8s-node, not per-repo. `r4t roster check --root <org-dir>` lints the org,
including a malformed `r4t-org.json`, a bad setting value, or a workplace repo
that does not exist. **Graduation is trivial:** copy the two files into the repo
and delete `r4t-org.json` — resolution falls back to the in-repo default with no
other change.

## Org settings

`r4t-org.json` also carries org-level settings that travel with the org, not the
machine. They are optional and may be the *only* thing the file holds — a
config without a `repo` key is an in-repo org that just wants settings.

| Key | Default | Governs |
|---|---|---|
| `comms` | `open` | `open` delivers a tell to any valid member (learned addresses); `closed` reroutes non-adjacent tells through the sender's lead |
| `leader_sees_lateral` | `false` | when `true`, a lateral (peer) delivery lands a read-only copy in the lead's history — no turn burned |
| `egress` | `true` | only the topmost leader may message outside the garden; a non-top member's external tell redirects up to it. `false` keeps the org silent outward. Bind the org's namespace `--opaque` and what leaves is stamped with the bare prefix — one name outside the walls; the default binding keeps member attribution (see [r4t-message-flow.md](r4t-message-flow.md)) |
| `doorbell_check` | *absent* | a shell command that gates every ring of an absent human's doorbell (see [r4t-verification.md](r4t-verification.md)); absent or empty means no gate |
| `run_as` | *absent* | OS-level isolation: wrap every member turn in `sudo -u <username>`. One user serves the whole roster (see [r4t-isolation.md](r4t-isolation.md)) |
| `container` | *absent* | OS-level isolation: run every member turn under `docker run --rm <image>`; mutually exclusive with `run_as` (see [r4t-isolation.md](r4t-isolation.md)) |
| `container_args` | *absent* | extra `docker run` args appended verbatim (credential mounts, `--network`); needs `container` |
