# Chapter 1 — Hello, Agent

**Teaches R and A — [r4t](../docs/r4t.md)'s engine, then
[a8s](../docs/a8s.md), the message router.**

## 1. Capability

You already have agents. A `CLAUDE.md` or `AGENTS.md` you tuned, a harness
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

- ar3 installed and on your PATH:

**Run**

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
```

  That clones the suite into `~/.ar3` and adds one `source` line to your
  shell rc. Open a new shell (or re-source the rc) so `PATH` picks it up.

- Python 3 installed (`python3 --version` answers). On Windows the python.org
  installers give you `python.exe` and no `python3.exe`, so that command fails
  there even with Python installed — alias `python3` to `python` in your shell,
  or install Python from the Microsoft Store, which ships `python3`.
- **What software are you using?** This chapter is written around **Claude
  Code** — the `claude` CLI, installed and logged in — and every pasted output
  below was captured on it. If you run `cursor`, `codex` or `agy` instead,
  swap that word in wherever `claude` appears: the engine id is the only thing
  that changes, and the chapter holds line for line. `cursor` is the one id
  that does not name its own binary — the CLI it drives is called `agent`, and
  `agent` is the row it shows up as in the `ar3 doctor` panel below.
- **No subscription?** There is a free path all the way through:
  [OpenCode](https://opencode.ai/) driven by `ollama`, on a model that runs on
  your own hardware. It parts company with the main path only where an engine
  id appears — the `r4t engine` commands, the a8s definition name, and the
  `--model` tag a local preset needs — and each of those steps carries a
  *free path* variant below; every other command in the chapter is the same
  line either way. Install OpenCode first
  ([opencode.ai](https://opencode.ai/) carries the installer for your
  platform), then start the model download — it is the only long wait in the
  chapter:

**Run** (free path)

```bash
ollama pull qwen3.6
```

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

ar3 — a8s routes the messages, r4t governs the roster,
k7e keeps what they learn. ar3 reads; each product owns its own verbs.

a8s — agent message router  (/home/you/.config/a8s)
  ✓ cli       a8s -> /home/you/.ar3/a8s
  ✗ registry  no registry at /home/you/.config/a8s/a8s.json   (try: a8s discover <dir>)

r4t — the roster  (/home/you/.config/r4t)
  ✓ cli      r4t -> /home/you/.ar3/r4t
  ✗ rigs     no rig config at /home/you/.config/r4t/rigs.json   (try: r4t rig add <rig> <preset>)
  ✗ rosters  none under /home/you/.config/r4t/rosters   (try: r4t add <dir> [<runbook>])

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
suite: 0.1.70 (latest)

Harnesses
  ✓ claude    2.1.226 (Claude Code)  (/home/you/.local/bin/claude)
  ✓ agent     2026.08.04-aaa8809  (/home/you/.local/bin/agent)
  ✓ codex     codex-cli 0.147.0  (/home/you/.local/bin/codex)
  ✓ copilot   GitHub Copilot CLI 1.0.80.  (/home/you/.local/bin/copilot)
  ✓ opencode  1.18.3  (/home/you/.local/bin/opencode)
  ✓ agy       1.1.10  (/home/you/.local/bin/agy)
  ✓ ollama    ollama version is 0.32.13  (/home/you/.local/bin/ollama)

Services
  ✓ ollama serve  3 model(s): qwen3.6:latest, qwen3:4b, nomic-embed-text:latest
  ✓ docker        daemon 29.7.2

Tooling
  ✓ git  git version 2.51.0

✓ core prerequisites satisfied  (10/10 probes green)
```

Your panel will show ✗ for harnesses you haven't installed — that is fine.
This chapter needs one harness's worth: `claude`, or `opencode` plus `ollama`
on the free path. Everything else can stay red.

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

If you already keep a `CLAUDE.md` or `AGENTS.md` for some project, that file
works here unchanged — copy it in and skip ahead. Claude Code loads `CLAUDE.md`
by itself and most other harnesses load `AGENTS.md` by themselves; r4t's engine
turn reads `AGENTS.md` by absolute path on top of that, which is what makes one
file reach every engine. Your agent keeps its character; the engine adds the
rest.

### One turn, no roster

`r4t engine <id> run` is the bare-metal layer: one CLI, one headless turn, no
`r4t.md`, no dispatcher, no budgets, nothing to set up first. Give it a
prompt, get an answer.

**Run**

```bash
cd ~/ark/solo
r4t engine claude run --timeout 600 "You are my release assistant for a project called Foghorn. Record in STATUS.md that I am shipping 0.4.0 and that it needs a changelog before it goes out. Acknowledge in one sentence."
```

You should see:

```
Recorded in STATUS.md: Foghorn 0.4.0 is shipping and needs a changelog before release.
```

Your sentence will read differently. A model writes that line fresh on every
turn, so match the shape rather than the words — here, one short
acknowledgement naming the release and the blocker. The same holds for every
model-written block in this chapter; the command output around them is exact.

**Run** (free path) — the same command with a different engine id, plus the
`--model` tag the local presets need:

```bash
cd ~/ark/solo
r4t engine ollama-opencode run --model qwen3.6 --timeout 600 "You are my release assistant for a project called Foghorn. Record in STATUS.md that I am shipping 0.4.0 and that it needs a changelog before it goes out. Acknowledge in one sentence."
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
# STATUS

## Current State
Acting as release assistant for a project called **Foghorn**.
- Shipping version **0.4.0**.
- **Blocker before release: 0.4.0 needs a changelog written.**
No work started beyond recording this. No Foghorn source located on this machine yet.

## Important Context
- Recorded 2026-08-15.
- This STATUS.md was created fresh this turn (none existed; no LESSONS.md either).
- Foghorn repo/path unknown — not provided by the user.

## Next Steps
1. Ask for (or locate) the Foghorn repo path.
2. Draft the 0.4.0 changelog.
3. Confirm the changelog with the user before 0.4.0 ships.

## Decisions
- Kept the record minimal and factual — the user asked only to record the release + changelog requirement, so no scope was added.
- Did not search the filesystem for Foghorn: the ask was to record, not to start work, and a blind search is wasted tokens without a path.
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

**Run**

```bash
r4t engine claude run --timeout 600 "What release am I shipping, and what is still needed before it goes out?"
```

You should see:

```
STATUS.md updated. No new durable lessons to append.
```

Terse, and deliberately so: the prelude tells the engine to stay idle and
spend no words, and nothing at the command line has told it that you are owed
a reply. Read what it wrote instead of what it said:

**Run**

```bash
cat ~/ark/solo/STATUS.md
```

You should see, in the middle of the file:

```
## Important Context
- Recorded 2026-08-15.
- 2026-08-15: user asked "what release am I shipping, and what's still needed?" — answered from
  this file (0.4.0; changelog outstanding). No new work performed.
- Foghorn repo/path still unknown — never provided by the user; blocks drafting the changelog.
- No LESSONS.md in this directory.
```

A fresh process with no transcript named your release and your blocker. That
is the whole win, and it cost one flag-free command. The answer landed in the
file rather than in your terminal because nothing yet obliges solo to talk to
anybody — and that is the system working, not a shortcoming. Once solo is on
the network, nobody watches its terminal: messages are the only thing it
sends, `STATUS.md` is the only thing it keeps, and words spent anywhere else
are tokens nobody reads. The rule that obliges it to answer arrives further
down, and from there on it answers in sentences — addressed to someone.

`STATUS.md` is the turn-to-turn state; `LESSONS.md` is the long file, appended
one bullet at a time and never rewritten. Hand it something worth keeping:

**Run**

```bash
r4t engine claude run --timeout 600 "Durable fact worth keeping: Foghorn's changelog lists breaking changes first, because our users read only the top section."
```

You should see:

```
Recorded. Foghorn 0.4.0 still blocked on the changelog; repo path still unknown.

- LESSONS.md created with the convention: breaking changes first (users read only the top section).
- STATUS.md updated — convention noted in Important Context and folded into Next Step 2.

No other work started or restarted.
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

# LESSONS

- Foghorn changelogs lead with breaking changes — users typically read only the top section.
```

Three files, and you wrote one of them. `LESSONS.md` grows append-only; past
a line cap (`--lessons-cap`, 200 by default) the oldest lines rotate into
`LESSONS-ARCHIVE.md` before the turn starts, so the file never quietly turns
into the whole context window.

### Seeing exactly what runs

Two commands answer "what did r4t actually execute?" without you guessing.
`--echo` prints the composed argv and the full prompt to stderr and then runs
the turn — it is an echo, not a dry run, so this command spends a turn and
rewrites `STATUS.md` and `LESSONS.md` exactly as the ones above did:

**Run**

```bash
r4t engine claude run --echo "Say OK."
```

You should see, on stderr, before the answer:

```
r4t engine echo: argv: claude --permission-mode dontAsk --allowedTools 'Bash(tell:*) Bash(a8s convo:*) Read Edit Write Glob Grep WebFetch WebSearch TodoWrite' --exclude-dynamic-system-prompt-sections -p '{prompt}'
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

Read the argv line too. Two shell commands are on the allowlist: `tell`, which
is how the engine mails you, and `a8s convo`, which is how it reads its own
conversation — that one subcommand, not the router's other verbs. Nothing else
reaches a shell — a closed list of two is the whole permission budget solo
needs, and the reply rule below is what spends it.

`check` answers the same question without spending a turn at all: it composes
the argv `run` would use and asks the installed CLI whether it still parses,
driving only `--help` and `--version`.

**Run**

```bash
r4t engine claude check
```

You should see:

```
  claude  2.1.226 (Claude Code)  accepted (help scan)

No turn is spent: a check drives each CLI's own --help/--version.
```

(Free path: `r4t engine ollama-opencode check --model qwen3.6`, which answers
`ollama-opencode  ollama version is 0.32.13  accepted (help scan)` — first
column the engine id being checked, second the version of the binary its argv
starts with, which on a local preset is `ollama` itself.
Bare `r4t engine check` walks every run-capable engine on the machine.)

### Giving solo an address

solo answers when you call it. Now put it on the network, so it answers mail.

An a8s agent is a directory plus a **definition** — a small JSON file naming
the command that wakes it. When a message arrives, a8s substitutes `$SENDER`,
`$RECIPIENT` and `$MESSAGE` into that command and runs it with the agent's own
directory as the working directory. That default working directory is exactly
what `run` wants, so the definition is the command you have been typing — and
the suite ships one per engine, already written. You name it; you do not write
it.

`~/ark/me` is your **filedrop seat**: a directory a8s delivers your mail
into, with no CLI to wake — you read it with `tells`. Register both, then
look at the roster:

**Run**

```bash
a8s add me ~/ark/me filedrop
# claude can be replaced with cursor, codex, or agy depending on your preferred software
a8s add solo ~/ark/solo engine-claude
a8s ls
```

**Free path** — the local engines need a model named, and `a8s add` takes it
on the same line as an a8s var:

```bash
a8s add solo ~/ark/solo engine-ollama-opencode --model=qwen3.6
```

`a8s vars solo set MODEL qwen3.6` changes that later without re-adding the
node.

You should see:

```
added me -> /home/you/ark/me
definition: /home/you/.ar3/apps/a8s/definitions/filedrop.json  (explicit)
wake_path: recorded this shell's PATH for every node's wakes
added solo -> /home/you/ark/solo
definition: /home/you/.ar3/apps/a8s/definitions/engine-claude.json  (explicit)
NAME   STATUS    DEFINITION      ROOT
me     stopped   filedrop        /home/you/ark/me
solo   stopped   engine-claude   /home/you/ark/solo
```

That third line appears once, on the first `a8s add` on a machine. A woken
agent gets the environment of whatever shell started its handler, which is
right when you start from a terminal and wrong when cron or ssh does it — so
a8s writes down the PATH of the shell you are typing in now and gives every
wake that instead. It is why `tell` resolves inside solo's turn when the reply
goes out. `a8s config` shows it as `wake_path`.

Now read what you attached. It is a file that shipped with the suite, and the
three keys in it are the three ways an agent wakes:

**Run**

```bash
cat ~/.ar3/apps/a8s/definitions/engine-claude.json
```

You should see:

```json
{
  "description": "Claude Code as a bare stateless engine node — `r4t engine claude run`, one headless turn per wake with no roster or dispatcher; STATUS.md/LESSONS.md in the node's own root are its only memory. Tune it with --permissions and --allowed-tools on the invoke lines below; `r4t engine claude check` proves the argv still parses.",
  "invoke": [
    "python3", "$A8S_DIR/../r4t/r4t.py", "engine", "claude", "run",
    "--agent", "$RECIPIENT",
    "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"
  ],
  "batch": {
    "limit": 20,
    "invoke": [
      "python3", "$A8S_DIR/../r4t/r4t.py", "engine", "claude", "run",
      "--agent", "$RECIPIENT"
    ]
  },
  "idle": {
    "timeout": 900,
    "invoke": [
      "python3", "$A8S_DIR/../r4t/r4t.py", "engine", "claude", "run",
      "--idle", "--agent", "$RECIPIENT"
    ]
  }
}
```

Two more substitutions appear there. `$A8S_DIR` is a8s's own directory inside
the suite, which is how the definition reaches `r4t.py` without a path that
depends on where you installed ar3; `$AGE` is how long the message sat
before this wake, in words (`3 minutes ago`), so the turn can tell fresh mail
from a backlog it is only now getting to.

(The file on disk puts each argv token on its own line; it is folded here to
fit the page. The free path's `engine-ollama-opencode.json` is the same shape
with `--model $MODEL` added — that is the var `--model=qwen3.6` filled in.)

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

The shipped file sets no `max_wake_seconds`, so the turn's own guard rail is
r4t's `--timeout` — 900 seconds unless you say otherwise. Add
`"max_wake_seconds"` to a copy of your own (`a8s defs add`) when you want a8s
to kill a harness that hangs instead of exiting, and keep the engine's
`--timeout` under it, so r4t kills the turn and reports rather than a8s killing
r4t. [docs/r4t-engine.md](../docs/r4t-engine.md) has the full flag list.

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

`$SENDER` is what puts a name in front of solo, and `$NOW` is what puts a date
in front of it — that is why the definition's prompt is
`"[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"` rather than the bare
message. A model told only that a message is *5 minutes old* has no way to
answer *what did I do yesterday*. r4t's roster does this from outside the turn
in chapter 2, where a member cannot forget to answer; here it is a rule in a
file you own.

Registered but stopped: nothing routes until a **handler** process is
attached. Start one for each:

**Run**

```bash
a8s start solo
a8s start me
```

You should see:

```
started solo as PID 73521
started me as PID 73526
```

## 5. Run it

Speak from your seat. `tell` figures out who you are from the directory you
stand in, and `tells` watches your inbox for the window you give it. It prints
each message the moment it lands and then keeps polling to the end of the
window, so the prompt does not come back when the answer does — Ctrl+C once
you have read it. Give this first wait a long window: a wake pays for the
harness's own cold start before the turn begins, and on the free path for the
model's as well.

Ask it something only its own notes can answer:

**Run**

```bash
cd ~/ark/me
tell solo "What release am I shipping, and what is still needed before it goes out?"
tells --timeout 300
```

## 6. Expected receipt

You should see:

```
tell -> solo: What release am I shipping, and what is still needed before it goes out?
solo: Shipping Foghorn 0.4.0. Outstanding before it goes out: the 0.4.0 changelog still needs to be written (breaking changes first — Foghorn users read only the top section), and it should be confirmed with you before release. Blocker on my side: I don't have the Foghorn repo path, so I can't draft it yet. Send the path and I'll write it.
```

That round trip crossed the full machinery: your envelope was written to
`~/ark/me/.outbox/`, the router stamped you as the sender and moved it to
solo's inbox, solo's handler ran `r4t engine ... run` with your text as the
routed input, the engine composed the cold-boot prompt around it, and the
reply rode the same road back into `~/ark/me/.inbox/`.

And solo knew about Foghorn 0.4.0. Nothing in that message mentioned it, and
this was a fresh process with no transcript — it read its own `STATUS.md`,
exactly as it did at the command line. The changelog convention in the answer
came out of `LESSONS.md`, which you never mentioned either. Same agent, same
memory, now with an address — and now, because `AGENTS.md` tells it who it
owes an answer to, it speaks in sentences instead of filing a note.

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
solo: sent SIGTERM to PID 73521
waiting up to 600s for stop…
solo: stopped
tell -> solo: Are you still there?
tells: no message within 20s
```

`stop` waits up to ten minutes for the handler to let go, because on macOS and
Linux the handler catches that SIGTERM and finishes a wake already in flight
before it detaches. Windows has no such signal — there `SIGTERM` is a
`TerminateProcess`, no handler runs, and a wake in flight dies with it; the
lines above are the same either way. Then `tell` accepted your message and
nothing answered. Nothing crashed and nothing warned you, which is exactly the
state worth learning to read.

## 8. Diagnose

Two reads settle any "where did my message go?" question. The registry says
who is attached:

**Run**

```bash
a8s ls
```

You should see:

```
NAME   STATUS                DEFINITION      ROOT
me     running (pid 73526)   filedrop        /home/you/ark/me
solo   stopped               engine-claude   /home/you/ark/solo
```

And the per-agent log says what actually moved:

**Run**

```bash
a8s logs solo --tail 2
```

You should see:

```
2026-08-15T18:40:43.775861Z [a8s] solo: detached
2026-08-15T18:40:44.856589Z received from me: Are you still there?
```

Read those two lines together. The handler **detached**, and after that the
message was **received** — routed out of your outbox into solo's inbox on
disk — with no wake line following it, because no process was there to wake.
It is not lost and it is not held in anyone's memory; it is a file waiting in
a directory. That is the whole durability story, and it is why the fix is one
command.

(That log is also where a wake's own output lands, one `solo>` line per line
the turn printed. It is the first place to look when a wake runs but no answer
arrives — a harness that refused a tool says so there — and
`r4t engine <id> check` tells you whether the argv itself is at fault.)

## 9. Fix

Attach a handler again:

**Run**

```bash
a8s start solo
tells --timeout 300
```

You should see:

```
started solo as PID 78149
solo: Still here. Nothing in progress — Foghorn 0.4.0, changelog still outstanding. Send me the repo path and I'll draft it (breaking changes first).
```

The waiting message woke solo the moment a handler claimed it. You did not
resend anything — and solo picked up the release context from disk on its way
through. `tells` is still polling out the rest of its 300 seconds behind that
answer, as it always does; Ctrl+C.

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
tells --timeout 300
```

You should see:

```
tell -> solo: What are you watching over today?
solo: Watching one thing: Foghorn 0.4.0. Blocker is the 0.4.0 changelog — not written yet, and I still need the Foghorn repo path (never provided). When drafted: breaking changes first. Weather: no weather feed wired up on this machine, so I can't report real conditions — flagging that rather than inventing a forecast.
```

New character, same memory. That split is the point: `AGENTS.md` is who solo
is and you own it outright, `STATUS.md` is what solo knows and it owns that.

## 12. Commit point

Your agent is one file you wrote — the definition came with the suite — so
keep that file under version control like anything else you built.
`STATUS.md` and `LESSONS.md` are solo's working memory rather than your
source, so leave them out:

**Run**

```bash
cd ~/ark/solo
git init -q
printf 'STATUS.md\nLESSONS.md\nLESSONS-ARCHIVE.md\n' > .gitignore
git add AGENTS.md .gitignore
git commit -q -m "solo: an engine-backed agent behind a8s"
```

A copy-paste version of solo's `AGENTS.md` lives in
[templates/01-solo-claude/](templates/01-solo-claude/),
[templates/01-solo-opencode-ollama/](templates/01-solo-opencode-ollama/) and
[templates/01-solo-cursor/](templates/01-solo-cursor/), carrying the plain
persona — the lighthouse is one paragraph away. The cursor and ollama
directories also carry a hand-written `solo.json`, which is what a definition
looks like when you do write one yourself; the claude one needs none, because
`engine-claude` ships with the suite.

## Beyond this machine

Three pointers for later — nothing in this chapter needs them:

- Every flag the engine turn takes, and the permission and continuation
  translations across harnesses: [r4t engine](../docs/r4t-engine.md).
- Messaging an agent on another machine: [a8s remotes](../docs/a8s.md#remotes-bin63).
- Reaching your agents by text message: [a8s-android](https://github.com/neilobremski/a8s-android).

## What you own

An agent with your instructions, a memory that survives every process it
runs in, an address, and a durable mailbox — yours, on your hardware, for as
long as you keep the handler running.

Keep `solo` and `me` running. You will notice what is missing the first time
you use it in anger: nothing bounds what a runaway wake costs you, a second
message that lands mid-turn waits on luck rather than a queue, and solo
reconstructs itself from notes each wake instead of resuming a conversation.
A second agent would need a second directory and its own registration. That is
chapter 2.
