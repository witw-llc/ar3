# Chapter 2 — The Founding

**Teaches R — [r4t](../docs/r4t.md), the roster.**

## 1. Capability

At the end of this chapter your agent has a roster around it. Same machine,
same model, same kind of answers — but the agent is **Wren**, the one member
of roster **silo**, and r4t holds the edges chapter 1's `solo` was holding
bare: a spend budget, a queue that runs one turn at a time instead of losing
the second message, and a conversation that persists from one wake to the
next. You will write the file that says what the team is, register it with one
command, prove the persistence with a codeword, then break the configuration
on purpose and read the fail-closed error that stops it from ever
half-running.

## 2. Time

About 20 minutes. Nothing new to install.

## 3. Starting state

- Chapter 1 complete: `solo` answers a `tell` from your seat at `~/ark/me`,
  and `ar3` shows the a8s section green.
- A harness chapter 1 got working — `ollama` with `qwen3.6` through OpenCode
  on the free path, the Cursor agent CLI (`agent`) on the subscription path.

## 4. The change

Chapter 1's agent rebuilds itself from its own notes on every wake. That is
enough to remember and not enough to be governed: nothing bounds what it
spends, a message that lands while it is mid-answer waits on nothing but luck,
and no CLI conversation stays open from one turn to the next. r4t takes all of
that off your hands — and it governs *rosters*, not lone agents, so the first
move is to give the agent a roster to belong to.

A roster is **one file**: `r4t.md`, at the top of the roster's own directory.
It says who exists, what each member runs, and how the team works. The
directory name is the roster name; r4t reads it off the folder. From outside,
the roster is one address, and who stands behind it is machinery the sender
never sees. Chapter 4 puts a second pair of hands behind that address without
the address changing.

Make the directory and let r4t write the starter:

**Run**

```bash
mkdir -p ~/ark/silo
cd ~/ark/silo
r4t init
```

You should see:

```
runbook: wrote starter /home/you/ark/silo/r4t.md
next: r4t add /home/you/ark/silo
```

That file is a **runbook**, and it did not start empty. Its frontmatter says
`extends: "triforce"` — a built-in runbook that ships with the suite, carrying
a three-member team (a lead who talks to you, a builder, and one whose job is
to break what the builder made) and a written charter about how they work. You
inherit all of it and edit only what differs.

Ask which layer each section is coming from:

**Run**

```bash
r4t runbook show --resolved --sources | head -14
```

You should see:

```
---
comms: "open"
egress: true
name: "silo"
workdir: "."
---

# silo

## Mission                                    [r4t.md]

Keep one small project moving, and answer the owner when he asks.

## Charter                                    [triforce]
```

A runbook has exactly six `##` sections and no others — `Mission`, `Charter`,
`Roster`, `Cells`, `Rigs`, `Rituals` — and a section you write **replaces the
base's whole**. It never blends. That rule is the one thing to hold on to
here, because it is about to bite in a useful way.

The starter's team is three. Ours is one. Replace the file:

**Replace** `~/ark/silo/r4t.md` (whole file)

```markdown
---
name: "silo"
extends: "triforce"
---

# silo

## Mission

Keep one small project moving, and answer the owner when he asks.

## Roster

### Wren
- **Rig:** silo
- **Leader:** yes
- **Continue:** on
- **Workdir:** agents/wren
- **Role:** The solo agent — does the work and answers the owner

Wren is a roster of one: leader, developer, and correspondent in a single
seat. Keep answers short and concrete.

## Rituals

None yet.
```

One member and a mission is all you wrote, and the charter you never wrote is
still there — `extends:` kept it. That is the trade the runbook makes: the
parts every team needs come from the base, and the file you maintain is only
what makes this roster different.

The prose under Wren's heading does the job `AGENTS.md` does in chapter 1: it
is the character the answers come out in. On a roster that prose lives beside
the name it belongs to, and r4t puts it in Wren's prompt at every turn.

Four bullets carry the rest of the weight. `Leader: yes` — mail addressed to
the roster lands here. `Rig: silo` — a symbolic name; what it runs comes next,
from outside the repo. `Continue: on` — Wren's turns resume the CLI's own
conversation instead of starting cold every wake, which chapter 1's agent
could only approximate by re-reading a file. `Workdir: agents/wren` — Wren
gets its own subfolder, so its conversation and files never collide with a
future member's.

The empty `## Rituals` section is doing real work. triforce declares a
standup and a mission review, both addressed to a member named `Lead` — and
you just deleted `Lead`. Leave the section out and those inherited rituals
address nobody, which r4t refuses to load. Writing the heading with nothing
under it replaces the base's whole and gives this roster no rituals at all.
Try it without and you get the error verbatim:

```
ritual standup: To 'Lead' names neither a member nor a cell
ritual mission-review: To 'Lead' names neither a member nor a cell
2 problem(s)
```

Now define the `silo` rig — the one thing that does *not* live in the repo.
A runbook edit can name a rig; only you, on this machine, can say what that
rig actually executes. That split is what stops a roster edit from smuggling
in commands. Pick your path — and note that these two presets are only the
blessed pair: the other popular harness CLIs are presets too (`r4t rig
presets` lists them), added by the same one-line command.

**Run** (free path)

```bash
r4t rig add silo ollama-opencode --model qwen3.6
r4t rig set silo echo true
```

You should see:

```
added rig 'silo' (ollama-opencode) to /home/you/.config/r4t/rigs.json
  invoke: ollama launch opencode --model qwen3.6 -- run --auto --dir {workdir} {prompt}
Reference it from your runbook: `- **Rig:** silo`
set silo echo = true in /home/you/.config/r4t/rigs.json
```

**Run** (subscription path)

```bash
r4t rig add silo cursor
r4t rig set silo echo true
```

You should see:

```
added rig 'silo' (cursor) to /home/you/.config/r4t/rigs.json
  invoke: agent --model auto -p --trust --force --approve-mcps {prompt}
Reference it from your runbook: `- **Rig:** silo`
set silo echo = true in /home/you/.config/r4t/rigs.json
```

Naming no model of your own is the deliberate part: the preset writes
`--model auto`, which is the Cursor CLI's own way of saying *whatever my
subscription defaults to* — covered by what you already pay. The pin itself
matters. Left off entirely, `agent` reuses the last model it was given on this
machine, so an invoke with no flag inherits a choice you cannot see and did
not make here. Name one yourself with `--model <name>` and you can land on a
frontier model billed as usage-based credits, which a chatty agent burns
through fast; do that only when you mean to. (`agent models` lists what your
account can run.)

That `invoke:` line is the argv `r4t engine <id> run` composed for you in
chapter 1. It is a rig now: named once, kept outside the repo, and available
to every member who asks for it by name. (The `Rig:` line the hint mentions is
the one already in your `r4t.md`.)

From here on the pasted output in this chapter comes from the free path; the
subscription path prints the same lines with `cursor` in place of
`ollama-opencode`.

`echo true` makes Wren **stdout-only**: its turn prompt carries no messaging
doctrine, and whatever it prints becomes its one reply. That is the right
shape for a roster of one — Wren has nobody to message but you — and it is the
reply rule you wrote into chapter 1's `AGENTS.md` done for you: capture what
the harness printed, send it to whoever asked. r4t does it from outside the
turn, so a member cannot forget. Without echo, a member has to run `tell`
itself, and prose answers under ~80 characters get discarded as terminal
chrome. Chapter 4 lifts echo when the roster grows.

Lint before going live — r4t fails closed on any runbook/rig disagreement:

**Run**

```bash
r4t runbook check
```

You should see:

```
runbook: triforce -> r4t.md
/home/you/ark/silo/r4t.md: OK (1 member(s), leader Wren)
```

The first line is the inheritance chain, base first. Now register the roster:

**Run**

```bash
r4t add ~/ark/silo
```

You should see:

```
runbook: triforce -> r4t.md
/home/you/ark/silo/r4t.md: OK (1 member(s), leader Wren)

added silo -> /home/you/ark/silo
  runbook:   /home/you/ark/silo/r4t.md
  address:   silo (leader Wren), silo:<member> for a member with Ingress:
  ceiling:   permissions auto

  tell silo "hello"
```

One command, three facts. **runbook** — the file it validated and will read on
every wake. **address** — `silo` reaches the leader, and `silo:<member>` would
reach a member that declared itself a door. **ceiling** — the strongest
permission stance any rig on this node may ask for, recorded on this machine
and never in the repo, so cloning the repo somewhere else cannot raise it.
(`r4t add --trust` is what lifts it, and chapter 1's engine turns never needed
one because they had no roster to govern.)

It also did the work chapter 1 did by hand: registered the directory as an a8s
agent, bound `silo:` as a namespace, and started the handler. Check it:

**Run**

```bash
a8s ls
```

You should see:

```
NAME   STATUS                DEFINITION               ROOT                       NAMESPACES
me     running (pid 27063)   filedrop                 /home/you/ark/me           
silo   running (pid 27414)   r4t                      /home/you/ark/silo         silo:
solo   running (pid 15721)   engine-ollama-opencode   /home/you/ark/solo         
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
solo: sent SIGTERM to PID 15721
waiting up to 600s for stop…
solo: stopped
removed solo
```

`~/ark/solo` and everything in it stays on disk — only the registration went
away. The address is what moved: one name on the registry reaches a whole
roster, and a roster can grow.

## 5. Run it

Nothing new to learn to speak to it. You are still standing at the seat
chapter 1 built, and `tell` still figures out who you are from the directory
you stand in — the roster is just another address on the same network. Give
this first wait a long window: the first turn pays for the harness's cold
start, and on the free path for the model's as well.

**Run**

```bash
cd ~/ark/me
tell silo "In one sentence: what is your job on this roster?"
tells --timeout 300
```

## 6. Expected receipt

You should see:

```
tell -> silo: In one sentence: what is your job on this roster?
silo:wren: I do the work on my project and answer the owner when he asks.
```

Wren read its own roster block and answered in character. Note the sender:
`silo:wren`, not `silo`. You mailed the roster; a *member* answered, and the
reply says which one.

Now the proof that `Continue: on` means what it says — plant a codeword in one
turn:

**Run**

```bash
tell silo "Remember this codeword: TIDEPOOL. Confirm you have it."
tells --timeout 300
```

You should see:

```
tell -> silo: Remember this codeword: TIDEPOOL. Confirm you have it.
silo:wren: TIDEPOOL confirmed.
```

Let that land, then ask for the codeword back in a second, separate turn:

**Run**

```bash
tell silo "What was the codeword?"
tells --timeout 300
```

You should see:

```
tell -> silo: What was the codeword?
silo:wren: TIDEPOOL
```

Two processes, two wakes, one conversation. That continuity is what
`Continue: on` buys, and it is the one thing chapter 1's agent could not do:
solo re-read its own notes each wake, but it never resumed a conversation.

Now read what the roster did while you waited. `r4t logs` is the **ticker** —
one line per lifecycle event, no message bodies, no transcripts:

**Run**

```bash
cd ~/ark/silo
r4t logs -n 12
```

You should see:

```
— log day 2026-08-17 UTC (this machine reads PDT)
r4t: QUEUED me -> wren thread=01ABC... hop=0 "Remember this codeword: TIDEPOOL. Confirm you have it." (depth 1)
turn: 1 message(s) -> Wren (threads 01ABC..., rig silo)
r4t: PROMPT wren echo 2.8k — intro 0.2k mission 0.1k charter 1.4k persona 0.3k history 0.7k messages 0.1k
done: Wren, exit 0 in 4.6s
r4t: ECHO-REPLY wren (rig silo) 19 bytes of cleaned stdout staged as the reply to me
r4t: RELEASED silo:wren -> me thread=01ABC... hop=1
r4t: QUEUED me -> wren thread=01ABC... hop=0 "What was the codeword?" (depth 1)
turn: 1 message(s) -> Wren (threads 01ABC..., rig silo)
r4t: PROMPT wren echo 3.0k — intro 0.2k mission 0.1k charter 1.4k persona 0.3k history 0.8k messages 0.1k
done: Wren, exit 0 in 6.9s
r4t: ECHO-REPLY wren (rig silo) 8 bytes of cleaned stdout staged as the reply to me
r4t: RELEASED silo:wren -> me thread=01ABC... hop=1
```

Six events per message, and the same six every time: **QUEUED** the message
landed in Wren's queue, **turn** the dispatcher started one, **PROMPT** what
that turn's prompt cost in bytes and where the bytes went, **done** the exit
code and the wall time, **ECHO-REPLY** stdout staged as the answer, and
**RELEASED** the envelope left for you. Nothing here is a guess about what
happened; it is the record. Note the `charter 1.4k` on the PROMPT line — that
is triforce's charter, riding into every turn because you inherited it.

Note also the date on the first line, and that it disagrees with your clock.
Log days are UTC so two machines' logs can be merged; every time a human reads
is local and says which zone it is in.

## 7. Break it

`Continue: on` needs a rig whose CLI can actually resume a conversation. Swap
Wren's rig to the bare `ollama` preset — a raw model prompt, no session store,
no continue support:

**Run**

```bash
r4t rig swap silo ollama --model qwen3.6
r4t runbook check
```

You should see:

```
swapped rig 'silo' to ollama in /home/you/.config/r4t/rigs.json
  invoke: ollama run qwen3.6 {prompt}
runbook: triforce -> r4t.md
Wren: Wren has Continue: on but rig 'silo' does not support it (preset ollama; presets that continue: agy, claude, codex, cursor, ollama-opencode, opencode) — try: r4t rig swap silo <preset>
1 problem(s)
```

## 8. Diagnose

Read the error end to end — it names the member, the rig, the reason, the
presets that would work, and the exact command to run. Exit code is 1, and the
same check runs at dispatch: a member in this state **does not run**, and
whoever messages it is told why. r4t never silently downgrades `Continue: on`
to cold prompts — that would look like working while quietly lobotomizing your
agent every turn.

## 9. Fix

Take the error's suggestion:

**Run**

```bash
r4t rig swap silo ollama-opencode --model qwen3.6
r4t runbook check
```

(Subscription path: swap back to `cursor` instead.)

You should see:

```
swapped rig 'silo' to ollama-opencode in /home/you/.config/r4t/rigs.json
  invoke: ollama launch opencode --model qwen3.6 -- run --auto --dir {workdir} {prompt}
runbook: triforce -> r4t.md
/home/you/ark/silo/r4t.md: OK (1 member(s), leader Wren)
```

## 10. Check

The codeword is the health check. Ask again:

**Run**

```bash
cd ~/ark/me
tell silo "What was the codeword?"
tells --timeout 300
```

You should see:

```
tell -> silo: What was the codeword?
silo:wren: TIDEPOOL.
```

Then ask r4t where the roster stands:

**Run**

```bash
cd ~/ark/silo
r4t status
```

You should see:

```
roster: silo
state: /home/you/.config/r4t/rosters/silo
time: 2026-08-16 23:30 PDT

Rotation  (one turn at a time)
  Now   —  idle 42s   (last: wren, exit 0)
  Next  —  nothing ready to run
  Idle     1 member(s) with nothing queued

Health
  ✓ no runaway signs (3 turn(s) last 10m)
  ✓ all 1 member(s) healthy

Roster  (repo settings: /home/you/ark/silo/r4t.md)
  ✓ Wren  rig=silo  budget=4.7/8  [leader]

Rigs  (your configuration: /home/you/.config/r4t/rigs.json)
  ✓ silo        ollama launch opencode --model qwen3.6 -- run --auto --dir {workdir} {prompt}  (timeout=900s budget=8/+4per-h sends=6)
    contract    one turn at a time  (cadence 0s)
    governance  cell_budget=16/+8per-h  breaker=5/600s

Activity
    dead letters  0
```

Read the **Rotation** block first, because it is the contract the whole
dispatcher is built on: *one turn at a time*. A roster never runs two members
at once. `Now` is what is running (or how long it has been quiet), `Next` is
who goes when the current turn ends, and everyone else waits in a queue on
disk. A second message arriving mid-turn does not race and does not vanish —
it takes its place in line. That is the thing chapter 1 had no answer for.

Then the guard rails a bare agent cannot give you: nothing is looping,
Wren is inside its spend budget (`budget=4.7/8` — every turn above drew from
that bucket, and it refills on a clock), and the rig's own limits are printed
where you can read them. Chapter 4 makes the budget bite.

## 11. Customize

One line in the runbook: bound how long Wren's conversation may sit idle.

**Replace** `~/ark/silo/r4t.md` — in Wren's block, swap the `Continue:` line
for a duration:

```markdown
- **Continue:** 15m
```

A conversation idle past fifteen minutes is retired — Wren is prompted once to
write its state to disk, and the next real message founds a fresh conversation
from that saved state. It keeps a long-lived agent from dragging weeks of
stale context into every turn, and it sits near where the providers stop
discounting a resumed conversation; the full mechanics are chapter 3's
subject.

**Run**

```bash
r4t runbook check
```

You should see:

```
runbook: triforce -> r4t.md
/home/you/ark/silo/r4t.md: OK (1 member(s), leader Wren)
```

## 12. Commit point

The runbook is repo state; commit it. (The rig config stays outside the repo
by design — that split is what makes a roster edit unable to smuggle in
commands.)

**Run**

```bash
cd ~/ark/silo
git init -q
git add r4t.md
git commit -q -m "silo roster: Wren, continue 15m"
```

Copy-paste templates for this chapter's final state live in
[templates/02-solo-opencode-ollama/](templates/02-solo-opencode-ollama/)
and [templates/02-solo-cursor/](templates/02-solo-cursor/).

Wren remembers everything inside one conversation. Chapter 3 ends the
conversation and asks what survives.
