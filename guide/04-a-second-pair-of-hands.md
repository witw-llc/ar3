# Chapter 4 — A Second Pair of Hands

**Teaches R — [r4t](../docs/r4t.md), the roster.**

## 1. Capability

At the end of this chapter the roster is two: Wren leads, and **Moss** — a
zero-cost helper on the same local model — answers his questions. You will
send Wren a task, watch him delegate to Moss with `tell`, watch Moss's
answer come back, and read the whole exchange in the roster log. You will
also see where a small free model fumbles the last step — and
why the machinery guarantees the *messages* even when it cannot guarantee
the model. Then you will starve Moss's budget and watch a message wait,
unharmed, for the refill.

This chapter also keeps chapter 2's promise: echo comes off Wren here. An
echo member never sees `tell`, and a leader who cannot `tell` cannot
delegate.

## 2. Time

About 20 minutes.

## 3. Starting state

- Chapter 3 complete: Wren on `Continue: 15m`, answering at the seat, with a
  `STATUS.md` he refounds from.
- The free path runs both members on one `qwen3.6` — no second model, no
  extra download. (Subscription path: Moss still runs local and free; only
  Wren's rig differs, exactly as in chapters 2–3.)

## 4. The change

Two edits: the roster grows a member, and the rig config grows a rig.

**Replace** `~/ark/silo/ROSTER.md` (whole file)

```markdown
# Roster

### You
- **Human:** yes
- **Role:** Owner

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

Moss gets no `Leader:`, no `Continue:` — external mail still enters at Wren,
and a helper that starts cold every turn is fine for quick lookups. The
separate `Workdir:` keeps Moss's conversation and files out of Wren's
directory (two members driving the same CLI in one directory would share
one conversation — `r4t roster check` warns about exactly that). Wren's
persona line still says "a roster of one" — as of this chapter that is a
lie he tells himself; persona is free prose, edit it whenever you like.

Now the rig. Moss is an **echo** member — stdout-only, no messaging
doctrine, the right shape for a small model that answers questions. Wren
loses echo in the same breath, because the leader now has somebody to
message:

**Run**

```bash
r4t rig add helper opencode-ollama --model qwen3.6
r4t rig set helper echo true
r4t rig unset silo echo
cd ~/ark/silo
r4t roster check
```

You should see:

```
added rig 'helper' (opencode-ollama) to /home/you/.config/r4t/rigs.json
  invoke: ollama launch opencode --model qwen3.6 -- run --auto --dir . {prompt}
Reference it from ROSTER.md: `- **Rig:** helper`
set helper echo = true in /home/you/.config/r4t/rigs.json
unset silo echo in /home/you/.config/r4t/rigs.json
You: note — Human without an Address (roster cannot tell them)
/home/you/ark/silo/ROSTER.md: OK (3 member(s), leader Wren)
```

From this turn on, Wren's prompt carries the messaging doctrine — the
`tell` command, and a member list naming You and Moss.

## 5. Run it

Give Wren a task that names Moss:

**Run**

```bash
r4t seat send --node silo "Ask Moss for three name ideas for our roster mascot, an octopus. Pick your favorite and tell me."
r4t seat inbox --node silo
```

## 6. Expected receipt

You should see:

```
(no unread messages)
```

Not a typo. On the free path this is the *real* result, and it is worth
more than a staged success. The work happened — read the thread's spine in
the roster log (`r4t logs --node silo` shows these events; turn boundaries
between them omitted here):

```
r4t: QUEUED silo:you -> wren thread=01ABC... hop=0 "Ask Moss for three name ideas for our roster mascot, an octopus. Pick your favo" (depth 1)
r4t: QUEUED silo:wren -> moss thread=01ABC... hop=1 "I need three name ideas for our roster mascot — an octopus. Keep them punchy, m" (depth 1)
r4t: ECHO-REPLY moss (rig helper) 232 bytes of cleaned stdout staged as the reply to silo:wren
r4t: QUEUED silo:moss -> wren thread=01ABC... hop=2 "1. **Inkwell** — evokes the octopus "pen" while sounding sharp and professional " (depth 1)
r4t: STDOUT-REPLY wren (rig silo) released nothing; 156 bytes of cleaned stdout staged as a reply to silo:moss
r4t: QUEUED silo:wren -> moss thread=01ABC... hop=3 "**Inkwell** is the pick. Sharp, professional, and the octopus pun lands without " (depth 1)
r4t: ECHO-REPLY moss (rig helper) 22 bytes of cleaned stdout staged as the reply to silo:wren
r4t: QUEUED silo:moss -> wren thread=01ABC... hop=4 "Noted. Inkwell it is 🐙" (depth 1)
```

Read it hop by hop. **Hop 1** is the delegation working: Wren ran
`tell moss` himself — a real shell command, composed and executed by a
small local model. **Hop 2** is Moss's echo: no tools, no doctrine, just
three names staged straight back as the reply. **Hop 3** is the fumble:
Wren *decided* ("Inkwell is the pick") but printed his answer instead of
telling you, so the `STDOUT-REPLY` fallback staged his stdout as a reply
to the newest sender — which was Moss, not you. Moss politely
acknowledged (hop 4), the two drifted into housekeeping for a few more
hops, and r4t ended the loop the boring way:

```
r4t: SILENT wren (rig silo) exit 0 with 85 bytes of stdout but nothing worth relaying survived transcript cleaning
```

Nothing crashed, nothing looped forever, nobody spent money — but you
never got your answer, because the last mile (Wren messaging *you*) is the
one step that needs the model to follow instructions, and small local
models follow them intermittently. Watch it happen in miniature — ask
again and demand the `tell`:

**Run**

```bash
r4t seat send --node silo "Which mascot name did you pick? Send me the answer with the tell command."
r4t seat inbox --node silo
r4t logs --node silo --full -n 8
```

You should see:

```
(no unread messages)
### Output (Wren, exit 0 in 13.2s)

[0m
> build · qwen3.6:latest
[0m
tell you "Inkwell 🐙"
```

Wren *printed* `tell you "Inkwell 🐙"` as text — the exact mistake the
doctrine warns about ("printing it as text sends nothing"). Twelve
characters of perfect answer, discarded as terminal chrome by the sub-80
threshold you met in chapter 2. This is why chapter 2 shipped Wren with
echo on, and it is the cost of lifting it.

Now the two ways you actually get answers out of this roster. First: the
stdout fallback *does* reach you whenever Wren's answer has substance,
because a fresh question from you makes you the newest sender:

**Run**

```bash
r4t seat send --node silo "Report in two or three full sentences: what did you ask Moss, what did Moss offer, and which mascot name won?"
r4t seat inbox --node silo
```

You should see:

```
── from silo:wren (2026-07-29T05:47:12.961106Z)
I asked Moss for three octopus name ideas. Moss offered Inkwell, Blotz, and Eight. Inkwell won — sharp, professional, with a subtle ink pun that lands cleanly.
```

There is the delegated round trip, synthesized and delivered — Wren never
ran `tell`, and the machinery covered for him: the log shows
`r4t: STDOUT-REPLY wren (rig silo) released nothing; 157 bytes of cleaned
stdout staged as a reply to silo:you`. Second: the direct route. The seat
can address any member, so for quick lookups skip the middleman:

**Run**

```bash
r4t seat send --node silo --to moss "Three verbs that describe what an octopus does. Just the list."
r4t seat inbox --node silo
```

You should see:

```
── from silo:moss (2026-07-29T05:47:58.265173Z)
1. Jet
2. Camouflage
3. Grasp
```

(On the subscription path Wren runs `tell` reliably and the first send
comes back synthesized on hop 3 — the machinery is identical, the model
discipline is what you are paying for.) Now look at the roster as a whole:

**Run**

```bash
r4t status --node silo
```

You should see (roster section):

```
Roster  (repo settings: /home/you/ark/silo/ROSTER.md)
    You  Human  address=(none)   (try: add an **Address:** line so the roster can reach them)
  ✓ Wren  rig=silo  budget=5.2/8  [leader]
  ✓ Moss  rig=helper  budget=7/8
```

Two members, two spend buckets, every turn above accounted for.

## 7. Break it

Budgets have been sitting in that status line since chapter 2 without ever
biting. Make one bite. Each rig can carry a machine-global spend bucket —
`rig_budget_max` units, refilled at `rig_budget_earn_per_hour` — sized for
the real subscription behind it. Give Moss's rig a bucket of one:

**Run**

```bash
r4t rig set helper rig_budget_max 1
r4t rig set helper rig_budget_earn_per_hour 1
r4t seat send --node silo --to moss "One fun fact about octopus arms."
r4t seat inbox --node silo
r4t seat send --node silo --to moss "And one about octopus hearts."
```

You should see:

```
set helper rig_budget_max = 1 in /home/you/.config/r4t/rigs.json
set helper rig_budget_earn_per_hour = 1 in /home/you/.config/r4t/rigs.json
── from silo:moss (2026-07-29T05:48:56.279303Z)
Each of an octopus's eight arms can taste, touch, and move independently—thanks to most of its neurons residing in the arms rather than the brain.

queued — Moss is resting — rig helper exhausted (0), ready in ~59 min
```

The first question ran — and spent the bucket's single unit. The second
came back with a receipt instead of an answer.

## 8. Diagnose

Read the receipt, then confirm it from the other two surfaces:

**Run**

```bash
r4t status --node silo
r4t logs --node silo -n 1
```

You should see (roster section, and the log line):

```
  ✓ Moss  rig=helper  budget=6.1/8  rig=0/1  1 queued  RESTING (rig helper, ready in ~59 min)
r4t: RESTING moss — resting — rig helper exhausted (0), ready in ~59 min (1 queued)
```

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
r4t idle --node silo
r4t seat inbox --node silo
```

You should see:

```
unset helper rig_budget_max in /home/you/.config/r4t/rigs.json
unset helper rig_budget_earn_per_hour in /home/you/.config/r4t/rigs.json
drained 1 queued turn(s); nudged the leader on 0 quiet thread(s)
pruned 0 stale lock(s); expired 0 thread(s); drained 0 more queued turn(s)
── from silo:moss (2026-07-29T05:50:20.969451Z)
An octopus has three hearts. Two pump blood to the gills, one to the body. The main heart stops when swimming, which is why they prefer crawling.
```

`drained 1 queued turn(s)` — the held message fired the moment the gate
opened, and the answer landed as if nothing had happened. Because, from
the message's point of view, nothing did.

## 10. Check

One full round trip through the leader, end to end:

**Run**

```bash
r4t seat send --node silo "In two or three full sentences: what has Moss contributed to this roster so far?"
r4t seat inbox --node silo
```

You should see:

```
── from silo:wren (2026-07-29T05:56:13.232267Z)
Two things, both concrete: Moss drafted three octopus name options for our mascot (we landed on Inkwell), then committed ROSTER.md with Moss added to the roster at `0a80bcf`. That's it — two small, done items.
```

Wren knows what his helper has done. (That commit hash is real: the
"housekeeping drift" in section 6 was the pair committing the roster on
their own initiative — `git log` in the roster repo will show it.) The roster
is whole — and it is two pairs of hands now.

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
quality — try one `--to moss` lookup and judge the trade; set it back the
same way. One more knob while you are here: `echo_max_chars` (default
1500) caps an echo member's reply body — anything longer is truncated
with the full text attached to the same envelope as a markdown file.

## 12. Commit point

The roster is repo state; commit it. (The `helper` rig lives outside the
repo with `silo`, by design.)

**Run**

```bash
cd ~/ark/silo
git add ROSTER.md
git commit -q -m "silo roster: Moss joins — helper rig, echo lifted from Wren"
```

Copy-paste templates for this chapter's final state live in
[templates/04-two-member/](templates/04-two-member/).
