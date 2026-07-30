# Chapter 3 — The Long Memory

**Teaches R — [r4t](../apps/r4t/README.md), the roster.**

## 1. Capability

At the end of this chapter Wren survives conversation death. You will end
his conversation on purpose — Wren is prompted once to write his state to
`STATUS.md`, the conversation is retired, and the next real message
**refounds** him from that file. Then you will do something harsher: swap
Wren's rig to a different CLI mid-life and watch him come back coherent on
the other side. The conversation is disposable; the agent is not.

## 2. Time

About 20 minutes.

## 3. Starting state

- Chapter 2 complete: roster `silo` registered on a8s, Wren answering at the
  seat, and the customize step left `Continue: 15m` in his roster block.
- Wren's conversation is live — if you just restarted the machine, send one
  seat message so there is a conversation to flush.

## 4. The change

`Continue: 15m` is a promise you made in chapter 2 without seeing it kept: a
conversation idle past fifteen minutes is retired on the node's next idle
pass. Fifteen minutes is the right setting and the wrong demo — you are not
going to sit here watching a clock. `r4t flush` is the same cycle on your
word:

```bash
r4t flush --node silo wren
```

It runs the dump turn, retires the conversation, and archives Wren's
message history — the transcript r4t keeps for him — so a refounded Wren
has exactly one place left to remember from. That last part is what makes
the rest of this chapter a test rather than a demonstration.

**Run**

```bash
cd ~/ark/silo
r4t roster check
```

You should see:

```
You: note — Human without an Address (team cannot tell them)
/home/you/ark/silo/ROSTER.md: OK (2 member(s), leader Wren)
```

The idle window rides the same field that turns the conversation on, so a
member who runs cold can never carry one.

## 5. Run it

Give Wren a fact to hold, and prove he has it:

**Run**

```bash
r4t seat send --node silo "Hold this fact for later: the supply cache is at grid square K-19. Confirm."
r4t seat inbox --node silo
```

You should see:

```
── from silo:wren (2026-07-29T05:19:47.051352Z)
Supply cache at grid square K-19 recorded. Confirming storage complete.
```

Now end the conversation:

**Run**

```bash
r4t flush --node silo wren
```

You should see:

```
flushed wren — dumped state to disk, retired the conversation, archived history as history-20260729T052013449271Z.md
```

One line per member. The full receipt lives in the roster log and on disk,
which is where the next section takes you.

## 6. Expected receipt

**Run**

```bash
r4t logs --node silo -n 6
```

You should see:

```
r4t: FLUSH dump turn -> wren (r4t flush)
turn: 1 message(s) -> Wren (threads 01ABC..., rig silo)
done: Wren, exit 0 in 28.8s
r4t: SILENT wren (rig silo) answered only r4t-internal senders; its 674 bytes of stdout stay transcript, nothing staged
r4t: FLUSH retired wren's conversation — the next real message refounds it from state on disk
r4t: FLUSH archived wren's history log as history-20260729T052013449271Z.md — a fresh one starts at the next turn
```

Three `FLUSH` events around a real turn. The **dump turn** is an ordinary
continuing turn whose prompt asks one thing: "Save your current state and
progress to STATUS.md." It is logged, captured, and budgeted like any
other turn, and only if it exits cleanly does the retirement — and the
archiving — follow. The `SILENT` line is that turn answering r4t rather than a
member: no one asked, so whatever Wren said on his way to disk stays in the
turn capture instead of landing in someone's inbox. Look at what Wren wrote:

**Run**

```bash
cat STATUS.md
```

You should see:

```
# STATUS.md — Wren

## Codeword
TIDEPOOL (confirmed)

## Supplied Facts
- Supply cache location: grid square K-19

## Current State
Active. No open tasks. Waiting for instructions.

## Progress
Session initialized. Stored codeword and supply cache location.
```

Wren chose that structure himself — the dump prompt names the file, not the
format. (Where the file lands depends on the harness: OpenCode anchors
relative paths at the repo root, so on the free path it appears at
`~/ark/silo/STATUS.md`; a harness that stays in Wren's `Workdir:` writes
`agents/wren/STATUS.md` instead. Either way it is inside the roster repo,
which matters at the commit point.)

The conversation is retired and the message history is out of the prompt
path. Ask for the fact back:

**Run**

```bash
r4t seat send --node silo "What was the codeword, and where is the supply cache?"
r4t seat inbox --node silo
```

You should see:

```
── from silo:wren (2026-07-29T05:23:14.719675Z)
The codeword was **TIDEPOOL** and the supply cache is located at **grid square K-19**.
```

That turn was a **refound**: no continue flag on the CLI (there is no
conversation left to continue), and the prompt opens with a preamble —
"Check your STATUS.md to refresh your memory." A fresh conversation,
founded from saved state, holding facts learned in a conversation that no
longer exists. There was one place that answer could have come from: the
file you just read.

### The rig swap

Flush is death you schedule or ask for. A rig swap is sudden death: the
conversation is keyed on the CLI that holds it, so a rig that now drives a
*different* CLI cannot resume it — and r4t does not try, because the old CLI
may be quota-dead and unable to run a dump turn. State on disk is whatever
the last flush left. Swap Wren to the other path's harness (this beat needs a
second continue-capable CLI installed — `agent` here; free-path readers
with only ollama can read along, the machinery is identical):

**Run**

```bash
r4t rig swap silo cursor --model claude-fable-5-medium
r4t seat send --node silo "New rig, same Wren? Tell me the codeword and the cache location."
r4t seat inbox --node silo
```

You should see:

```
swapped rig 'silo' to cursor in /home/you/.config/r4t/rigs.json
  invoke: agent --model claude-fable-5-medium -p --trust --force --approve-mcps {prompt}
── from silo:wren (2026-07-29T05:26:27.150992Z)
Same Wren. Codeword: **TIDEPOOL**. Supply cache: **grid square K-19**. Both verified against STATUS.md.
```

And the log names what happened:

**Run**

```bash
r4t logs --node silo -n 8
```

You should see:

```
r4t: QUEUED silo:you -> wren thread=01ABC... hop=0 "New rig, same Wren? Tell me the codeword and the cache location." (depth 1)
r4t: CONTINUE-SWAP wren (rig silo) drives 'agent' but the conversation lives on 'opencode' — retired; this turn refounds from state on disk
turn: 1 message(s) -> Wren (threads 01ABC..., rig silo)
done: Wren, exit 0 in 12.4s
r4t: ECHO-REPLY wren (rig silo) 102 bytes of cleaned stdout staged as the reply to silo:you
r4t: SEAT you <- silo:wren (parked)
r4t: RELEASED-internal silo:wren -> silo:you thread=01ABC... hop=1
r4t: ANSWERED thread=01ABC... silo:wren -> silo:you (originator answered, thread closed)
```

`CONTINUE-SWAP`: the conversation lived on `opencode`, the rig now drives
`agent`, so the old conversation is retired and this turn refounds — same
mechanics as after a flush, minus the dump. Wren's identity crossed a CLI
boundary on the strength of a markdown file. Swap back:

**Run**

```bash
r4t rig swap silo opencode-ollama --model qwen3.6
r4t seat send --node silo "Back on the local rig. Codeword and cache location, one line."
r4t seat inbox --node silo
```

You should see:

```
swapped rig 'silo' to opencode-ollama in /home/you/.config/r4t/rigs.json
  invoke: ollama launch opencode --model qwen3.6 -- run --auto --dir . {prompt}
── from silo:wren (2026-07-29T05:27:48.329830Z)
Codeword: TIDEPOOL — Cache: grid square K-19
```

The log shows the same `CONTINUE-SWAP` event with the CLIs reversed. One
subtlety the event teaches: a swap that keeps the CLI (a model change, or
`opencode` ↔ `opencode-ollama`, which both drive `opencode`) keeps the
conversation — only a real CLI change retires it.

(If a send comes back with `queued — Wren is resting (member budget ...)`,
you have simply outrun Wren's spend budget with all this demo traffic. The
message is queued, not lost; wait the minutes it names and run
`r4t clear --node silo`. Chapter 4 makes that machinery the main event.)

## 7. Break it

`STATUS.md` is now the single point of failure, so attack it. Flush again,
delete the state file, then send the recall:

**Run**

```bash
r4t flush --node silo wren
rm STATUS.md
r4t seat send --node silo "What was the codeword?"
r4t seat inbox --node silo
```

You should see:

```
── from silo:wren (2026-07-29T05:34:01.509705Z)
I have no record of a codeword — STATUS.md is missing and I have nothing else on file.
```

The lobotomy worked. Worth understanding rather than panicking over.

## 8. Diagnose

Two layers hold a refounded Wren's memory, and you have now removed both.
`STATUS.md` is the layer Wren writes himself — judgments, progress,
structure. The other is machinery-kept: every turn's prompt carries a
section r4t maintains for him,

```
## Your conversation so far (messages you received and sent)
```

— the rolling message history, capped by the rig's history budget. `r4t
flush` archives that log instead of carrying it forward, which is why the
recall in section 6 proved anything at all:

**Run**

```bash
ls ~/.config/r4t/rosters/silo/agents/wren/
```

You should see:

```
history-20260729T052013449271Z.md  history.md  meta.json
history-20260729T053355901744Z.md  live.log    turns
```

Nothing is deleted — every archived word is still readable — but an
archived log is out of the prompt, so a refounded Wren answers from
`STATUS.md` or not at all. On a long-lived agent that is the real stake: a
fact old enough to have scrolled past the history cap survives only where
Wren wrote it down.

## 9. Fix

Nothing is broken in the machinery — what's missing is the file, and the
machinery that wrote it once will write it again. Give Wren the fact and
flush:

**Run**

```bash
r4t seat send --node silo "The codeword is TIDEPOOL and the supply cache is at grid square K-19."
r4t flush --node silo wren
head -8 STATUS.md
```

You should see:

```
flushed wren — dumped state to disk, retired the conversation, archived history as history-20260729T053902117630Z.md
# STATUS.md — Wren

## Codeword
TIDEPOOL (confirmed)

## Supplied Facts
- Supply cache location: grid square K-19
```

The dump turn wrote it back out of the live conversation.

## 10. Check

One full recall-after-flush round trip — the same health check the whole
chapter has been circling:

**Run**

```bash
r4t seat send --node silo "Codeword and cache location, one line."
r4t seat inbox --node silo
```

You should see:

```
── from silo:wren (2026-07-29T05:38:30.929831Z)
Codeword: TIDEPOOL — Cache: grid square K-19
```

Flushed, refounded, swapped, swapped back, state file deleted and
rewritten — and Wren still answers. The roster is whole.

## 11. Customize

Park the whole roster on your way out — one flag, every member:

**Run**

```bash
r4t flush --node silo --all
```

You should see:

```
flushed wren — dumped state to disk, retired the conversation, archived history as history-20260729T054417338205Z.md
skipped you — human member
```

That is the habit worth keeping: before a long break, after a run that
filled a conversation with dead ends, or when a member starts answering
from stale context. `--no-dump` skips the save turn for a conversation you
do not want banked — a rig that is out of quota and cannot run the turn, or
one whose recent context is exactly what you are trying to be rid of.

The `Continue:` window covers the same ground while you are not looking, and
fifteen minutes is a good default: short pauses keep their momentum, and
after a longer one the flush has already banked what matters in
`STATUS.md`, so the next message starts fresh without starting over. Set it
late enough that normal working pauses don't cost Wren his short-term
memory, early enough that he isn't dragging yesterday into today. On paid
rigs a short window also keeps resumed context from billing at full length;
the mechanics behind that live in
[Prompt-Cache Economics](https://github.com/neilobremski/bin/wiki/Prompt-Cache-Economics).

## 12. Commit point

`STATUS.md` is Wren's own writing and it lives in the repo — version it.
The snapshot you commit is the state Wren would refound from if the
machine died right now:

**Run**

```bash
cd ~/ark/silo
git add ROSTER.md STATUS.md
git commit -q -m "silo: Wren's first memory dump"
```

Chapter 4 gives Wren something no amount of memory provides: a second member.
