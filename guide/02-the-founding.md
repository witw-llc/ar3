# Chapter 2 — The Founding

**Teaches R — [r4t](../docs/r4t.md), the roster.**

## 1. Capability

At the end of this chapter your agent has a roster around it. Same machine,
same model, same kind of answers — but the agent is **Wren**, the one member
of roster **silo**, and r4t holds the edges chapter 1's `solo` was holding
bare: a spend budget, a queue that parks messages instead of losing them, a
conversation that persists across turns, and a seat you speak from as
yourself. You will prove the persistence with a codeword, break the
configuration on purpose, and read the fail-closed error that stops it from
ever half-running.

## 2. Time

About 20 minutes. Nothing new to install.

## 3. Starting state

- Chapter 1 complete: `solo` answers a `tell` from the seat at `~/ark/me`,
  and `ar3` shows the a8s section green.
- The same harness chapter 1 used — `ollama` with `qwen3.6` on the free
  path, the Cursor agent CLI (`agent`) on the subscription path.

## 4. The change

Chapter 1's agent rebuilds itself from its own notes on every wake. That is
enough to remember and not enough to be governed: nothing bounds what it
spends, a message that lands while it is mid-answer waits on nothing but
luck, and no CLI conversation stays open from one turn to the next. r4t takes
all of that off your hands —
and it governs *rosters*, not lone agents, so the first move is to give the
agent a roster to belong to.

A roster is two files with two jobs: `ROSTER.md` in the roster repo says *who*
exists, and `~/.config/r4t/rigs.json` — outside the repo, where a repo edit
can't reach it — says what each member's **rig** actually runs.

The directory name is the roster name; r4t reads it off the folder. From
outside, the roster is one address, and who stands behind it is machinery the
sender never sees. Chapter 4 puts a second pair of hands behind that address
without the address changing.

Make the roster directory and let r4t write the starters:

**Run**

```bash
mkdir -p ~/ark/silo
cd ~/ark/silo
r4t init
```

You should see:

```
roster: wrote starter /home/you/ark/silo/ROSTER.md
rig config: wrote starter /home/you/.config/r4t/rigs.json

Register and start the roster (a namespace prefix cannot share a
name with its agent, so the node is registered as <roster>-node):

  a8s add silo-node /home/you/ark/silo r4t
  a8s namespace silo silo-node
  a8s start silo-node
  tell silo "hello"            # bare namespace -> roster leader
  tell silo:dev "hello"        # namespace:member -> specific member
```

The starter roster has three members. Ours is smaller. Replace it
with a roster of one AI and one human — you:

**Replace** `~/ark/silo/ROSTER.md` (whole file)

```markdown
# Roster

### You
- **Human:** yes
- **Role:** Owner

### Wren
- **Rig:** silo
- **Leader:** yes
- **Continue:** on
- **Workdir:** agents/wren
- **Role:** The solo agent — does the work and answers the owner

Wren is a roster of one: leader, developer, and correspondent in a single
seat. Keep answers short and concrete.
```

The prose under Wren's heading does the job `AGENTS.md` does in chapter 1: it
is the character the answers come out in. On a roster that prose lives beside
the name it belongs to, and r4t puts it in Wren's prompt at every turn.

Four lines carry the rest of the weight. `Leader: yes` — external mail
enters at Wren. `Rig: silo` — a symbolic name; what it runs comes next, from
outside the repo. `Continue: on` — Wren's turns resume its CLI's own
conversation instead of starting cold every wake, which chapter 1's agent
could only approximate by re-reading a file. `Workdir: agents/wren` — Wren gets its own subfolder, so
its conversation and files never collide with a future member's.

Now define the `silo` rig. Pick your path — and note that these two
presets are only the blessed pair: the other popular harness CLIs are
presets too (`r4t rig presets` lists them, and later guide branches may
walk through more of them), added by the same one-line command.

**Run** (free path)

```bash
r4t rig add silo ollama-opencode --model qwen3.6
r4t rig set silo echo true
```

You should see:

```
added rig 'silo' (ollama-opencode) to /home/you/.config/r4t/rigs.json
  invoke: ollama launch opencode --model qwen3.6 -- run --auto --dir {workdir} {prompt}
Reference it from ROSTER.md: `- **Rig:** silo`
set silo echo = true in /home/you/.config/r4t/rigs.json
```

That `invoke:` line is the argv `r4t engine ollama-opencode run` composed for
you in chapter 1. It is a rig now: named once, kept outside the repo, and
available to every member who asks for it by name.

**Run** (subscription path)

```bash
r4t rig add silo cursor
r4t rig set silo echo true
```

You should see:

```
added rig 'silo' (cursor) to /home/you/.config/r4t/rigs.json
  invoke: agent --model auto -p --trust --force --approve-mcps {prompt}
Reference it from ROSTER.md: `- **Rig:** silo`
set silo echo = true in /home/you/.config/r4t/rigs.json
```

Naming no model of your own is the deliberate part: the preset writes
`--model auto`, which is the Cursor CLI's own way of saying *whatever my
subscription defaults to* — covered by what you already pay. The pin
itself matters. Left off entirely, `agent` reuses the last model it was
given on this machine, so an invoke with no flag inherits a choice you
cannot see and did not make here. Name one yourself with
`--model <name>` and you can land on a frontier model billed as
usage-based credits, which a chatty agent burns through fast; do that
only when you mean to. (`agent models` lists what your account can run.)

`echo true` makes Wren **stdout-only**: its turn prompt carries no
messaging doctrine, and whatever it prints becomes its one reply to you.
That is the right shape for a roster of one — Wren has nobody to message but
you — and it is the reply rule you wrote into chapter 1's `AGENTS.md` done
for you: capture what the harness printed, send it to whoever asked. r4t does
it from outside the turn, so a member cannot forget. Without echo, a
member has to run `tell` itself, and prose answers under ~80 characters get
discarded as terminal chrome. Chapter 4 lifts echo when the roster grows.

Lint before going live — r4t fails closed on any roster/rig disagreement:

**Run**

```bash
cd ~/ark/silo
r4t roster check
```

You should see:

```
You: note — Human without an Address (roster cannot tell them)
/home/you/ark/silo/ROSTER.md: OK (2 member(s), leader Wren)
```

The note is expected: you have no a8s doorbell address yet, so the roster
can't ring you when you're away. You'll read your mail at the seat
instead. Finally, register the node on a8s exactly as `r4t init` printed:

**Run**

```bash
a8s add silo-node ~/ark/silo r4t
a8s namespace silo silo-node
a8s start silo-node
```

You should see:

```
added silo-node -> /home/you/ark/silo
definition: /home/you/.ar3/apps/a8s/definitions/r4t.json  (explicit)
bound silo: -> silo-node
started silo-node as PID 23851
```

Wren has chapter 1's job now, so retire the bare node and let the roster's
address take over:

**Run**

```bash
a8s stop solo
a8s remove solo
```

You should see:

```
solo: sent SIGTERM to PID 11786
waiting up to 600s for stop…
solo: stopped
removed solo
```

`~/ark/solo` and everything in it stays on disk — only the registration went
away. The address is what moved: one name on the registry reaches a whole
roster, and a roster can grow.

## 5. Run it

You are the roster's Human, and r4t gives you a **seat**: send as
yourself, and read what parks for you. `seat send` queues Wren's turn and
returns immediately — it does not wait for the reply. Poll the inbox (or
just wait a beat and read it once) until something is there: the first turn
takes a minute on the free path while the model loads, later ones take
seconds. r4t also holds each rig to a cadence floor —
`min_seconds_between_turn_starts`, 15s by default — so a send fired before
the previous turn has started just queues behind it instead of running
immediately.

**Run**

```bash
cd ~/ark/silo
r4t seat send --node silo "In one sentence: what is your job on this roster?"
r4t seat inbox --node silo
```

(`--node silo` is needed the first time; once the roster has dispatched a
turn, r4t finds the node from inside the repo on its own.)

## 6. Expected receipt

You should see:

```
── from silo:wren (2026-07-29T04:55:04.121285Z)
My job is to do the work and answer to the owner — handling everything from leadership to development as the sole member of the silo roster.
```

Wren read its own roster block and answered in character. Now the proof
that `Continue: on` means what it says — plant a codeword in one turn. Let
the reply above land before you send this one; three sends fired back to
back can outrun Wren's spend budget on top of the cadence floor, and a
reply of `queued — Wren is resting (member budget ...)` means exactly that —
wait the minutes it names, or run `r4t clear --node silo` to drop the
queue and retry (chapter 3 covers both in depth):

**Run**

```bash
r4t seat send --node silo "Remember this codeword: TIDEPOOL. Confirm you have it."
r4t seat inbox --node silo
```

You should see:

```
── from silo:wren (2026-07-29T04:55:15.702909Z)
Codeword confirmed: TIDEPOOL.
```

Let that confirmation land, then ask for the codeword back in a second,
separate turn:

**Run**

```bash
r4t seat send --node silo "What was the codeword?"
r4t seat inbox --node silo
```

You should see:

```
── from silo:wren (2026-07-29T04:55:30.722365Z)
TIDEPOOL.
```

Two processes, two wakes, one conversation. That continuity is what
`Continue: on` buys, and it is the one thing chapter 1's agent could not do:
solo re-read its own notes each wake, but it never resumed a conversation.

## 7. Break it

`Continue: on` needs a rig whose CLI can actually resume a conversation.
Swap Wren's rig to the bare `ollama` preset — a raw model prompt, no
session store, no continue support:

**Run**

```bash
r4t rig swap silo ollama --model qwen3.6
r4t roster check
```

You should see:

```
swapped rig 'silo' to ollama in /home/you/.config/r4t/rigs.json
  invoke: ollama run qwen3.6 {prompt}
You: note — Human without an Address (roster cannot tell them)
Wren: Wren has Continue: on but rig 'silo' does not support it (preset ollama; presets that continue: agy, claude, codex, cursor, ollama-opencode, opencode) — try: r4t rig swap silo <preset>
1 problem(s)
```

## 8. Diagnose

Read the error end to end — it names the member, the rig, the reason, the
presets that would work, and the exact command to run. Exit code is 1, and
the same check runs at dispatch: a member in this state **does not run**,
and whoever messages it is told why. r4t never silently downgrades
`Continue: on` to cold prompts — that would look like working while
quietly lobotomizing your agent every turn.

## 9. Fix

Take the error's suggestion:

**Run**

```bash
r4t rig swap silo ollama-opencode --model qwen3.6
r4t roster check
```

(Subscription path: swap back to `cursor` instead.)

You should see:

```
swapped rig 'silo' to ollama-opencode in /home/you/.config/r4t/rigs.json
  invoke: ollama launch opencode --model qwen3.6 -- run --auto --dir {workdir} {prompt}
You: note — Human without an Address (roster cannot tell them)
/home/you/ark/silo/ROSTER.md: OK (2 member(s), leader Wren)
```

## 10. Check

The codeword is the health check. Ask again:

**Run**

```bash
r4t seat send --node silo "What was the codeword?"
r4t seat inbox --node silo
```

You should see:

```
── from silo:wren (2026-07-29T04:55:49.122781Z)
TIDEPOOL.
```

Wren still knows. Then ask r4t where the roster stands:

**Run**

```bash
r4t status --node silo
```

You should see (health and roster sections):

```
Health
  ✓ nothing waiting on you
  ✓ no runaway signs (3 turn(s) last 10m)
  ✓ all 1 member(s) healthy

Roster  (repo settings: /home/you/ark/silo/ROSTER.md)
    You   Human  address=(none)   (try: add an **Address:** line so the roster can reach them)
  ✓ Wren  rig=silo  budget=5.1/8  [leader]
```

Three answers a bare agent cannot give you: nothing is waiting on you,
nothing is looping, and Wren is inside its spend budget. `budget=5.1/8` is
the guard rail chapter 1 had nowhere to put — every turn above drew from that
bucket, and it refills on a clock. Chapter 4 makes it bite.

## 11. Customize

One line in the roster: bound how long Wren's conversation may sit idle.

**Replace** `~/ark/silo/ROSTER.md` — in Wren's block, swap the `Continue:`
line for a duration:

```markdown
- **Continue:** 15m
```

A conversation idle past fifteen minutes is retired — Wren is prompted
once to write its state to disk, and the next real message founds a fresh
conversation from that saved state. It keeps a long-lived agent from
dragging weeks of stale context into every turn, and it sits near where
the providers stop discounting a resumed conversation; the full mechanics
are chapter 3's subject.

**Run**

```bash
r4t roster check
```

You should see:

```
You: note — Human without an Address (roster cannot tell them)
/home/you/ark/silo/ROSTER.md: OK (2 member(s), leader Wren)
```

## 12. Commit point

The roster is repo state; commit it. (The rig config stays outside the
repo by design — that split is what makes a roster edit unable to smuggle
in commands.)

**Run**

```bash
cd ~/ark/silo
git init -q
git add ROSTER.md
git commit -q -m "silo roster: Wren, continue 15m"
```

Copy-paste templates for this chapter's final state live in
[templates/02-solo-opencode-ollama/](templates/02-solo-opencode-ollama/)
and [templates/02-solo-cursor/](templates/02-solo-cursor/).

Wren remembers everything inside one conversation. Chapter 3 ends the
conversation and asks what survives.
