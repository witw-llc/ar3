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
                     [--scaffold on|off] [--no-scaffold] [--protect-agents-md on|off]
                     [--lessons append|free|off] [--lessons-cap N] [--lessons-cap-bytes N]
                     [--idle] [--echo]
                     [--continue] [--session UUID] [--max-credits N]
                     [--permissions MODE] [--allowed-tools SPEC]
                     [--add-dir DIR]... [--allow-tool RULE]... [--deny-tool RULE]...
                     [--rig-config PATH]
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
- `--scaffold on|off`, `--no-scaffold` — include the cold-boot scaffold and
  resident wrap-up turn (`on`, default) or send `PROMPT` untouched (`off` /
  `--no-scaffold`). Bundled a8s engine definitions expose `SCAFFOLD`.
- `--protect-agents-md on|off` — forbid editing `AGENTS.md` in the scaffold and
  resident wrap-up (`on`), or allow the agent to update its own `AGENTS.md`
  (`off`, default). Bundled a8s engine definitions expose `PROTECT_AGENTS_MD`.
- `--lessons append|free|off`, `--lessons-cap N`, `--lessons-cap-bytes N` —
  the `LESSONS.md` policy (`append` by default: one lesson per line plus
  consolidation at 90% of cap; `free`: unrestricted updates with no
  consolidation; `off`: omit `LESSONS.md`) and caps (default 200 lines and
  35840 bytes, 35 KB); see [`LESSONS.md` upkeep](#lessonsmd-upkeep). Bundled
  a8s engine definitions expose `LESSONS`, `LESSONS_CAP`, and
  `LESSONS_CAP_BYTES`.
- `--session UUID` — pin this turn to one named conversation (copilot only);
  see [the copilot section](#copilot-officially-supported).
- `--max-credits N` — cap this turn's AI credits (copilot only, minimum 30,
  and soft); see the same section.
- `--continue`, `--permissions MODE`, `--allowed-tools SPEC` — the three
  translated parameters; see below.
- `--add-dir DIR`, `--allow-tool RULE`, `--deny-tool RULE` — the access
  knobs: one more directory, approval rule or deny rule in the engine's own
  flags. Repeatable; a comma list works too; see [access knobs](#access-knobs).
  `--rig-config` names the file whose `deny_floor` applies to the turn.
- `--git-name NAME`, `--git-email EMAIL` — the author and committer of each
  new commit the turn makes; see [git identity](#git-identity).
- `--resident` — keep one long-running session alive across arrivals
  (`claude`, `ollama-claude`, `agy`, `codex`, `cursor`, `devin`, and `muse`); `--timeout`
  then sets the quiet window. See
  [resident sessions](#resident-sessions).
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

#### Access knobs

`--add-dir`, `--allow-tool` and `--deny-tool` translate to each engine's own
flags: claude `--add-dir` / `--allowedTools` / `--disallowedTools`; copilot
`--add-dir=D` / `--allow-tool=R` / `--deny-tool=R` (URLs are `url(...)` rules);
codex, cursor and agy take `--add-dir` only; `opencode`, `devin`, `muse` and
bare `ollama` take none, and the turn fails before it starts, naming the
engines that do. `ollama-claude`, `ollama-codex` and `ollama-copilot` use the
wrapped engine's flags. A deny always wins; the node's `deny_floor` is added to
the denies; `--resident` refuses the knobs and any applicable floor; codex
refuses `--add-dir` with `--continue`. The full table, grammar, layers and the
floor are in [Access knobs](r4t-rigs.md#access-knobs-add_dirs-allow_tools-deny_tools).
The turn records the resolved binary and `--version` (`access` in the record);
`check` proves the argv parses and nothing more.

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

**Denials.** A headless copilot turn that touches a path, URL or tool it has
no rule for is denied and carries on; the reply may not even say so. After each
turn r4t reads the session's `events.jsonl` (under `COPILOT_HOME` as the child
saw it, or the child's `HOME` plus `.copilot`) and reports one line:

```
r4t engine: denied: 0 (complete)
r4t engine: denied: at least 2 (incomplete: no-shutdown)
r4t engine: denied: unknown (home-unknown)
```

`rig run --json` carries the same data as `denials`; a roster turn writes the
line to the node's day log and keeps the object as `last_denials` in the
member's meta. It is a **diagnostic, never a gate**: it cannot change an exit
code, a reader failure is `unknown` and the turn is untouched, and a denial is
logged and **never told to the owner**. It is read on every exit path of the
turn, a spawn failure or an interrupt included, and cannot change the exit code
or the exception in flight.

Copilot 1.0.105 writes a denial in two ways, and both are counted: the
no-approval-rule fallback (`permission.completed` with a `denied-...` result
and `unattended_fallback`, then a failed tool record), and a deny rule such as
`--deny-tool=shell(touch)`, which writes **no** `permission.*` record at all,
only a failed `tool.execution_complete` with `error.code` `denied`. One denial
is counted once per tool call, whichever records it left.

```json
{"observed": 1, "complete": true, "reason": null,
 "by_tool": {"read": 1},
 "by_class": {"denied-no-approval-rule-and-could-not-request-from-user": 1},
 "event_ids": ["..."], "rule_tools": [], "rules_omitted": 0,
 "session": "<uuid>"}
```

The record is bounded metadata. `by_tool` uses a fixed set (`shell`, `read`,
`write`, `url`, `mcp`, `memory`, `other`); `by_class` is the denial kind as a
slug (`denied-by-rule` for a deny rule); `event_ids` holds at most ten
validated ids. When a deny rule fired, `rule_tools` names the fixed tools its
rules were for and `rules_omitted` counts the rules left out; the rule text,
even a bare command name such as `touch`, is never kept. No argument, command,
path, URL, rule, prompt or error message ever reaches stderr, a log, JSON or
meta. `session` is the opaque UUID; no path is stored. Every field passes one
gate before it leaves the reader; a summary that does not fit the vocabulary is
replaced by `reader-error`.

A denial record that lacks a usable correlation id (`toolCallId`, `requestId`)
is still a denial. Uncorrelated records may describe a call already counted
from the other record family, so they are never added to the correlated count:
the figure is the number of distinct correlated calls, or 1 when only
uncorrelated denials were seen, and coverage drops to `unknown-shape`
(`at least N`).

**When it says "complete".** "Complete" is a proof, decided in one place from a
positive checklist; anything not proven is not complete. `denied: N (complete)`
means the reader proved all of:

- the file it read, on the one descriptor it opened (without blocking and
  without following a link), has the identity it had before the turn (absent
  then created, or the same device and inode and not shrunk), is still what
  the path names after the read, was read in full, and did not grow while it
  was read (a file that grew is `file-grew`, with the count read so far kept as
  a lower bound; nothing is read again);
- this turn minted the session (a UUID passed as `--session-id`) or founded the
  pin under r4t's per-member lock;
- the slice is at most 8 MiB with no partial last line, and at most 1000
  distinct denials were found;
- every line parsed, and every record is either an event kind on the explicit
  ignorable list or is validated in full: a session start naming this session;
  a tool start with a call id and a tool name, and an outcome for every start;
  a tool outcome with a call id that matches a start (successes too), whose
  failure code is a denial; a permission request that has its
  `permissionRequest`, an `ownerSessionId` naming this session and an answer;
  an answer that agrees with its request about the call and the verdict;
- every id is consistent: no call is recorded with two outcomes, and no request
  names one call while its answer names another;
- exactly one `session.shutdown` with a known `shutdownType` is present;
- the binary is copilot 1.0.105, the one build whose events have been read.

Otherwise the line says `at least N` and the reason (`file-replaced`,
`truncated`, `file-grew`, `oversize`, `partial-line`, `no-shutdown`, `unknown-shape`,
`unverified-version`, `shared-pin`), or `unknown` when nothing could be read
(`home-unknown`, `session-unknown`, `reader-error`, `not-started` for a turn
refused or failed before its instruments existed, a refused state home
included, and `not-armed` for a turn that had no denial probe: an unpinned turn
on a build without the usage flag, or an unresolvable binary, whether the turn
ran or was refused). Every copilot turn of a roster member writes exactly one
`last_denials`, so a member's meta is never left as the previous turn's). An event kind that is not on the list, or a failed
tool call whose code is not a denial, is `unknown-shape` until live evidence
adds it: a turn is never called clean on a record the reader does not know. A
resumed pin is always `shared-pin`: a native CLI could attach to it. A pin
founded by `engine run` is `shared-pin` too, because only a roster turn holds
the member's lock. A run
under `run_as` or a container is `home-unknown`, because the child's home is
not this process's. Past 1000 denials `observed` stays at 1000 with the reason
`oversize`, and `by_tool` and `by_class` stop at the same 1000. "0, complete"
means none observed in the proven slice, nothing more.

A turn that is not pinned now gets a minted `--session-id` (on a build that
takes `--usage-output-file`, 1.0.82 and later), so the events file is known
before the CLI starts. `ollama-copilot` has no instruments and no denial
record.

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
[resident session](#resident-sessions) start
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
   Append; edit an existing line only when told to consolidate.
                                     [with --protect-agents-md on, appends
                                      "Never edit AGENTS.md."]

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

### Resident sessions

Resuming `claude -p` across a process boundary misses the provider's prompt
cache 40.6% of the time against a 2.5% same-process baseline (measured, #155).
`--resident` keeps one engine process alive instead of starting a new one per
message:

```bash
r4t engine claude run --resident --agent my-node "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"
r4t engine agy run --resident --agent my-node "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"
r4t engine codex run --resident --agent my-node "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"
r4t engine cursor run --resident --agent my-node "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"
r4t engine claude stop --agent my-node
```

The first `run` starts a detached bridge for `(DIR, agent)` and returns 0 at
once, so the router is never held up. The bridge spawns the engine in its
long-running mode: `claude -p --input-format stream-json --output-format
stream-json --verbose --replay-user-messages` with the `a8s_tell` MCP server injected via
`--mcp-config`; `agy --input-format stream-json --output-format stream-json
--print=`; `codex app-server` (see [Codex](#codex-app-server) below); or
`agent acp` (see [Cursor](#cursor-agent-acp) below). The opening turn carries the same [scaffold](#the-scaffold-default-on---no-scaffold-sends-prompt-untouched)
a one-shot turn gets, with resident closing steps in place of the exit steps:
the tell step (`a8s_tell` on Claude, Codex, and Cursor; the `tell` CLI on `agy`, which
has no per-invocation MCP flag), the [`LESSONS.md` note](#lessonsmd-upkeep) when a
consolidation is due, and the first message. The session consolidates first,
then answers, and keeps running. Each later `run` queues its
message and exits 0; the bridge pushes it into the same conversation as a new
user turn, so the session needs no `tells` monitor of its own.

All resident adapters serialize protocol writes with a five-second deadline
covering writer admission, writing and flushing. Stop or fatal state interrupts
the wait. A failed write ends the session without retry. Normal EOF close has
one second to finish before the engine's quiet grace is forfeited. Abrupt exits
terminate the engine before buffered stdin close or writer joins; these waits
are bounded too.

- `--timeout S` (default `2700`, 45 minutes, with `--resident`) — the
  quiet window, not the session's life. A turn in flight never ends the
  session, and any turn sent or frame received restarts the window. After
  this many seconds with no turn in flight and no activity, the bridge lets
  a racing turn finish (up to 2 minutes), sends one wrap-up turn to rewrite
  `STATUS.md` and `LESSONS.md` (up to 5 minutes), and closes the engine.
  agy's own `--print-timeout` is dropped in this mode.
- On Claude, a message that arrives while a turn runs tools joins that turn,
  and one result answers the whole turn. The bridge writes a `uuid` on each
  message. Claude echoes each message it takes (`--replay-user-messages`)
  with that `uuid`, and its result lists the `uuid`s the turn answered
  (`user_message_uuids`, at most 64). At a result the bridge counts as
  answered the messages the list names together with the messages echoed
  since the last result that counted any, each once. A result with no list,
  or a list that is not all strings, counts the echoed messages only. A
  result with an empty list counts none; echoed messages wait for a later
  result. An id the bridge did not write, such as a turn that a peer
  session or a channel injects, counts for nothing, and so does a second
  echo of one id. A message that arrives during the turn's final reply gets
  a turn of its own. With the flag, a result never counts a message by
  order alone: a `claude` that took the flag but neither echoed nor listed
  a message would leave it in flight. Claude Code 2.1.x does both.
- A `claude` that does not know `--replay-user-messages` exits at start with
  `error: unknown option '--replay-user-messages'` before it writes a frame.
  The bridge then starts the session once more without the flag and keeps
  every message; a message it cannot put back in the inbox is reported
  failed at once. It writes `echo-refused.json` in the session's state
  directory so later sessions skip the flag. The file names the program's
  path, size and modification time (and those of the `claude` on `PATH`
  under a launcher such as `ollama`); when any of them changes, the flag is
  tried again. Without the flag, each result counts for its oldest message.
- Sessions run one after another, never side by side. A message that arrives
  during the wrap-up waits, and the same bridge starts the next session from
  the `STATUS.md` the wrap-up wrote.
- `--idle` with `--resident` runs no turn: the session wraps itself up.
  `--memory` still gets its idle pass.
- The bridge outlives the wake that started it. It leads its own session
  on POSIX; on Windows it starts outside the a8s wake's job object, so a
  forced stop of that wake that the node's handler carries out leaves it
  running. A handler too hung to take the stop is killed from the CLI with
  the wake's process tree, which reaches the bridge while the process that
  launched it still runs. A job that forbids breakaway
  refuses that launch; the bridge then starts inside the job, the turn's
  log says so in one line, and a forced stop of the wake ends it.
- `r4t engine <id> stop [--dir DIR] --agent NAME` ends the session now: no
  wrap-up turn, and queued messages are dropped, as is a message that arrives
  during the stop. The bundled resident a8s
  definitions run it from `stop.invoke`, so `a8s stop`, `restart` and `kill`
  end the session too.
- `r4t engine <id> status [--dir DIR] --agent NAME` prints one word and
  exits 0: `busy` when a turn is in flight (or the bridge is starting with a
  message queued), `resident` when the session is up with no turn in flight
  (a closing session counts), and `idle` with no live bridge or a stopping
  one. It exits 1, printing nothing, only when it cannot read the session's
  state; an update then treats the node as possibly mid-turn and does not
  stop it. The bridge keeps the in-flight count in
  `state.json` (`turns`). The bundled resident a8s definitions run it from
  `status.invoke`, which is how `a8s ps` shows `busy` and `resident`.
- If the engine exits before it reads its first message, the queued messages
  are dropped and the bridge exits; the next message starts again.
- State lives in `<DIR>/.engine-resident/<agent>/`, which ignores itself in
  git. `resident.log` there holds the bridge's log and the engine's replies.
- Supported on `claude`, `ollama-claude`, `agy`, `codex`, `cursor`, `devin`, and `muse`; other
  engines fail closed with the supported set. Cannot be combined with
  `--continue` or `--session`.

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
- Initialization and verified thread creation share a 20-second deadline from
  session creation. Both must succeed before queued input reaches the model.
  A missing response ends the session and records held messages as failed.
- One turn is on the wire at a time. app-server merges a turn sent mid-turn
  into the running turn, so a message that arrives mid-turn waits for the next
  turn.
- Verified child completion notices request a bounded reconciliation turn after
  active turns and queued messages finish. The parent is asked to inspect the
  child results and continue only unfinished authorized work. Notices coalesce;
  each notice gets at most one attempt, with at most 32 notices per turn and
  32 attempts per session.
  Stop, failure and session closing prevent new attempts. Rejection or a failed
  write does not retry the notice. Identity or attempt limits end the session
  with an error. This applies only to Codex and never starts an idle session.
  Native activity and a completed reconciliation turn do not prove that the
  model used the child result or avoided repeating work; live acceptance remains
  unverified.
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
- On Linux, stop and replacement enqueue can sweep a dead bridge's recorded
  process group after Codex has exited, including descendants that ignore
  SIGTERM. Matching pid/state records and a captured start token are required;
  a live leader must match that token and group, and an absent leader must be
  from the current boot. Missing or mismatched evidence prevents this sweep.
- `r4t engine codex check --resident` runs the handshake without a turn:
  `initialize` for the version and an ephemeral `thread/start` for the policy
  in force. A version other than the proven 0.163.0 is accepted with a note.

#### Cursor: `agent acp`

A resident `cursor` session is one `agent acp` process (JSON-RPC on stdio)
holding one session. Proven on agent 2026.10.22-27dcf5f, on WSL only.
Windows and macOS are untested. This machine has no Mac, and no `agent` on
the Windows `PATH`. On Windows, `terminate()` hits `agent.cmd` and leaves
the node child, the same gap the codex resident already has.

`AcpSession` in `engines/acp.py` is the generic session. It checks that
initialize and authenticate both answered and that the protocol version is
1, holds every prompt but one, assembles `agent_message_chunk` text into a
single assistant message per turn, and answers `session/request_permission`
by the offered option's kind or with `cancelled` when that kind is absent.
Every line it writes is JSON-RPC 2.0. A malformed frame that belongs to the
session ends it, and once that error is set the session stays failed.
Authenticate runs only when the subclass's method is in `authMethods`; an
unoffered method fails closed in the resident session as well as in
`check --resident`. The deadline ends a process that never becomes ready.
Another engine whose CLI speaks ACP subclasses this and does not reimplement
it.

`CursorSession` is that subclass for Cursor. It supplies `cursor_login`,
the catalog pin via `session/set_config_option`, the proven-version note,
and the replies to `cursor/ask_question` and `cursor/create_plan`. The
resident bridge registers the engine, ends a handshake that misses its
deadline, and stops an ACP process with the sequence below. Other engines
still exit on stdin EOF.

- The preset `agent -p --trust --force --approve-mcps` becomes `agent acp`.
  `-p`, `{prompt}`, `--trust`, `--approve-mcps`, `--model`, and `--force`
  are not spawned. `--permissions bypass` becomes `agent --force acp`.
  The model slug is carried beside the argv and pinned after `session/new`.
  The restricted resident definition does not pass `--force`, so a tool
  call is asked and rejected once. Measured on agent
  2026.10.22-27dcf5f: with `--force` a shell call is not asked; without it
  the server asks. The two handshakes match once the session id is set
  aside. Stdin EOF does not end this agent. The engine is its own
  process group. Every exit (quiet expiry, a fatal frame, a missed
  handshake, `check --resident`, and `stop`) is one sequence:
  `session/cancel` when a turn is in flight and the session is not already
  fatal, at most 1.5 s including cancellation delivery and settlement. Normal
  shutdown then closes stdin. The EOF grace applies only to a
  quiet expiry and a wrap-up that finished: cursor waits 1.5 s and codex,
  claude and agy wait 10 s. A stop, a fatal, a missed deadline, a failed
  write, and an engine exit terminate before buffered stdin close. A fatal
  that arrives during the grace ends the wait, and the signal goes out at once. On POSIX,
  SIGTERM to the group recorded at spawn. The
  stop polls that group, reaping the leader, until it is empty or 2 s
  pass, then SIGKILL. The group id is the engine pid from
  `start_new_session`, stored at spawn; the stop does not call getpgid. A
  leader that has already exited is not passed to `os.kill`; only the
  recorded group is signaled. On Windows there is no SIGKILL and no
  killpg: wait out the grace, then terminate(), then up to 2 s, then
  kill() on the leader. `terminate()` hits `agent.cmd` and leaves the
  node child. Windows and macOS were not run. The bridge's own group is not
  signaled. A failed write, a fatal frame, or a missed handshake settles
  and logs the held message and does not run the wrap-up. A turn still in
  flight when the session ends comes off the count, and one dropped line
  names the cause: stop, deadline, failed write, fatal, or engine exit.
  Deadline is only a handshake or a wrap-up whose clock ran out. `turns`
  is 0 after every end. A session error
  does not disarm the handshake deadline; the deadline stays armed until
  the handshake completes. `stop` also signals the engine group published
  at spawn, so a bridge that died outright does not leave that group
  behind. A dead leader's group is signaled only when its start token is
  from this boot. That boot check is Linux only; macOS has no boot id to
  read, so the check does not apply there. On Windows a new bridge does
  not end the engine a dead bridge published; only `stop` signals a
  recorded pid. Memory is decided after
  the engine has exited and the
  reader has stopped. It is 0 only when there was no fatal frame, no turn
  left in flight, the engine did not exit by itself with a non-zero code,
  and no deadline was missed. A quiet expiry and an idle stop are 0. A
  stop that interrupts a turn, a fatal frame, a missed deadline, or an
  engine exit with a turn in flight is not. A signal the bridge sent,
  including the 143 from `agent acp` on SIGTERM, does not count as the
  engine's own exit. Claude and agy merge stderr onto the frame pipe, and
  a bare JSON value there is logged and skipped. ACP and codex do not; a
  frame that is not an object ends the session.
  Measure the next ACP engine before assuming it exits on EOF.
- After `session/new`, the bridge pins the catalog value with
  `session/set_config_option`. The slug `composer-2.5` is the option's name;
  its catalog value is `composer-2.5[fast=true]`. A bracketed slug such as
  `grok-4.6[effort=high]` matches the catalog value that contains those
  pairs. Two such values say the model name is ambiguous. A `currentValue`
  that already matches is left alone.
  Unset `MODEL` means `composer-2.5`. A separate `EFFORT` variable is
  ignored, with one warning; effort belongs in the model slug's brackets.
- The a8s MCP server goes on `session/new` as `mcpServers` (`env` is a list
  of `{name, value}`). The tell step names `a8s_tell`. One-shot cursor turns
  stay on the `tell` CLI.
- One prompt is on the wire at a time. Text arrives as `session/update`
  chunks (`agent_message_chunk`) and is delivered as one assistant text
  per turn.
- When a permission request does arrive, the client selects the offered
  option whose kind is `allow_once` if `--force` is set, and `reject_once`
  if it is not. Measured option ids are `allow-once`, `allow-always`, and
  `reject-once`. When the wanted kind was not offered, the reply is
  `cancelled`. Ids are opaque and are never invented.
  `cursor/ask_question` and `cursor/create_plan` are declined so a headless
  turn cannot block.
- Stderr goes to `resident.log`; stdout carries only JSON-RPC.
- `r4t engine cursor check --resident` sends `initialize` first.
  `authenticate` (`cursor_login`) follows only when that method is in the
  initialize `authMethods`; otherwise the check fails closed. `session/new`
  follows the authenticate answer. No prompt is sent. A version other than
  2026.10.22-27dcf5f is accepted with a note.

### Devin ACP

`r4t engine devin run --resident` uses `devin acp` with the existing CLI login.
The adapter sends initialize then session/new; it never calls the advertised
browser authentication method. Missing login fails locally without retry.

The session pins its advertised controls before sending any prompt: default
permissions use `accept-edits`, `auto` uses `smart`, `ask` uses Devin's read-only
`ask` mode, and `bypass` uses `bypass`. Standard mode permits one a8s tell
when native metadata identifies that exact tool in the active turn. Missing,
conflicting, stale or consumed call identities are rejected. Ask and smart
reject client permission requests; bypass selects only the offered `allow_once`.
After 256 distinct tracked call IDs, standard mode denies further grants in
that session instead of forgetting replay protection.
These are vendor permission controls, not an operating-system sandbox.

`--model` requires an exact value from the session's model catalog. `--effort`
selects a value from the resulting model's `thought_level` catalog. Missing
controls, unknown values, rejected pins, or an effective-value mismatch fail
before prompts. One-shot Devin retains its separate permission restrictions.
The a8s MCP server is supplied through session/new without writing Devin config.

`r4t engine devin check --resident` verifies that handshake and effective pins
without a model prompt. Devin 3000.11.205 was measured on Linux: idle EOF exits
cleanly; quiet teardown permits 10 seconds. Explicit stop has no EOF grace.
Native Windows behavior requires separate host verification.
`engine-devin-resident` and `engine-devin-resident-unrestricted` provide message,
batch, idle, stop, and status hooks; the latter explicitly selects bypass.

### Muse MSP

A resident Muse session uses MSP on stdio. The adapter is measured on
Muse Code 1.4.13 on Linux with its local echo provider. Windows and provider
model turns require separate validation.

The bridge sends `initialize`, waits for schema version 1 and the
`sessionMcp` capability, then sends `initialized` and `session/start`.
Every command carries a UUIDv7 command id. The returned session must confirm
the requested model and approval mode before the first turn is released.
`MODEL` selects `session/start.modelId`; `EFFORT` is supplied on every
`turn/start`. Unset values keep the engine defaults.

The base definition and `auto` select the native `:ask-me` profile with
`onRequest` approvals. Before prompts, the adapter verifies the profile's
human reviewer authority, sandboxed filesystem and disabled local-command
network access through the experimental permission-state API. It grants only
the offered once choice for an exact a8s tell in the active parent turn;
other approval requests are rejected. `--permissions ask` selects the same
profile but rejects every client approval request. Missing policy evidence,
changed profiles or unexpected automatic review fail the session.
After 256 tracked call identities, the session denies further tell grants
instead of forgetting consumed identities.

The unrestricted definition selects `allowAll`, disables the shell sandbox,
and trusts the workspace's rules for this run. An explicit approval request
from an enforced policy is still rejected with its offered reject-once choice,
including native `abort`. Review completion alone never triggers a decision.
No permission grant is saved or user configuration file replaced. User-input
requests are cancelled. One-shot Muse retains its separate permission controls.

The a8s MCP server is required session configuration, with line-delimited
JSON framing. Muse connects and lists its tools when the first turn starts;
session creation alone does not prove tool availability. A missing
`sessionMcp` grant fails startup. Required-server failures are surfaced in
the local session log and turn outcome. The echo-provider proof verifies
server initialization and tool listing; it does not prove a provider model
can use the tool under every policy.

Only one turn is submitted at a time. `turn/start` acceptance is not
completion: the bridge correlates the turn id and waits for
`turn/completed`. Failed or cancelled outcomes are failures. A reclaimed
queued turn or a reported gap in the event stream fails the session. Final
`item/completed` agent messages supply the text. Stop sends `turn/cancel`
only for an admitted active turn, within the shared bounded cancellation
budget. Abrupt teardown terminates the process group before closing stdin.
Measured idle and echo-turn EOF exits took less
than 0.1 seconds; the normal EOF allowance is a conservative 10 seconds.
An immediate stop skips that allowance. Cursor keeps its separate measured
1.5-second EOF allowance.

`r4t engine muse check --resident` checks `serve --help` and creates an MSP
session to verify policy and model. Restricted checks require a durable empty
session because Muse's ephemeral sessions cannot select a named profile.
The check sends no turn and does not test the lazy MCP connection. No
authentication action is issued.

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
[Codex](#codex-app-server). `r4t engine cursor check --resident` also runs the
ACP handshake (`initialize`, `authenticate`, `session/new`) and sends no
prompt; see [Cursor](#cursor-agent-acp).

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
setting a separate `EFFORT` on Cursor or one-shot Devin is ignored, with one warning.
Resident Devin validates `EFFORT` against the session model’s thinking-level catalog.
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

The 22 non-resident definitions also carry `--add-dir=$ADD_DIRS?`,
`--allow-tool=$ALLOW_TOOLS?` and `--deny-tool=$DENY_TOOLS?` on all three
wakes, so a node's [access knobs](#access-knobs) are a8s vars with no
definition edit. r4t reads the string, so the engine's own spelling stays in
one table:

```bash
a8s vars ada set ADD_DIRS /srv/repos/shared-lib
a8s vars ada set ALLOW_TOOLS 'url(dev.example.org),url(github.com)'
a8s vars ada set DENY_TOOLS 'shell(git push),shell(rm)'
a8s vars ada unset ADD_DIRS     # the element drops; an empty value gives no entries
```

An engine with no flag for a knob fails the turn with the reason. The resident
definitions carry none of the three. Login tokens never go in vars.

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
`engine-codex-resident`, `engine-codex-resident-unrestricted`,
`engine-cursor-resident`, `engine-cursor-resident-unrestricted`,
`engine-muse-resident`, and `engine-muse-resident-unrestricted` wire
`r4t engine <id> run --resident` across `invoke`, `batch.invoke` and
`idle.invoke`, `r4t engine <id> stop` as `stop.invoke`, and `r4t engine <id> status` as
`status.invoke`:

```bash
a8s add my-resident ~/somewhere engine-claude-resident
a8s add my-resident ~/somewhere engine-claude-resident-unrestricted
a8s add my-resident ~/somewhere engine-agy-resident
a8s add my-resident ~/somewhere engine-codex-resident
a8s add my-resident ~/somewhere engine-cursor-resident
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
state database first, then the `agent` CLI's `auth.json`, then on macOS the
CLI's Keychain item (`cursor-access-token`). The answer notes which store
answered: `token from the IDE state database`, `token from the agent CLI's
auth.json`, or `token from the agent CLI's Keychain item`. The auth file is
`$XDG_CONFIG_HOME/cursor/auth.json` or `~/.config/cursor/auth.json` on Linux,
`%APPDATA%/Cursor/auth.json` on Windows, and `~/.cursor/auth.json` on macOS
(used there when `AGENT_CLI_CREDENTIAL_STORE=file`; the Keychain is the usual
store). A Windows profile this process cannot stat is skipped. When `mode=ro`
on `state.vscdb` raises SQLite `disk I/O error` — a WSL read of the Windows
database while the IDE has it open — the read retries `mode=ro&immutable=1`.
The retry is that error text only, not a path prefix.
Immutable ignores an uncheckpointed WAL; the auth file covers a token that
lives only there. The CLI login does not require the IDE.

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

A failed probe with structured subscription quota detail reports
`Subscription Limit` at 0% remaining and the provider's reset time, even
when no usage window arrives. This live reading replaces the snapshot.
Typed quota refusals with an unknown scope or unusable reset report
unavailable and remove the saved reading, so subsequent automatic fuel
checks also refuse until a new reading succeeds. Missing usage or an
untyped rate-limit error does not establish exhaustion.

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

### `quota --live` — a reading taken now

```bash
r4t engine claude quota --live --json
```

`--live` answers from a query made during the call or not at all. It never
loads a snapshot, never writes one, and accepts only an answer the adapter
marks `"origin": "live"`. It never exits `3`. Any other action refuses the flag
with exit `2`.

Exit `0` prints the usual payload. Exit `1` with `--json` prints, on stdout:

```json
{"origin": "unavailable", "engine": "claude", "applicable": true, "reason": "..."}
```

`engine` is null for a name that is no engine or preset. `applicable` is false
for such a name and for an engine with no subscription to read (`ollama`).

`reason` is fixed text, never provider output. An adapter's ordinary failure
may quote a provider body or stderr, so live mode replaces it with
`<engine> live quota query failed — <login repair hint>, then retry`. Only a
failure an adapter raises as the explicit safe subtype (`LiveQuotaError`: an
account guard, a missing binary, a version or schema refusal) keeps its own
fixed text. The plain `quota` path is unchanged.

Adapters read the process's own environment and working directory; a caller
that wants a node's account launches `r4t` under that environment. A context an
adapter cannot honor is refused before any query, with a reason that starts
`account mismatch` and names the variable, never its value:

- `claude` reads `$HOME/.claude/.credentials.json` or the macOS Keychain. It
  refuses a `CLAUDE_CONFIG_DIR` other than that folder, an API-key or cloud
  mode (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`,
  `_VERTEX`, `_FOUNDRY`), `CLAUDE_CODE_OAUTH_TOKEN` beside a stored login, and
  on macOS a `HOME` that is not the user's home.
- `codex`, `claude` and `copilot` refuse a state-home variable that is not an
  absolute path; a caller `env` the adapter cannot read is refused.
- `agy` runs `agy -p /usage` only. The machine-wide language-server lookup
  cannot be tied to the requested account, so live mode neither falls back to it
  nor reads a plan name from it.
- `cursor` is unavailable in live mode (unsupported live binding): the plain
  check takes the first login that answers across the IDE database, every WSL
  Windows profile and the CLI's `auth.json`, so no reading can be tied to the
  configured CLI account. Live mode makes no query and reads no credential.
- `opencode` is unavailable in live mode: its providers' checks read separate
  credentials from opencode's own, so no reading can be tied to its account.

- `copilot` reads its own login and never `gh`: the token comes from
  `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, then the login stored
  under `COPILOT_HOME`, and the direct endpoint answers. `gh` signs in through
  its own store and can be another account. With no readable token the reading
  is unavailable and the repair is `copilot login` in the node's environment.
- `claude` never runs `claude doctor` in live mode. An expired or rejected
  stored login is unavailable with the fixed instruction to run `claude` once
  to refresh it; a refresh rotates the login and can outlast the caller's bound.

Residual: `devin` and `codex` follow `HOME`/XDG and the vendor CLI's
own state; a differing macOS Keychain account is not detectable for them. Codex API-key billing overrides are not independently resolved by this quota read; the result is the CLI subscription account's allowance. Copilot credentials held only in an OS keychain are unavailable to the direct-token adapter.

**Interruption and bounds.** `quota --live` turns SIGTERM into a normal exit
(status 143) that unwinds the adapter's `finally`, so children started in their
own sessions are ended, and restores the previous handler on the way out. A
caller's grace after SIGTERM must cover the longest teardown, 2.5 seconds; a8s
gives 3. Each engine's reading fits a caller's 60 second bound with its
teardown: muse 35 (5 + 25 plus two teardowns), devin 43.5 (20 + 1 + 20 + 0.5 +
2), agy 45, codex 15, claude 50 at worst with no refresh. A test asserts these.

**Muse.** `--live` uses `muse usage --json` from the `muse` on `PATH`, which
reports the subscription window, weekly block and tier with no model turn.
It first runs `muse --version` (5 seconds) and requires 1.4.13 or later; an
older or unidentifiable binary is unavailable with an upgrade hint, and
`usage` is never called on it. Both calls run with stdin closed, in their own
process group, with `MUSE_NO_AUTO_UPDATE=1` so the launcher starts no
background updater. `usage` waits at most 25 seconds, and the group is ended
when each call finishes. Provider stderr is never relayed.

Live refuses, as `account mismatch`, `META_API_KEY`, `TBH_CREDENTIAL_BACKEND`,
a `MUSE_AUTH_PATH` other than `$XDG_CONFIG_HOME/muse/auth.json` (default
`$HOME/.config/muse/auth.json`; the binary ignores the variable), and on macOS
a `HOME` that is not the user's home.

The answer must be schema version 1, `source: "mint"`, with percentages from 0
to 100, reset times in the future and a bounded distance away, and an `asOfMs`
no earlier than this call's start (two seconds of skew) and no later than its
finish. `asOfMs` is a local request stamp, so freshness rests on the `mint`
source. Stale, cached, empty, malformed, timed-out or non-zero answers are
unavailable; a reason names Muse's own `reason`/`hint` only when it is one of
the verified codes (`no_allowance`, `server_sent_no_usage`) and omits anything
else. A tier is kept as the plan only when it is plain letters, digits and
`_.- `. The payload adds `requested_at` (the request stamp, not a provider
observation time) and a note that no model turn was made.

Each call ends with its group torn down: SIGTERM, a 0.5 second grace, SIGKILL,
then a reap that waits at most 2 seconds. The leader is observed without being
reaped, so its pid cannot be reused before the group is signalled: `waitid`
with `WNOWAIT` (Linux, and macOS on Python 3.13 and later), else kqueue
`NOTE_EXIT` (older macOS). Where neither exists the leader is reaped by an
ordinary wait and nothing is signalled by its pid afterwards; descendants that
outlive it are not reached. The same holds on Windows, where a leader that
exited normally is not ended by pid at all; a timeout, where the leader is
still running, ends the tree with `taskkill`. Nothing else may reap the child
(no ignored `SIGCHLD`, no `waitpid(-1)`). A successful read may refresh
Muse's own `auth.json`. The plain
`r4t engine muse quota` path is unchanged.

Live failure JSON also carries a fixed `code`: `account_mismatch`,
`login_refresh_required`, `upgrade_required`, `unsupported_binding`,
`not_applicable`, or `query_failed`. Callers can show repair hints without
copying provider text.

## `models` and `efforts` — what `--model` and `--effort` accept

```
r4t engine <id> efforts [--model MODEL] [--json]
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
| muse | MSP `model/list` asked of `muse serve` (no session, no turn) | fixed list |
| copilot | no list verb | fixed list |
| codex | `model/list` asked of `codex app-server` (no thread, no turn) | the same catalog's `supportedReasoningEfforts` |
| opencode | no list verb | any value |

### Codex catalog

`r4t engine codex models` lists each visible catalog entry's `model` string, the value
`-m` takes. Hidden entries are excluded, and every page of the catalog is
followed. `r4t engine codex efforts` lists the union of efforts across the catalog, with a
note that support varies by model. `r4t engine codex efforts --model MODEL` lists exactly
the efforts that model supports, and fails when the catalog does not list it.
`--model` filters `efforts` for codex only; every other engine, and `models`,
refuse it.

The catalog is the installed Codex's own, which may be bundled or cached. A
listed model is not proof that the account can use it, and `run` still passes
`--model` and `--effort` through unvalidated. A missing CLI, RPC error, timeout,
or malformed or empty catalog fails with exit 1 and no values, as does a
missing or non-list `supportedReasoningEfforts`. An empty list is a fact: the
union skips that model, `--model` returns no values with a note, and an
all-empty union returns none with a note. Codex's stderr is discarded; protocol
errors are still reported. `ollama-codex` keeps `ollama list` for its models.

Each verb ends in one of three outcomes:

- **A list.** Exit 0, one value per line.
- **Any value.** The provider decides, so the list is empty and the note says
  so. Exit 0.
- **A refusal.** The engine has no flag or no list verb; the message names it
  and where the string goes. Exit 1.
