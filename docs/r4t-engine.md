# Engine — talking to a CLI directly, with no roster

`r4t engine` is the layer below the roster: one CLI, no `ROSTER.md`, no
dispatcher, no budgets. Two verbs — `quota` asks what a subscription has left
without spending a turn; `run` spends one. Both accept an engine id or any rig
preset id (`r4t engine list` shows which presets serve which engine, and
which verbs each engine answers).

## `run` — one headless turn as a bare stateless agent

```bash
r4t engine claude run --agent my-node "check the deploy and report"
r4t engine codex run --dir ~/work/proj --model o4 --timeout 600 "fix the lint errors"
echo "long prompt piped in" | r4t engine agy run -
```

```
r4t engine <id> run [--dir DIR] [--model M] [--agent NAME] [--timeout S]
                     [--no-scaffold] [--idle] [--echo] [--lessons-cap N]
                     [--] PROMPT
```

Supported engines: `claude`, `codex`, `agy`, `copilot`, `cursor`, `opencode`,
and the `ollama-claude` / `ollama-codex` / `ollama-opencode` local launchers —
the ones whose headless, unattended invocation is verified (an unsupported id
is a clear error naming this set). The `ollama-*` engines run local models
through `ollama launch`: no cloud quota spent, but each needs `--model` (an
ollama tag) since the launcher has no default. The bare `ollama` preset is
not run-capable — `ollama run` has no file tools, and the scaffold below
needs them. `ollama-copilot` is excluded too: every file write it makes lands
in copilot's session-state mirror rather than the real working directory, so
it cannot honor the scaffold's contract (cloud `copilot` is unaffected — see
[r4t-harness-ollama-launch.md](r4t-harness-ollama-launch.md)). Argv
composition rides `rig.build_preset_invoke` — the same preset table `r4t rig
add` reads — plus what an unattended, roster-less turn needs on top: agy gets
an explicit `--print-timeout` matching `--timeout` (its own default silently
undercuts a longer one), and copilot gets `--no-ask-user` if the preset does
not already carry it (unattended, it otherwise hangs on its `ask_user`
tool). No permission-bypass flags are added beyond what the preset itself
already chooses.

- `PROMPT` is one positional string; `-` reads it from stdin.
- `--dir DIR` — the turn's working directory (default: CWD).
- `--model M` — appended in the preset's own flag pattern; a preset with no
  model flag (copilot) or no live resolver (agy needs one — `agy models` is
  queried fresh every run) errors the same way `r4t rig add --model` does.
- `--timeout S` — default 900. On expiry the whole process group is killed
  (a harness CLI forks tool subprocesses `kill()` alone would leak) and the
  command exits 124, naming the timeout.
- `--echo` — before spawning, print the composed argv and the exact prompt
  (scaffold prelude included) to stderr; the turn still runs, stdout still
  carries only the engine's own reply stream.
- `--lessons-cap N` — the `LESSONS.md` rotation line cap for this turn
  (default 200); see below.
- Exit code is the CLI's own (124 on a timeout kill); stdout/stderr stream
  through unchanged.

### Why bare agents need a scaffold

A roster member gets continuity from r4t: budgets, a dispatcher, a ROSTER.md
persona. A bare engine node has none of that — every wake is a fresh CLI
session with zero transcript memory, so whatever it knew has to already be
sitting in a file. `run` supplies the reading and writing discipline that
makes that work: `STATUS.md` and `LESSONS.md`, read on the way in and
rewritten/appended on the way out, are the entire memory.

### The scaffold (default on; `--no-scaffold` sends `PROMPT` untouched)

Prepended to `PROMPT`, with the fixed prelude byte-identical across runs in
the same `--dir` (no timestamps, no counters) so the prompt cache never
misses on anything but the routed input itself, which always comes last:

```
Smart cold boot:
1. Read <DIR>/STATUS.md, then <DIR>/AGENTS.md and <DIR>/LESSONS.md if
   present. Use these absolute paths even if your workspace root differs.
   They are the durable source of truth; you have no transcript memory.
2. Run `a8s convo NAME` and reconcile the newest routed messages with
   STATUS.md before acting.          [only with --agent NAME; steps renumber
                                       cleanly without it]
3. Stay idle and exit unless there is clear direction or active work. Never
   restart completed work. Be token-frugal; no wordy prose.
4. Before exit, rewrite <DIR>/STATUS.md with sections: Current State,
   Important Context, Next Steps, Decisions (with rationale). Append
   genuinely new durable insights to <DIR>/LESSONS.md — append-only, one
   short bullet each, never rewrite or delete existing lessons. Never edit
   AGENTS.md.

Routed input:
<PROMPT>
```

If `LESSONS.md` exists and is strictly over the line cap (`--lessons-cap`,
default 200) when `run` starts, the oldest lines rotate out to
`LESSONS-ARCHIVE.md` (created if absent, appended to in order) so the live
file lands at exactly the cap — whole lines only, nothing deleted, no model
ever touches either file. `run` prints one stderr line naming what moved:
`r4t engine: rotated N lines from <DIR>/LESSONS.md to <DIR>/LESSONS-ARCHIVE.md`.

r4t's own dispatcher (`dispatch.run_harness`) never uses this scaffold: it
already builds the roster's own prompt, and stacking this one on top would
double it. This is strictly the bare, roster-less path.

### `--idle` — the Cody-pattern debounce

A bare node commonly wakes on a timer with nothing new to do. `--idle` skips
the wasted turn: if `<DIR>/.engine-idle` exists, `run` exits 0 printing
nothing. Otherwise it creates the marker and runs one turn — the routed
input is `PROMPT` if given, else the built-in idle consolidation prompt
("reconcile STATUS.md with reality, then append any new durable lessons").
Any run *without* `--idle` removes the marker first — real work re-arms the
latch, so the next quiet tick gets exactly one consolidation pass again.

## a8s integration

`apps/a8s/definitions/engine-claude.json` wakes a bare node into `r4t engine
claude run --agent $RECIPIENT $MESSAGE`, cwd already the node's own root (an
a8s wake sets it, matching `run`'s `--dir`-less default). To point another
supported engine at the same pattern, copy the file and change `"claude"` to
`codex`, `agy`, `copilot`, `cursor`, `opencode`, or one of the `ollama-*`
launchers:

```bash
cp apps/a8s/definitions/engine-claude.json apps/a8s/definitions/engine-codex.json
# edit the "claude" in "invoke" to "codex"
a8s add my-bare-node ~/somewhere engine-codex
```

## `quota` — remaining subscription, no turn spent

```bash
r4t engine list           # every engine, and the presets each one serves
r4t engine codex quota    # remaining fraction + reset time
r4t engine claude quota --json
```

One component per engine under `apps/r4t/engines/`; live answers persist as
snapshots that still answer, age-stamped, when the live check cannot.
