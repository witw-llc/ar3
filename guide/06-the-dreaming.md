# Chapter 6 — The Dreaming

**Teaches R — [r4t](../docs/r4t.md), the roster, with
[k7e](../docs/k7e.md) underneath.**

## 1. Capability

At the end of this chapter Wren has a memory of his own. One roster line gives
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
  alone needs no roster)? Don't rebuild by hand: `r4t init` gets you the repo
  and prints the `a8s add`/`namespace`/`start` lines chapter 2 runs, then
  [templates/06-roster-memory/](templates/06-roster-memory/) is the one-shot
  rest — its `ROSTER.md` already carries `Knowledge: on`, `rig-setup.sh` adds
  both rigs, and `seed-store.sh` seeds Wren's store and points it at chapter
  5's bridge (build that first from
  [templates/05-k7e-bridge/](templates/05-k7e-bridge/) if it isn't there
  either). No `STATUS.md` needed — the first turn below just refounds instead
  of continuing.
- Chapter 4 complete: roster `silo` with Wren (leader, `Continue: 15m`) and
  Moss (helper, echo), both answering at the seat.
- Chapter 5 complete: `~/ark/bin/ask`, the stdin→stdout bridge, executable and
  working. This chapter needs it a second time, for a second store.
- Wren has a `STATUS.md` from chapter 3. If his conversation has gone idle
  since you last worked, the first turn below refounds from it — chapter 3's
  machinery, unchanged.

The `Knowledge:` field is experimental and off by default; the budget sizes
here are lab settings, not settled numbers. Nothing changes for a roster that
never names it — with the field absent, a member's prompt is byte-identical to
one built by an r4t that had never heard of knowledge.

## 4. The change

First, establish what Wren does not know. Ask him something an owner would
know and a roster could not derive:

**Run**

```bash
cd ~/ark/silo
r4t seat send --node silo "I want to ship the new ROSTER.md this Friday. Any reason not to?"
r4t seat inbox --node silo
```

You should see:

```
── from silo:wren (2026-07-31T18:10:49.949015Z)
There's only one reason: I don't see the work. `ROSTER.md` hasn't been changed and `agents/` is empty — no diff, no new file to ship.

Send me what you're planning to ship (or where it lives), and I'll get it committed.
```

A good answer to a different question. He read the repo, found nothing to
ship, and asked for the work — he has no way to know that Friday is the
problem, because that fact lives in your head and nowhere on this machine.

Put it somewhere. A knowledge-carrying member's store lives host-side under
the roster's state directory, one store per member, and `K7E_HOME` is how you
reach it with the CLI from chapter 5:

**Run**

```bash
export WREN_STORE=~/.config/r4t/rosters/silo/agents/wren/k7e
K7E_HOME=$WREN_STORE k7e store "Ship window for the silo roster" --tags ops,deploy --content "Ship on Tuesday mornings. Friday ships are forbidden — nobody reads the logs over the weekend, and a bad ROSTER.md takes the whole node down until someone notices on Monday."
K7E_HOME=$WREN_STORE k7e store "Who signs off a roster change" --tags ops,roster --content "The owner signs off every ROSTER.md edit before it ships. Moss drafts, Wren commits, the owner approves."
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

**Replace** `~/ark/silo/ROSTER.md` — in Wren's block, add one field under
`Continue:`

```markdown
- **Knowledge:** on
```

`on` takes the default inject budget of 2 KiB. A size sets it exactly (`4k`,
`4096`); `off` or the absent field is zero and no section is built. Lint it:

**Run**

```bash
r4t roster check
```

You should see:

```
You: note — Human without an Address (roster cannot tell them)
/home/you/ark/silo/ROSTER.md: OK (3 member(s), leader Wren)
```

Nothing new to report — the field is one member's private business, and if
you had turned it on for a member with no store at all, the section would
simply not be built. Knowledge never costs the turn.

## 5. Run it

Ask the same question again:

**Run**

```bash
r4t seat send --node silo "Same question again: Friday for the ROSTER.md ship. Yes or no, and why?"
r4t seat inbox --node silo
```

## 6. Expected receipt

You should see:

```
── from silo:wren (2026-07-31T18:11:22.312901Z)
No. Ship window is Tuesday mornings — Friday ships are forbidden. A bad ROSTER.md over the weekend takes the whole node down until Monday. If it's urgent, we can do a mid-week emergency edit; otherwise push to Tuesday.
```

Nothing in that message told him, nothing in the repo says it, and his own
last words on the subject were that he saw nothing to ship — so his history
did not hold it either. He read it off his own store on the way in. Every turn
is captured whole under `agents/<member>/turns/`, prompt included, so the
words he actually saw are on disk. Take the newest capture:

**Run**

```bash
ls ~/.config/r4t/rosters/silo/agents/wren/turns | tail -1
awk '/^## Knowledge/{f=1} /^## Output/{f=0} f' ~/.config/r4t/rosters/silo/agents/wren/turns/<THAT_FILE>
```

You should see (the capture's filename, then the injected section):

```
20260731T181122311011Z-01ABC....md
```

```
## Knowledge (recalled from your private store)
Notes your past turns distilled — background that may be stale or wrong. When they disagree with the messages above or your own files, the messages and files win.

### Who signs off a roster change (K7E-000-00002, 2026-07-31)

## Verified Protocol

The owner signs off every ROSTER.md edit before it ships. Moss drafts, Wren commits, the owner approves.

### Ship window for the silo roster (K7E-000-00001, 2026-07-31)

## Verified Protocol

Ship on Tuesday mornings. Friday ships are forbidden — nobody reads the logs over the weekend, and a bad ROSTER.md takes the whole node down until someone notices on Monday.
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
r4t logs --node silo --agent wren --full | grep "^- prompt:" | tail -5
```

You should see:

```
- prompt: refound 2573 bytes — preamble 45, intro 739, persona 304, history 118, messages 130, doctrine 1232
- prompt: continue 2623 bytes — intro 739, persona 304, history 198, messages 146, doctrine 1232
- prompt: continue 2613 bytes — intro 739, persona 304, history 198, messages 136, doctrine 1232
- prompt: refound 2588 bytes — preamble 45, intro 739, persona 304, history 118, messages 145, doctrine 1232
- prompt: continue 3318 bytes — intro 739, persona 324, history 198, messages 152, doctrine 1232, knowledge 668
```

Your own numbers will differ — history length tracks how much of chapters
2–4 you actually ran before reaching this one, so the field to look for is
`knowledge`, not the totals around it. Wakes that cost about 2.6 KB, and
then the last one — the wake that knew the answer — carrying one new field,
`knowledge 668`, for 3318 bytes total. That is the entire price of the
capability, per turn, in bytes. The same breakdown
goes to the day log live (`r4t: PROMPT wren continue 3.3k — … knowledge
0.7k`), so a store that quietly bloats into every prompt shows up as a number
instead of an archaeology dig.

## 7. Break it

Injection is half the loop. The other half is supposed to run on its own: when
the node goes idle, each knowledge-carrying member's finished turn captures
are fed to `k7e distill` and whatever they yield lands in that member's store.
Ask for an idle pass and watch it not happen:

**Run**

```bash
r4t idle --node silo
r4t logs --node silo -n 6 | grep DREAM
```

You should see:

```
drained 0 queued turn(s); nudged the leader on 0 quiet thread(s)
pruned 0 stale lock(s); expired 0 thread(s); drained 0 more queued turn(s)
r4t: DREAM-SKIP wren distill exit 1 (k7e distill requires an LLM command. Set llm_command (or a purpose-specific override) — stdin in, stdout out — then retry (see `k7e status`).); 5 capture(s) wait
```

## 8. Diagnose

The idle command itself reported nothing wrong, because nothing *is* wrong
with the roster — the failure is one layer down, in a store that has no way to
think:

**Run**

```bash
K7E_HOME=$WREN_STORE k7e status | grep -E "distill|Home"
ls $WREN_STORE
```

You should see:

```
  Home: /home/you/.config/r4t/rosters/silo/agents/wren/k7e
  LLM distill: unavailable
    • Set llm_command (stdin→stdout CLI) for distill/recall/compile
assets
mocs
nodes
```

Chapter 5's bridge was configured in *your* store, at `~/.config/k7e`
(`K7E_HOME`). This is a different store, with no bridge configured — no
`config.json` on disk yet — never told about a model. Two details in that
log line are the design working:

- **`5 capture(s) wait`** — the captures are not consumed, not marked, not
  lost. A watermark file (`.dreamed`) advances only after a successful pass,
  so a store that cannot dream today dreams the backlog the day it can.
- **`DREAM-SKIP`, not an error** — no turn failed, no message was delayed,
  nobody was told anything. Dreaming is an idle-time luxury and it is built to
  be skippable. (Failed turns are never distilled either: facts pulled out of
  a turn that crashed would be premature.)

## 9. Fix

Give this store the same bridge:

**Run**

```bash
K7E_HOME=$WREN_STORE k7e config llm_command "$HOME/ark/bin/ask"
r4t idle --node silo
r4t logs --node silo -n 40 | grep DREAM
```

(The wider `-n` is deliberate: a successful pass can wake the leader on its
way out, so the dream line is a few events back by the time you look.)

You should see:

```
llm_command = /home/you/ark/bin/ask
drained 0 queued turn(s); nudged the leader on 0 quiet thread(s)
pruned 0 stale lock(s); expired 0 thread(s); drained 0 more queued turn(s)
r4t: DREAM-SKIP wren distill exit 1 (k7e distill requires an LLM command. Set llm_command (or a purpose-specific override) — stdin in, stdout out — then retry (see `k7e status`).); 5 capture(s) wait
r4t: DREAM wren distilled 5 capture(s) into the knowledge store
```

The skip you caused and the pass that replaced it, one after the other.

That pass took over a minute on the free path — five captures, chunked, each
chunk a model call — and it ran entirely outside anyone's turn. That placement
is the point: extraction never happens while a member is answering you, it is
bounded per member per pass (five captures), and it costs the roster nothing
but idle time.

## 10. Check

Look at what he dreamed:

**Run**

```bash
K7E_HOME=$WREN_STORE k7e list
K7E_HOME=$WREN_STORE k7e get <A_NEW_ID> | sed -n '12,14p'
```

You should see:

```
  K7E-000-00001  Ship window for the silo roster  [active]  conf:0.5
  K7E-000-00002  Who signs off a roster change  [active]  conf:0.5
  K7E-000-00003  Solo agent role composition  [active]  conf:0.5
  K7E-000-00004  Repo work completion definition  [active]  conf:0.4
## Verified Protocol

Repository work is considered incomplete until the changes are committed.
```

(Distill count varies per pass — check the `k7e list` output above for your
real IDs, and pick one of the new ones for `<A_NEW_ID>`; titles will differ
too.) The first two entries are the ones you wrote by hand; the rest Wren
distilled out of his own working turns. Judge them plainly: they are thin,
and they are *about* him rather than about the work — a small local model
reading transcripts of itself produces exactly this. That is the knob to
reach for first when dreams disappoint (`k7e config distill_command` on his
store, pointed at a stronger model), and it is why the inject framing calls
these fallible in the first place.

The front door reads a member's store like any other, given the path:

**Run**

```bash
K7E_HOME=$WREN_STORE ar3
r4t status --node silo
```

You should see (`k7e` section, then health and roster):

```
k7e — knowledge engine  (/home/you/.config/r4t/rosters/silo/agents/wren/k7e)
  ✓ cli    k7e -> /home/you/.ar3/k7e
  ✓ store  4 entr(ies) under /home/you/.config/r4t/rosters/silo/agents/wren/k7e/nodes
  ✓ index  56 KiB at /home/you/.config/r4t/rosters/silo/agents/wren/k7e/.index.db
```
```
Health
  ✓ nothing waiting on you
  ✓ no runaway signs (7 turn(s) last 10m)
  ✓ all 2 member(s) healthy

Roster  (repo settings: /home/you/ark/silo/ROSTER.md)
    You   Human  address=(none)   (try: add an **Address:** line so the roster can reach them)
  ✓ Wren  rig=silo  budget=1.5/8  [leader]
  ✓ Moss  rig=helper  budget=8/8
```

A healthy roster with a memory in it, and Wren's spend budget down to 1.5 of 8
after an afternoon of this — the guard rail from chapter 2 doing its quiet
arithmetic while you learned something else.

## 11. Customize

The budget is a real dial, and the cheapest way to feel it is to make it too
small:

**Replace** `~/ark/silo/ROSTER.md` — Wren's `Knowledge:` line

```markdown
- **Knowledge:** 512
```

**Run**

```bash
r4t seat send --node silo "One line: what is the ship rule?"
r4t seat inbox --node silo
r4t logs --node silo --agent wren --full | grep "^- prompt:" | tail -1
```

You should see (or, as often, `(no unread messages)` — a one-line ask is the
sub-80-character shape terminal chrome cleans, the same fumble chapter 4
walks through; nothing failed either way, and the `knowledge N` field on the
prompt line below is the actual check for this section):

```
── from silo:wren (2026-07-31T18:13:43.793861Z)
Ship ROSTER.md edits Tuesday mornings only; Friday ships are forbidden regardless of urgency.

- prompt: continue 3208 bytes — intro 739, persona 325, history 198, messages 113, doctrine 1232, knowledge 596
```

The budget bounds the entry blocks and the header and framing line ride on
top, which is why the log reads 596 for a 512-byte dial. Two entries fit, in
ranked order; the next one would have overflowed and was left out — truncation
is deterministic, never a random half-entry. Set the dial where the trade sits
for your rig: a wide context window and a store worth reading want more, a
small local model already drowning wants less. Put it back to `on` when you
are done.

Two related knobs while you are here. Echo members never get the section at
all, so Moss stays exactly as chapter 4 left him until you lift echo. And
`Reinforce:` still lands after knowledge, which is what keeps a recalled note
from ever having the last word in the prompt.

## 12. Commit point

The roster line is repo state. The store is not:

**Run**

```bash
cd ~/ark/silo
git add ROSTER.md
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

Every piece of it is a file you can open. The roster is markdown, the rigs are
JSON outside the repo, the mailboxes are directories, the knowledge is
markdown with a disposable index over it. Nothing here needs a subscription,
and nothing here phones anyone.

What is not built yet is *shape*: one leader and one helper is a roster, not an
organization. Cells, leads that hide detail from each other, a `MISSION.md`
the roster reviews itself against — that is where the guide goes next.
