# Chapter 6 — The Dreaming

**Teaches R — [r4t](../docs/r4t.md), the roster, with
[k7e](../docs/k7e.md) underneath.**

## 1. Capability

At the end of this chapter Wren has a memory of his own. One runbook line gives
him a private knowledge store, and two things happen around it without you
doing anything again: every turn he wakes with a `## Knowledge` section built
from that store, and every idle pass distills his finished turns back into it.
You will watch him fail a question, put the answer in his store without
telling him, watch him answer it on the next turn — then read the
byte-by-byte accounting of what that cost his prompt, break the dreaming, and
fix it.

This is the last piece of memory the guide raises. After it, the roster is
governed, it survives its own conversations, it delegates, and it remembers.

## 2. Time

About 20 minutes, most of it waiting on turns.

## 3. Starting state

- No live `silo` roster because chapter 5's escape hatch skipped 02–04 (k7e
  alone needs no roster)? Don't rebuild by hand: `r4t init` writes the runbook
  and prints the `r4t add` line chapter 2 runs, then
  [templates/06-roster-memory/](templates/06-roster-memory/) is the one-shot
  rest — its `r4t.md` already carries `Knowledge: on`, `rig-setup.sh` adds
  both rigs, and `seed-store.sh` seeds Wren's store. No `STATUS.md` needed —
  the first turn below just refounds instead of continuing.
- Chapter 4 complete: roster `silo` with Wren (leader, `Continue: 15m`) and
  Moss (helper, echo), both answering.
- Chapter 5 is useful background and not a prerequisite: this chapter drives
  k7e through r4t rather than by hand, so the `~/ark/bin/ask` bridge you built
  there is not needed here. A member's store distills through the member's own
  rig.

The `Knowledge:` field is experimental and off by default; the budget sizes
here are lab settings, not settled numbers. Nothing changes for a roster that
never names it — with the field absent, a member's prompt is byte-identical to
one built by an r4t that had never heard of knowledge.

## 4. The change

First, establish what Wren does not know. Ask him something an owner would
know and a roster could not derive:

**Run**

```bash
cd ~/ark/me
tell silo "I want to ship the new r4t.md this Friday. Any reason not to?"
tells --timeout 300
```

Wren's answer may land in your inbox, and on the free path it often will not —
that is chapter 4's last mile again, and echo has been off Wren since then.
It makes no difference here, because this chapter reads everything off disk
anyway. Every turn is captured whole under the roster's state directory,
prompt and output together, and the newest file is the turn you just caused:

**Run**

```bash
export TURNS=~/.config/r4t/rosters/silo/agents/wren/turns
sed -n '/^## Output/,$p' "$(ls -d $TURNS/* | tail -1)"
```

You should see:

```
## Output

[0m
> build · qwen3.6:latest
[0m
No reason from my end — Friday works. Want to review the diff before it ships?
```

(The `[0m` lines are the harness's own terminal colour codes, captured raw.)
Wren has no objection, because he has no way to know that Friday is the
problem: that fact lives in your head and nowhere on this machine.

Put it somewhere. A knowledge-carrying member's store lives host-side under
the roster's state directory, one store per member, and `K7E_HOME` is how you
reach it with the CLI from chapter 5:

**Run**

```bash
export WREN_STORE=~/.config/r4t/rosters/silo/agents/wren/k7e
K7E_HOME=$WREN_STORE k7e store "Ship window for the silo roster" --tags ops,deploy --content "Ship on Tuesday mornings. Friday ships are forbidden — nobody reads the logs over the weekend, and a bad r4t.md takes the whole node down until someone notices on Monday."
K7E_HOME=$WREN_STORE k7e store "Who signs off a roster change" --tags ops,roster --content "The owner signs off every r4t.md edit before it ships. Moss drafts, Wren commits, the owner approves."
K7E_HOME=$WREN_STORE k7e list
```

You should see:

```
Stored K7E-000-00001: Ship window for the silo roster
Stored K7E-000-00002: Who signs off a roster change
  K7E-000-00001  Ship window for the silo roster  [active]  conf:0.5
  K7E-000-00002  Who signs off a roster change  [active]  conf:0.5
```

Same k7e, same entry format, same `nodes/` directory as chapter 5 — a
different folder. That folder boundary is the security model: separate members
get physically separate stores, because tags organize *within* a store and
enforce nothing between them. Moss cannot see this. Nothing merges these into
a roster-wide pool, and the shared tier of memory stays what it always was —
the repo both of them read.

Now the line that turns it on:

**Replace** `~/ark/silo/r4t.md` — in Wren's block, add one field under
`Continue:`

```markdown
- **Knowledge:** on
```

`on` takes the default inject budget of 2 KiB. A size sets it exactly (`4k`,
`4096`); `off` or the absent field is zero and no section is built. Lint it:

**Run**

```bash
cd ~/ark/silo
r4t runbook check
```

You should see:

```
runbook: triforce -> r4t.md
warning: Wren: Knowledge is on with rig 'silo' — a small-model class that smooths specifics out of distilled notes; consider a distill-rig override, and note budgets are bytes, not tokens (see docs/r4t-knowledge.md)
/home/you/ark/silo/r4t.md: OK (2 member(s), leader Wren, 1 warning(s))
```

The warning is r4t telling you the truth about the free path before you find
it out yourself: a small local model reading its own transcripts writes
vague notes. Injection is unaffected — what you *write* by hand lands whole,
which is the half this section proves. Section 10 is where the warning earns
itself.

## 5. Run it

Ask the same question again:

**Run**

```bash
cd ~/ark/me
tell silo "Same question again: Friday for the r4t.md ship. Yes or no, and why?"
tells --timeout 300
sed -n '/^## Output/,$p' "$(ls -d $TURNS/* | tail -1)"
```

## 6. Expected receipt

You should see:

```
## Output

[0m
> build · qwen3.6:latest
[0m
No. Friday ship is forbidden — nobody reads logs over the weekend, and a broken r4t.md takes the whole node down until someone notices Monday. Tuesday morning is the window.
```

Nothing in that message told him, nothing in the repo says it, and his own
last words on the subject were that Friday was fine — so his history did not
hold it either. He read it off his own store on the way in, and the prompt he
read is in the same capture. Take the newest one:

**Run**

```bash
ls $TURNS | tail -1
awk '/^## Knowledge/{f=1} /^## Output/{f=0} f' "$(ls -d $TURNS/* | tail -1)"
```

You should see (the capture's filename, then the injected section):

```
20260817T070907821013Z-01ABC....md
```

```
## Knowledge (recalled from your private store)
Notes your past turns distilled — background that may be stale or wrong. When they disagree with the messages above or your own files, the messages and files win.

### Who signs off a roster change (K7E-000-00002, today)

## Verified Protocol

The owner signs off every r4t.md edit before it ships. Moss drafts, Wren commits, the owner approves.

### Ship window for the silo roster (K7E-000-00001, today)

## Verified Protocol

Ship on Tuesday mornings. Friday ships are forbidden — nobody reads the logs over the weekend, and a bad r4t.md takes the whole node down until someone notices on Monday.
```

Read the framing line, because it is the whole posture: *background that may
be stale or wrong … the messages and files win*. Chapter 5 showed you why —
that store will eventually hold something a model over-reached on, and an
agent that treats recalled notes as orders acts on it. Notes arrive with
provenance (id, date) and a ranking, placed after the how-to-work doctrine and
before the closing reinforcement, so nothing recalled gets the last word.

The retrieval seed is assembled for him, not by him: the newest message, his
name and role, and the mission's first line. He never sees that query and
cannot steer it. Reading an entry for a prompt bumps its usage counter, so
`k7e stats` on his store shows which notes are earning their place.

None of this is free, and r4t prices it per wake. Every capture carries a
`- prompt:` meta line, so Wren's whole history of wakes reads as a bill:

**Run**

```bash
cd ~/ark/silo
r4t logs --agent wren --full | grep "^- prompt:" | tail -5
```

You should see:

```
- prompt: continue 4175 bytes — intro 747, mission 113, charter 1402, persona 304, history 198, messages 211, doctrine 1194
- prompt: continue 4211 bytes — intro 747, mission 113, charter 1402, persona 304, history 198, messages 247, doctrine 1194
- prompt: continue 4246 bytes — intro 747, mission 113, charter 1402, persona 304, history 198, messages 282, doctrine 1194
- prompt: continue 4105 bytes — intro 747, mission 113, charter 1402, persona 304, history 198, messages 141, doctrine 1194
- prompt: continue 4785 bytes — intro 747, mission 113, charter 1402, persona 324, history 198, messages 148, doctrine 1194, knowledge 652
```

Four wakes costing about 4.2 KB each, and then the last one — the wake that
knew the answer — carrying one new field, `knowledge 652`, for 4785 bytes
total.

Your own numbers will differ — history length tracks how much of chapters
2–4 you actually ran before reaching this one, so the field to look for is
`knowledge`, not the totals around it. The wake that knew the answer carries
one field the others do not, and that is the entire price of the capability,
per turn, in bytes. The same breakdown goes to the ticker live
(`r4t: PROMPT wren continue 3.3k — … knowledge 0.7k`), so a store that quietly
bloats into every prompt shows up as a number instead of an archaeology dig.

## 7. Break it

Injection is half the loop. The other half runs on its own: when the node goes
idle, each knowledge-carrying member's finished turn captures are fed to
`k7e distill`, and whatever they yield lands in that member's store. There is
nothing to configure for it. Chapter 5's store needed a bridge because you
were driving k7e by hand; a *member's* store borrows the member's own rig,
which r4t already knows how to run — that is what the lint warning in section
4 was about.

Which means the way to break it is to point the dreaming somewhere else.
`Knowledge:` takes a rig name after the size — the knob you reach for when the
turn rig is too small to write good notes, exactly as that warning suggests.
Name a rig you never created:

**Replace** `~/ark/silo/r4t.md` — Wren's `Knowledge:` line

```markdown
- **Knowledge:** medium scribe
```

**Run**

```bash
r4t runbook check
r4t idle
r4t logs -n 10 | grep DREAM
```

You should see:

```
runbook: triforce -> r4t.md
Wren: Knowledge distill rig 'scribe' not found in /home/you/.config/r4t/rigs.json
1 problem(s)
```

```
drained 0 queued turn(s)
pruned 0 stale lock(s); drained 0 more queued turn(s)
r4t: DREAM-SKIP wren Knowledge distill rig 'scribe' not found in /home/you/.config/r4t/rigs.json; 1 capture(s) wait
```

## 8. Diagnose

The lint caught it before the idle pass did, which is the order you want: a
name that resolves to nothing is a member error, not a soft skip, and
`r4t runbook check` refuses the runbook rather than letting a member dream
into the void. The idle pass then says the same thing in the ticker. Two
details there are the design working:

- **`1 capture(s) wait`** — the captures are not consumed, not marked, not
  lost. A watermark file (`.dreamed`) advances only after a successful pass,
  so a store that cannot dream today dreams the backlog the day it can. (Your
  count will be higher if you have run more turns than this walkthrough; a
  pass distills at most five captures at a time.)
- **`DREAM-SKIP`, not an error** — no turn failed, no message was delayed,
  nobody was told anything. Dreaming is an idle-time luxury and it is built to
  be skippable. (Failed turns are never distilled either: facts pulled out of
  a turn that crashed would be premature.)

Note also what the idle pass printed on its own two lines: `drained 0 queued
turn(s)`. Nothing about the roster is unwell. The failure is one layer down,
in a member's private machinery, and it stayed there.

## 9. Fix

Take the override off and let the dreaming fall back to Wren's own rig:

**Replace** `~/ark/silo/r4t.md` — Wren's `Knowledge:` line

```markdown
- **Knowledge:** on
```

**Run**

```bash
r4t runbook check
r4t idle
r4t logs -n 40 | grep DREAM
```

(The wider `-n` is deliberate: a successful pass can wake the leader on its
way out, so the dream line is a few events back by the time you look.)

You should see:

```
r4t: DREAM wren distilled 1 capture(s) into the knowledge store
r4t: DREAM-EMBED wren embedded 4 entries in 0.2s (54ms each)
```

The skip you caused and the pass that replaced it, and behind the second line
a job you never asked for: `DREAM-EMBED` keeps the store's semantic index over
whatever it now holds, which is why recall gets better as the store grows
without you reindexing anything.

That pass took a while on the free path — every capture is chunked, and each
chunk is a model call — and it ran entirely outside anyone's turn. That
placement is the point: extraction never happens while a member is answering
you, it is bounded per member per pass, and it costs the roster nothing but
idle time.

## 10. Check

Look at what he dreamed:

**Run**

```bash
K7E_HOME=$WREN_STORE k7e list
```

You should see something like:

```
  K7E-000-00002  Who signs off a roster change  [active]  conf:0.5
  K7E-000-00004  Silo charter branch rule  [active]  conf:0.7
  K7E-000-00005  Owner's question to Wren  [active]  conf:0.5
  K7E-000-00007  Empty workdir observation  [active]  conf:0.5
  K7E-000-00008  Thread queue status at review time  [active]  conf:0.5
  K7E-000-00009  Next-step delegation directive  [active]  conf:0.5
  K7E-000-00011  turn exit and timing  [active]  conf:0.5
  K7E-000-00012  Wren roster config  [active]  conf:0.5
```

(Titles and ids will differ — a distill pass writes whatever it found.) The
two you wrote by hand are in there; the rest Wren distilled
out of his own working turns. Judge them plainly: they are thin, and they are
*about* him rather than about the work — a small local model reading
transcripts of itself produces exactly this, and it is what section 4's lint
warning was predicting. The knob to reach for first is the one you broke on
purpose in section 7: add a rig name to the `Knowledge:` line
(`- **Knowledge:** medium scribe`, with a real `scribe` rig on a stronger
model) and the dreaming runs there while the turns stay cheap. It is also why
the inject framing calls these notes fallible in the first place.

The front door reads a member's store like any other, given the path:

**Run**

```bash
K7E_HOME=$WREN_STORE ar3
r4t status
```

You should see (the `k7e` panel, then the roster):

```
k7e — knowledge engine  (/home/you/.config/r4t/rosters/silo/agents/wren/k7e)
  ✓ cli    k7e -> /home/you/.ar3/k7e
  ✓ store  20 entr(ies) under /home/you/.config/r4t/rosters/silo/agents/wren/k7e/nodes
  ✓ index  156 KiB at /home/you/.config/r4t/rosters/silo/agents/wren/k7e/.index.db
```

```
Rotation  (one turn at a time)
  Now   —  idle 23s   (last: wren, exit 0)
  Next  —  nothing ready to run
  Idle     2 member(s) with nothing queued

Health
  ✓ no runaway signs (2 turn(s) last 10m)
  ✓ all 2 member(s) healthy

Roster  (repo settings: /home/you/ark/silo/r4t.md)
  ✓ Wren  rig=silo  budget=6.6/8  [leader]
  ✓ Moss  rig=helper  budget=8/8
```

The same front door that has been reading your own store since chapter 5 reads
a member's, given the path — nothing about Wren's memory is a special format.
A healthy roster with a memory in it, and Wren's spend budget doing its quiet
arithmetic while you learned something else.

## 11. Customize

The budget is a real dial, and the cheapest way to feel it is to make it too
small:

**Replace** `~/ark/silo/r4t.md` — Wren's `Knowledge:` line

```markdown
- **Knowledge:** 512
```

**Run**

```bash
cd ~/ark/me
tell silo "One line: what is the ship rule?"
tells --timeout 300
cd ~/ark/silo
r4t logs --agent wren --full | grep "^- prompt:" | tail -1
```

You should see the answer, and then the bill for it:

```
## Output

[0m
> build · qwen3.6:latest
[0m
Ship windows are Tuesday morning only — never Friday, because nobody reads logs over the weekend and a broken r4t.md takes the node down until Monday.
```

```
- prompt: continue 4587 bytes — intro 747, mission 113, charter 1402, persona 325, history 198, messages 127, doctrine 1194, knowledge 474
```

`knowledge 474` against a 512-byte dial: the budget bounds whole entry blocks,
so what fits is the highest-ranked entries that still come in under it, and
the leftover is simply not spent. Truncation is deterministic and never a
random half-entry — the next entry would have overflowed, so it was left out
whole. Wren still answered correctly, because the one note that mattered was
the one that ranked first. Set the dial where the trade sits for your rig: a
wide context window and a store worth reading want more, a small local model
already drowning wants less. Put it back to `on` when you are done.

Two related knobs while you are here. Echo members never get the section at
all, so Moss stays exactly as chapter 4 left him until you lift echo. And
`Reinforce:` still lands after knowledge, which is what keeps a recalled note
from ever having the last word in the prompt.

## 12. Commit point

The runbook line is repo state. The store is not:

**Run**

```bash
cd ~/ark/silo
git add r4t.md
git commit -q -m "silo roster: Wren remembers — Knowledge on"
```

Wren's store lives under `~/.config/r4t/` (`R4T_HOME`), outside the repo,
host-side — it never crosses into an isolated turn, and cloning this repo
onto another machine brings the roster without bringing anyone's memory. If
a member's accumulated knowledge is worth keeping, back it up where it
lives, with chapter 5's trick:

**Run**

```bash
cd $WREN_STORE
git init -q
printf '.index.db\n' > .gitignore
git add -A
git commit -q -m "wren: what he knows so far"
```

Copy-paste templates for this chapter's final state live in
[templates/06-roster-memory/](templates/06-roster-memory/).

## Where you stand

Six chapters ago you had a shell prompt. You now have a roster on your own
hardware that is governed, durable, and remembering: messages that survive a
dead handler, budgets that queue instead of overspend, conversations that can
be retired and refounded from disk, a second member to delegate to, and now a
memory per member that fills itself from work already done and rides into
every wake at a price you can read in the log.

Every piece of it is a file you can open. The roster is one markdown file, the
rigs are JSON outside the repo, the mailboxes are directories, the knowledge is
markdown with a disposable index over it. Nothing here needs a subscription,
and nothing here phones anyone.

What is not built yet is *shape*: one leader and one helper is a roster, not an
organization. Cells, leads that hide detail from each other, and the rituals
you left empty in chapter 2 — that is where the guide goes next.
