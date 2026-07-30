# Getting started with r4t

This tutorial walks a new developer through their first roster on a fresh
machine: what the two config files mean, how to define rigs, and
what happens when the roster and rig config disagree.

For governance rationale see [r4t-governance.md](r4t-governance.md). For the knob
table see [r4t-rigs.md](r4t-rigs.md#governance-knobs).

## Two files, two jobs

| File | Where | What it defines |
|------|-------|-----------------|
| **`ROSTER.md`** | In the roster repo | *Who* is on the roster and which **symbolic rig** each AI member uses |
| **`~/.config/r4t/rigs.json`** | Out of repo (`R4T_HOME`) | What each rig **actually runs** — CLI argv, timeouts, budgets |

The roster never contains shell commands. A line like `Rig: opencode` is
wrong — `opencode` is a CLI, not a rig name. You write `Rig: worker`
and define `worker` in the rig config.

This split is deliberate: the in-repo roster cannot smuggle in arbitrary
commands. Only rigs declared in the out-of-repo rig config can execute.

## Fresh machine walkthrough

### 1. See where you stand

```bash
r4t
```

With no arguments, r4t prints local status: `R4T_HOME`, rig config path,
configured rigs, registered rosters, whether the current directory has a
roster, available commands, and suggested next steps.

### 2. Bootstrap the repo and rig config

```bash
cd ~/my-roster-repo
r4t init
```

`r4t init` writes (if missing):

- **`ROSTER.md`** — a Human owner, an AI Lead on rig `leader`, an AI Dev on
  rig `member`
- **`~/.config/r4t/rigs.json`** — matching `leader` and `member` rig
  definitions (default invoke: `opencode run --auto --dir {workdir}`)

It then prints the exact **a8s registration** sequence for your repo name:

```bash
a8s add myrepo-node /path/to/my-roster-repo r4t
a8s namespace myrepo myrepo-node
a8s start myrepo-node
tell myrepo-node "hello"       # bare agent name -> roster leader
tell myrepo "hello"            # bare namespace prefix -> roster leader
```

Run those commands (adjust paths and names) before expecting live traffic.

### 3. Add rigs

Rig **names** are yours (`leader`, `member`, `reviewer`, `worker`, …).
**Presets** are CLI templates aligned with [a8s definitions](../apps/a8s/definitions/)
(`claude`, `cursor`, `opencode`, `agy`, `codex`, `copilot`).

```bash
r4t rig presets                              # list presets + invoke lines
r4t rig add reviewer claude                # add rig "reviewer" from preset
r4t rig add worker opencode
r4t rig add local opencode-ollama --model qwen2.5-coder:7b
r4t rig add lead cursor --force            # replace an existing rig
r4t rig swap lead agy                      # switch preset, keep settings
```

Each preset documents its **headless** entry point (`-p`, `--print`, `run
--auto`, etc.) so r4t turns never open an interactive session.

### 4. Wire the roster to those rigs

Edit `ROSTER.md`. Each AI member needs a `Rig:` line naming a rig that
exists in `rigs.json`:

```markdown
### Reviewer
- **Rig:** reviewer
- **Role:** Code reviewer
```

AI is the default and carries no marker. The human seat is marked
`Human: yes` and must not carry a rig. Optional `Address:` is their a8s
name for outbound tells.

### 5. Lint before going live

```bash
r4t roster check      # roster shape + every Rig line resolves to a rig
r4t rig ls        # rigs, limits, and roster resolution (--wide adds invokes)
```

Fix anything `roster check` reports before registering the roster or sending
work.

### 6. Operate

```bash
r4t status --node myrepo    # budgets, queues, threads, dead letters
a8s logs myrepo-node -f     # traffic + r4t governance lines
```

### 7. (Optional) point the roster with a mission

Drop a short, human-owned `MISSION.md` at the repo root — why the repo exists,
what "done" looks like, the current milestone; never the how. r4t injects it
into every **lead's** turn prompt (members with reports); leads restate it
down to their ICs. Keep it under a page — `roster check` warns past ~40 lines.

### 8. (Optional) keep the org outside the repo

`ROSTER.md` and `MISSION.md` default to the repo, but you can put them in an
**org directory** instead — add an `r4t-org.json` there naming the workplace
repo (`{ "repo": "/path/to/repo" }`) and register the a8s node at the org dir.
Turns run in the repo; the roster and mission read from the org dir. Two org
dirs can point at two clones of one project (same mission, different rosters)
without their state colliding. See [r4t-org.md](r4t-org.md#portable-orgs);
graduate by copying the two files into the repo and deleting `r4t-org.json`.

## Mental model

```
ROSTER.md                      rigs.json
───────────                    ────────────────
Lead   → Rig: leader    →  "leader":  { invoke: [...] }
Dev    → Rig: member    →  "member":  { invoke: [...] }
Reviewer → Rig: reviewer → "reviewer": { ... }   ← you must add this
```

**Preset names** (`claude`, `opencode`, …) populate rig definitions via
`r4t rig add`. **Rig names** (`leader`, `reviewer`, …) are what the
roster references.

Optional **pins** in `rigs.json` override a member's roster rig
silently — an in-repo roster edit cannot upgrade a pinned agent:

```json
"pins": { "gerry": "leader" }
```

## Missing rig? No default — fail closed

There is **no fallback harness**. If `ROSTER.md` names a rig that is not
defined in `rigs.json`, that member **does not run**.

### At check time

```bash
$ r4t roster check
Reviewer: rig 'reviewer' not found in /Users/you/.config/r4t/rigs.json (fail closed) — try: r4t rig add reviewer <preset>
1 problem(s)
```

Exit code 1.

### At runtime

When a message targets that member, r4t tells the **sender** and never
spawns the harness:

```
Reviewer cannot run: rig 'reviewer' not found in /Users/you/.config/r4t/rigs.json (fail closed) — try: r4t rig add reviewer <preset>
```

The same applies when the rig config file is entirely missing:

```
Dev cannot run: rig config not found at ~/.config/r4t/rigs.json — rig 'member' cannot be resolved (fail closed)
```

### What exists by default

Only what `r4t init` creates: **`leader`** and **`member`** rigs, wired to
the starter roster. Any other rig name must be added explicitly:

```bash
r4t rig add junior-dev opencode
```

## How a message flows

`tell myrepo "..."` routes through a8s to the roster node, enters at the
roster leader, and r4t runs the rig's CLI with a prompt; replies stage via
ordinary `tell` and release after the turn. Full walk-through:
[r4t-message-flow.md](r4t-message-flow.md).

## Command reference

| Command | Purpose |
|---------|---------|
| `r4t` | Local status, harness summary, next steps |
| `r4t init` | Starter `ROSTER.md` + `~/.config/r4t/rigs.json` |
| `r4t rig presets` | Named CLI templates (from a8s definitions) |
| `r4t rig add <rig> <preset>` | Define a rig in the rig config |
| `r4t rig list` (alias `ls`) | Show rigs and how roster members resolve (`--wide` for invoke lines) |
| `r4t roster check` | Lint roster and rig mappings |
| `r4t status --node <roster>` | Member budgets, queue depths, threads, dead letters |
| `r4t sandbox --fake` | End-to-end plumbing test without LLM calls |
| `r4t sandbox --preset opencode-ollama --model M` | Live sandbox via local Ollama + OpenCode (stderr progress, report on stdout) |

## Example: existing repo

```bash
r4t init --root ~/repos/acme           # keeps an existing ROSTER.md if present
r4t rig add junior-dev opencode   # if roster references junior-dev
r4t roster check
a8s add acme-node ~/repos/acme r4t
a8s namespace acme acme-node
a8s start acme-node
tell acme "Ship the refactor; report when reviewed."
```

Watch: `a8s logs acme-node -f`, `r4t status --node acme`, and dead letters
under `~/.config/r4t/rosters/acme/dead-letter/`.
