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
`ollama launch`-wrapped local variants (`ollama-opencode`, `ollama-claude`,
`ollama-codex`, `ollama-copilot` — see
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

## `rig run` — one headless turn as a rig

```bash
r4t rig run ark-eng "summarize what changed on this branch"
r4t rig run ark-eng --dir ~/work/proj --agent my-node --wait "check the deploy"
r4t rig run cheap --idle          # the quiet-tick consolidation pass
```

```
r4t rig run <rig> [--wait | --now] [--json] [--dir DIR] [--model M]
                  [--agent NAME] [--timeout S] [--no-scaffold] [--idle]
                  [--echo] [--lessons-cap N] [--continue]
                  [--permissions MODE] [--allowed-tools SPEC]
                  [--rig-config PATH] [--] PROMPT
```

Same turn as [`r4t engine <id> run`](r4t-engine.md) — the same preset table,
the same smart cold-boot scaffold, the same `--idle` latch, the same
`LESSONS.md` rotation, the same exit code (the CLI's own, 124 on a timeout
kill) and the same stdout, which stays the engine's own reply stream byte for
byte. `engine` is the bare metal; `rig` is that engine with the rig's tuning
already on it and the rig's budget in front of it. No roster and no
`ROSTER.md` are involved either way.

**What the rig supplies.** The `preset` is the engine, and it must be one the
`run` verb supports (a rig with no preset, or one riding `ollama` /
`ollama-copilot`, is refused with the engines that can). On top of it the rig
supplies `model`, `permissions`, `allowed_tools`, `timeout_seconds` and the
`env` map. **Precedence is flag > rig > preset**: a per-invocation flag wins,
then the rig's own key, then unset — which is the preset's own flags, byte for
byte. `--echo` prints the composed argv so the resolution is readable in one
command.

Three things the rig carries that this verb does not read, each for a reason
worth knowing:

- **`invoke`** — the argv is recomposed from the rig's `preset` rather than
  replayed from its stored array, so a hand-edited invoke or a rotation pool
  does not reach the turn. The model is the one value read back out of the
  array, since only the live-resolver presets record `model` as a setting.
- **`echo` / `echo_max_chars`** — those stage a member's stdout as a reply to
  whoever sent the message. A bare turn has no sender and no staging outbox;
  its stdout goes to the caller's terminal. (`--echo` on this verb is the
  engine layer's own flag: print the argv, then run.)
- **`mcp`** — the `a8s_tell` idioms write their config beside a member's
  per-turn staging outbox, which only a dispatched turn has.

There is **no rig-level continue key**, deliberately. The preset declares
whether a CLI can resume at all and a roster member declares whether it
should; a third setting between them would have to lose to the member flag
every time the two disagreed. Continuation stays a per-invocation
`--continue`, and `--idle --continue` is refused here exactly as it is on
`engine run`.

### The budget gate

A rig that declares `rig_budget_max` / `rig_budget_earn_per_hour` charges its
[machine-global bucket](#the-economics-budgets-not-cuts) one unit per turn —
the same bucket, the same charge, and the same refill arithmetic dispatch
uses, so a roster and a bare `rig run` on the same rig spend one subscription
between them. A rig with neither key runs immediately with no gate at all,
which is bare-metal parity.

When the bucket holds less than one turn:

- **Default: refuse.** Nothing runs, nothing is charged, and stderr names the
  level, the wait until one turn is back, and both flags below. The exit code
  is 1, since [ark.md](ark.md) reserves exit-code meanings to the foundation;
  `--json`'s `reason` is `resting`, which is how a caller tells a rig that
  needs retrying later from a turn that failed.
- **`--wait`** holds for the refill and then runs. One stderr line states the
  wait up front; the poll after it is silent. The bucket is machine-global, so
  another process finishing early ends the wait early.
- **`--now`** is the ripcord: run regardless. The turn still charges, and
  since the charge clamps at zero the bucket rests at its floor rather than
  going into debt.

`--json` prints one JSON object to **stderr** — stdout belongs to the engine —
carrying `rig`, `engine`, `dir`, `ran`, `reason` (`ran`, `resting`,
`idle-latched`, `usage`, `error`), `exit_code`, and `budget`: `null` on an
ungated rig, else `max`, `earn_per_hour`, `level_before`, `level_after`,
`waited_seconds`, `forced`, plus `seconds_until` on a refusal.

## `rig fuel` — the tank as one number

```bash
r4t rig fuel ark-eng          # 0.00-1.00, the dial the next turn hits first
r4t rig fuel ark-eng --json
```

```
r4t rig fuel <rig> [--json] [--rig-config PATH]
```

An engine has dials; only a rig has a tank. [`r4t engine <id>
quota`](r4t-engine.md) reports every bucket an account carries, and no two
engines' panels are shaped alike. `rig fuel` narrows that answer to the
buckets the rig's **model** burns and reports the lowest of them.

**Which buckets count.** A bucket whose label names a model family is a dial
only that family turns; every other bucket constrains everything the engine
runs. A claude rig on Opus reads `min(five-hour, weekly, weekly-Opus)`, and the
same account's Fable rig reads `min(five-hour, weekly, weekly-Fable)` — two rigs
on one subscription, two different numbers, and neither counts the other's
weekly. A label may name more than one family, and then it counts for each of
them. An `agy` rig reads the pool its model belongs to. An `ollama` rig reads
1.00, because a local model has no cloud tank to empty. The mapping is one
table, `SCOPED_BUCKETS` in `apps/r4t/engines/__init__.py`, beside the bucket
shape it selects over.

**A rig that pins no model** counts only the unscoped buckets, and its JSON
says so with `"model": null`. Charging a model-scoped weekly against a rig
that may not run that model reports an empty tank that is not empty, so the
number is deliberately optimistic: a reading that is wrongly low halts dispatch
for a rig with fuel to burn, while one that is wrongly high costs a single
failed turn. Pin the model when the number has to be exact.

**Read `state`, not `fuel` alone.** `fuel` is the rig-level answer — one number
selected from the bucket-level `remaining_fraction` readings under it — and it
is `null` in two different situations that must not be treated alike:

| `state` | `fuel` | means |
| --- | --- | --- |
| `gauged` | 0.00-1.00 | a bucket answered (a local engine's `1.00` included) |
| `unlimited` | `null` | buckets constrain this model, none expresses a fraction |
| `unconstrained` | `null` | no bucket constrains this model at all |

`unconstrained` is the normal answer for an `agy` rig that pins no model: every
dial that account carries is scoped to a family, so none of them applies. A
dispatcher that reads `null` as "unlimited seat" will keep sending turns into
an account that may be empty. Branch on `state`.

A bucket that cannot express a fraction is dropped rather than read as empty.
The answer rides `quota`'s snapshot fallback, so a live check that fails yields
an aged number that says its age instead of nothing at all.

Nothing runs and nothing is charged. The rig's budget bucket is a separate
thing entirely — [that gate](#the-budget-gate) is how often this rig may spend,
fuel is how much subscription is left to spend. `--json` prints to **stdout**
(there is no engine reply stream to protect):

```json
{
  "rig": "ark-eng",
  "preset": "claude",
  "quota_engine": "claude",
  "model": "opus",
  "fuel": 0.15,
  "state": "gauged",
  "binding_label": "Weekly Limit (Opus)",
  "binding_reset": "2026-08-20T00:00:00+00:00",
  "origin": "live",
  "age_seconds": null,
  "plan": "Personal (max)",
  "buckets": [
    {"label": "Five Hour Limit", "remaining_fraction": 0.9,
     "reset_time": "2026-08-15T18:00:00+00:00"},
    {"label": "Weekly Limit", "remaining_fraction": 0.4,
     "reset_time": "2026-08-20T00:00:00+00:00"},
    {"label": "Weekly Limit (Opus)", "remaining_fraction": 0.15,
     "reset_time": "2026-08-20T00:00:00+00:00"}
  ],
  "note": null
}
```

`preset` is the rig's preset id — the same value [`rig run
--json`](#rig-run--one-headless-turn-as-a-rig) reports under `engine` — and
`quota_engine` is the engine component that answered, which differs whenever a
launcher preset rides another engine's quota (`ollama-claude` → `ollama`).
`origin` is `live` or `snapshot`; `age_seconds` is `null` on a live answer and
the snapshot's age in seconds otherwise. `binding_reset` is the binding
bucket's own `reset_time`, lifted out so nothing has to match `binding_label`
back against the `buckets` list.

## Continuing a conversation

A member with `- **Continue:** on` in the roster runs its turns inside its
CLI's own conversation instead of a cold prompt every wake: the agent keeps
its recent work, and the provider cache prices the wake as a continuation.
It needs a rig whose preset supports it — `claude`, `codex`, `cursor`,
`opencode`, `ollama-opencode`, `agy` (`r4t rig presets` marks them); anything
else fails closed at `r4t roster check` and at dispatch. Most presets append a
`--continue` flag; `codex` resumes through the `exec resume --last` subcommand,
so its tokens are inserted after `exec` instead. `copilot` is the one
unsupported CLI: its `--continue` resumes the machine's most recent session
whatever the directory, so members cannot be kept apart, and supporting it
cleanly means pinning a session id per member (bin#256).

### What a continuation costs

Continuation is a price optimisation, not a memory mechanism. r4t carries the
member's history in the prompt either way; `Continue:` only decides whether the
CLI *also* replays its own. That replay is nearly free while the provider
serves the conversation's prefix from cache, and expensive when it does not:
the entire conversation is sent again and charged at a premium to write back.

Measured production telemetry (tables on the wiki's Engine pages) shows that
miss is a **process-boundary phenomenon**: a resume seconds after a successful
turn — inside every cache lifetime, on the same task — re-writes the whole
conversation roughly 16× as often as staying in one process. No warmth window
or size cap prevents it, so r4t has none. Writing `Continue:` on a member IS
the acceptance of that risk; the default (no flag) founds fresh from durable
state on every wake, which is the safe regime. Engines where continuation is
observably cheap (cursor, local models) are the reasonable use.

What r4t does instead of gating is measure. Every completed turn on a probed
harness logs a `CACHE` line — tokens read, tokens written, and the size of the
context now in play. A continued turn that read only a stable prefix while
re-creating most of its own history — the miss signature — logs `CACHE-MISS`
with the same numbers. `claude` is probed today; the rest await their research
page.

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
so an `ollama-*` member's harness runs in the workdir (measured on ollama
0.32.5 against all four wrapped integrations — the launcher, the harness, and
every descendant report the workdir as their cwd). `ollama-opencode` therefore
carries the same advertised-root caveat as `opencode`, and `ollama-claude` /
`ollama-codex` / `ollama-copilot` stay in the workdir like their parents.

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
the echo keys `echo` / `echo_max_chars`, the harness stance keys
`permissions` / `allowed_tools`, and `env.<NAME>` for a
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

## Permission stance and tool allowlist (`permissions` / `allowed_tools`)

```bash
r4t rig set ark-eng permissions bypass
r4t rig set ark-eng allowed_tools "Bash(git:*) Bash(gh:*) Read Edit Write"
```

`permissions` takes `ask`, `auto` or `bypass` — the Ark's three words for a
stance each CLI spells its own way. r4t translates the word into the harness's
own flags for every turn on the rig; the table, the asymmetry rule (a mode
below the engine's floor errors, one above its ceiling proceeds with a note),
and what `bypass` costs per engine are in
[r4t-engine.md](r4t-engine.md#the-three-translated-parameters). Unset is the
preset's own flags, byte for byte.

`allowed_tools` is the engine's own allowlist string, replacing the preset's
list. claude's preset ships a deliberately narrow one, so a rig whose members
develop a repo sets `git` and `gh` here; only claude and `ollama-claude` take
an allowlist per invocation, and the rest error with the reason.

Both keys survive `rig swap` — and are re-validated against the incoming
preset, so a swap onto a harness that cannot express the stance is refused
rather than silently dropped.

**Neither is a roster field, deliberately.** A rig lives out-of-repo in
`~/.config/r4t/rigs.json`, and `ROSTER.md` may only NAME a rig. A member
editing the repo therefore cannot raise its own permissions — the same
boundary [r4t-security.md](r4t-security.md) draws for argv. Choosing the rig
is choosing the stance, and `r4t rig get <rig> permissions` says what it is.

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
| `permissions` (rig) | unset — the preset's own flags | The rig's permission stance in three words (`ask` / `auto` / `bypass`), translated into each harness's own flags (see [the three translated parameters](r4t-engine.md#the-three-translated-parameters)). A mode below the engine's floor is refused at `rig set`; one above its ceiling resolves to the strongest the engine has | A stance that lives out-of-repo, where a member editing ROSTER.md cannot raise it |
| `allowed_tools` (rig) | unset — the preset's own list | The engine's own tool-allowlist string, replacing the preset's for every turn. claude and `ollama-claude` only; the rest error with the reason | The claude preset's narrow list blocking a member that has to run `git` and `gh` — and hand edits that `rig swap` used to revert |
| `env` (rig) | empty | Static `NAME=value` pairs handed to the harness every turn — harness knobs r4t has no flag for (see [Harness env knobs](#harness-env-knobs-env)); set one at a time with `r4t rig set <rig> env.<NAME> <value>`. Frugal by doctrine; r4t's own turn variables are refused | Money burned on a harness default you cannot reach any other way — the first case is `ENABLE_PROMPT_CACHING_1H=1` on claude, the 1-hour prompt-cache tier for wakes minutes apart |
| `timeout_seconds` (rig) | 900 | Harness wall clock; the process group is killed | Hung harnesses |
| `concurrency` (rig) | 1 | Live turns within one rig | Rig-wide pile-ups |
| `cell_budget_max` / `cell_budget_earn_per_hour` | 16 / 8 | Shared cell spend bucket; a turn also costs 1 cell unit. When empty, everyone rests | Whole-cell money burn |
| `throttle.max_concurrent` | 1 | Live turns across ALL rigs | Roster-wide pile-ups |
| `throttle.min_seconds_between_turn_starts` | 15 | Cadence floor between turn starts; a member that can't start yet keeps its queue and runs later | Invisible burn — a storm degrades into a watchable drip |
| `quiet_task_seconds` | 1800 | Backstop: an open thread whose originator has not been answered and that has seen no activity for this long wakes the leader with a nudge to report current state. 0 disables the sweep | A thread that dangles — a turn "succeeds" without replying and the originator never hears back |
| `log_retention_days` | 14 | Days of roster transcript kept under `log/`; maintenance deletes older days whole and says so in the log. 0 keeps everything. Turn economics is not pruned — finished months rotate into `velocity-<month>.csv` and stay | Weeks of full prompts and transcripts filling the disk |
| `breaker_cap` / `breaker_cooldown_seconds` | 5 / 600 | Failure breaker: after N consecutive failed turns (nonzero exit or timeout) the member's turns pause; one probe runs per cooldown until a turn succeeds. Queued messages hold — nothing is dropped | A broken harness (bad flag, revoked key, dead local model) burning turn after turn while messages pile up |
