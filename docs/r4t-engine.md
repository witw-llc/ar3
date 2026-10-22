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
r4t engine <id> run [--dir DIR] [--model M] [--effort LEVEL] [--agent NAME] [--timeout S]
                     [--no-scaffold] [--idle] [--echo] [--lessons-cap N]
                     [--lessons-cap-bytes N]
                     [--continue] [--session UUID] [--max-credits N]
                     [--permissions MODE] [--allowed-tools SPEC]
                     [--git-name NAME] [--git-email EMAIL]
                     [--resident]
                     [--memory on|off|small|medium|large] [--memory-home DIR]
                     [--memory-writer RIG] [--memory-people SENDERS]
                     [--] PROMPT
```

**Private memory:** add `--memory on` for automatic K7E retrieval and background
learning. Each named `--agent` retains its own store across engine and directory
changes. For a bundled a8s node, use `a8s vars NAME set MEMORY on`.
See [engine memory](r4t-engine-memory.md) for portability, corrections, writer
selection, and diagnostics.

Supported engines: `claude`, `codex`, `agy`, `copilot`, `cursor`, `opencode`,
`muse`, `devin`, and the `ollama-claude` / `ollama-codex` / `ollama-opencode`
local launchers —
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
tool) plus this turn's own measurement flags (see
[copilot](#copilot-officially-supported)). Nothing else is added: an unset
`--permissions` leaves the preset's own flags byte for byte.

- `PROMPT` is one positional string; `-` reads it from stdin.
- `--dir DIR` — the turn's working directory (default: CWD).
- `--model M` — appended in the preset's own flag pattern. Every preset takes
  one; a preset that needs a live resolver (agy — `agy models` is queried fresh
  every run) errors the same way `r4t rig add --model` does when the string
  does not resolve. copilot's string is passed through untouched: it has no
  list verb to check against, so a bad id fails at spawn with copilot's own
  `Model "<id>" from --model flag is not available`.
- `--timeout S` — default 900. On expiry the whole process group is killed
  (a harness CLI forks tool subprocesses `kill()` alone would leak) and the
  command exits 124, naming the timeout.
- `--echo` — before spawning, print the composed argv and the exact prompt
  (scaffold prelude included) to stderr; the turn still runs, stdout still
  carries only the engine's own reply stream.
- `--lessons-cap N`, `--lessons-cap-bytes N` — the `LESSONS.md` caps for
  this turn (default 200 lines and 35840 bytes, 35 KB); see
  [`LESSONS.md` upkeep](#lessonsmd-upkeep).
- `--session UUID` — pin this turn to one named conversation (copilot only);
  see [the copilot section](#copilot-officially-supported).
- `--max-credits N` — cap this turn's AI credits (copilot only, minimum 30,
  and soft); see the same section.
- `--continue`, `--permissions MODE`, `--allowed-tools SPEC` — the three
  translated parameters; see below.
- `--git-name NAME`, `--git-email EMAIL` — the author and committer of each
  new commit the turn makes; see [git identity](#git-identity).
- `--resident` — keep one long-running `stream-json` session alive across
  arrivals (`claude`, `ollama-claude`, and `agy`); `--timeout` then sets the
  quiet window. See
  [resident sessions](#resident-sessions--claude-ollama-claude-agy-and-codex).
- Exit code is the CLI's own (124 on a timeout kill); stdout/stderr stream
  through unchanged.

`engine run` is bare metal: no budget, no rig, nothing named. The same
turn with a model, a permission stance, an env map and a spend budget already
attached is [`r4t rig run <rig>`](r4t-rigs.md#rig-run--one-headless-turn-as-a-rig),
which composes through this very path and gates it on the rig's bucket.

### Reasoning effort

`--effort LEVEL` selects the engine's reasoning depth. Each preset owns its
accepted values; there is no global list that discards engine-specific levels.
Unset leaves the invocation unchanged. An unsupported engine or a value outside
its advertised vocabulary fails before the harness starts.

| Engine | Native option | Accepted values |
| --- | --- | --- |
| claude / ollama-claude | `--effort` | low, medium, high, xhigh, max |
| copilot / ollama-copilot (dispatch preset) | `--reasoning-effort` | none, minimal, low, medium, high, xhigh, max |
| muse | `--reasoning-effort` | none, minimal, low, medium, high, xhigh, max, ultra |
| agy | model variant when pinned; `--effort` otherwise | low, medium, high; pinned models must offer the requested variant |
| codex / ollama-codex | `-c model_reasoning_effort=VALUE` | non-empty string; model-specific acceptance belongs to Codex |
| opencode / ollama-opencode | `--variant` | non-empty provider-specific variant |
| cursor | inside `--model` | preserve bracket syntax; separate `--effort` errors |
| devin / bare ollama | unsupported | separate `--effort` errors |

Codex CLI 0.147.0's generated `ReasoningEffort` schema is a non-empty string,
so r4t passes it through without imposing an enum. OpenCode's provider variants
also pass through. These engines can still reject a value for the chosen model.
Wrapped engines receive effort after `ollama launch`'s `--` separator.

```bash
r4t engine claude run --effort low "summarize the diff"
r4t engine muse run --effort ultra "review the architecture"
r4t engine agy run --model "Gemini 3.1 Pro (High)" --effort low "check the tests"
r4t engine cursor run --model 'claude-opus-4-8[context=1m,effort=high]' "review the diff"
```

For AGY, explicit effort overrides the model name's `(Low)`, `(Medium)`, or
`(High)` suffix. r4t resolves the family against the live `agy models` list,
then selects that family's requested variant. The example selects
`Gemini 3.1 Pro (Low)` without a native `--effort` flag: AGY rejects that flag
alongside these model variants even when the values agree.
A missing variant errors with the family's available
choices; it never switches families or interprets `(Thinking)` as a numeric
level. Without a pinned model, r4t passes the native flag to AGY; AGY rejects
it if the current model does not support a separate effort setting.
Without explicit effort, the existing model selection rules apply.

Rig effort is a separate persisted setting: `rig add` and `rig swap` accept
`--effort`, and `rig set NAME effort LEVEL` / `rig unset NAME effort` tune it.
A `rig run --effort` override affects only that turn. Precedence is the runtime
flag, then the rig setting, then embedded model effort or the engine default.
Engine swaps preserve effort and refuse incompatible values before saving;
unset it before switching to Cursor or Devin. Dispatch and knowledge
distillation also use the rig setting. `--echo` shows the resulting argv.

### Git identity

Engine agents that run under one Unix user share one git config, so every
commit they make carries the same author. `--git-name NAME` and
`--git-email EMAIL` give one turn its own. The turn's child environment gets
`GIT_AUTHOR_NAME` and `GIT_COMMITTER_NAME` from the name, and
`GIT_AUTHOR_EMAIL` and `GIT_COMMITTER_EMAIL` from the email. git reads these
before any config file, in any repo or worktree. A new commit the turn makes
names the agent as its author and committer. A commit the turn rewrites with
`git commit --amend` or `git rebase` keeps its original author and names the
agent as its committer.

```bash
r4t engine claude run --git-name "Ada (agent)" --git-email ada@example.com "fix the lint errors"
```

- Each flag stands alone. An unset or empty flag leaves the environment the
  turn inherits exactly as it was, so git's own config decides, or a `GIT_*`
  identity already in that environment.
- A set flag wins over an inherited value. It is the operator's setting for
  this agent; the environment a daemon happens to run under is not.
- A value with a line break, `<` or `>` is refused before the turn starts
  (exit 1). git drops those characters from an identity without a word.
- No identity is derived from the agent's name. The owner sets one per agent;
  an agent with none commits as git's config says.
- `--echo` prints the names the turn sets, on one `r4t engine echo: env:` line.

A hosting service still records the account that pushed, and a squash merge
is authored by whoever merges. `r4t rig run` takes no identity flags: a rig
carries the same four names in its [`env` map](r4t-rigs.md#harness-env-knobs-env)
(`r4t rig set <rig> env.GIT_AUTHOR_NAME "Ada (agent)"`).

### The three translated parameters

Each engine spells "do not ask me" and "pick up where you left off" its own
way. These three flags are AR3's words for those stances, translated per
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
  Nine of the eleven presets already sit here.
- `bypass` — the engine's strongest available auto-approval.

| Engine | `ask` | `auto` | `bypass` |
|---|---|---|---|
| `claude` | drop `--permission-mode` + `--allowedTools` | `--permission-mode dontAsk` | `--permission-mode bypassPermissions` |
| `codex` | **error** — `codex exec` hard-codes its policy to `never` | `--sandbox workspace-write` | `--dangerously-bypass-approvals-and-sandbox` |
| `cursor` | drop `--trust --force --approve-mcps` | `--trust --force --approve-mcps` | = `auto` + a note |
| `copilot` | **error** — `-p` needs `--allow-all-tools` at all | `--allow-all-tools` | `--allow-all` |
| `agy` | **error** — see below | **error** — see below | `--dangerously-skip-permissions --mode accept-edits` |
| `devin` | **error** — see below | **error** — see below | `--permission-mode dangerous` |
| `opencode` | drop `--auto` | `--auto` | = `auto` + a note |
| `muse` | drop `--approval-mode never` + `--user-input-auto-resolve` | `--approval-mode never` | `--yolo` |
| `ollama-*` | as the wrapped engine | as the wrapped engine | as the wrapped engine |

**The asymmetry rule.** A mode *below* an engine's floor is a hard error naming
the reason: a safety request that silently went unmet is the failure this error
style exists to prevent. A mode *above* its ceiling proceeds with one stderr
note (`r4t engine: opencode's strongest mode is 'auto'; 'bypass' means the same
here`), because the argv is then the most permissive one available, which is
what was asked for. agy's floor is `bypass`: 1.1.3+ auto-denies command tools
in headless `--print` runs, so anything weaker is a turn that cannot run `tell`
or `git`. devin's floor is `bypass` for the same shape of reason: every mode
below `--permission-mode dangerous` still prompts for exec and file writes,
which a print-mode turn cannot answer. (Watch the naming collision: devin's own
`--permission-mode auto` is its *normal* mode — auto-approving only reads —
not this table's `auto`.)

**Two things the table does not make obvious.**

`bypass` on codex also removes the sandbox. codex fuses approvals and isolation
into `--dangerously-bypass-approvals-and-sandbox`, so a user who learns
`bypass` on claude — where it means "stop asking" inside whatever
`settings.json` configures — gets something materially stronger on codex. This
is the one place AR3 cannot keep permissions and isolation apart.

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
per tool; cursor, opencode and agy express tool policy only in config files;
devin takes `permissions` maps in `.devin/config.json` and
`~/.config/devin/config.json`, never per invocation; muse takes a named
`--permission-profile` rather than a list).

#### `--continue`

Resumes the conversation the CLI already has in `--dir`, in the preset's own
idiom — `--continue` for claude, cursor, agy, devin and opencode,
`exec resume --last --include-non-interactive` for codex. `muse` refuses:
`muse resume` is an interactive subcommand that opens the workspace session
picker, and `muse exec` rejects `--last` outright, so there is no headless
resume to pass through. **The caller asserts
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
copilot is the notable refusal, and its message says why and where to go
instead: `copilot --continue` resumes the machine's most recent session
whatever the directory, and on 2026-09-02 it attached to a seat's own **live**
session and injected the prompt there as a new user turn, interrupting a tool
call in flight. The flag exists in `copilot --help`; r4t declines to pass it,
and offers `--session` instead.

Continuation also drops what the resume path cannot carry: `codex exec resume`
is its own subcommand and takes no `--sandbox`, so the pair comes out.

### copilot, officially supported

GitHub Copilot CLI is a supported engine rather than one r4t merely knows the
name of. That claim has six parts, and each one is a thing r4t composes or
reads on every copilot turn.

**A model slot.** `--model` passes through untouched. copilot has no list verb
to enumerate against and the ids in GitHub's own documentation are stale
against a live seat — `claude-sonnet-4.6` errors where `claude-sonnet-5` runs
— so r4t never adjudicates the string, and a bad one fails at spawn with
copilot's own message. r4t sets the **flag** only; `COPILOT_MODEL` and
`~/.copilot/settings.json` stay the operator's, and the flag outranks both.
`auto` resolves against the live catalogue, at the cost of being unpredictable
across seats, which is the reason a rig that cares pins an id.

**Clean stdout.** The preset carries `-s`, so stdout is the agent's reply and
nothing else. The stats copilot would otherwise print as a footer are read from
the usage file instead, in machine form.

**A pinned binary.** The preset carries `--no-auto-update`. The CLI updates
itself by default, and on 2026-09-02 it moved 1.0.80 to 1.0.82 between two
turns of one session, changing the flag surface underneath the run in progress.

**Per-turn spend.** Every turn runs with `--usage-output-file` on a path unique
to it, and the JSON is read back into a `spend` object — AI credits, the model
that served the turn, premium requests, input/output/cache-read/cache-write
tokens and the cache expiry copilot reports. One line goes to stderr:

```
r4t engine: spend: 3.92 credits · gpt-5.6-terra · cache write 16019 / read 15947
```

`r4t rig run --json` carries the same object as `spend`. The flag is 1.0.82+,
so it is feature-detected once per process rather than baked into the preset,
which would make `check` reject the composition on an older seat. Nothing
special-cases a timeout: r4t's teardown is SIGTERM, a 0.5 s grace, then
SIGKILL, and copilot flushes the file inside that grace, so the turns most
worth accounting for still produce a record.

**"Every turn" means the roster's turns too.** A dispatched member composes its
argv in `dispatch.run_harness` rather than here, so the two paths would drift
if each armed its own instruments; they call one helper
(`engines.copilot.turn_instruments`) instead. A member on a copilot rig carries
its rig's `max_ai_credits` fuse, writes its usage file and its spans, and lands
`spend` and `otel` under `last_spend` in the member's meta with the line in the
node's day log. A roster turn puts those files in the **member's own working
directory** — the one path writable by the child under every isolation mode,
since a container bind-mounts it rw at the same path and `run_as` probes it for
write before the turn starts; they are dot-named, cleared before the turn as
well as after, and the spans are moved to the durable store on the way past.
The one preset deliberately left out is `ollama-copilot`: its launcher owns the
head of the argv, so flags spliced next to the binary would reach `ollama`
rather than the CLI behind it. A rig that names `max_ai_credits` on a preset
with no fuse fails to load rather than running turns whose budget quietly does
not apply.

**A spend fuse.** `--max-credits N` composes copilot's `--max-ai-credits`, and
a rig can carry the same value as `max_ai_credits` for every one of its turns.
Minimum 30, refused below that before a spawn is spent. It is a **soft** cap:
copilot learns a response's usage only after that response returns, so a
response can overshoot and the NEXT model call is what gets blocked. Hidden
work such as compaction counts against it.

**Best-effort OTEL.** Each turn is handed its own
`COPILOT_OTEL_FILE_EXPORTER_PATH`, which both enables OpenTelemetry and selects
the file exporter, on that turn's environment alone — nothing machine-wide, and
the operator's own sessions are untouched. When the file arrives it is kept
under `~/.config/r4t/copilot/otel/`; it carries `chat` and `invoke_agent` spans
with per-model-call cache reads and writes, which no other surface on this
engine reports. When it does not, the turn says so and carries on:

```
r4t engine: otel: not written (org telemetry policy or race)
```

That is a normal outcome, not an error. An organisation telemetry policy,
fetched from the server after the variable is honoured, reconfigures the
exporter to `otlp-http` before the SDK initialises; the spans go to the
organisation's collector and no file appears. Accounting is built on the usage
file, which is reliable; the exporter is a bonus that arrived.

#### `--session UUID` — the pin

```bash
r4t engine copilot run --dir ~/work/proj --session 3f1c… "carry on with the migration"
```

A session id that does not exist yet is founded under exactly that id
(`--session-id`); one that does is resumed (`--resume=<id>`), with the turn's
own `--dir` forced through `-C`. The `-C` is not decoration: a resumed copilot
session runs in the directory it was **founded** in whatever the invoking cwd,
so pinning the session without pinning the directory moves the problem rather
than fixing it. Which of the two applies is read off copilot's own session
store under `COPILOT_HOME`, so any caller that knows the id can pick the
conversation up.

`--session` and `--continue` contradict and are refused together: the id is the
continuation, and it says which one.

**On a roster** a member with `Continue:` on a copilot rig gets the same pin
without naming anything. r4t mints the member's UUID on the turn that founds
its conversation, records it, and names it on every turn that resumes — and
mints a fresh one on a refound, because the point of a refound is not to walk
back into a conversation r4t never saw finish. Two members on one host never
reach each other's sessions, which is the failure bare `--continue` makes
unavoidable.

**Quota without `gh`.** `r4t engine copilot quota` uses `gh` when it is there
and a token when it is not — `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`,
`GITHUB_TOKEN`, then the login copilot stored for itself — calling the
entitlement endpoint over HTTPS directly. On a token-based-billing seat the
percentage fields are degenerate whatever has been spent, so the signal is
each bucket's cumulative `credits_used` plus the reset date; `credits_used` is
a number on the bucket as well as text in the note.

### Why bare agents need a scaffold

A roster member gets continuity from r4t: budgets, a dispatcher, a ROSTER.md
persona. A bare engine node has none of that — every wake is a fresh CLI
session with zero transcript memory, so whatever it knew has to already be
sitting in a file. `run` supplies the reading and writing discipline that
makes that work: `STATUS.md` and `LESSONS.md`, read on the way in and
rewritten/appended on the way out, carry working state. Optional K7E memory
adds retrieved knowledge and asynchronous learning across turns.

### The scaffold (default on; `--no-scaffold` sends `PROMPT` untouched)

Prepended to `PROMPT`. The prelude is byte-identical across runs in the same
`--dir` (no timestamps, no counters), so the prompt cache misses only on what
follows it: the `LESSONS.md` note when one applies, then the routed input,
which always comes last. A one-shot turn and a
[resident session](#resident-sessions--claude-ollama-claude-agy-and-codex) start
from the same prelude; only the closing steps differ.

```
Smart cold boot:
1. Read <DIR>/STATUS.md, then <DIR>/AGENTS.md and <DIR>/LESSONS.md if
   present. Use these absolute paths even if your workspace root differs.
   They are the durable source of truth; you have no transcript memory.
2. You are 'NAME'. Run `a8s convo NAME` and reconcile the newest routed
   messages with STATUS.md before acting.
3. Reply only by message: call the `a8s_tell` MCP tool ... Printing text
   sends nothing, and no one reads this session's output. ...
                                     [steps 2 and 3 only with --agent NAME;
                                      the tell step is below; steps
                                      renumber cleanly without them]
4. Stay idle and exit unless there is clear direction or active work. Never
   restart completed work. Be token-frugal; no wordy prose.
5. Before exit, rewrite <DIR>/STATUS.md with sections: Current State,
   Important Context, Next Steps, Decisions (with rationale). Append
   genuinely new durable insights to <DIR>/LESSONS.md. Every line of it is
   one lesson, one short line each, with no sections, headings or tiers.
   Append; edit an existing line only when told to consolidate. Never edit
   AGENTS.md.

<LESSONS.md note>                    [only when a consolidation is due]

Routed input:
<PROMPT>
```

**The tell step.** A node's output reaches nobody, so with `--agent` the
prelude says that a reply is a message or nothing. Without `--agent` a one-shot
turn is a person's at a terminal, stdout is the answer, and there is no tell
step and no splice. (A resident `claude` session carries the a8s server in
every case; only its tell step needs `--agent`.) An engine that takes the a8s MCP server
by a flag — `claude`, `codex`, `copilot`, `ollama-claude`, `ollama-codex` —
gets `a8s mcp serve` spliced into its argv for the turn, the same server a
roster rig's `mcp` knob injects, and the step teaches the `a8s_tell` tool,
with the `tell` CLI as the fallback. The `codex` engines name the tool as
Codex lists it, `mcp__a8s__tell`: told `a8s_tell`, a Codex model searches its
tool list for that name and finds nothing. The `claude` engines also get
`mcp__a8s__tell` added to `--allowedTools`. Every other engine is taught the
`tell` CLI with the body on a quoted heredoc: their MCP idioms write a config
file, and an a8s node's outbox sits inside the agent's own directory.
`--no-scaffold` splices nothing.

r4t's own dispatcher (`dispatch.run_harness`) never uses this scaffold: it
already builds the roster's own prompt, and stacking this one on top would
double it. This is strictly the bare, roster-less path.

### `LESSONS.md` upkeep

`LESSONS.md` grows by appends. r4t never edits it: the agent keeps it under
its caps, and r4t tells it when, before the turn answers anything.

**Caps.** 200 lines (`--lessons-cap`) and 35 KB (`--lessons-cap-bytes`,
35840 bytes). Every size counts the file with LF line endings. A
`LESSONS.md` that is not UTF-8 never triggers a consolidation.

**Consolidation.** A consolidation is due when `LESSONS.md` passes 90% of
either cap (more than 180 lines or 31.5 KB at the defaults), or when the
spillover file `LESSONS-ARCHIVE.md` holds a non-blank line. The note after
the prelude then reads:

```
Consolidate <DIR>/LESSONS.md before you answer any message. <DIR>/LESSONS.md
is 184 lines / 30.1 KB; the cap is 200 lines / 35 KB. Rewrite it to at most
150 lines and 26.2 KB.
- Every line is one lesson. Remove sections, headings and tiers.
- Merge each duplicate into the lesson it repeats.
- Drop a lesson only when a newer lesson supersedes it. Never drop a live rule.
- <DIR>/LESSONS-ARCHIVE.md holds 40 lines that no turn reads. Fold the
  lessons that still hold into <DIR>/LESSONS.md, then delete
  <DIR>/LESSONS-ARCHIVE.md.                 [only when the spillover holds lines]
Copies from before this turn: <DIR>/archive/LESSONS-<date>-<hash>.md, ...
This is the one rewrite of LESSONS.md that the append-only rule allows.
```

The target is 75% of each cap. Under the warning line, with no spillover, the
scaffold carries no note.

**Lossless.** Before the note grants a rewrite, `run` copies each of the two
files that exists to `<DIR>/archive/<name>-<date>-<hash>.md` and reads the
copy back. A line the model merges or drops, or a rewrite that dies partway
loses, is still on disk. `<date>` is UTC, like every date in a filename, and
`<hash>` is the first eight hex digits of the content's SHA-256, so a turn
that finds the same bytes reuses the copy instead of writing another. When a
copy cannot be written or does not match, the note says so, tells the turn
not to edit existing lines, and one stderr line names the cause.
`--no-scaffold` makes no copy and carries no note.

### `--idle` — the Cody-pattern debounce

A bare node commonly wakes on a timer with nothing new to do. `--idle` skips
the wasted turn: if `<DIR>/.engine-idle` exists, `run` exits 0 printing
nothing. Otherwise it creates the marker and runs one turn — the routed
input is `PROMPT` if given, else the built-in idle consolidation prompt
("reconcile STATUS.md with reality, then append any new durable lessons").
Any run *without* `--idle` removes the marker first — real work re-arms the
latch, so the next quiet tick gets exactly one consolidation pass again.

### Resident sessions — `claude`, `ollama-claude`, `agy`, and `codex`

Resuming `claude -p` across a process boundary misses the provider's prompt
cache 40.6% of the time against a 2.5% same-process baseline (measured, #155).
`--resident` keeps one engine process alive instead of starting a new one per
message:

```bash
r4t engine claude run --resident --agent my-node "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"
r4t engine agy run --resident --agent my-node "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"
r4t engine codex run --resident --agent my-node "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"
r4t engine claude stop --agent my-node
```

The first `run` starts a detached bridge for `(DIR, agent)` and returns 0 at
once, so the router is never held up. The bridge spawns the engine in its
long-running mode: `claude -p --input-format stream-json --output-format
stream-json --verbose` with the `a8s_tell` MCP server injected via
`--mcp-config`; `agy --input-format stream-json --output-format stream-json
--print=`; or `codex app-server` (see [Codex](#codex-app-server) below). The opening turn carries the same [scaffold](#the-scaffold-default-on---no-scaffold-sends-prompt-untouched)
a one-shot turn gets, with resident closing steps in place of the exit steps:
the tell step (`a8s_tell` on Claude and Codex; the `tell` CLI on `agy`, which
has no per-invocation MCP flag), the [`LESSONS.md` note](#lessonsmd-upkeep) when a
consolidation is due, and the first message. The session consolidates first,
then answers, and keeps running. Each later `run` queues its
message and exits 0; the bridge pushes it into the same conversation as a new
user turn, so the session needs no `tells` monitor of its own.

- `--timeout S` (default `2700`, 45 minutes, with `--resident`) — the
  quiet window, not the session's life. A turn in flight never ends the
  session, and any turn sent or frame received restarts the window. After
  this many seconds with no turn in flight and no activity, the bridge lets
  a racing turn finish (up to 2 minutes), sends one wrap-up turn to rewrite
  `STATUS.md` and `LESSONS.md` (up to 5 minutes), and closes the engine.
  agy's own `--print-timeout` is dropped in this mode.
- Sessions run one after another, never side by side. A message that arrives
  during the wrap-up waits, and the same bridge starts the next session from
  the `STATUS.md` the wrap-up wrote.
- `--idle` with `--resident` runs no turn: the session wraps itself up.
  `--memory` still gets its idle pass.
- `r4t engine <id> stop [--dir DIR] --agent NAME` ends the session now: no
  wrap-up turn, and queued messages are dropped, as is a message that arrives
  during the stop. The bundled resident a8s
  definitions run it from `stop.invoke`, so `a8s stop`, `restart` and `kill`
  end the session too.
- `r4t engine <id> status [--dir DIR] --agent NAME` prints one word and
  exits 0: `busy` when a turn is in flight (or the bridge is starting with a
  message queued), `resident` when the session is up with no turn in flight
  (a closing session counts). With no live bridge, or a stopping one, it
  prints nothing and exits 1. The bridge keeps the in-flight count in
  `state.json` (`turns`). The bundled resident a8s definitions run it from
  `status.invoke`, which is how `a8s ps` shows `busy` and `resident`.
- If the engine exits before it reads its first message, the queued messages
  are dropped and the bridge exits; the next message starts again.
- State lives in `<DIR>/.engine-resident/<agent>/`, which ignores itself in
  git. `resident.log` there holds the bridge's log and the engine's replies.
- Supported on `claude`, `ollama-claude`, `agy`, and `codex`; other engines
  fail closed with the supported set. Cannot be combined with `--continue` or
  `--session`.

#### Codex: `app-server`

A resident `codex` session is one `codex app-server` process (JSON-RPC on
stdio) holding one thread. Proven on codex-cli 0.163.0.

- app-server applies only `-c` keys. It parses the top-level `-s`, `-a`, `-m`
  and `--dangerously-bypass-approvals-and-sandbox` and then ignores them, so
  the bridge rewrites the `codex exec` argv: `-c model="..."`,
  `-c sandbox_mode="workspace-write"` (`danger-full-access` with
  `--permissions bypass`), `-c approval_policy="never"`, and the a8s MCP
  server as `-c mcp_servers.a8s={...}`.
- The a8s server table carries `default_tools_approval_mode = "approve"`.
  Without it, under approvals `never`, every `a8s_tell` call fails with "MCP
  tool call requires approval, but approval policy is never". One-shot
  `codex exec` turns get the same key.
- The tell step names the tool `mcp__a8s__tell`, the name in Codex's own
  tool list.
- The thread/start answer names the policy in force. The session ends before
  its first turn when that is not approvals `never` and the composed sandbox;
  `resident.log` names both values.
- One turn is on the wire at a time. app-server merges a turn sent mid-turn
  into the running turn, so a message that arrives mid-turn waits for the next
  turn, as on Claude.
- An approval request that comes anyway is declined. The bridge never grants
  more than the configured sandbox.
- The engine's stderr goes to `resident.log` directly; its stdout carries only
  JSON-RPC.
- On Linux the codex sandbox runs commands under bubblewrap, which needs
  unprivileged user namespaces. With
  `kernel.apparmor_restrict_unprivileged_userns = 1` (Ubuntu 24.04's default)
  and no AppArmor profile for `bwrap`, every shell command in a restricted
  codex turn fails, one-shot or resident; the `a8s_tell` MCP call still works.
  `resident.log` shows codex's warning ("Codex's Linux sandbox uses bubblewrap
  and needs access to create user namespaces"). Use `--permissions bypass` on
  such a host only for an agent on its own machine and its own account.
- `r4t engine codex check --resident` runs the handshake without a turn:
  `initialize` for the version and an ephemeral `thread/start` for the policy
  in force. A version other than the proven 0.163.0 is accepted with a note.

## `check` — does the installed CLI still accept this argv?

```bash
r4t engine check                       # every run-capable engine
r4t engine codex check                 # one engine
r4t engine claude check --permissions bypass --allowed-tools "Read Edit"
r4t engine check --json
```

`check` composes the exact argv `run` would spend a turn on — same `--model`,
`--effort`, `--permissions`, `--allowed-tools`, `--continue`, `--resident` — and asks the installed
binary whether it parses. **No turn is spent and no tokens are billed:** the
prompt is removed from the argv, and the only things run are the CLI's own
`--help` and `--version`. `r4t engine codex check --resident` also runs the
app-server handshake (`initialize`, an ephemeral `thread/start`) and rejects
an argv whose policy in force is not the one composed; see
[Codex](#codex-app-server).

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
CLI that is not installed, which is not a failure. A CLI that is installed as
a script and whose `#!` interpreter is not on `PATH` is also `unverifiable`:
the detail names the script and the interpreter, and `--json` carries
`"runnable": false`. The exit code is 1 if any
run-capable engine's argv is rejected, so this belongs in a release check as
much as at a keyboard.

## a8s integration

Every engine in `RUN_ENGINES` ships its own bundled
`apps/a8s/definitions/engine-<id>.json` (`engine-claude.json`,
`engine-codex.json`, `engine-agy.json`, `engine-copilot.json`,
`engine-cursor.json`, `engine-opencode.json`, `engine-muse.json`,
`engine-devin.json`, `engine-ollama-claude.json`, `engine-ollama-codex.json`,
`engine-ollama-opencode.json`), each wiring all three wake paths:

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

Every one of these definitions takes the a8s var `MODEL` (`a8s add
my-bare-node ~/somewhere engine-ollama-claude --model=qwen3.6`, or `a8s vars
my-bare-node set MODEL qwen3.6` after the fact). The three `ollama-*`
definitions require it, since the launcher has no default model of its own;
the rest carry it as `--model=$MODEL?`, an optional reference (see
[docs/a8s.md](a8s.md)) — set, the turn pins that model, unset, the flag drops
and the engine picks its own default. For cursor that default is
`composer-2.5`, not the CLI's own `auto`: `auto` reuses the last `--model`
run on the machine, which spends the subscription on the newest model
instead (#282).

Every one also carries `--timeout=$TIMEOUT?` on all three wakes:
`a8s vars my-bare-node set TIMEOUT 1800` raises the turn timeout
for that node, unset drops the flag and the run keeps the engine default
of 900s.

All definitions also carry `--effort=$EFFORT?` on invoke, batch, and idle:
`a8s vars my-bare-node set EFFORT low` selects reasoning effort for that node.
Unset drops the token. Cursor keeps effort inside `MODEL` bracket syntax;
setting a separate `EFFORT` on Cursor or Devin produces an unsupported error.
See [reasoning effort](#reasoning-effort) for each engine's values.
`r4t engine <id> efforts` and `r4t engine <id> models` list what `MODEL` and `EFFORT` accept (see [`models` and `efforts`](#models-and-efforts--what---model-and---effort-accept)).

All definitions also carry `--git-name=$GIT_NAME?` and
`--git-email=$GIT_EMAIL?` on invoke, batch, and idle, so each node commits
under its own [git identity](#git-identity):

```bash
a8s vars ada set GIT_NAME "Ada (agent)"
a8s vars ada set GIT_EMAIL ada@example.com
# or at registration:
a8s add ada ~/agents/ada engine-claude --GIT_NAME="Ada (agent)" --GIT_EMAIL=ada@example.com
```

Unset drops the flag, and the node commits as git's own config says. Nothing
is derived from the node's name: the owner sets each node's identity.

Each of the eleven also ships an `engine-<id>-unrestricted` variant: the same
three wakes invoked with `--permissions bypass`. What that buys differs by
engine, and each variant's own description says which — codex trades its
sandbox for `--dangerously-bypass-approvals-and-sandbox`, claude moves to
`--permission-mode bypassPermissions` with settings.json deny rules still in
force, copilot moves to `--allow-all`, while cursor, opencode,
ollama-opencode, agy and devin already run at their strongest mode in the
base preset, so those five variants compose the same argv as the base
today:

```bash
a8s add amos ~/agents/amos engine-codex-unrestricted
```

An unrestricted node acts on untrusted inbound mail — anyone who can reach its
inbox, including over a broker or a shared folder, is driving a CLI that will
not ask. Only for an agent on its own machine and its own account.

The stance lives on the definition's own invoke lines, chosen by name at `add`
time — the base variants never grow it.

For long-running seats that stay warm across bursts of messages,
`engine-claude-resident`, `engine-claude-resident-unrestricted`,
`engine-agy-resident`, `engine-agy-resident-unrestricted`,
`engine-codex-resident`, and `engine-codex-resident-unrestricted` wire
`r4t engine <id> run --resident` across `invoke`, `batch.invoke` and
`idle.invoke`, `r4t engine <id> stop` as `stop.invoke`, and `r4t engine <id> status` as
`status.invoke`:

```bash
a8s add my-resident ~/somewhere engine-claude-resident
a8s add my-resident ~/somewhere engine-claude-resident-unrestricted
a8s add my-resident ~/somewhere engine-agy-resident
a8s add my-resident ~/somewhere engine-codex-resident
```

They take the same per-node vars as the headless nodes. `TIMEOUT` is the
quiet window: the session stays alive while it works and ends after `TIMEOUT`
seconds of quiet (default 2700, 45 minutes).

A custom node beyond these eleven is a copy: `a8s defs add` installs a template
into the a8s state root, not the hidden bundled directory — see the wiki for
recipes.

A wake's `PATH` is the start shell's, plus the directories a8s remembers from
the operator's terminals — see [Wake environment](a8s.md#wake-environment). `a8s start` and `a8s
define` both probe an engine definition's own binary (not just the always-
present interpreter) against that environment and warn by name before the
node ever wakes; if a turn still fails to spawn, `failed to spawn 'codex':
not on PATH (<the PATH searched>)` names exactly what was looked for and
where, instead of a bare `[Errno 2] No such file or directory`.

### Mapping a definition's parameters onto `engine run`

The definition is argv, so a flag goes on the `invoke` array as its own
element — never as one string with spaces in it, which is the wiring that
fails. The three translated parameters are how a node is tuned:

```json
"invoke": ["$PYTHON", "$A8S_DIR/../r4t/r4t.py", "engine", "claude", "run",
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
snapshots that still answer when the live check cannot. A snapshot lives at
`<r4t home>/quota/<engine>.json` — the same home as rigs and rosters,
relocatable with `R4T_HOME`.

**Claude's token.** `claude` reads the OAuth access token from the macOS
Keychain item `Claude Code-credentials`, then `~/.claude/.credentials.json`,
then the environment variable `CLAUDE_CODE_OAUTH_TOKEN`. When a stored token
is expired, or the usage endpoint rejects it, r4t runs `claude doctor` once —
no turn, no terminal — so the CLI refreshes and rewrites its own stored token,
then reads the credentials again and retries the probe once. A refreshed
answer carries the note `token refreshed by claude doctor`. If the retry is
rejected too, or `claude doctor` fails, the error says to run
`claude auth login`. r4t never calls the refresh endpoint itself. A token from
`CLAUDE_CODE_OAUTH_TOKEN` is used as given and never refreshed.

**Cursor's token.** `r4t engine cursor quota` takes its token from the IDE's
state database, or on macOS from the `agent` CLI's own Keychain item
(`cursor-access-token`) when the IDE is not installed; that answer carries the
note `token from the agent CLI's Keychain item`. A Linux or Windows machine with
only the CLI is not covered yet: where the CLI keeps its login there is not
verified.

**Devin's screen.** Devin reports its allowance only in the `/usage` view of
an interactive session, so `r4t engine devin quota` starts
`devin --respect-workspace-trust false` in a pseudo-terminal, in an empty
temporary directory, types `/usage`, and reads the Daily and Weekly bars off
the screen (about 7 seconds). No prompt reaches a model, so no turn is spent;
r4t never reads Devin's credentials; no directory is added to Devin's trusted
list. The plan name comes from the start banner when it is drawn. The screen
carries no contract: one bar is enough, and a screen with no usable bar is an
error that quotes what the screen said (`No quota data available`, `Not logged in`). POSIX only: Windows has no
pseudo-terminal, and the verb refuses there.

`muse` answers, with two caveats — freshness and cost. Its quota rides
`muse serve`, the MSP session host, whose `usage/read` returns the host's
last-observed subscription window. The observation is in-memory and minted
by provider traffic, and a host spawned for the check has run no turn, so a
cold check mints one itself: a session and a single minimal turn
(`denyUnmatched` approvals, a throwaway workspace, `reasoningEffort: none`),
after which `usage/changed` carries the window. That makes muse the one
engine whose live read *spends a turn* — and whose speed is the provider's,
anywhere from a few seconds to over a minute when the queue is congested.
On a terminal the wait announces itself and heartbeats every few seconds
rather than sitting silent.

Because the live read spends, it runs only where a turn was asked for:
`r4t engine muse quota` opts in, and nowhere else does — `rig detect` and
`rig fuel` serve muse's last snapshot or report no reading, so automatic
checks never start a provider turn. A snapshot answer
carries `"origin": "snapshot"` and `age_seconds` — its age as a number, like
every other duration r4t reports; the text lines render the human string.

A snapshot answers only while it can still be true, and two independent guards
say when it cannot. Both apply to every snapshot; either one refuses it.

The first is its stated reset. A bucket whose `reset_time` is already behind us
is quoting numbers that no longer exist, however young the snapshot is — a
reading taken a minute before a reset is a minute old and already wrong.

The second is age. Past four hours a snapshot is refused even when every reset
it names is still ahead: a reset in the future says the window has not turned
over, but it says nothing about how much of that window was spent in the hours
since the reading.

A snapshot dated more than a minute in the future is refused as well, because
its age no longer measures anything — under a minute is a slewing clock and is
treated as fresh. Every refusal reports the live failure instead, carrying the
reason. When a snapshot *is* served, the reason the live check failed prints
*above* the figures rather than after them.

The exit code names which of the three happened, so a script never has to read
the prose:

| exit | meaning |
|---|---|
| `0` | a live answer |
| `3` | a snapshot, served because the live check failed |
| `1` | no answer at all — no live check, and no snapshot that can still be true |

This is every dial the account carries, raw. Each bucket's own reading is
`remaining_fraction`. One number for one rig is [`r4t rig fuel
<rig>`](r4t-rigs.md#rig-fuel--the-tank-as-one-number), which reads this same
answer, keeps the buckets the rig's model burns, and reports the lowest of them
as `fuel`.

## `models` and `efforts` — what `--model` and `--effort` accept

```
r4t engine <id> efforts [--json]
r4t engine <id> models  [--json]
```

`<id>` is an engine id or any rig preset id, as for `run`. Values print one per
line on stdout; a note prints on stderr as `r4t engine: <note>`. `--json`
prints `{"values": [...], "note": ...}`. Neither verb starts a session or
spends a turn. `r4t engine list` shows which engines answer which verb.

| Engine | `models` source | `efforts` source |
|---|---|---|
| claude | aliases read from `claude --help`, or a full model name | fixed list |
| ollama, ollama-* | `ollama list` | fixed list for ollama-claude and ollama-copilot; any value for ollama-codex and ollama-opencode; none for bare ollama |
| agy | `agy models` ids; effort is part of each id | fixed list |
| cursor | `cursor-agent models` | none; effort goes inside `--model` brackets |
| devin | `devin models list` ids; effort is part of each id | none |
| copilot, muse | no list verb | fixed list |
| codex, opencode | no list verb | any value |

Each verb ends in one of three outcomes:

- **A list.** Exit 0, one value per line.
- **Any value.** The provider decides, so the list is empty and the note says
  so. Exit 0.
- **A refusal.** The engine has no flag or no list verb; the message names it
  and where the string goes. Exit 1.
