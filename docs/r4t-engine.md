# Engine — talking to a CLI directly, with no roster

`r4t engine` is the layer below the roster: one CLI, no `ROSTER.md`, no
dispatcher, no budgets. Three verbs — `quota` asks what a subscription has left
without spending a turn; `run` spends one; `check` asks the installed CLI
whether the argv r4t composes still parses, spending nothing. All three accept
an engine id or any rig preset id (`r4t engine list` shows which presets serve
which engine, and which verbs each engine answers).

## `run` — one headless turn as a bare stateless agent

```bash
r4t engine claude run --agent my-node "check the deploy and report"
r4t engine codex run --dir ~/work/proj --model o4 --timeout 600 "fix the lint errors"
echo "long prompt piped in" | r4t engine agy run -
```

```
r4t engine <id> run [--dir DIR] [--model M] [--agent NAME] [--timeout S]
                     [--no-scaffold] [--idle] [--echo] [--lessons-cap N]
                     [--continue] [--permissions MODE] [--allowed-tools SPEC]
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
tool). Nothing else is added: an unset `--permissions` leaves the preset's own
flags byte for byte.

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
- `--continue`, `--permissions MODE`, `--allowed-tools SPEC` — the three
  translated parameters; see below.
- Exit code is the CLI's own (124 on a timeout kill); stdout/stderr stream
  through unchanged.

`engine run` is bare metal: no budget, no identity, nothing named. The same
turn with a model, a permission stance, an env map and a spend budget already
attached is [`r4t rig run <rig>`](r4t-rigs.md#rig-run--one-headless-turn-as-a-rig),
which composes through this very path and gates it on the rig's bucket.

### The three translated parameters

Each engine spells "do not ask me" and "pick up where you left off" its own
way. These three flags are ar3's words for those stances, translated per
engine from one table (`PERMISSION_TRANSLATION` in `apps/r4t/rig.py`, beside
the preset table). **Every one is unset by default, and unset means the
preset's own flags** — naming none of them composes exactly the argv the preset
composes on its own.

**Read the translation with `--echo`.** It prints the exact composed argv
before the turn runs, which is the whole diagnostic for a layer whose job is
rewriting argv: a translation the caller cannot see is one the caller cannot
debug. `r4t engine <id> check` proves the same argv still parses without
spending a turn.

#### `--permissions ask | auto | bypass`

- `ask` — add no auto-approval flags, and drop the preset's own. This is the
  CLI's own default, which headless usually answers by denying tool calls; it
  is the mode for an operator whose `settings.json` or `opencode.json` already
  carries a permission map r4t must stop overriding.
- `auto` — the engine approves tool use without prompting. The engine's deny
  rules still apply, so `auto` means "ask nothing", never "permit everything".
  Eight of the nine presets already sit here.
- `bypass` — the engine's strongest available auto-approval.

| Engine | `ask` | `auto` | `bypass` |
|---|---|---|---|
| `claude` | drop `--permission-mode` + `--allowedTools` | `--permission-mode dontAsk` | `--permission-mode bypassPermissions` |
| `codex` | **error** — `codex exec` hard-codes its policy to `never` | `--sandbox workspace-write` | `--dangerously-bypass-approvals-and-sandbox` |
| `cursor` | drop `--trust --force --approve-mcps` | `--trust --force --approve-mcps` | = `auto` + a note |
| `copilot` | **error** — `-p` needs `--allow-all-tools` at all | `--allow-all-tools` | `--allow-all` |
| `agy` | **error** — see below | **error** — see below | `--dangerously-skip-permissions --mode accept-edits` |
| `opencode` | drop `--auto` | `--auto` | = `auto` + a note |
| `ollama-*` | as the wrapped engine | as the wrapped engine | as the wrapped engine |

**The asymmetry rule.** A mode *below* an engine's floor is a hard error naming
the reason: a safety request that silently went unmet is the failure this error
style exists to prevent. A mode *above* its ceiling proceeds with one stderr
note (`r4t engine: opencode's strongest mode is 'auto'; 'bypass' means the same
here`), because the argv is then the most permissive one available, which is
what was asked for. agy's floor is `bypass`: 1.1.3+ auto-denies command tools
in headless `--print` runs, so anything weaker is a turn that cannot run `tell`
or `git`.

**Two things the table does not make obvious.**

`bypass` on codex also removes the sandbox. codex fuses approvals and isolation
into `--dangerously-bypass-approvals-and-sandbox`, so a user who learns
`bypass` on claude — where it means "stop asking" inside whatever
`settings.json` configures — gets something materially stronger on codex. This
is the one place ar3 cannot keep permissions and isolation apart.

claude's `auto` is fail-closed and every other engine's is fail-open.
`--permission-mode dontAsk` denies whatever the allowlist does not cover, while
cursor's `--force`, copilot's `--allow-all-tools` and opencode's `--auto`
permit by default. So `auto` on claude means "never prompt, and run only the
listed tools", and `auto` on opencode means "never prompt, and run almost
anything". That is the composition working: `--permissions` says whether the
engine prompts, `--allowed-tools` says what it may run, and claude is the one
engine where r4t supplies the second half by default.

`engine run` has no org file, so **the boundary here is the operator's job**:
run it as the user, or inside the container, the work deserves. Isolation is an
org property (`run_as` / `container` in `r4t-org.json`, see
[r4t-isolation.md](r4t-isolation.md)) and there is no `--sandbox` at any level.

#### `--allowed-tools SPEC`

The engine's own allowlist syntax, opaque to r4t, replacing the preset's list
for this turn:

```bash
r4t engine claude run --allowed-tools "Bash(git:*) Bash(gh:*) Read Edit" "land the fix"
```

Only `claude` and `ollama-claude` take a tool allowlist per invocation; every
other engine errors with the reason (copilot takes `--allow-tool`/`--deny-tool`
per tool; cursor, opencode and agy express tool policy only in config files).

#### `--continue`

Resumes the conversation the CLI already has in `--dir`, in the preset's own
idiom — `--continue` for claude, cursor, agy and opencode,
`exec resume --last --include-non-interactive` for codex. **The caller asserts
this turn continues live work; an idle or independent wake must not pass it.**
`engine run` is one CLI and one operator decision, so the engine layer cannot
enforce that rule — it can only refuse to pretend otherwise. `--idle
--continue` is an error, since an idle wake is a cold start by definition.

This is why the flag is unaffected by the roster's continuation grades: a
roster turn is always a new process, and `r4t engine run --continue` is one
process the operator chose to resume. A member on a roster is gated instead —
see [the three refound gates](r4t-rigs.md#the-three-refound-gates) and the
per-engine grades.

An engine with no verified continuation errors, naming the ones that have it.
copilot is the notable refusal, and its message says why: `copilot --continue`
resumes the machine's most recent session whatever the directory, so it crosses
members and workdirs
([#17](https://github.com/witw-llc/ar3-private/issues/17)) — the flag exists in
`copilot --help`, and r4t declines to pass it.

Continuation also drops what the resume path cannot carry: `codex exec resume`
is its own subcommand and takes no `--sandbox`, so the pair comes out.

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

## `check` — does the installed CLI still accept this argv?

```bash
r4t engine check                       # every run-capable engine
r4t engine codex check                 # one engine
r4t engine claude check --permissions bypass --allowed-tools "Read Edit"
r4t engine check --json
```

`check` composes the exact argv `run` would spend a turn on — same `--model`,
`--permissions`, `--allowed-tools`, `--continue` — and asks the installed
binary whether it parses. **No turn is spent and no tokens are billed:** the
prompt is removed from the argv, and the only things run are the CLI's own
`--help` and `--version`.

Two probe shapes, one per parser class, each recorded with its engine in
`apps/r4t/engines/check.py`:

- **parse probe** (codex) — clap reports an unexpected argument even when
  `--help` is present, so the composed argv goes to the CLI itself and its exit
  code is the verdict.
- **help scan** (everything else) — these CLIs print help and exit before they
  validate the rest of argv, so each long flag in the composed argv is matched
  against the flags the CLI's own help lists. It is the only shape that catches
  a lenient parser, which accepts an unknown flag and ignores it.

Each engine reports its binary and version, then `accepted`, `rejected` (with
what the CLI said, or which flag its help never lists), or `unverifiable` — a
CLI that is not installed, which is not a failure. The exit code is 1 if any
run-capable engine's argv is rejected, so this belongs in a release check as
much as at a keyboard.

## a8s integration

Every engine in `RUN_ENGINES` ships its own bundled
`apps/a8s/definitions/engine-<id>.json` (`engine-claude.json`,
`engine-codex.json`, `engine-agy.json`, `engine-copilot.json`,
`engine-cursor.json`, `engine-opencode.json`, `engine-ollama-claude.json`,
`engine-ollama-codex.json`, `engine-ollama-opencode.json`), each wiring all
three wake paths:

```bash
a8s add my-bare-node ~/somewhere engine-cursor
```

These are usable as they ship — no copy, no edit. cwd is already the node's
own root on every wake, which matches `run`'s `--dir`-less default:

- **a message** — `r4t engine <id> run --agent $RECIPIENT "$SENDER tells
  $RECIPIENT ($AGE): $MESSAGE"`, so a bare node's cold-boot turn knows who it
  is answering, the same prompt shape every other bundled definition uses.
- **a batch** — `batch.invoke` carries no prompt, and a8s appends one composed
  prompt carrying every pending envelope in arrival order. N messages cost one
  invocation and one cold context load, not N. Declaring `batch` also
  debounces the wake by 3 seconds, so a burst arrives as a burst. Leave
  `batch.format` unset: the prose form is what a `PROMPT` positional takes, and
  `"envelopes"` (a JSON array) is for `r4t dispatch`, which has member queues
  to ingest one into.
- **quiet** — `idle.invoke` adds `--idle` and no prompt. It fires after
  `idle.timeout` seconds of quiet, and `--idle`'s own latch means only the
  first quiet tick spends a turn; any real turn re-arms it.

The three `ollama-*` definitions additionally need `--model` — set the a8s var
`MODEL` (`a8s add my-bare-node ~/somewhere engine-ollama-claude
--model=qwen3.6`, or `a8s vars my-bare-node set MODEL qwen3.6` after the
fact) — since the launcher has no default model of its own.

Each of the nine also ships an `engine-<id>-unrestricted` variant: the same
three wakes invoked with `--permissions bypass`. What that buys differs by
engine, and each variant's own description says which — codex trades its
sandbox for `--dangerously-bypass-approvals-and-sandbox`, claude moves to
`--permission-mode bypassPermissions` with settings.json deny rules still in
force, copilot moves to `--allow-all`, while cursor, opencode,
ollama-opencode and agy already run at their strongest mode in the base
preset, so those four variants compose the same argv as the base today:

```bash
a8s add amos ~/agents/amos engine-codex-unrestricted
```

An unrestricted node acts on untrusted inbound mail — anyone who can reach its
inbox, including over a broker or a shared folder, is driving a CLI that will
not ask. Only for an agent on its own machine and its own account.

The stance lives on the definition's own invoke lines, chosen by name at `add`
time — the base variants never grow it.

A custom node beyond these nine is a copy: `a8s defs add` installs a template
into the a8s state root, not the hidden bundled directory — see the wiki for
recipes.

### Mapping a definition's parameters onto `engine run`

The definition is argv, so a flag goes on the `invoke` array as its own
element — never as one string with spaces in it, which is the wiring that
fails. The three translated parameters are how a node is tuned:

```json
"invoke": ["python3", "$A8S_DIR/../r4t/r4t.py", "engine", "claude", "run",
           "--permissions", "bypass",
           "--allowed-tools", "Bash(git:*) Read Edit",
           "--agent", "$RECIPIENT",
           "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"]
```

Rules that keep a node's wiring right:

- The composed prompt is the LAST element, because `PROMPT` is a positional.
  `[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE` is the shape every
  bundled definition uses — a bare `$MESSAGE` gives the node no way to know
  who it is answering, and no absolute time to resolve *tomorrow* against.
- Never put `--continue` in a definition. Every wake a8s fires is an
  independent message, and the idle wake is a cold start; `--idle --continue`
  is refused outright.
- The `ollama-*` engines need `--model <tag>`, since the launcher has no
  default.
- `--timeout S` should sit under the definition's own `max_wake_seconds`, so
  r4t kills the turn and reports rather than a8s killing r4t.
- Verify a new definition before it wakes on anything real:
  `r4t engine claude check --permissions bypass` proves the argv parses, and
  adding `--echo` to `run` prints exactly what a wake will execute.

## `quota` — remaining subscription, no turn spent

```bash
r4t engine list           # every engine, and the presets each one serves
r4t engine codex quota    # remaining fraction + reset time
r4t engine claude quota --json
```

One component per engine under `apps/r4t/engines/`; live answers persist as
snapshots that still answer when the live check cannot. A snapshot answer
carries `"origin": "snapshot"` and `age_seconds` — its age as a number, like
every other duration r4t reports; the text lines render the human string.

This is every dial the account carries, raw. Each bucket's own reading is
`remaining_fraction`. One number for one rig is [`r4t rig fuel
<rig>`](r4t-rigs.md#rig-fuel--the-tank-as-one-number), which reads this same
answer, keeps the buckets the rig's model burns, and reports the lowest of them
as `fuel`.
