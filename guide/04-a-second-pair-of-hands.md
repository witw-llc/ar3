# Chapter 4 — A Second Pair of Hands

**Teaches R — [r4t](../docs/r4t.md), the roster.**

## 1. Capability

At the end of this chapter the roster is two: Wren leads, and **Moss** — a
zero-cost helper on the same local model — answers his questions. You will
send Wren a task, watch him delegate to Moss with `tell`, and read the whole
exchange hop by hop in the ticker. You will open a second door so you can
reach Moss directly, learn the command that speaks *into* the roster as a
member, then starve Moss's budget and watch a message wait, unharmed, for the
refill.

This chapter also keeps chapter 2's promise: echo comes off Wren here. An
echo member never sees `tell`, and a leader who cannot `tell` cannot
delegate.

## 2. Time

About 20 minutes.

## 3. Starting state

- Chapter 3 complete: Wren on `Continue: 15m`, answering a `tell` from your
  seat, with a `STATUS.md` he refounds from.
- The free path runs both members on one `qwen3.6` — no second model, no
  extra download. (Subscription path: Moss still runs local and free; only
  Wren's rig differs, exactly as in chapters 2–3.)

## 4. The change

Two edits: the runbook grows a member, and the rig config grows a rig.

**Replace** `~/ar3/silo/r4t.md` — the `## Roster` section (leave the
frontmatter, `## Mission` and `## Rituals` as they are)

```markdown
## Roster

### Wren
- **Rig:** silo
- **Leader:** yes
- **Continue:** 15m
- **Workdir:** agents/wren
- **Role:** The solo agent — does the work and answers the owner

Wren is a roster of one: leader, developer, and correspondent in a single
seat. Keep answers short and concrete.

### Moss
- **Rig:** helper
- **Workdir:** agents/moss
- **Role:** Helper — quick lookups and drafts for Wren

Moss answers fast and short: facts, lists, first drafts. No long essays.
```

Moss gets no `Leader:`, no `Continue:` — the roster's own address still enters
at Wren, and a helper that starts cold every turn is fine for quick lookups.
The separate `Workdir:` keeps Moss's conversation and files out of Wren's
directory (two members driving the same CLI in one directory would share one
conversation). Wren's persona line still says "a roster of one" — as of this
chapter that is a lie he tells himself; persona is free prose, edit it
whenever you like.

Notice what Moss does *not* get: a way for you to reach him. The roster has
one door and Wren is standing in it. That is the shape chapter 2 promised —
one address, and who stands behind it is machinery the sender never sees —
and section 6 shows the one command that gets you past it when you need to
debug rather than converse.

Now the rig. Moss is an **echo** member — stdout-only, no messaging doctrine,
the right shape for a small model that answers questions. Wren loses echo in
the same breath, because the leader now has somebody to message:

**Run**

```bash
r4t rig add helper ollama-opencode --model qwen3.6
r4t rig set helper echo true
r4t rig unset silo echo
cd ~/ar3/silo
r4t runbook check
```

You should see:

```
added rig 'helper' (ollama-opencode) to /home/you/.config/r4t/rigs.json
  invoke: ollama launch opencode --model qwen3.6 -- run --auto --dir {workdir} {prompt}
Reference it from your runbook: `- **Rig:** helper`
set helper echo = true in /home/you/.config/r4t/rigs.json
unset silo echo in /home/you/.config/r4t/rigs.json
runbook: triforce -> r4t.md
/home/you/ar3/silo/r4t.md: OK (2 member(s), leader Wren)
```

Two members, still one leader. From this turn on, Wren's prompt carries the
messaging doctrine — the `tell` command, and a member list naming Moss. You
can watch it arrive: the `PROMPT` lines in the ticker gain a `doctrine` field
for Wren that was not there in chapters 2 and 3.

## 5. Run it

Give Wren a task that names Moss:

**Run**

```bash
cd ~/ar3/me
tell silo "Ask Moss for three name ideas for our roster mascot, an octopus. Pick your favorite and tell me."
tells --timeout 300
```

## 6. Expected receipt

You should see:

```
tell -> silo: Ask Moss for three name ideas for our roster mascot, an octopus. Pick your favo…
tells: no message within 300s
```

Not a typo. On the free path this is the *real* result, and it is worth more
than a staged success. The work happened — read the thread's spine in the
ticker:

**Run**

```bash
cd ~/ar3/silo
r4t logs -n 40
```

You should see (turn boundaries between them omitted here):

```
r4t: QUEUED me -> wren thread=01ABC... hop=0 "Ask Moss for three name ideas for our roster mascot, an octopus. Pick your favor" (depth 1)
r4t: QUEUED silo:wren -> moss thread=01ABC... hop=1 "Three name ideas for our roster's octopus mascot — short, punchy, one line each." (depth 1)
r4t: RELEASED-internal silo:wren -> silo:moss thread=01ABC... hop=1
r4t: ECHO-REPLY moss (rig helper) 27 bytes of cleaned stdout staged as the reply to silo:wren
r4t: QUEUED silo:moss -> wren thread=01ABC... hop=2 "1. Inkwell 2. Siph 3. Eight" (depth 1)
r4t: RELEASED-internal silo:moss -> silo:wren thread=01ABC... hop=2
r4t: STDOUT-REPLY wren (rig silo) released nothing; 87 bytes of cleaned stdout staged as a reply to silo:moss
r4t: QUEUED silo:wren -> moss thread=01ABC... hop=3 "I'll go with **Siph**. Short, punchy, and octopus-ready. Tell you know what that" (depth 1)
r4t: RELEASED-internal silo:wren -> silo:moss thread=01ABC... hop=3
r4t: ECHO-REPLY moss (rig helper) 177 bytes of cleaned stdout staged as the reply to silo:wren
r4t: QUEUED silo:moss -> wren thread=01ABC... hop=4 "Siph — the mantle funnel. Octopuses shoot water through it to jet away, and fire" (depth 1)
```

Read it hop by hop. **Hop 1** is the delegation working: Wren ran `tell moss`
himself — a real shell command, composed and executed by a small local model,
and `RELEASED-internal` is r4t noting the envelope never left the roster.
**Hop 2** is Moss's echo: no tools, no doctrine, just three names staged
straight back as the reply. **Hop 3** is the fumble: Wren *decided* ("I'll go
with **Siph**") but printed his answer instead of telling you, so the
`STDOUT-REPLY` fallback staged his stdout as a reply to the newest sender —
which was Moss, not you. Moss helpfully explained what a siphon is (hop 4),
Wren read it, and the thread ended with a turn that staged nothing:

```
turn: 1 message(s) -> Wren (threads 01ABC..., rig silo)
r4t: PROMPT wren continue 4.2k — intro 0.7k mission 0.1k charter 1.4k persona 0.3k history 0.2k messages 0.3k doctrine 1.2k
done: Wren, exit 0 in 29.8s
```

Nothing crashed, nothing looped forever, nobody spent money — but you never
got your answer, because the last mile (Wren messaging *you*) is the one step
that needs the model to follow instructions, and small local models follow
them intermittently. Note the `doctrine 1.2k` on that `PROMPT` line: those are
the messaging instructions Wren gained when you lifted echo, and this is what
they cost every turn whether he obeys them or not.

### Speaking as a member

When a thread goes sideways like that, you want to talk to Moss directly —
and from your seat you cannot, because the roster has one door. `r4t tell --as`
is the way in from the operator's side: it speaks into the roster *as* a
member you name, which is how you jumpstart a stalled roster or reproduce
what one member sends another.

**Run**

```bash
cd ~/ar3/silo
r4t tell --as Wren --to Moss "Three verbs that describe what an octopus does. Just the list."
r4t logs -n 7
```

You should see the send, then the turn it caused:

```
r4t: QUEUED moss from silo:wren thread=01ABC... hop=0 depth=1
— log day 2026-08-17 UTC (this machine reads PDT)
r4t: PROMPT moss echo 3.1k — intro 0.2k charter 1.4k persona 0.2k history 1.1k messages 0.1k
done: Moss, exit 0 in 53.9s
r4t: ECHO-REPLY moss (rig helper) 29 bytes of cleaned stdout staged as the reply to silo:wren
r4t: QUEUED silo:moss -> wren thread=01ABC... hop=1 "1. Jet 2. Camouflage 3. Grasp" (depth 2)
r4t: RELEASED-internal silo:moss -> silo:wren thread=01ABC... hop=1
```

There is Moss's answer — `1. Jet 2. Camouflage 3. Grasp` — sitting in the
`QUEUED` line's preview, on its way to Wren.

The answer is in the ticker, not in your inbox, and that is the rule rather
than a rough edge. A roster lets its **top leader alone** send mail out past
the wall; anything Moss addresses to the outside is redirected to Wren, and
the ticker names it with an `EGRESS-REDIRECT` line when it happens. So
`r4t tell --as` is a diagnosis tool:
it is how you see a member's answer, never how you receive one. When you want
an answer for *yourself*, `tell silo` from your seat, so the reply has
somewhere to go.

(The same rule is why `Ingress:` on a non-leader is rarely what you want. It
opens a second door *inward* — `tell silo:moss` would reach Moss — but the
reply still cannot cross back out, so it lands on Wren. `r4t runbook check`
warns whenever a member other than the leader declares one.)

Now look at the roster as a whole:

**Run**

```bash
cd ~/ar3/silo
r4t status
```

You should see:

```
roster: silo
state: /home/you/.config/r4t/rosters/silo
time: 2026-08-17 00:03 PDT

Rotation  (one turn at a time)
  Now   wren  running 4s of 15m   rig silo   2 msg
  Next  —     nothing ready to run
  Idle        1 member(s) with nothing queued

Health
  ✓ no runaway signs (10 turn(s) last 10m, 1 live now)
  ✓ all 2 member(s) healthy

Roster  (repo settings: /home/you/ar3/silo/r4t.md)
  ✓ Wren  rig=silo  budget=7/8  [leader, turn running, pid 43251]
  ✓ Moss  rig=helper  budget=7.1/8

Rigs  (your configuration: /home/you/.config/r4t/rigs.json)
  ✓ helper      ollama launch opencode --model qwen3.6 -- run --auto --dir {workdir} {prompt}  (timeout=900s budget=8/+4per-h sends=6)
  ✓ silo        ollama launch opencode --model qwen3.6 -- run --auto --dir {workdir} {prompt}  (timeout=900s budget=8/+4per-h sends=6)
    contract    one turn at a time  (cadence 0s)
    governance  cell_budget=16/+8per-h  breaker=5/600s

Activity
    dead letters  0
```

Two members, two spend buckets, every turn above accounted for — and the
`Rotation` block caught Wren mid-turn, with Moss idle behind him. Two members
now, and still exactly one running: that is the contract chapter 2 showed you
with one member, holding with two.

## 7. Break it

Budgets have been sitting in that status line since chapter 2 without ever
biting. Make one bite. Each rig can carry a machine-global spend bucket —
`rig_budget_max` units, refilled at `rig_budget_earn_per_hour` — sized for
the real subscription behind it. Give Moss's rig a bucket of one:

**Run**

```bash
r4t rig set helper rig_budget_max 1
r4t rig set helper rig_budget_earn_per_hour 1
r4t tell --as Wren --to Moss "One fun fact about octopus arms."
r4t tell --as Wren --to Moss "And one about octopus hearts."
```

You should see:

```
set helper rig_budget_max = 1 in /home/you/.config/r4t/rigs.json
set helper rig_budget_earn_per_hour = 1 in /home/you/.config/r4t/rigs.json
r4t: QUEUED moss from silo:wren thread=01ABC... hop=0 depth=1
r4t: QUEUED moss from silo:wren thread=01ABC... hop=0 depth=1
r4t: RESTING moss resting — rig helper exhausted (0), ready in ~59 min (1 queued)
queued — Moss is resting — rig helper exhausted (0), ready in ~59 min
```

The first question ran and spent the bucket's single unit. The second came
back with a receipt instead of a turn.

## 8. Diagnose

The first question ran and spent the bucket's single unit; the second one did
not run at all. Confirm why from the two surfaces that know:

**Run**

```bash
cd ~/ar3/silo
r4t status
r4t logs -n 2
```

You should see (the rotation, the health line, Moss's row, and the ticker):

```
Rotation  (one turn at a time)
  Now   wren  running 20s of 15m   rig silo   1 msg
  Next  —     nothing ready to run
  Held  moss  RESTING — rig helper exhausted (0), ready in ~59 min   1 queued

Health
  ✓ no runaway signs (9 turn(s) last 10m, 1 live now)
  ⚠ Moss resting — rig helper exhausted, 1 queued, ready in ~59 min   (try: raise rig_budget_max/rig_budget_earn_per_hour, or the subscription is out of quota)

Roster  (repo settings: /home/you/ar3/silo/r4t.md)
  ✓ Wren  rig=silo  budget=7/8  [leader, turn running, pid 43251]
  ✓ Moss  rig=helper  budget=8/8  rig=0/1  1 queued  RESTING (rig helper, ready in ~59 min)

Activity
    queued        moss  1 message(s) waiting
```

```
r4t: RESTING moss — resting — rig helper exhausted (0), ready in ~59 min (1 queued)
```

Three surfaces, one story, and a **Held** row on the rotation that did not
exist while everyone was healthy. Read Moss's roster row closely:
`budget=8/8` — his own per-member bucket is untouched and full — but
`rig=0/1`, the machine-global bucket you just shrank, is empty. The gate that
stopped him is not his.

Nothing was refused and nothing was dropped: the message is **queued**,
durably, in Moss's queue on disk. An empty bucket means *resting*, not
*muted* — the queue simply holds until the bucket refills, and then the
turn runs with every queued message in one batch. This is the doctrine the
whole dispatcher is built on: gates govern *when* a member runs, never
*whether* a message survives. Messages never die.

## 9. Fix

You could wait the ~59 minutes and the machinery would fire on its own.
Or lift the ceiling you just installed:

**Run**

```bash
r4t rig unset helper rig_budget_max
r4t rig unset helper rig_budget_earn_per_hour
r4t idle
r4t logs -n 6
```

You should see:

```
unset helper rig_budget_max in /home/you/.config/r4t/rigs.json
unset helper rig_budget_earn_per_hour in /home/you/.config/r4t/rigs.json
drained 0 queued turn(s)
pruned 0 stale lock(s); drained 0 more queued turn(s)
r4t: DEFERRED (one turn at a time: wren is already running) moss (1 queued)
done: Wren, exit 0 in 38.4s
turn: 1 message(s) -> Moss (threads 01ABC..., rig helper)
r4t: PROMPT moss echo 3.5k — intro 0.2k charter 1.4k persona 0.2k history 1.6k messages 0.1k
done: Moss, exit 0 in 38.9s
r4t: ECHO-REPLY moss (rig helper) 198 bytes of cleaned stdout staged as the reply to silo:wren
r4t: QUEUED silo:moss -> wren thread=01ABC... hop=1 "An octopus has three hearts: two pump blood to the gills, and one pumps it to th" (depth 1)
```

`drained 0 queued turn(s)` — and then it ran anyway. Those two facts are not
in conflict, and the `DEFERRED` line between them is why: the budget gate you
just lifted was never the only thing holding Moss. Wren was mid-turn, and a
roster runs **one turn at a time**, so the drain found the rotation busy and
declined to start a second. The moment Wren exited, Moss's held message became
the next turn on its own — no resend, no second command from you. The answer
about three hearts is the message you queued nearly an hour's worth of budget
ago, arriving as if nothing had happened. Because, from the message's point of
view, nothing did.

Two gates, two different questions. A **budget** says whether a member may
spend a turn; the **rotation** says whether anybody may run right now. Both
hold messages, neither drops them.

## 10. Check

Ask the front door where the roster stands now that both members have
worked:

**Run**

```bash
r4t status
```

You should see:

```
Rotation  (one turn at a time)
  Now   wren  running 30s of 15m   rig silo   1 msg
  Next  —     nothing ready to run
  Idle        1 member(s) with nothing queued

Health
  ✓ no runaway signs (9 turn(s) last 10m, 1 live now)
  ✓ all 2 member(s) healthy

Roster  (repo settings: /home/you/ar3/silo/r4t.md)
  ✓ Wren  rig=silo  budget=7/8  [leader, turn running, pid 43251]
  ✓ Moss  rig=helper  budget=7.1/8

Rigs  (your configuration: /home/you/.config/r4t/rigs.json)
  ✓ helper      ollama launch opencode --model qwen3.6 -- run --auto --dir {workdir} {prompt}  (timeout=900s budget=8/+4per-h sends=6)
  ✓ silo        ollama launch opencode --model qwen3.6 -- run --auto --dir {workdir} {prompt}  (timeout=900s budget=8/+4per-h sends=6)
    contract    one turn at a time  (cadence 0s)
    governance  cell_budget=16/+8per-h  breaker=5/600s

Activity
    dead letters  0
```

The `rig-budget=1/+1per-h` you added to the `helper` line in section 7 is
gone, both members are healthy, and nothing is queued or dead-lettered. The
roster is whole — and it is two pairs of hands now.

## 11. Customize

Moss's model is one setting. Any model in `ollama ls` slots in:

**Run**

```bash
r4t rig set helper model qwen3:1.7b
```

You should see:

```
set helper model = qwen3:1.7b in /home/you/.config/r4t/rigs.json
```

A 1.7B Moss answers in a fraction of the time and a fraction of the
quality — try one `r4t tell --as Wren --to Moss` lookup and judge the trade;
set it back the same way. One more knob while you are here: `echo_max_chars` (default
1500) caps an echo member's reply body — anything longer is truncated
with the full text attached to the same envelope as a markdown file.

## 12. Commit point

The runbook is repo state; commit it. (The `helper` rig lives outside the
repo with `silo`, by design.)

**Run**

```bash
cd ~/ar3/silo
git add r4t.md
git commit -q -m "silo roster: Moss joins — helper rig, echo lifted from Wren"
```

Copy-paste templates for this chapter's final state live in
[templates/04-two-member/](templates/04-two-member/).

Chapter 5 gives you something a second pair of hands cannot: a memory that
outlives the conversation.
