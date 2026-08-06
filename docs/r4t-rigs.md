# Rig configuration reference

Everything that lives in the out-of-repo rig config
(`~/.config/r4t/rigs.json`, relocatable with `R4T_HOME`): presets, model
selection, the settings surface, and the governance knob table. For the
roster side see the [tutorial](r4t-tutorial.md); for why each governance layer
exists see [r4t-governance.md](r4t-governance.md).

## Presets

Rig **names** are yours (`leader`, `member`, `reviewer`, …); **presets** are
CLI templates aligned with [a8s definitions](../apps/a8s/definitions/):
`claude`, `codex`, `cursor`, `opencode`, `copilot`, `agy`, plus the
`ollama launch`-wrapped local variants (`opencode-ollama`, `claude-ollama`,
`codex-ollama`, `copilot-ollama` — see
[r4t-harness-ollama-launch.md](r4t-harness-ollama-launch.md)).

```bash
r4t rig presets                       # list presets + invoke lines
r4t rig add worker opencode           # add rig "worker" from a preset
r4t rig add brain agy --model sonnet  # pick a model for the preset
r4t rig swap worker agy               # switch a rig's preset, keep settings
r4t rig remove worker                 # drop a rig (alias: rm)
```

`r4t rig remove <rig>...` (alias `rm`) deletes one or more rigs. It refuses
if a roster member or pin still references the rig, naming what does; pass
`--force` to remove anyway.

## Continuing a conversation

A member with `- **Continue:** on` in the roster runs its turns inside its
CLI's own conversation instead of a cold prompt every wake: the agent keeps
its recent work, and the provider cache prices the wake as a continuation.
It needs a rig whose preset supports it — `claude`, `codex`, `cursor`,
`opencode`, `opencode-ollama`, `agy` (`r4t rig presets` marks them); anything
else fails closed at `r4t roster check` and at dispatch. Most presets append a
`--continue` flag; `codex` resumes through the `exec resume --last` subcommand,
so its tokens are inserted after `exec` instead. `copilot` is the one
unsupported CLI: its `--continue` resumes the machine's most recent session
whatever the directory, so members cannot be kept apart, and supporting it
cleanly means pinning a session id per member (bin#256).

### What a continuation costs

Continuation is a price optimisation, not a memory mechanism. r4t carries the
member's history in the prompt either way; `Continue:` only decides whether the
CLI *also* replays its own. That replay is nearly free while the provider still
holds the conversation's prefix in cache, and expensive the moment it does not:
the entire conversation is sent again and charged at a premium to write back.

Two separate things make it expensive, and only one is about time.

- **Age.** Cache lifetimes are minutes. Past that the whole prefix is new again.
- **Size.** A large enough conversation is rewritten even during active use,
  because the cache breakpoints move as it grows. Waking a member often enough
  to keep it warm does not help — it keeps the liability alive.

So a preset may carry three limits: `continue_warm_seconds`,
`continue_max_context_tokens` and `continue_max_transcript_bytes`. Past any of
them the turn silently drops its continue tokens and the CLI founds a fresh,
small conversation, logged as `CONTINUE-CHILL`. Nothing is lost: that turn's
prompt carries r4t's own bounded transcript, and later turns continue the new
conversation cheaply. Every completed turn also logs a `CACHE` line — tokens
read, tokens written, and the size of the context now in play — which is the
signal to tune the limits against.

The limits are per-harness because cache behaviour is, and they are set only
for harnesses somebody has actually measured. An unmeasured preset is not
gated, because guessing a window is a way to pay the premium on purpose.
`claude` is measured today; the rest await their research page.

A CLI keeps ONE conversation per directory, so two members running the same
CLI from the same effective directory (the workplace root, or their resolved
`Workdir:`) land in the same one. `r4t roster check` warns when that happens —
it never blocks, but the fix is another CLI or distinct `Workdir:` lines.

`- **Continue:** 4h` (bare seconds or a duration with an s/m/h/d suffix)
continues the conversation *and* bounds how long it may sit idle before it is
retired — dumped to disk, then refounded from that state on the next real
message. `on` leaves the window open; anything but `on`, `off`, or a positive
duration is a roster error, so an idle window can never ride a member that
runs cold. That retirement is the **flush** on an
[idle pass](r4t-idle.md): the pass retires a conversation idle past the
duration by running a budget-gated dump turn (a normal continuing turn
prompting the member to save its state to STATUS.md). A rig swap that changes the CLI retires the conversation
immediately, with no dump turn — the old CLI may be quota-dead. A retired
member's next turn runs cold with a read-your-state preamble; the dump prompt
and preamble are overridable via the node definition's `prompts` object (keys
`flush_dump` and `refound_preamble`).

`r4t flush <member> [<member> ...]` (or `--all` for the whole roster) does the
same on demand, for any member — no window and no idle wait. It runs
the dump turn, retires the conversation, and then archives the member's
`agents/<member>/history.md` to a timestamped sibling, so the refound reads
STATUS.md rather than a transcript of everything it was told. Nothing is
deleted; the archive stays on disk. `--no-dump` skips the turn for a
conversation that cannot dump (quota-dead) or should not (one whose recent
context is itself the problem — a dump would bank it into STATUS.md). A dump
turn that fails changes nothing: the conversation and the history stay put,
and the exit code names the member. The idle sweep keeps the history in place,
because there the refound is meant to carry rolling context forward.

## `Workdir:` and the root the harness advertises

`- **Workdir:** <path>` in the roster runs a member's turns from its own
directory (relative paths resolve against the workplace; absolute and `~`
paths may live outside the repo). r4t sets the harness subprocess cwd to it,
states it in the prompt as the absolute root everything the member writes
belongs under, and hands it to any harness that takes its working directory as
an argument — `{workdir}` anywhere in an `invoke` is substituted with the
member's resolved absolute path (the opencode presets pass it as `--dir`).

Two knobs, because the cwd alone does not reach every harness. `PWD` is a shell
convention no kernel maintains, so a spawned process inherits the `PWD` of
whoever started r4t however its cwd is set, and a harness that resolves paths
against `PWD` lands outside the workdir. r4t pins `PWD` to the workdir in every
turn env for that reason; `{workdir}` is the second pin, an absolute path in
the argv that depends on no environment at all.

**Rigs still do not all treat that directory as the project root.** The
opencode-family presets keep two paths: the working directory (which their file
tools anchor on, and which `--dir` pins) and a separate *workspace root*
discovered by walking up for a `.git` — the enclosing repo. Both are shown to
the model, and a model that takes the advertised root at its word writes there
by absolute path. `claude`/`cursor` advertise no competing root, so their
members stay in the workdir. No opencode flag or env var pins that root:
`--dir` sets the working directory, and the root is always the git walk-up from
it.

The `ollama launch`-wrapped presets inherit their parent's behavior and nothing
worse: the launcher execs the integration in the directory r4t spawned it in,
so an `*-ollama` member's harness runs in the workdir (measured on ollama
0.32.5 against all four wrapped integrations — the launcher, the harness, and
every descendant report the workdir as their cwd). `opencode-ollama` therefore
carries the same advertised-root caveat as `opencode`, and `claude-ollama` /
`codex-ollama` / `copilot-ollama` stay in the workdir like their parents.

So the prompt is the portable mitigation, and the only one that reaches every
rig: the intro states the member's absolute working directory, tells it to
write under that path rather than trusting a bare relative one, and tells it
to ignore any workspace/project root its tools advertise. It is doctrine, not
a guarantee — models obey it imperfectly, and it is overridable per node via
the `prompts` object's `intro` key. When placement has to be certain, change
the filesystem instead: give the workdir its own `.git`
(`git worktree add agents/bob`), or point `Workdir:` at an absolute path with
no repo above it. See bin#273 for the investigation and the obedience
measurements.

## Picking a model (`--model`)

`r4t rig add` and `r4t rig swap` take an optional `--model`. For most presets
(`claude`, `codex`, `cursor`, `opencode`) it is spliced into the invoke at add
time — `--model <alias>` for claude, `-m <id>` after `exec` for codex, after
`run` for opencode — and omitting it lets the CLI's own default apply. The
`ollama` preset and the `ollama launch`-wrapped presets have no default, so
their `--model` is required and names a local model tag.

`agy` is different: its `--model` takes an exact display name from `agy models`
(short aliases are silently ignored), and those names carry version numbers
that change as agy ships releases. So r4t stores the friendly string you give
(`--model sonnet`) and resolves it against the live `agy models` list before
**every** turn — never a pinned table that could go stale. Matching is
case-insensitive with dashes and spaces interchangeable (`gemini-3.5-flash`
matches "Gemini 3.5 Flash (Medium)"); when several names match, the tie-break
prefers the fewest extra tokens, then the highest effort suffix
(thinking > high > medium > low), then alphabetical order. A string that
matches nothing fails the turn loudly with the available names — an unresolved
value is never passed through, because agy would silently run its default.

The `agy` preset runs **without** `--sandbox`. agy's sandbox confines the
agent's child-process writes to the CWD, which blocks `tell` (its staging
outbox lives outside the workplace repo) — the whole capability map and the
2026-07-14 incident are in [r4t-harness-agy.md](r4t-harness-agy.md). Like every
other r4t preset, agy is trusted with normal filesystem permissions.

## Editing a rig's settings (`configure` / `set` / `get` / `unset`)

Rig settings never need hand-edited JSON. The configurable keys are
`concurrency`, `rig_budget_max`, `rig_budget_earn_per_hour`, the context knobs
`history_max_bytes` / `history_body_max` / `prompt_body_max`, `model`, `mcp`,
the echo keys `echo` / `echo_max_chars`, and `env.<NAME>` for a
[harness env knob](#harness-env-knobs-env)
(each detailed in the [knob table](#governance-knobs) below).

```bash
r4t rig configure specialist          # walk every setting, Enter keeps each
r4t rig set specialist concurrency 2  # write one explicit value
r4t rig get specialist                # list effective settings, source-annotated
r4t rig get specialist concurrency    # one value on stdout (script-friendly)
r4t rig unset specialist concurrency  # drop it back to the default
```

`configure` prompts one key at a time, showing the effective value and its
source in brackets (`history_max_bytes [25000, from preset opencode]:`).
**Plain Enter keeps the current state exactly** — an explicit value stays
explicit and an inherited tier default stays inherited; it is never written
into `rigs.json`, so `rig swap` can still re-resolve the tier. Only typed input
becomes an explicit value. Piped stdin works (one answer per line, EOF keeps
the rest), so an agent can drive it non-interactively.

`get` annotates each value's source: `explicit`, `from preset <name>` (a
context knob inheriting the preset's text tier), `built-in default`, or
`not set` (an `env.<NAME>` the rig does not carry). With a
key it prints the bare value on stdout and the source on stderr, so
`conc=$(r4t rig get specialist concurrency)` captures cleanly.

`model` is special: `set`/`configure` re-resolve the invoke through the rig's
recorded preset, exactly like `rig add --model` (agy keeps its live fuzzy match
per turn). A rig with no recorded preset errors, pointing at
`r4t rig swap <rig> <preset> --model ...`. Raw `invoke` arrays are never
exposed through this surface; use `rig add`/`swap` to change the harness.

## Echo rigs

`r4t rig set <rig> echo true` makes the rig's members stdout-only: their turn
prompt carries no `tell` instructions or messaging doctrine — just who they
are, their history, and the new messages — and the turn's cleaned stdout is
staged as the one reply to the sender (`ECHO-REPLY` in the log), through the
same release gates every send passes. Use it for models that misuse `tell`
(small models told to message via a shell tool can loop, while the same model
simply asked a question just answers). A reply longer than `echo_max_chars`
(default 1500) is truncated in the body with the full text attached to the
same envelope as a markdown file; empty or chrome-only output stays silent.

`r4t:<node>` is the dispatcher's own voice — the flush dump prompt, an error
notice, a mission review — and no mailbox. A turn whose whole batch came from
it has no sender to answer, so the stdout stays transcript (`SILENT` in the
log); the same rule holds the `STDOUT-REPLY` fallback on non-echo rigs. A batch
mixing r4t's prompt with a real message still replies to the real sender.
On non-echo rigs, `- **ProseReply:** off` on a roster member (default on) mutes
the `STDOUT-REPLY` fallback for that member: its prose-only turns log `SILENT`
instead of staging a reply.

## The `a8s_tell` tool (`mcp`)

The `mcp` knob gives the rig's members a real tool instead of a shell command:
every turn spawns `a8s mcp serve` through the harness's own config, and the
prompt teaches the `a8s_tell` tool by name. The message body travels as a tool
argument, so `$1.25`, backticks and Windows paths reach the recipient byte-exact
with no quoting for the model to get right — and on a small local model that
also lifts the "said it, never sent it" rate, which is what the tool buys over
the heredoc teaching.

It is **on by default** on `claude`, `codex`, `copilot` and `opencode` (and
their `ollama launch` variants): their idioms are a flag, a `-c` override or a
config file under the member's own state dir, so nothing lands in the roster repo.
`cursor` is **opt-in** — its only idiom writes `.cursor/mcp.json` into the
working tree, and writing a file into your repo is a different consent level
than passing a flag. `r4t rig set <rig> mcp off` is the escape hatch on any rig,
and `r4t rig get <rig> mcp` says whether the value is `explicit` or came `from
preset <name>`. Two presets have no per-turn path at all — `agy` reads MCP
config only from `~/.gemini`, bare `ollama` has no tool use — so they resolve
off silently and `rig set <rig> mcp on` errors there with a
`(try: r4t rig swap <rig> ...)` hint rather than running turns whose tool never
appears. Under an org boundary (`run_as` / `container`) r4t carries each
harness's idiom across and fails the turn closed when it cannot — see
[isolation](r4t-isolation.md#the-a8s_tell-tool-behind-the-boundary).

## Harness env knobs (`env`)

A rig may carry static `NAME=value` pairs handed to its harness on every turn.
**Use it frugally.** Every entry earns its place with a documented reason: this
is the one rig key whose effect r4t cannot see, so a dumping ground here is a
roster whose behaviour nobody can explain from the config.

```bash
r4t rig set brain env.ENABLE_PROMPT_CACHING_1H 1  # the 1-hour prompt-cache tier
r4t rig get brain env.ENABLE_PROMPT_CACHING_1H    # bare value, source on stderr
r4t rig unset brain env.ENABLE_PROMPT_CACHING_1H
```

The proven case is Claude Code's prompt-cache TTL. On API-key auth the CLI
writes the 5-minute cache tier, so a member woken ten minutes after its last
turn re-reads its whole context at full price; `ENABLE_PROMPT_CACHING_1H=1`
opts into the 1-hour tier, which is the highest-leverage cost knob there is for
scheduled wakes minutes to tens of minutes apart. On subscription auth the
1-hour tier already applies and the variable is a no-op, so it is safe on any
claude rig.

Values are literal strings — no `{prompt}`-style substitution, nothing
expanded, no shell. Names are validated where they are written: r4t's own turn
variables (`TELL_OUTBOX_DIR`, `PWD`, and the `R4T_*` family, which carry the
member's staging outbox, its pinned workdir, and node / member / isolation /
continue state) are refused by `rig set` and fail a hand-edited rig closed at
`rig get` / `roster check` / the first turn. The turn owns those, and a rig that
could quietly redirect them would steer dispatch from a config file. r4t's
per-turn `mcp` injection likewise wins any variable it sets (`OPENCODE_CONFIG`).

Under an org boundary the map is named to the wrapper the same way the `mcp`
idiom's env is — re-exported past sudoers `env_reset`, passed as `docker run -e`
— so an isolated org's rig env still arrives (see
[isolation](r4t-isolation.md)).

## The economics: budgets, not cuts

A member runs while its own spend bucket, the shared cell bucket, and (if the
rig declares one) the rig's own bucket all hold ≥1 unit (a turn costs 1 of
each). An empty bucket means the member is *resting* — its queue holds and it
runs again when the bucket refills. Messages are never dropped for lack of
budget.

The rig bucket is the quota answer. A rig maps to a real subscription (an
Antigravity plan good for ~20 prompts an hour, a Claude seat), so its ceiling
is set **on the rig** and is **machine-global**: it binds every r4t roster on
the machine that shares the rig, so one subscription is safely shared across
projects. Its bucket lives in `~/.config/r4t/rig-buckets.json` (outside any
roster) and every node charges it atomically. Budget refill IS the retry: an
exhausted rig rests every member on it, on every roster, and the held queues
catch up when it refills — r4t is the retry system so a8s stays dumb delivery.

A subscription can run dry mid-plan without any error: agy/claude/opencode all
exit 0 with a **blank** response when out of quota. So a turn that exits 0,
releases nothing, and prints not one byte is treated as quota-suspect
(`QUOTA-SUSPECT` in the log) and drains the rig bucket, resting the whole rig
until it refills. The rule is deliberately conservative — only a *truly empty*
transcript triggers it, never chrome-only output from a quiet-but-alive member.

## Governance knobs

Per-rig keys go inside a rig block; the rest are top-level. Governance
defaults apply with no extra configuration — a rig config with only rig
invoke lines is a fully governed roster. Rationale and prior art per layer:
[r4t-governance.md](r4t-governance.md).

| Key | Default | Governs | Failure mode it stops |
|---|---|---|---|
| `budget_max` / `budget_earn_per_hour` (rig) | 8 / 4 | Per-member spend bucket. A turn costs 1 unit regardless of how many queued messages it consumes; empty = resting. Put frontier rigs on a low budget (slow, smart), local rigs on a high one (near-free) | Money burn; a fast rig outrunning its quota |
| `rig_budget_max` / `rig_budget_earn_per_hour` (rig) | unset (no rig gate) | Machine-global rig spend bucket for the subscription behind the rig. A turn also costs 1 rig unit; when empty, every member on that rig rests on every roster. Set both together to bind a shared plan (e.g. 20 / 20 for ~20 prompts an hour) | A shared subscription outrunning its real quota across projects |
| `max_sends_per_turn` (rig) | 6 | Envelopes released per turn; excess dead-letters | Runaway fan-out width |
| `history_max_bytes` / `history_body_max` / `prompt_body_max` (rig) | by preset tier — big (agy/codex/claude) 50k/12k/24k · moderate (cursor/opencode/copilot) 25k/6k/12k · small (ollama variants, or no preset) 8192/2000/4000 | Context sizing on the rig: rolling-history budget, per-entry history clip, and per-message prompt clip. `rig add`/`swap` record the preset; explicit values override the tier | A weak rig drowning in context, or a strong one starved of it |
| `echo` / `echo_max_chars` (rig) | false / 1500 | Stdout-only members (see [Echo rigs](#echo-rigs)): no messaging scaffolding in the prompt, cleaned stdout staged as the one reply, bodies past the cap truncated with the full text attached | A model that misuses `tell`, looping "I did it" messages instead of answering |
| `mcp` (rig) | by preset — **on** for claude/codex/copilot/opencode and their `ollama launch` variants; **off** for cursor (its idiom writes `.cursor/mcp.json` into your repo) and for agy / bare ollama (no per-turn idiom) | Members send with the `a8s_tell` tool instead of the `tell` shell command (see [The `a8s_tell` tool](#the-a8s_tell-tool-mcp)): `a8s mcp serve` is injected per turn through the harness's own idiom and the prompt names the tool. `mcp off` is the escape hatch anywhere; `mcp on` errors on agy and bare ollama | Shell quoting mangling a body, and a member that describes a message instead of sending one |
| `env` (rig) | empty | Static `NAME=value` pairs handed to the harness every turn — harness knobs r4t has no flag for (see [Harness env knobs](#harness-env-knobs-env)); set one at a time with `r4t rig set <rig> env.<NAME> <value>`. Frugal by doctrine; r4t's own turn variables are refused | Money burned on a harness default you cannot reach any other way — the first case is `ENABLE_PROMPT_CACHING_1H=1` on claude, the 1-hour prompt-cache tier for wakes minutes apart |
| `timeout_seconds` (rig) | 900 | Harness wall clock; the process group is killed | Hung harnesses |
| `concurrency` (rig) | 1 | Live turns within one rig | Rig-wide pile-ups |
| `cell_budget_max` / `cell_budget_earn_per_hour` | 16 / 8 | Shared cell spend bucket; a turn also costs 1 cell unit. When empty, everyone rests | Whole-cell money burn |
| `throttle.max_concurrent` | 1 | Live turns across ALL rigs | Roster-wide pile-ups |
| `throttle.min_seconds_between_turn_starts` | 15 | Cadence floor between turn starts; a member that can't start yet keeps its queue and runs later | Invisible burn — a storm degrades into a watchable drip |
| `quiet_task_seconds` | 1800 | Backstop: an open thread whose originator has not been answered and that has seen no activity for this long wakes the leader with a nudge to report current state. 0 disables the sweep | A thread that dangles — a turn "succeeds" without replying and the originator never hears back |
| `log_retention_days` | 14 | Days of roster transcript kept under `log/`; maintenance deletes older days whole and says so in the log. 0 keeps everything. Turn economics is not pruned — finished months rotate into `velocity-<month>.csv` and stay | Weeks of full prompts and transcripts filling the disk |
| `breaker_cap` / `breaker_cooldown_seconds` | 5 / 600 | Failure breaker: after N consecutive failed turns (nonzero exit or timeout) the member's turns pause; one probe runs per cooldown until a turn succeeds. Queued messages hold — nothing is dropped | A broken harness (bad flag, revoked key, dead local model) burning turn after turn while messages pile up |
