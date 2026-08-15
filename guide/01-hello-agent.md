# Chapter 1 — Hello, Agent

**Teaches R and A — [r4t](../docs/r4t.md)'s engine, then
[a8s](../docs/a8s.md), the message router.**

## 1. Capability

You already have agents. An `AGENTS.md` or `CLAUDE.md` you tuned, a harness
CLI you pay for or run locally, prompts you rewrote until they behaved. This
chapter does not replace any of it. It runs what you already have through
`r4t engine <id> run` — one headless turn, no roster, nothing to configure —
and hands back an agent that **remembers**: `STATUS.md` and `LESSONS.md` on
disk, read on the way in and written on the way out, so a stateless CLI stops
starting from zero every time you call it.

Then you hang that same agent on a8s, so `tell` wakes it and it answers you by
mail. At the end you will have `solo`: your instructions, r4t's engine turn,
and an address on the network. You will also have broken it, watched a message
wait on disk instead of dying, and put it back with one command.

## 2. Time

About 20 minutes of hands-on time. On the free path start the model download
first — it runs in the background while you read, and it is the only long
wait in the chapter.

## 3. Starting state

- No leftover `TELL_OUTBOX_DIR` from an earlier a8s seat on this machine —
  `tell` checks it before anything else, so a stale value silently sends
  your mail to that other seat's outbox instead of the one you build below:

**Run**

```bash
unset TELL_OUTBOX_DIR
```

- The Ark installed and on your PATH:

**Run**

```bash
curl -fsSL https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
```

  That clones the suite into `~/.ar3` and adds one `source` line to your
  shell rc. Open a new shell (or re-source the rc) so `PATH` picks it up.

- Python 3 installed (`python3 --version` answers).
- One harness. **Pick your path** — the free path is the default, and every
  pasted output below was captured on it:
  - **Free path** — [OpenCode](https://opencode.ai/) plus `ollama`. Start the
    download now:

**Run**

```bash
ollama pull qwen3.6
```

  - **Subscription path** — the Cursor agent CLI (`agent`), installed and
    logged in.

Now type the suite's front door command:

**Run**

```bash
ar3
```

You should see:

```
A R K
8 4 7
S T E

The Ark — a8s routes the messages, r4t governs the roster,
k7e keeps what they learn. ar3 reads; each product owns its own verbs.

a8s — agent message router  (/home/you/.config/a8s)
  ✓ cli       a8s -> /home/you/.ar3/a8s
  ✗ registry  no registry at /home/you/.config/a8s/a8s.json   (try: a8s discover <dir>)

r4t — the roster  (/home/you/.config/r4t)
  ✓ cli      r4t -> /home/you/.ar3/r4t
  ✗ rigs     no rig config at /home/you/.config/r4t/rigs.json   (try: r4t init)
  ✗ rosters  none under /home/you/.config/r4t/rosters   (try: r4t init)

k7e — knowledge engine  (/home/you/.config/k7e)
  ✓ cli    k7e -> /home/you/.ar3/k7e
  ✗ store  no store at /home/you/.config/k7e   (try: k7e store <title>)

next: ar3 doctor — probe the harnesses and tools the suite runs on
```

Three CLIs found, nothing configured — the correct fresh-machine state.
`ar3` never changes anything; it reads and tells you which command owns the
next move. Take its suggestion:

**Run**

```bash
ar3 doctor
```

You should see:

```
ar3 doctor — probes only; nothing here is installed, started, or changed
suite: 0.1.68 (latest)

Harnesses
  ✓ claude    2.1.226 (Claude Code)  (/home/you/.local/bin/claude)
  ✓ agent     2026.08.04-aaa8809  (/home/you/.local/bin/agent)
  ✓ codex     codex-cli 0.147.0  (/home/you/.local/bin/codex)
  ✓ copilot   GitHub Copilot CLI 1.0.80.  (/home/you/.local/bin/copilot)
  ✓ opencode  1.18.3  (/home/you/.local/bin/opencode)
  ✓ agy       1.1.10  (/home/you/.local/bin/agy)
  ✓ ollama    ollama version is 0.32.11  (/home/you/.local/bin/ollama)

Services
  ✓ ollama serve  3 model(s): qwen3.6:latest, qwen3:4b, nomic-embed-text:latest
  ✓ docker        daemon 29.7.2

Tooling
  ✓ git  git version 2.50.1 (Apple Git-155)

✓ core prerequisites satisfied  (10/10 probes green)
```

Your panel will show ✗ for harnesses you haven't installed — that is fine.
This chapter needs one path's worth: `opencode` plus `ollama` on the free
path, or `agent` on the subscription path. Everything else can stay red.

## 4. The change

Nothing to build yet — start with what you already have. Make a directory for
the agent and write its instructions:

**Run**

```bash
mkdir -p ~/ark/solo ~/ark/me
```

**Create** `~/ark/solo/AGENTS.md`

```markdown
# solo

You are solo, an agent on this machine. Keep answers short and concrete.

You have no memory between turns. STATUS.md is where you leave what the next
turn needs to know — write it before you exit, every time.
```

If you already keep an `AGENTS.md` or `CLAUDE.md` for some project, that file
works here unchanged — copy it in and skip ahead. Harnesses load their own
instruction file on their own (`CLAUDE.md` for Claude Code, `AGENTS.md` for
most others), and r4t's engine turn reads `AGENTS.md` by absolute path on top
of that. Your agent keeps its character; the engine adds the rest.

### One turn, no roster

`r4t engine <id> run` is the bare-metal layer: one CLI, one headless turn, no
`ROSTER.md`, no dispatcher, no budgets, nothing to set up first. Give it a
prompt, get an answer.

**Run** (free path)

```bash
cd ~/ark/solo
r4t engine ollama-opencode run --model qwen3.6 --timeout 600 "You are my release assistant for a project called Foghorn. Record in STATUS.md that I am shipping 0.4.0 and that it needs a changelog before it goes out. Acknowledge in one sentence."
```

**Run** (subscription path) — same command, a different engine id, and no
`--model`, because the `cursor` preset already asks your subscription for its
default:

```bash
cd ~/ark/solo
r4t engine cursor run --timeout 600 "You are my release assistant for a project called Foghorn. Record in STATUS.md that I am shipping 0.4.0 and that it needs a changelog before it goes out. Acknowledge in one sentence."
```

You should see:

```
Files don't exist yet. Creating STATUS.md with your direction:
Recorded: you're shipping Foghorn 0.4.0 and it needs a changelog before release. See STATUS.md for details.
```

`r4t engine list` names every engine id and which rig presets each one serves.
The `ollama-*` ids run a local model through `ollama launch` and need a
`--model` tag; the cloud ids (`claude`, `codex`, `cursor`, `copilot`, `agy`,
`opencode`) take your subscription's default when you name none.

### The scaffold — why the second turn knows things

Look at what landed on disk:

**Run**

```bash
cat ~/ark/solo/STATUS.md
```

You should see:

```
# STATUS.md

## Current State
- User is shipping Foghorn 0.4.0

## Important Context
- CHANGELOG needed before release go-ahead

## Next Steps
- Write changelog for 0.4.0
```

Nobody told the agent that format. `run` prepends a fixed prelude to your
prompt — the **smart cold boot** — that tells it to read `STATUS.md`,
`AGENTS.md` and `LESSONS.md` on the way in, and to rewrite `STATUS.md` and
append to `LESSONS.md` before it exits. A CLI turn has no transcript memory,
so those files *are* the memory. That is the sharp edge this chapter softens:
you get continuity out of a stateless harness without holding a session open
or paying to re-send a transcript.

Prove it. Start a completely new turn — new process, nothing carried over —
and ask about work you never mention:

**Run** (free path)

```bash
r4t engine ollama-opencode run --model qwen3.6 --timeout 600 "What release am I shipping, and what is still needed before it goes out?"
```

You should see:

```
- **Release:** Foghorn 0.4.0
- **What's still needed:** Write changelog for 0.4.0 before release go-ahead

STATUS.md is already accurate. No remaining direction beyond "write the changelog" — I'm idle until that work arrives.
```

That is the whole win, and it cost one flag-free command. `STATUS.md` is the
turn-to-turn state; `LESSONS.md` is the long file, appended one bullet at a
time and never rewritten. Hand it something worth keeping:

**Run** (free path)

```bash
r4t engine ollama-opencode run --model qwen3.6 --timeout 600 "Durable fact worth keeping: Foghorn's changelog lists breaking changes first, because our users read only the top section."
```

You should see:

```
No active work — just persisted the durable fact to LESSONS.md. Idle.
```

**Run**

```bash
ls ~/ark/solo
cat ~/ark/solo/LESSONS.md
```

You should see:

```
AGENTS.md
LESSONS.md
STATUS.md

## Durable Facts
- Foghorn's changelog lists breaking changes first, because our users read only the top section.
```

Three files, and you wrote one of them. `LESSONS.md` grows append-only; past
a line cap (`--lessons-cap`, 200 by default) the oldest lines rotate into
`LESSONS-ARCHIVE.md` before the turn starts, so the file never quietly turns
into the whole context window.

### Seeing exactly what runs

Two commands answer "what did r4t actually execute?" without you guessing.
`--echo` prints the composed argv and the full prompt to stderr before the
turn runs:

**Run** (free path)

```bash
r4t engine ollama-opencode run --model qwen3.6 --echo "Say OK."
```

You should see, on stderr, before the answer:

```
r4t engine echo: argv: ollama launch opencode --model qwen3.6 -- run --auto --dir /home/you/ark/solo '{prompt}'
r4t engine echo: --- prompt ---
Smart cold boot:
1. Read /home/you/ark/solo/STATUS.md, then /home/you/ark/solo/AGENTS.md and /home/you/ark/solo/LESSONS.md if present. Use these absolute paths even if your workspace root differs. They are the durable source of truth; you have no transcript memory.
2. Stay idle and exit unless there is clear direction or active work. Never restart completed work. Be token-frugal; no wordy prose.
3. Before exit, rewrite /home/you/ark/solo/STATUS.md with sections: Current State, Important Context, Next Steps, Decisions (with rationale). Append genuinely new durable insights to /home/you/ark/solo/LESSONS.md — append-only, one short bullet each, never rewrite or delete existing lessons. Never edit AGENTS.md.

Routed input:
Say OK.
r4t engine echo: --- end prompt ---
```

Every word of that prelude is fixed — no timestamps, no counters — so your
harness's prompt cache hits on all of it and pays only for the routed input
at the end.

`check` answers the same question without spending a turn at all: it composes
the argv `run` would use and asks the installed CLI whether it still parses,
driving only `--help` and `--version`.

**Run**

```bash
r4t engine ollama-opencode check --model qwen3.6
```

You should see:

```
  ollama-opencode  ollama version is 0.32.11  accepted (help scan)

No turn is spent: a check drives each CLI's own --help/--version.
```

(Subscription path: `r4t engine cursor check`. Bare `r4t engine check` walks
every run-capable engine on the machine.)

### Giving solo an address

solo answers when you call it. Now put it on the network, so it answers mail.

An a8s agent is a directory plus a **definition** — a small JSON file naming
the command that wakes it. When a message arrives, a8s substitutes `$SENDER`,
`$RECIPIENT` and `$MESSAGE` into that command and runs it with the agent's own
directory as the working directory. That default working directory is exactly
what `run` wants, so the definition is the command you have been typing:

**Create** `~/ark/solo/solo.json` (free path)

```json
{
  "description": "solo — a bare engine node: one r4t engine turn per wake",
  "max_wake_seconds": 900,
  "invoke": ["r4t", "engine", "ollama-opencode", "run",
             "--model", "qwen3.6", "--timeout", "600",
             "--agent", "$RECIPIENT",
             "$SENDER tells $RECIPIENT ($AGE): $MESSAGE"],
  "batch": {
    "limit": 20,
    "invoke": ["r4t", "engine", "ollama-opencode", "run",
               "--model", "qwen3.6", "--timeout", "600",
               "--agent", "$RECIPIENT"]
  },
  "idle": {
    "timeout": 900,
    "invoke": ["r4t", "engine", "ollama-opencode", "run",
               "--model", "qwen3.6", "--timeout", "600",
               "--idle", "--agent", "$RECIPIENT"]
  }
}
```

**Subscription path** — the same file with `"cursor"` in place of
`"ollama-opencode"`, and the two `"--model", "qwen3.6"` pairs dropped.

Three wake paths, one engine:

- **`invoke`** — one message, one turn. `$MESSAGE` is last because `PROMPT`
  is a positional.
- **`batch`** — twenty messages that arrived together cost one turn and one
  cold context load, not twenty. a8s composes the pending envelopes into the
  prompt itself, so this invoke names no `$MESSAGE`.
- **`idle`** — after 900 seconds of quiet, one consolidation turn with no
  prompt: reconcile `STATUS.md` with reality, append anything new to
  `LESSONS.md`, exit. Its own latch means only the first quiet tick spends a
  turn.

`max_wake_seconds` is the outer guard rail — a harness that hangs instead of
exiting gets killed rather than holding the agent forever. Keep the engine's
own `--timeout` under it, so r4t kills the turn and reports rather than a8s
killing r4t. (`apps/a8s/definitions/engine-claude.json` in the repo is the
shipped version of this file, and
[docs/r4t-engine.md](../docs/r4t-engine.md) has the full flag list.)

One more thing before mail can work: nothing so far has told solo to answer
anybody. Add that rule to the file that already holds its character.

**Replace** `~/ark/solo/AGENTS.md` (whole file — one new section at the end)

```markdown
# solo

You are solo, an agent on this machine. Keep answers short and concrete.

You have no memory between turns. STATUS.md is where you leave what the next
turn needs to know — write it before you exit, every time.

## How you reply

The routed input names who is asking. Your last act on every wake is to
**run** the shell command:

    tell <sender> "<your answer>"

Run it. Printing that line instead of running it means nobody hears you.
```

`$SENDER` is what puts a name in front of solo — that is why the definition's
prompt is `"$SENDER tells $RECIPIENT ($AGE): $MESSAGE"` rather than the bare
message. r4t's roster does this from outside the turn in chapter 2, where a
member cannot forget to answer; here it is a rule in a file you own.

`~/ark/me` is your **filedrop seat**: a directory a8s delivers your mail
into, with no CLI to wake — you read it with `tells`. Register both, then
look at the roster:

**Run**

```bash
a8s add me ~/ark/me filedrop
a8s add solo ~/ark/solo ~/ark/solo/solo.json
a8s ls
```

You should see:

```
added me -> /home/you/ark/me
definition: /home/you/.ar3/apps/a8s/definitions/filedrop.json  (explicit)
wake_path: recorded this shell's PATH for every node's wakes
added solo -> /home/you/ark/solo
definition: /home/you/ark/solo/solo.json  (explicit)
NAME   STATUS    DEFINITION   ROOT
me     stopped   filedrop     /home/you/ark/me
solo   stopped   solo         /home/you/ark/solo
```

That third line appears once, on the first `a8s add` on a machine. A woken
agent gets the environment of whatever shell started its handler, which is
right when you start from a terminal and wrong when cron or ssh does it — so
a8s writes down the PATH of the shell you are typing in now and gives every
wake that instead. It is also why `"r4t"` on the invoke line resolves at wake
time. `a8s config` shows it as `wake_path`.

Registered but stopped: nothing routes until a **handler** process is
attached. Start one for each:

**Run**

```bash
a8s start solo
a8s start me
```

You should see:

```
started solo as PID 88529
started me as PID 88534
```

## 5. Run it

Speak from your seat. `tell` figures out who you are from the directory you
stand in, and `tells` watches your inbox for the window you give it — Ctrl+C
as soon as the answer lands. Give this first wait a long window: solo's first
wake through a8s pays for the model's cold start on top of the harness's own.

Ask it something only its own notes can answer:

**Run**

```bash
cd ~/ark/me
tell solo "What release am I shipping, and what is still needed before it goes out?"
tells --timeout 420
```

## 6. Expected receipt

You should see:

```
tell -> solo: What release am I shipping, and what is still needed before it goes out?
solo: Foghorn 0.4.0 — blocked because no changelog exists yet and I have no access to the Foghorn repo or commit history to derive one. If you point me at the repo (or list the changes for 0.4.0), I'll write the changelog immediately.
```

That round trip crossed the full machinery: your envelope was written to
`~/ark/me/.outbox/`, the router stamped you as the sender and moved it to
solo's inbox, solo's handler ran `r4t engine ... run` with your text as the
routed input, the engine composed the cold-boot prompt around it, and the
reply rode the same road back into `~/ark/me/.inbox/`.

And solo knew about Foghorn 0.4.0. Nothing in that message mentioned it, and
this was a fresh process with no transcript — it read its own `STATUS.md`,
exactly as it did at the command line. Same agent, same memory, now with an
address.

## 7. Break it

Take the handler away and message solo anyway:

**Run**

```bash
a8s stop solo
tell solo "Are you still there?"
tells --timeout 20
```

You should see:

```
solo: sent SIGTERM to PID 88529
waiting up to 600s for stop…
solo: stopped
tell -> solo: Are you still there?
tells: no message within 20s
```

`stop` waits up to ten minutes for the handler to let go, because a wake in
flight finishes before the handler detaches. Then `tell` accepted your message
and nothing answered. Nothing crashed and nothing warned you, which is exactly
the state worth learning to read.

## 8. Diagnose

Two reads settle any "where did my message go?" question. The registry says
who is attached:

**Run**

```bash
a8s ls
```

You should see:

```
NAME   STATUS                DEFINITION   ROOT
me     running (pid 88534)   filedrop     /home/you/ark/me
solo   stopped               solo         /home/you/ark/solo
```

And the per-agent log says what actually moved:

**Run**

```bash
a8s logs solo --tail 2
```

You should see:

```
2026-08-14T22:13:28.825778Z [a8s] solo: detached
2026-08-14T22:13:29.958278Z received from me: Are you still there?
```

Read those two lines together. The handler **detached**, and after that the
message was **received** — routed out of your outbox into solo's inbox on
disk — with no wake line following it, because no process was there to wake.
It is not lost and it is not held in anyone's memory; it is a file waiting in
a directory. That is the whole durability story, and it is why the fix is one
command.

(That log is also where a wake's own output lands. When a definition is
mis-wired — a flag and its value fused into one argv element is the usual
one — the harness's complaint shows up here, and `r4t engine <id> check`
tells you whether the argv itself is at fault.)

## 9. Fix

Attach a handler again:

**Run**

```bash
a8s start solo
tells --timeout 420
```

You should see:

```
started solo as PID 91731
solo: Yes, I'm here. Pending: you need to provide access to the Foghorn repo (or a list of commits/changes) so I can write the 0.4.0 changelog and we can proceed with release.
```

The waiting message woke solo the moment a handler claimed it. You did not
resend anything — and solo picked up the release context from disk on its way
through.

## 10. Check

Ask the front door where the suite stands now:

**Run**

```bash
ar3
```

You should see (a8s section):

```
a8s — agent message router  (/home/you/.config/a8s)
  ✓ cli       a8s -> /home/you/.ar3/a8s
  ✓ registry  2 agent(s), 0 alias(es), 0 namespace(s)
  ✓ router    attached: me, solo
```

The registry has your two agents and both handlers are attached. The r4t
section still shows ✗ for rigs and rosters — the engine layer needs neither,
and chapter 2 is where they arrive. k7e waits until chapter 5.

## 11. Customize

solo's character is the file you brought with you. Change it:

**Replace** `~/ark/solo/AGENTS.md` — the opening paragraph only, leaving the
memory line and the `## How you reply` section as they are:

```markdown
You are solo, the keeper of a lighthouse on this machine. Keep answers
short and concrete, and always mention the weather.
```

No restart needed — every wake is a fresh turn that re-reads the file:

**Run**

```bash
cd ~/ark/me
tell solo "What are you watching over today?"
tells --timeout 420
```

You should see:

```
tell -> solo: What are you watching over today?
solo: Watching Foghorn 0.4.0 release readiness. Still need changelog data — no repo access to draft it myself, so I'm waiting on you for that. Weather's clear by the light, if a bit foggy out past the glass.
```

New character, same memory. That split is the point: `AGENTS.md` is who solo
is and you own it outright, `STATUS.md` is what solo knows and it owns that.

## 12. Commit point

Your agent is two files you wrote; keep them under version control like
anything else you built. `STATUS.md` and `LESSONS.md` are solo's working
memory rather than your source, so leave them out:

**Run**

```bash
cd ~/ark/solo
git init -q
printf 'STATUS.md\nLESSONS.md\nLESSONS-ARCHIVE.md\n' > .gitignore
git add AGENTS.md solo.json .gitignore
git commit -q -m "solo: an engine-backed agent behind a8s"
```

Copy-paste versions of solo's files live in
[templates/01-solo-opencode-ollama/](templates/01-solo-opencode-ollama/) and
[templates/01-solo-cursor/](templates/01-solo-cursor/), carrying the plain
persona — the lighthouse is one paragraph away.

## Beyond this machine

Three pointers for later — nothing in this chapter needs them:

- Every flag the engine turn takes, and the permission and continuation
  translations across harnesses: [r4t engine](../docs/r4t-engine.md).
- Messaging an agent on another machine: [a8s remotes](../docs/a8s.md#remotes-issue-63).
- Reaching your agents by text message: [a8s-android](https://github.com/neilobremski/a8s-android).

## What you own

An agent with your instructions, a memory that survives every process it
runs in, an address, and a durable mailbox — yours, on your hardware, for as
long as you keep the handler running.

Keep `solo` and `me` running. You will notice what is missing the first time
you use it in anger: nothing bounds what a runaway wake costs you, a second
message that lands mid-turn waits on luck rather than a queue, and solo
reconstructs itself from notes each wake instead of resuming a conversation.
A second agent would need a second directory and a second definition. That is
chapter 2.
