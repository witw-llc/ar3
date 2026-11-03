# Rig configuration reference

Everything that lives in the out-of-repo rig config
(`~/.config/r4t/rigs.json`, relocatable with `R4T_HOME`): presets, model
selection, the settings surface, and the governance knob table. For the
roster side see the [tutorial](r4t-tutorial.md); for why each governance layer
exists see [r4t-governance.md](r4t-governance.md).

## Reasoning effort

```bash
r4t rig add worker claude --effort low
r4t rig set worker effort high
r4t rig run worker --effort low "summarize the diff"
r4t rig unset worker effort
```

The `effort` key is stored separately from `invoke`. It applies to roster turns,
knowledge distillation, and `rig run`; a runtime `--effort` overrides it without
changing the saved value. `rig swap` retains it and validates it against the new
engine before writing; `rig swap --effort LEVEL` supplies a replacement.
`rig get`, `rig show`, and `rig configure` expose the setting.

See [reasoning effort](r4t-engine.md#reasoning-effort) for accepted values,
AGY model-family selection, and Cursor's explicit model bracket convention.

## Detect what you already have

The first rig should not be configured. `r4t rig detect` probes every
run-capable preset against the CLI actually installed on this machine, asks
each one what subscription is left, and prints the line that turns it into a
rig:

```bash
r4t rig detect          # the table; writes nothing
r4t rig detect --add    # create a rig per detected preset, named after it
r4t rig detect --json   # every probed preset, machine-readable
```

```
  PRESET   ENGINE   INSTALLED              FUEL  GRADE        ADD IT WITH
  codex    codex    codex-cli 0.147.0      44%   run-capable  r4t rig add codex codex
  copilot  copilot  GitHub Copilot CLI 1.  n/a   official     r4t rig add copilot copilot

  not detected: agy (agy is not on PATH), muse (muse is not on PATH)
```

**Nothing is spent.** Installation is `r4t engine <id> check` — the composed
argv handed to the CLI's own `--help`, never a turn — and the fuel column is
`r4t rig fuel`'s reading asked with no model pinned. A quota surface that
cannot answer costs the row its number and nothing else.

`rig detect` and `rig fuel` omit plan labels containing `@` from text and JSON
reports. An explicit `engine <id> quota` request retains the vendor's plan label.

Three grades:

| Grade | Means |
| --- | --- |
| `official` | a supported engine: model slot, quota surface, verified argv ([copilot](r4t-engine.md#copilot-officially-supported)) |
| `run-capable` | a verified headless invocation, and no more |
| `needs --model` | an `ollama-*` launcher, which has no bare form |

A preset whose binary is present but whose composed argv is **rejected** is
not detected — adding a rig that cannot run its first turn is worse than
saying nothing — and the reason is printed so `r4t engine <id> check` has
somewhere to start.

`--add` creates one rig per detected preset, named after the preset, and
skips the `needs --model` ones with the flag they wanted. It is idempotent: a
rig already in the config is reported, never replaced, so a re-run cannot
overwrite settings you have since tuned. The write lands in the machine-global
config (`~/.config/r4t/rigs.json`), which is the file every other r4t command
reads; `--dir DIR` puts `rigs.json` somewhere else, and `--rig-config PATH`
names the file outright.

Exit code is 0 with at least one engine detected, 1 with none — and with none,
the last line names the binaries to install.

## Presets

Rig **names** are yours (`leader`, `member`, `reviewer`, …); **presets** are
CLI templates aligned with [a8s definitions](../apps/a8s/definitions/):
`claude`, `codex`, `cursor`, `opencode`, `copilot`, `agy`, `muse`, `devin`,
plus the `ollama launch`-wrapped local variants (`ollama-opencode`, `ollama-claude`,
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
r4t rig run ar3-eng "summarize what changed on this branch"
r4t rig run ar3-eng --dir ~/work/proj --agent my-node --wait "check the deploy"
r4t rig run cheap --idle          # the quiet-tick consolidation pass
```

```
r4t rig run <rig> [--wait | --now] [--json] [--dir DIR] [--model M] [--effort LEVEL]
                  [--agent NAME] [--timeout S] [--no-scaffold] [--idle]
                  [--echo] [--lessons-cap N] [--lessons-cap-bytes N]
                  [--continue]
                  [--permissions MODE] [--allowed-tools SPEC]
                  [--add-dir DIR]... [--allow-tool RULE]... [--deny-tool RULE]...
                  [--resident]
                  [--rig-config PATH] [--] PROMPT
```

Same turn as [`r4t engine <id> run`](r4t-engine.md) — the same preset table,
the same smart cold-boot scaffold, the same `--idle` latch, the same
`LESSONS.md` upkeep (the consolidation past 90% of a cap), the same exit code (the CLI's own, 124 on a timeout
kill) and the same stdout, which stays the engine's own reply stream byte for
byte. `engine` is the bare metal; `rig` is that engine with the rig's tuning
already on it and the rig's budget in front of it. No roster and no
`ROSTER.md` are involved either way.

**What the rig supplies.** The `preset` is the engine, and it must be one the
`run` verb supports (a rig with no preset, or one riding `ollama` /
`ollama-copilot`, is refused with the engines that can). On top of it the rig
supplies `model`, `effort`, `permissions`, `allowed_tools`, `add_dirs`,
`allow_tools`, `deny_tools`, `timeout_seconds` and the `env` map. The access
lists are the one exception to *flag wins*: a flag adds to the rig's list
(rig entries first, then the flag's, duplicates dropped), and a deny can only
grow. **Precedence is flag > rig > preset**: a per-invocation flag wins,
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
  is 1, since [ar3-foundation.md](ar3-foundation.md) reserves exit-code meanings to the foundation;
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

A turn on a copilot rig adds `spend`, `otel` and `denials` when it measured them.
`denials` is the bounded record of what copilot refused during the turn
(`observed`, `complete`, `reason`, `by_tool`, `by_class`, `event_ids`,
`rule_tools`, `rules_omitted`, `session`); `complete: false` means `observed` is "at least", and `observed:
null` means unknown. It is a diagnostic and never changes `exit_code`. A
dispatched member keeps the same object as `last_denials` in its meta; see
[r4t-engine.md](r4t-engine.md).

## `rig fuel` — the tank as one number

```bash
r4t rig fuel ar3-eng          # 0.00-1.00, the dial the next turn hits first
r4t rig fuel ar3-eng --json
```

```
r4t rig fuel <rig> [--json] [--rig-config PATH]
```

An engine has dials; only a rig has a tank. [`r4t engine <id>
quota`](r4t-engine.md) reports every bucket an account carries, and no two
engines' panels are shaped alike. `rig fuel` narrows that answer to the
buckets the rig's **model** burns and reports the lowest of them.

**Whose account.** A rig whose `env` sets `COPILOT_HOME` (or a token
variable) is read with that environment, as the turn runs: the router's `gh`
login and stored token answer for the router's rigs only, and a reading is
cached per account, keyed by a hash of the credential the rig resolves
(never the token). A rig with its own account and no readable credential
caches nothing and gets no stale snapshot. A claude or codex rig with its own `CLAUDE_CONFIG_DIR` or
`CODEX_HOME` still reads the router's login; `note` says so.

**Which buckets count.** A bucket whose label names a model family is a dial
only that family turns; every other bucket constrains everything the engine
runs. A claude rig on Opus reads `min(five-hour, weekly, weekly-Opus)`, and the
same account's Fable rig reads `min(five-hour, weekly, weekly-Fable)` — two rigs
on one subscription, two different numbers, and neither counts the other's
weekly. A label may name more than one family, and then it counts for each of
them, except a line metered apart from its family: the Gemini 4 Argon pool
names Gemini too, and it counts for Argon models alone. An `agy` rig reads
the pool its model belongs to. An `ollama` rig reads 1.00, because a local
model has no cloud tank to empty. The mapping is one table, `SCOPED_BUCKETS`
with `SEPARATE_LINES` in `apps/r4t/engines/__init__.py`, beside the bucket
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
  "rig": "ar3-eng",
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
It needs a rig whose preset carries continuation tokens — `r4t rig presets`
prints a `continue:` line for each one that does (`agy`, `claude`, `codex`,
`cursor`, `devin`, `opencode`, `ollama-opencode`); anything else fails closed at
`r4t roster check` and at dispatch. Most presets append a `--continue` flag;
`codex` resumes through the `exec resume --last --include-non-interactive`
subcommand, so its tokens are inserted after `exec` instead. `copilot` is the
CLI with no usable tokens at all: its `--continue` resumes the machine's most
recent session whatever the directory, so members cannot be kept apart, and
supporting it cleanly means pinning a session id per member (bin#256).

Carrying tokens is not enough on its own. `claude` has them and a roster may
not use them, because its measured grade disables the member (below). And
`Continue:` is the opt-in, not a guarantee that any given turn resumes: three
gates send a turn back to a cold start.

### The three refound gates

A turn founds the conversation cold — no continue argv, a read-your-state
preamble on the prompt — whenever one of these holds, and says so in the log:

| Log line | When | Why |
|---|---|---|
| `CONTINUE-SWAP` | the rig now drives a different CLI than the conversation lives on | the conversation is keyed on the CLI; the old one may be quota-dead, so there is no dump turn and state on disk is whatever the last flush left. A swap that keeps the CLI (model-only, launcher variant) keeps the conversation |
| `CONTINUE-DIRTY` | the member's previous turn exited nonzero or timed out | r4t never saw that conversation finish; resuming carries the wreckage into every later turn, so the member reads its state off disk instead |
| `CONTINUE-STALE` | the conversation sat idle past the member's `Continue: <duration>` window | the graceful path is the idle pass's flush, which spends a dump turn first; this is the backstop for a roster whose idle pass has not run. The flush's own dump turn is exempt — it is the turn that still needs the conversation |

`Continue: on` has no window, so it never trips `CONTINUE-STALE` — and never
flushes either (see [idle pass](r4t-idle.md)).

### The continuation grade

Every r4t turn is a **new process**, so a roster continuation is always a
process-boundary resume. Presets the engine research measured against that
exact case carry a grade, and the grade is a fact about the binary rather than
a knob:

| Preset | Grade | What was measured |
|---|---|---|
| `cursor` | **good** | the resume namespace is an MD5 of the absolute working directory, so two members in two workdirs can never resume into each other's chat — verified by construction |
| `codex` | **moderate** | cwd filtering is the default, but `--last` cannot see sessions created by `codex exec` unless `--include-non-interactive` is passed, which is the only kind of session a roster creates. The preset passes it |
| `claude` | **poor** | resuming `claude -p` across a process boundary re-wrote the whole conversation 40.6% of the time (197 boundaries) against a 2.5% same-process baseline (4,040 calls). The miss is indifferent to task shape, and one miss costs on the order of a hundred warm hits |

A **poor** grade makes `Continue:` on that preset a member-disabling config
error naming the measurement: drop the `Continue:` line, or swap to a preset
that continues well. Presets the research did not measure are **ungraded** and
continue as before — silence here means "not researched", never "known bad".

`r4t engine run --continue` is untouched by any of this: that is one process
and the operator's own choice (see [engine](r4t-engine.md#--continue)).

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
or size cap foresees it, so r4t carries none; what it carries instead is the
grade above, which refuses the engines where the miss was measured. On an
ungraded preset, writing `Continue:` on a member IS the acceptance of that
risk; the default (no flag) founds fresh from durable state on every wake,
which is the safe regime. Engines where continuation is observably cheap
(cursor, local models) are the reasonable use.

What r4t does beyond the grade is measure. Every completed turn on a probed
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
prompting the member to save its state to STATUS.md). A retired
member's next turn runs cold with a read-your-state preamble, the same shape
the three gates above produce; the dump prompt
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
`rig_budget_max`, `rig_budget_earn_per_hour`, the context knobs
`history_max_bytes` / `history_body_max` / `prompt_body_max`, `model`, `mcp`,
the echo keys `echo` / `echo_max_chars`, the harness stance keys
`permissions` / `allowed_tools`, the access lists `add_dirs` / `allow_tools` /
`deny_tools`, and `env.<NAME>` for a
[harness env knob](#harness-env-knobs-env)
(each detailed in the [knob table](#governance-knobs) below).

```bash
r4t rig configure specialist             # walk every setting, Enter keeps each
r4t rig set specialist rig_budget_max 20 # write one explicit value
r4t rig get specialist                   # list effective settings, source-annotated
r4t rig get specialist rig_budget_max    # one value on stdout (script-friendly)
r4t rig unset specialist rig_budget_max  # drop it back to the default
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
`cap=$(r4t rig get specialist rig_budget_max)` captures cleanly.

`model` is special: `set`/`configure` re-resolve the invoke through the rig's
recorded preset, exactly like `rig add --model` (agy keeps its live fuzzy match
per turn). A rig with no recorded preset errors, pointing at
`r4t rig swap <rig> <preset> --model ...`. Raw `invoke` arrays are never
exposed through this surface; use `rig add`/`swap` to change the harness.

## Permission stance and tool allowlist (`permissions` / `allowed_tools`)

```bash
r4t rig set ar3-eng permissions bypass
r4t rig set ar3-eng allowed_tools "Bash(git:*) Bash(gh:*) Read Edit Write"
```

`permissions` takes `ask`, `auto` or `bypass` — AR3's three words for a
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
`~/.config/r4t/rigs.json`, and a roster may only NAME a rig. A runbook's
`## Rigs` block is the one in-repo place a stance is written, and the machine
trust ceiling caps it: `auto` unless `r4t add <dir> --trust` raised it for that
node, re-checked every turn. So a member editing the repo still cannot raise
its own permissions — the same boundary
[r4t-security.md](r4t-security.md) draws for argv. Choosing the rig is choosing
the stance, and `r4t rig get <rig> permissions` says what it is.

## Access knobs (`add_dirs` / `allow_tools` / `deny_tools`)

```bash
r4t rig set ar3-eng add_dirs /srv/repos/shared-lib
r4t rig set ar3-eng allow_tools "url(example.org),url(example.net)"
r4t rig set ar3-eng deny_tools "shell(git push),shell(rm)"
```

Three lists that widen or narrow what an engine may touch, spelled in each
CLI's own flags from one table. They are not an OS sandbox: `add_dirs` means
what the engine means by an extra directory, and a rule is opaque text in the
engine's own syntax (copilot takes URLs as `url(...)` rules).

| Engine | `add_dirs` | `allow_tools` | `deny_tools` |
|---|---|---|---|
| `claude` | `--add-dir` | appended to the `--allowedTools` element | `--disallowedTools` |
| `copilot` | `--add-dir=D` | `--allow-tool=R` | `--deny-tool=R` |
| `codex` | `exec --add-dir` (refused with `--continue`) | no | no |
| `cursor` | `--add-dir` | no | no |
| `agy` | `--add-dir` | no | no |
| `ollama-claude`, `ollama-codex`, `ollama-copilot` | as the wrapped engine, after the launcher's anchor | same | same |
| `opencode`, `ollama-opencode`, `devin`, `muse`, `ollama` | no | no | no |

An engine with no direct flag fails the turn before it starts, naming the
reason and the engines that take the knob; nothing is dropped. `allow_tools`
adds approval rules and is not an exclusive allowlist (`allowed_tools` stays
the claude-only replacement of the preset's list, resolved first).

**Grammar.** A list is split on commas outside parentheses, trimmed, emptied of
blanks and deduplicated (first kept), and never shell-expanded. Refused, not
repaired: unbalanced parentheses, a control character, and an entry that starts
with `-`. Each repeated flag is parsed on its own. A `add_dirs` entry is an
absolute path or starts with `~/` (the agent's `HOME`, not the router's); it
must be a directory in the child's view before the turn starts. Container
isolation refuses `add_dirs`.

**Layers and denies.** `allow_tools` and `add_dirs` are unioned across layers
(rig, then invocation). The deny set is the **node floor**, the denies of the
machine rig a runbook rig shadows, the effective rig's, and the invocation's,
emitted once. A deny wins over an allow, survives every stance (`bypass`,
`--allow-all`) and `allowed_tools`, and a runbook rig that shadows a machine
rig keeps that rig's denies (the one exception to whole-block shadowing,
because a deny only narrows). If the shadow runs a different engine, the turn
is refused rather than translating or dropping the denies. A deny can be left
behind by moving a member to a differently named rig; mandatory policy belongs
in the floor. `rig swap` to another engine is refused while `allow_tools` or
`deny_tools` are saved.

**Node floor.** `rigs.json` takes a reserved key mapping an engine to deny
rules:

```json
{ "deny_floor": { "copilot": ["shell(git push)"], "claude": "Bash(git push:*)" } }
```

It applies to every turn of that engine on the node: roster members, inline
`Engine:` members, `rig run`, `engine run`, `engine check` and `ollama-*`
wrappers. An engine with no deny flag is refused when the file loads. While the
floor names an engine, a launch that cannot carry it (`--resident`) is refused
even when no knob was passed. The floor is also part of every background model
call: knowledge distill and compile (the default rig and a named `Knowledge:`
rig), the engine-memory writer and the judge carry it, with the rig's own
access lists. One that cannot is refused and logged, never run bare.

**What a checked-in runbook may set in `Env:`.** Below `bypass` a checked-in
`Env:` may set only the names on one short allowlist, `rig.IN_REPO_ENV_SAFE`
(today: `ENABLE_PROMPT_CACHING_1H`), plus the names the operator grants in the
machine `rigs.json`:

```json
{ "env_allow": ["NODE_OPTIONS"] }
```

Everything else is refused when the rig is resolved, before any `--version` or
`--help` probe, with one line naming the rig, the variable and the fix ("set it
in the machine rig, or add it to the node's trusted env list"). An allowlist,
because an environment is open ended: no denylist can name every variable that
picks an executable (`PATH`, `NODE_OPTIONS`, `LD_PRELOAD`), a runtime or loader,
a home, a shell wrapper (`CLAUDE_CODE_SHELL_PREFIX`, `CLAUDE_ENV_FILE`), an
endpoint, proxy or certificate (`*_BASE_URL`, `HTTPS_PROXY`, `SSL_CERT_FILE`,
`NODE_EXTRA_CA_CERTS`), a credential (`*_API_KEY`, `GH_TOKEN`) or a permission
(`COPILOT_ALLOW_ALL`). None of those is on the safe list, for any engine; one
rule covers them all.

**`env_allow` is a privileged machine grant. Allowing a name lets the repo choose
that variable's value**, so grant only names whose every value you accept from
the repo. It is a list of exact names (letters, digits, underscore; no wildcards,
no prefixes), and names match exactly. On Linux and macOS environment names
are case-sensitive, so a grant of `node_options` does not admit `NODE_OPTIONS`
(list the spelling the repo uses), and the safe name is only
`ENABLE_PROMPT_CACHING_1H`. On Windows names are case-insensitive, so a grant
matches in any case, a rig's `env` names are stored upper case (as Python's
`os.environ` does there), and a rig that lists two names differing only by case
(`Path` and `PATH`) is refused. Every environment r4t hands a child is merged by
one rule: a later layer (caller env, rig `env`, instruments, MCP, turn) replaces
an earlier one whatever the case, so a granted `copilot_home` selects the rig's
home, token and quota cache. On Linux and macOS names stay as written. It is read
from the machine `rigs.json` only: a runbook rig key,
a `## Rigs` block that shadows a machine rig, an `extends` chain and a runbook's
own frontmatter cannot set or add to it. A wrong-typed or malformed value is not
read as "no grants" or as "all": it refuses every in-repo `Env:` on the node
with one error until it is fixed. At or above `bypass` the rule does not apply.
In every rig, machine or runbook, a `PATH` entry must be an absolute folder (no
relative, no empty entry) and a state-home variable must be an absolute path with
no `~`.

**The floor follows the binary.** A rig whose preset is unknown is refused at
load. When a node has a `deny_floor`, a rig with no preset is refused, and so is
any rig (or pool variant) whose executable is not the preset's own CLI by file
name (`.exe`/`.cmd` stripped): a launcher such as `env` or `npx`, a wrapper
script, or another engine, would be handed flags that are not its own. The
`ollama-*` presets are the known launchers. A runbook rig that shadows a machine
rig which is itself invalid is refused, not run without that rig's denies.

**Scope.** An `extends` child that redefines `## Rigs` replaces its base's rigs
whole, denies included, so mandatory policy belongs in the node `deny_floor`. A
repo's own engine settings files (a checked-in `.claude/settings.json`, a
project `.github/copilot` file, and the like) are out of scope: they are the
engine's own configuration, read by the engine, and r4t does not police them.

**The boundary tools are the router's.** `sudo` and `docker` are found once, from
the router's own PATH and working directory, as absolute paths; the probes and
the spawn use that same path, and the rig's `env` reaches the child only through
the boundary's `env_pass`.

**An unreadable config is not an empty one.** Only a file that is genuinely
absent counts as no config. One that exists but cannot be read (a closed folder
above it, no read bit, a directory in its place) refuses the turn with the path
and the fix, because a floor that vanishes with its file is not mandatory. The
runbook, roster and org config follow the same rule.

**Build record.** A turn that carries any knob resolves the engine binary,
runs `--version`, and records the path and version (`ACCESS` line in the node
log, `access` in `rig run --json`). A build older than the one the flags were
read against refuses the knobs; a newer or unreadable one runs and is recorded
as `unverified <version>`. The probe runs the executable the turn will spawn
(the selected pool variant's `argv[0]`, an absolute path included), not the
name on `PATH`. A launcher such as `ollama launch` starts a CLI r4t cannot
name, so it is recorded `unverified launcher`.
The executable, the complete environment and the working directory are
resolved once and used by the probe and the spawn alike; a relative path or PATH
entry resolves against the member's directory. A file replaced at the probed
path between the probe and the start is not detected. A launch is never
resolved with `shutil.which` (on Windows it searches the current directory
first); `ar3.proc.find_executable` reads the child's PATH and PATHEXT. The
copilot usage-flag answer is cached by the file's path, mtime and size; a shim
that answers differently by environment or cwd is a documented residual. A file
whose name is not the engine's CLI is refused, not run unverified, when knobs or
a floor are set. `r4t engine <id> check` proves
only that the argv parses.

**a8s vars.** An `engine-*` node takes `ADD_DIRS`, `ALLOW_TOOLS` and
`DENY_TOOLS` vars (see [a8s](a8s.md)).

**Copilot's state home.** `COPILOT_HOME` holds copilot's login, approvals and
`session-state/`. r4t sets no per-seat default; a seat opts in with
`env.COPILOT_HOME` and one `copilot login`. Session lookup, the quota token and
the transcript probe read the home the spawned environment names (a non-empty
variable, else the child's `HOME` plus the default). A shared home does not
isolate seats or their saved approvals. r4t writes no copilot settings or hook
files; `.github/copilot/settings.local.json` is the operator's.

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
`cursor` and `agy` are **opt-in** — their idioms write a file into a directory
r4t does not own (`.cursor/mcp.json` in the working tree; `~/.gemini` in the
member's home), which is a different consent level than passing a flag.
`r4t rig set <rig> mcp off` is the escape hatch on any rig, and
`r4t rig get <rig> mcp` says whether the value is `explicit` or came `from
preset <name>`. Bare `ollama` has no tool use at all, so it resolves off
silently and `rig set <rig> mcp on` errors there with a
`(try: r4t rig swap <rig> ...)` hint rather than running turns whose tool never
appears.

`agy` has no per-invocation flag in any released version: it reads
`$HOME/.gemini/config/mcp_config.json` and nothing else. That is a per-member
file exactly when the member has a home of its own, so the knob is allowed on an
org with `run_as` isolation and refused everywhere else — bare (the home is the
router's), in a container, or with a second agy member on the same Unix user
(one config names one staging outbox, so their sends would cross). Those three
name the rig and the fix at `roster check` and again at the turn.

A roster is not the whole story, though: that file is global to the Unix user
across every org and every node, so two rosters can each scan clean and still
write the same `~/.gemini`. The file therefore carries its own ownership record
— the staging outbox in the entry r4t writes is unique per node and member — and
a turn that finds an a8s entry naming a **different** outbox refuses by that
path rather than overwriting it. Otherwise r4t merges the a8s server into
whatever that file already holds and leaves every other server and every field
it does not recognise alone.

`mcp off` on an agy rig takes the entry back out again, because agy reads that
file whether r4t wrote it this turn or not: a member left holding `a8s_tell`
while its prompt teaches the shell command sends into whichever outbox the stale
entry names. Only r4t's own entry for that member is removed — an entry naming
another outbox is another member's, and it is left alone, though it is logged:
that member loads the tool anyway, and no turn of its own will ever clear it. A
removal that cannot happen is logged too, rather than made the turn's verdict.

Under an org boundary (`run_as` / `container`) r4t carries each harness's idiom
across and fails the turn closed when it cannot — see
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
runs again when the bucket refills. No message is dropped for lack of budget.

The rig bucket is the quota answer. A rig maps to a real subscription (an
Antigravity plan good for ~20 prompts an hour, a Claude seat), so its ceiling
is set **on the rig** and is **machine-global**: it binds every r4t roster on
the machine that shares the rig, so one subscription is safely shared across
projects. Its bucket lives in `~/.config/r4t/rig-buckets.json` (outside any
roster) and every node charges it atomically. An exhausted rig rests every
member on it, on every roster, and the held queues run when it refills.

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
| `history_max_bytes` / `history_body_max` / `prompt_body_max` (rig) | by preset tier — big (agy/claude/codex/devin/muse) 50k/12k/24k · moderate (cursor/opencode/copilot) 25k/6k/12k · small (ollama variants, or no preset) 8192/2000/4000 | Context sizing on the rig: rolling-history budget, per-entry history clip, and per-message prompt clip. `rig add`/`swap` record the preset; explicit values override the tier | A weak rig drowning in context, or a strong one starved of it |
| `echo` / `echo_max_chars` (rig) | false / 1500 | Stdout-only members (see [Echo rigs](#echo-rigs)): no messaging scaffolding in the prompt, cleaned stdout staged as the one reply, bodies past the cap truncated with the full text attached | A model that misuses `tell`, looping "I did it" messages instead of answering |
| `mcp` (rig) | by preset — **on** for claude/codex/copilot/opencode and their `ollama launch` variants; **off** for cursor (its idiom writes `.cursor/mcp.json` into your repo), for devin (its idiom writes `.devin/mcp_config.local.json` into the same tree), for agy (its idiom writes `~/.gemini` in the member's home, and needs `run_as`) and for bare ollama (no idiom at all) | Members send with the `a8s_tell` tool instead of the `tell` shell command (see [The `a8s_tell` tool](#the-a8s_tell-tool-mcp)): `a8s mcp serve` is injected per turn through the harness's own idiom and the prompt names the tool. `mcp off` is the escape hatch anywhere; `mcp on` errors on bare ollama | Shell quoting mangling a body, and a member that describes a message instead of sending one |
| `permissions` (rig) | unset — the preset's own flags | The rig's permission stance in three words (`ask` / `auto` / `bypass`), translated into each harness's own flags (see [the three translated parameters](r4t-engine.md#the-three-translated-parameters)). A mode below the engine's floor is refused at `rig set`; one above its ceiling resolves to the strongest the engine has | A stance the machine caps: a runbook may name one, and the trust ceiling (`auto` unless `r4t add --trust`) is what a repo cannot raise |
| `effort` (rig) | unset — embedded model effort or engine default | Reasoning depth translated through the preset; runtime `--effort` overrides it for one turn. Invalid values and unsupported engines error | Tune routine turns cheaply or increase reasoning for difficult work |
| `allowed_tools` (rig) | unset — the preset's own list | The engine's own tool-allowlist string, replacing the preset's for every turn. claude and `ollama-claude` only; the rest error with the reason | The claude preset's narrow list blocking a member that has to run `git` and `gh` — and hand edits that `rig swap` used to revert |
| `add_dirs` / `allow_tools` / `deny_tools` (rig) | unset | Extra directories, approval rules and deny rules in the engine's own flags (see [Access knobs](#access-knobs-add_dirs-allow_tools-deny_tools)). A runbook rig may set the first two only on a node whose ceiling is `bypass` | A second repo or one URL needing `bypass`; a push or a delete a stance cannot take away |
| `deny_floor` (top-level) | unset | Engine to deny rules every turn of that engine on the node carries; a runbook cannot drop one | A checked-in file loosening a node's mandatory denies |
| `env` (rig) | empty | Static `NAME=value` pairs handed to the harness every turn — harness knobs r4t has no flag for (see [Harness env knobs](#harness-env-knobs-env)); set one at a time with `r4t rig set <rig> env.<NAME> <value>`. Frugal by doctrine; r4t's own turn variables are refused | Money burned on a harness default you cannot reach any other way — the first case is `ENABLE_PROMPT_CACHING_1H=1` on claude, the 1-hour prompt-cache tier for wakes minutes apart |
| `timeout_seconds` (rig) | 900 | Harness wall clock; the process group is killed and the batch is a dead letter. A dispatch wake starts no turn whose timeout exceeds the time left before the a8s definition's `max_wake_seconds` | Hung harnesses |
| `cell_budget_max` / `cell_budget_earn_per_hour` | 16 / 8 | Shared cell spend bucket; a turn also costs 1 cell unit. When empty, everyone rests | Whole-cell money burn |
| `throttle.min_seconds_between_turn_starts` | 0 | Cadence floor between turn starts; a member that can't start yet keeps its queue and runs later. Off by default: one turn at a time already rules out pile-ups, so a standing gap is dead air. Opt in to watch a rotation in slow motion | Invisible burn — a storm degrades into a watchable drip |

There is no concurrency knob at either level. One turn at a time per node is a
contract held by the admission lock, not a setting (see
[the rotation](r4t-operations.md#the-rotation)); parallelism is a second node.
| `log_retention_days` | 14 | Days of roster transcript kept under `log/`; maintenance deletes older days whole and says so in the log. 0 keeps everything. Turn economics is not pruned — finished months rotate into `velocity-<month>.csv` and stay | Weeks of full prompts and transcripts filling the disk |

## Memory on a direct rig run

`r4t rig run NAME --agent wren --memory on "Do the work"` uses the same
private memory lifecycle as an engine run. Model and effort overrides apply
to the default memory writer, and the rig's environment is resolved from its
configuration. `--memory-writer` selects a separate configured rig. See
[engine memory](r4t-engine-memory.md).
