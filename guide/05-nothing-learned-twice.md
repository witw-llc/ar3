# Chapter 5 — Nothing Learned Twice

**Teaches K — [k7e](../docs/k7e.md), the knowledge engine.**

## 1. Capability

At the end of this chapter you have a knowledge store: a folder of flat
markdown files you can read, grep, edit, and commit, with a search index over
it that answers questions asked in your own words. You will write entries by
hand, hand a page of scratch notes to a local model and watch it extract what
is worth keeping, get a cited answer back — then delete the entire index on
purpose and rebuild it from the files, because the files are the truth and the
index is only a cache. Nothing here touches the roster; chapter 6 hands what
you build to Wren.

## 2. Time

About 20 minutes, nothing to download — the model from chapter 1 does all the
thinking.

## 3. Starting state

- Chapter 4 complete, or at least The Ark installed with `k7e` on your PATH,
  and Python 3 (`python3 --version` answers) for the one script you write here.
- **Free path** — `ollama` serving with `qwen3.6` pulled (`ar3 doctor` lists
  your models on its `ollama serve` line). **Subscription path** — the Cursor
  agent CLI (`agent`), logged in.

The `k7e` panel in `ar3` has been ✗ since chapter 1 and stays ✗ until the
first thing is written: there is no create step, and the store appears
under `~/.config/k7e` (`K7E_HOME`) the moment you store an entry — chapter
1's panel hints `(try: k7e init)`, but that verb does not exist (issue #78
tracks the hint).

## 4. The change

k7e's core — storage and keyword search — is Python's standard library and
nothing else, offline and modelless. Three commands need a model anyway:
`distill` (pull knowledge out of raw text), `recall` (answer a question over
the store), and `compile` (synthesize a tag into a page). k7e does not go
looking for one. You hand it **a shell command that reads a
prompt on stdin and writes the answer on stdout**, and that is the only model
it ever uses: no auto-detection, no bundled provider, no key in a config file
you didn't write. On the free path the bridge is twelve lines against ollama's
local HTTP API:

**Run**

```bash
mkdir -p ~/ark/bin
```

**Create** `~/ark/bin/ask`

```python
#!/usr/bin/env python3
"""stdin -> stdout bridge to a local ollama model, for k7e's LLM commands."""
import json, os, sys, urllib.request

body = json.dumps({
    "model": os.environ.get("ASK_MODEL", "qwen3.6"),
    "prompt": sys.stdin.read(),
    "stream": False,
    "think": False,
}).encode()
request = urllib.request.Request(
    "http://localhost:11434/api/generate",
    data=body,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=600) as response:
    print(json.loads(response.read())["response"].strip())
```

The obvious one-liner — `ollama run qwen3.6` — is the wrong bridge, and its
failure is worth knowing once. It word-wraps at 80 columns even into a pipe,
and it wraps by printing a partial word and rewinding the cursor, so the
answer comes back with `\x1b[1D\x1b[K` spliced through it. Those escape bytes
land inside the JSON k7e asked for, the parse fails, and `k7e distill` says
`No new knowledge extracted.` with no error anywhere. The HTTP API returns the
text and nothing else, and `"think": false` keeps qwen3.6's reasoning out of
the answer for the same reason.

**Run**

```bash
chmod +x ~/ark/bin/ask
k7e config llm_command "$HOME/ark/bin/ask"
k7e status
```

**Subscription path** — your bridge is already installed: chapter 2's rig
invoke with the prompt argument left off, because the Cursor CLI reads stdin
when you don't hand it one. Replace the `chmod` and `k7e config llm_command`
lines above with this one, then run `k7e status` the same as before:

```bash
k7e config llm_command 'agent --model auto -p --trust --force --approve-mcps'
k7e status
```

You should see (the `k7e config` confirmation, then the bridge and search
rows of `k7e status`):

```
llm_command = /home/you/ark/bin/ask
  LLM fallback: /home/you/ark/bin/ask ✓
  LLM summarize: /home/you/ark/bin/ask (via llm_command)
  LLM decompose: /home/you/ark/bin/ask (via llm_command)
  LLM distill: /home/you/ark/bin/ask (via llm_command)
  LLM compile: /home/you/ark/bin/ask (via llm_command)
  LLM rerank: /home/you/ark/bin/ask (via llm_command)
  Embeddings: ollama running but model 'nomic-embed-text' not found
    → Install: ollama pull nomic-embed-text
  Search: FTS5 (keyword) ✓
  Search: Semantic (embeddings) ✗ — FTS5-only mode
```

One bridge, five purposes, each repointable later (`k7e config
distill_command ...`) without disturbing the rest. The embeddings ✗ is real,
and the chapter runs fine without it: keyword search is SQLite's FTS5 —
offline, exact, fast.

## 5. Run it

Four chapters of hard-won facts are sitting in your head and in these pages.
Put two of them where they outlive both. Content comes from `--content` or
stdin — use one of each:

**Run**

```bash
k7e store "Echo members answer with stdout" --tags r4t,roster --content "A rig with echo true makes its member stdout-only: the turn prompt carries no messaging doctrine, and whatever the member prints becomes its one reply. The right shape for a helper that answers questions. Lift it (r4t rig unset <rig> echo) the moment that member has to reach anyone but its caller."
k7e store "A rig swap retires the conversation" --tags r4t,memory <<'EOF'
The conversation is keyed on the CLI that holds it. Swap a member's rig to a
preset driving a different CLI and the conversation is retired — r4t logs
CONTINUE-SWAP and the next turn refounds from STATUS.md.

A swap that keeps the CLI (a model change, or opencode <-> ollama-opencode)
keeps the conversation.
EOF
find ~/.config/k7e
```

You should see (sorted, directories omitted):

```
Stored K7E-000-00001: Echo members answer with stdout
Stored K7E-000-00002: A rig swap retires the conversation
/home/you/.config/k7e
/home/you/.config/k7e/.index.db
/home/you/.config/k7e/assets
/home/you/.config/k7e/config.json
/home/you/.config/k7e/mocs
/home/you/.config/k7e/mocs/memory.md
/home/you/.config/k7e/mocs/r4t.md
/home/you/.config/k7e/mocs/roster.md
/home/you/.config/k7e/nodes
/home/you/.config/k7e/nodes/000/K7E-000-00001.md
/home/you/.config/k7e/nodes/000/K7E-000-00002.md
```

Two entries, two files. `mocs/` holds an auto-written Map of Content per tag,
so `mocs/r4t.md` links both; `.index.db` is the search index, derived and
disposable and the subject of section 7. Read an entry as it sits on disk:

**Run**

```bash
head -21 ~/.config/k7e/nodes/000/K7E-000-00002.md
```

You should see:

```
---
id: K7E-000-00002
title: A rig swap retires the conversation
aliases: []
status: active
confidence: 0.5
verification_count: 0
last_updated: 2026-07-31
tags: [r4t, memory]
---

## Verified Protocol

The conversation is keyed on the CLI that holds it. Swap a member's rig to a
preset driving a different CLI and the conversation is retired — r4t logs
CONTINUE-SWAP and the next turn refounds from STATUS.md.

A swap that keeps the CLI (a model change, or opencode <-> ollama-opencode)
keeps the conversation.

## Edge Cases
```

Frontmatter you can read, the prose you wrote, and empty `Edge Cases`,
`False Paths`, and `History` sections waiting to be filled. Nothing here needs
k7e to be interpretable — that is the storage contract entire. But hand-typing
does not scale past the entries you care most about, which is what `distill`
is for. Write the mess down the way it actually looks:

**Run**

```bash
mkdir -p ~/ark/silo
```

**Create** `~/ark/silo/NOTES.md`

```markdown
# scratch notes — the silo roster, thursday

tell only sends when the member actually RUNS it. Wren printed
`tell you "Inkwell"` as text and it vanished — cleaned off as terminal
chrome, anything under about 80 characters goes.

budgets: rig_budget_max lives on the rig and is machine-global. Set helper
to 1 and Moss went RESTING with the message queued, ready in ~59 min.
Nothing was dropped — gates govern when a member runs, never whether a
message survives.

r4t idle drains the queue by hand instead of waiting out the refill.

2>/dev/null on a harness invoke matters: the progress UI paints on stderr,
the finished answer is alone on stdout.

flush = dump turn, then retire the conversation, then archive the history
log. STATUS.md is what a refounded member reads on the way back up.

two members driving the same CLI in one Workdir share one conversation.
give every member its own Workdir.
```

**Run**

```bash
k7e distill ~/ark/silo/NOTES.md --dry-run
k7e distill ~/ark/silo/NOTES.md
```

## 6. Expected receipt

You should see:

```
  [would_store]  Tell message length limit
  [would_store]  Rig budget configuration scope
  [would_store]  Harness invoke stdout usage
  [would_store]  Flush sequence definition
  [would_store]  Workdir isolation requirement
  [stored] K7E-000-00003 Terminal chrome message limit
  [stored] K7E-000-00004 Rig global budget max
  [stored] K7E-000-00005 Harness invoke stderr usage
```

Your titles will differ, and the dry run's list differs from the real run's on
the same file — the extractor is a model, not a parser. Candidates are also
diffed against what the store already holds before anything is written, so
distilling the same notes twice mostly stores nothing. On current releases a
model-chosen tag containing a slash (`I/O` is the common one) can crash the
MOC write partway through a batch — issue #89, fix in flight — so a real run
can store fewer entries than the dry run promised; §8 below covers what
`k7e check` says when that happens. Now ask a question the
way you would ask a person:

**Run**

```bash
k7e search "my member printed the answer instead of sending it"
k7e get <THE_TOP_ID> | sed -n '12,14p'
```

You should see:

```
  K7E-000-00003  Terminal chrome message limit  (score: 0.0142)
  K7E-000-00001  Echo members answer with stdout  (score: 0.0139)
  K7E-000-00005  Harness invoke stderr usage  (score: 0.0135)
  K7E-000-00004  Rig global budget max  (score: 0.0133)
  K7E-000-00002  A rig swap retires the conversation  (score: 0.0131)
```
```
## Verified Protocol

Messages under approximately 80 characters sent via 'tell' are cleared as terminal chrome; use Wren to print text that must persist.
```

(`get` prints the whole entry — the `sed` keeps the three lines that say
something.) The tell/80-character entry should rank near the top for a
question sharing almost no words with it. Find the distilled "Terminal
chrome message limit" entry and read its stored sentence to the end: *Use
Wren to print text that must persist* is nonsense the extractor invented on
its way past a real fact — worth checking for even when a hand-written entry
takes the top spot instead. Distilled knowledge is a lead, not a law, which
is exactly how chapter 6's injector frames it. So you do not delete it; you
correct it where the correction belongs, using the ID your own run
produced:

**Run**

```bash
k7e append <YOUR_ID> --section "Edge Cases" --content "The 80-character floor applies to a member's stdout, not to a tell it actually runs. The second clause above is the extractor over-reaching — Wren has nothing to do with it."
k7e recall "a member answered me by printing instead of telling — what do I check?"
```

You should see:

```
Appended to K7E-000-00003 [Edge Cases]
If an Echo-enabled member answers by printing rather than telling, this is expected behavior because an "echo true" rig makes the member stdout-only, meaning whatever it prints becomes its only reply [K7E-000-00001]. This configuration is appropriate for helpers answering questions but should be disabled (`r4t rig unset <rig> echo`) if the member needs to communicate with anyone other than its caller [K7E-000-00001].

---
Sources: K7E-000-00001, K7E-000-00003, K7E-000-00005, K7E-000-00002, K7E-000-00004
```

That is `recall`: retrieve, then synthesize, every claim carrying the ID it
came from. Search hands you doors; recall walks through them and reports back.
Both are reads — the only thing that changes is the usage counter that makes
well-used entries rank higher next time.

## 7. Break it

Everything since section 5 came out of `.index.db`. Delete it:

**Run**

```bash
rm ~/.config/k7e/.index.db
k7e search "my member printed the answer instead of sending it"
k7e list
k7e stats
```

You should see:

```
No results.
Entries: 0  MOCs: 12  Assets: 0
Avg confidence: 0.0
```

Total amnesia, reported calmly: `list` printed nothing at all, `stats` counts
zero, every command exited 0. Worth recognizing on sight, because nothing
about it looks like a failure.

## 8. Diagnose

One question settles it — is the knowledge gone, or is the *index* gone?

**Run**

```bash
ls ~/.config/k7e/nodes/000
k7e check
```

You should see:

```
K7E-000-00001.md
K7E-000-00002.md
K7E-000-00003.md
K7E-000-00004.md
K7E-000-00005.md
Clean.
```

Five files, and `k7e check` — which audits the markdown, not the database —
calls the store sound. Both readings are true at once: the knowledge is
intact, and the cache every read went through is empty. That gap is the whole
design in one screen. If §6's slash-tag crash hit your run, `k7e check`
reports missing MOCs here instead of `Clean.` — the entries are still whole,
only the tag/MOC bookkeeping is off.

## 9. Fix

Rebuild the cache from the truth:

**Run**

```bash
k7e reindex
k7e search "my member printed the answer instead of sending it"
```

You should see:

```
Reindex complete.
  K7E-000-00003  Terminal chrome message limit  (score: 0.014)
  K7E-000-00001  Echo members answer with stdout  (score: 0.014)
  K7E-000-00005  Harness invoke stderr usage  (score: 0.0137)
  K7E-000-00004  Rig global budget max  (score: 0.0133)
  K7E-000-00002  A rig swap retires the conversation  (score: 0.0131)
```

Instant here, and it stays cheap because a rebuild only reads markdown. Two
costs are real: `--embeddings` recomputes vectors at the model's pace, and a
plain reindex resets the use-count ranking signals by design — a rebuilt index
has no opinion about which entries earned their keep. It is also why editing
an entry in your text editor is legal: change the file, reindex, done.

## 10. Check

The front door reads the store the way it reads everything else:

**Run**

```bash
ar3
```

You should see (`k7e` section):

```
k7e — knowledge engine  (/home/you/.config/k7e)
  ✓ cli    k7e -> /home/you/.ar3/k7e
  ✓ store  5 entr(ies) under /home/you/.config/k7e/nodes
  ✓ index  56 KiB at /home/you/.config/k7e/.index.db
```

Three greens where there were none since chapter 1 — and `ar3` counted those
entries by walking `nodes/`, not by asking the index. Even the front door
treats the files as the truth.

## 11. Customize

A store is a directory, and `K7E_HOME` decides which one a command talks to.
A second store costs nothing:

**Run**

```bash
K7E_HOME=~/ark/lore k7e store "Inkwell is the roster mascot" --tags silo --content "An octopus. Moss drafted three names, Wren picked Inkwell."
K7E_HOME=~/ark/lore k7e list
```

You should see:

```
Stored K7E-000-00001: Inkwell is the roster mascot
  K7E-000-00001  Inkwell is the roster mascot  [active]  conf:0.5
```

One entry, numbered from 1 again, with the five in `~/.config/k7e` untouched
and invisible from here. Two stores, two ID sequences, nothing shared —
separation is by folder, not by tag, and tags organize *within* a store while
enforcing nothing between them. Remember that shape: chapter 6 gives a member
a store of its own, and this is how.

One more knob is a download away: `ollama pull nomic-embed-text`, then `k7e
reindex --embeddings`, and search fuses meaning with keywords instead of
keywords alone — worth it once the store holds entries whose exact words you
no longer remember.

## 12. Commit point

The store version-controls like anything else you wrote — and the one file
that must stay out is the one you just proved is disposable:

**Run**

```bash
cd ~/.config/k7e
git init -q
printf '.index.db\n' > .gitignore
git add -A
git commit -q -m "k7e: the first five things I do not want to learn twice"
git ls-files nodes
git status --short
```

You should see:

```
nodes/000/K7E-000-00001.md
nodes/000/K7E-000-00002.md
nodes/000/K7E-000-00003.md
nodes/000/K7E-000-00004.md
nodes/000/K7E-000-00005.md
```

Nothing after that — a clean tree with the index ignored. Clone the repo onto
another machine, run `k7e reindex`, and the store is whole there too: that is
the portability claim and its entire mechanism. The bridge script and this
chapter's notes file live in
[templates/05-k7e-bridge/](templates/05-k7e-bridge/).

## What you own

A memory that depends on no conversation staying alive: entries you wrote,
entries a model pulled out of your scratch notes, a correction pinned to a
fact that was half wrong, and a search that finds all of it from a question
phrased however you happened to phrase it that day. It is yours and it is
inert — nobody reads it but you, and no agent on your roster knows it exists.
Chapter 6 fixes that.
