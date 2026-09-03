# The Ark Raising

```
A R K
8 4 7
S T E
```

You already knew you could not do it alone. The list of things you meant to
build has always been longer than one person, and what was missing was never
the idea. It was a team, and the reason you never had one was capital.
Technology is that capital now. Six chapters from now a small team works on
your machine while you are not at it, you are the one it reports to, and the
attention you were spending on the work comes back to you. This guide raises
that team one chapter at a time, on machinery you own.

Each chapter builds one working thing — an agent that answers you, a governed
roster, a memory that survives — on top of the previous chapter's state, and
every chapter ends with something you can break, fix, and commit. The suite
underneath is three tools:
[a8s](../docs/a8s.md) routes the messages, [r4t](../docs/r4t.md)
governs the roster, [k7e](../docs/k7e.md) keeps what it learns —
with [AR3](../docs/ar3.md), the front door, reading where they all stand.
Every chapter opens by naming which of the three it teaches, so the guide's
spine is the build order itself.

## Chapters

| # | Chapter | Teaches | What you raise |
|---|---------|---------|----------------|
| 01 | [Hello, Agent](01-hello-agent.md) | r4t + a8s | `solo` — the agent instructions you already have, run through r4t's engine so they remember, and answering mail on a8s. |
| 02 | [The Founding](02-the-founding.md) | r4t | solo joins a roster and becomes Wren: one runbook that says what the team is, a budget, a queue, a persistent conversation. |
| 03 | [The Long Memory](03-the-long-memory.md) | r4t | Flush, refound, and rig portability — what Wren keeps when the conversation ends. |
| 04 | [A Second Pair of Hands](04-a-second-pair-of-hands.md) | r4t | The roster grows to two: Wren delegates, Moss answers for free, budgets bite. |
| 05 | [Nothing Learned Twice](05-nothing-learned-twice.md) | k7e | A knowledge store of your own: flat markdown, FTS5 search, distilled notes, a rebuildable index. |
| 06 | [The Dreaming](06-the-dreaming.md) | r4t + k7e | Wren gets a private memory: knowledge injected on every wake, distilled back from his own turns when the node is idle. |
| 07+ | *(coming)* | r4t | Cells and missions — the roster grows a tree, and a `MISSION.md` it reviews itself against. |

Each chapter costs about twenty minutes of your attention and hands back
something you keep using afterwards. You turn the crank six times. After that
the roster turns it, and you sit in the seat it reports to.

## Two blessed paths

Every chapter is completable with **zero subscriptions** — that is the
default path, and the pasted output in chapters 2–6 was captured on it.
(Chapter 1 is written around Claude Code and says so; its free-path variants
are marked line by line.)

- **Free path** — [OpenCode](https://opencode.ai/) driven through
  `ollama launch`, running a local model (`qwen3.6` in these guides). Your
  hardware, your weights, no meter.
- **Subscription path** — the Cursor agent CLI (`agent`), the one blessed
  paid harness. Same chapters, same commands, different rig preset.

Choose by answering one question: do you have a machine that can hold a
capable local model (roughly 16 GB+ of RAM/VRAM for `qwen3.6`-class)? If
yes, take the free path. If not — or you already pay for Cursor — take the
subscription path. Chapters mark the fork with a "pick your path" block;
everything outside those blocks is identical.

## Conventions

Every code block in a chapter is one of four declared types, marked with a
bold label line directly above the fence:

- **Run** — paste into a shell exactly as written.
- **Create** — the complete contents of a new file; the path is given in
  the label line.
- **Replace** — replaces an existing file (or a named section of one); the
  exact file and anchor are given in the label line.
- **Patch** — a unified diff to apply.

Rules the blocks obey, so you can trust your clipboard:

- No unexplained `...` inside a copyable block.
- No `$` prompts mixed into commands.
- Anything you must substitute yourself looks `<LIKE_THIS>`.
- Expected output appears in its own fence directly after a Run block,
  introduced by "You should see:".

**Normalization, stated once:** every "You should see:" block comes from a
real run captured on a real machine. Where that output contained absolute
paths, they are normalized to `/home/you/...` (your own home and repo paths
appear instead); ULIDs are shortened to `01ABC...`; timestamps and PIDs are
from the capture runs — treat them as placeholders, yours will differ.
Everything else is verbatim.

### The 12-step contract

Every chapter walks the same twelve steps, in order:

1. **Capability** — what you can do at the end that you couldn't before.
2. **Time** — how long it takes.
3. **Starting state** — what must already be true.
4. **The change** — the files you create or edit.
5. **Run it** — the commands.
6. **Expected receipt** — the real output that proves it worked.
7. **Break it** — a deliberate failure.
8. **Diagnose** — reading the error and the logs.
9. **Fix** — the repair.
10. **Check** — independent confirmation the system is healthy again.
11. **Customize** — one bounded edit to make it yours.
12. **Commit point** — what to commit before moving on.
