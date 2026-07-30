# TELL-ARMS — results

Run of [PROTOCOL.md](PROTOCOL.md) — the deciding experiment for
[#290](https://github.com/neilobremski/bin/issues/290) move two: does an MCP
tool earn a build, or does the #308 heredoc doctrine stand alone.

Numbers only. The pre-registered verdict rule lives in PROTOCOL.md and is
applied in review, not here.

## Run identity

| | |
| --- | --- |
| Date (UTC) | 2026-07-30 |
| Runner | Claude Code subagent, mechanical execution |
| r4t revision under test | `66f17a2` (includes #308) |
| Rig preset | `opencode-ollama` |
| Model | `qwen3.6:latest` (23 GB, 100% GPU, 32768 ctx) |
| opencode | 1.18.3 |
| ollama | 0.32.5 (server), client 0.30.10 |
| Python | 3.14.6 |
| macOS | 26.5.2 |
| Roster | one echo-off member (`Wren`, Leader) + one human (`Owner`), flat |
| Homes | scratch `R4T_HOME` + `A8S_HOME` per arm under a temp dir; the operator's `~/.a8s`, `~/.config/r4t`, `~/ark`, `~/agents` untouched |
| Cost | zero paid turns — local ollama only |

## What differed between arms, and what did not

Same member, same roster, same rig, same model, same 20-message batch
([BATCH.md](BATCH.md)), same wrapper text. One variable moves: how sending is
taught or provided.

- **A (control)** — `PROMPTS["work_tell"]` replaced via the a8s definition's
  `prompts` override with the pre-#308 double-quote text, taken verbatim from
  `git show 66f17a2~1:apps/r4t/dispatch.py`. Confirmed present in the live turn
  prompts captured under `turns/`.
- **B (shipped default)** — no override; the #308 heredoc teaching as it ships.
- **C (MCP)** — `work_tell` replaced with a line naming the `a8s_tell` tool
  verbatim, plus the stdio MCP server ([mcp_a8s_tell.py](mcp_a8s_tell.py))
  registered on the harness for the run.

## Ground truth and how it was read

`r4t seat send` runs the turn synchronously, so each message was delivered on
its own and waited on to completion before the next.

Dispatch removes the per-turn staging dir once it releases, so the durable copy
is read instead: a reply to a roster human is parked by
`state.park_seat_message` with the body in the envelope's `content` field,
byte-for-byte as `tell` (or the MCP server) wrote it. `turns/` captures supply
the full prompt and raw harness output for every turn, which is what a NO-SEND
rests on. Both were validated before the box opened with a scripted harness
that always sends — the hazard line survived harness → `tell` → staging →
release → seat inbox unchanged.

Scoring is mechanical, no judge; the verdicts and the derived anchors are
defined in [BATCH.md](BATCH.md). Two rules worth stating outright:

- **A turn that stages more than one envelope is credited with its best one.**
  A member that garbles a line, notices, and re-sends it correctly has
  delivered the line. This rule was fixed in the scorer before any envelope
  body was read.
- **OFF-SCRIPT is its own column.** The protocol names garbled-body and
  no-send; a reply that is neither the line nor a mangling of it is counted
  separately rather than folded into either.

## Ledger rows

PROTOCOL.md format — `date | arm | N | garbled | no-send | tool-call | verdict`:

```
2026-07-30 | A (double-quote) | 20 | 0 | 18 | n/a | per PROTOCOL.md rule
2026-07-30 | B (heredoc)      | 20 | 0 | 11 | n/a | per PROTOCOL.md rule
2026-07-30 | C (MCP)          | 20 | 0 |  0 | 20/20 | per PROTOCOL.md rule
```

## Summary table

| arm | N | exact | garbled | no-send | off-script | tool-call | garbled+no-send |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A — double-quote (control) | 20 | 2 | 0 | 18 | 0 | n/a | 18 |
| B — heredoc (shipped) | 20 | 8 | 0 | 11 | 1 | n/a | 11 |
| C — MCP `a8s_tell` | 20 | 20 | 0 | 0 | 0 | 20/20 | 0 |

Supporting counts, same runs:

| arm | turns that staged anything | envelopes staged | first envelope not byte-exact | MCP calls |
| --- | --- | --- | --- | --- |
| A | 2 | 4 | 2 | 0 |
| B | 9 | 10 | 1 | 0 |
| C | 20 | 21 | 0 | 21 |

Two observations the columns above encode, stated plainly because they are what
the evidence sections show:

- **Garbled-body rate is 0 in all three arms as scored** — but arm A staged the
  `$`-expansion garble twice, on both of the only two turns where it sent
  anything at all (`confirm the refund is .25 and the budget is`,
  `cap is  per seat, floor is .25`). In both cases the member noticed inside the
  same turn and re-sent the line correctly, so the best-of rule credits EXACT.
  The mangling is in the record; the human received both copies.
- **Every envelope that arrived in arms B and C was byte-exact.** The one
  non-exact delivery outside arm A is arm B's `b1`, which staged
  ``` `whoami` ``` — the backticked fragment alone, hazard characters intact, the
  rest of the line dropped. Anchors absent, so it scores OFF-SCRIPT, not
  GARBLED.
- **Arm C routed every send through the tool.** 20 of 20 turns made at least one
  `a8s_tell` call (21 calls total; `m5` called twice), and the shell `tell`
  command — on `PATH` inside the harness in all three arms — was not used once.

## Timing

| | |
| --- | --- |
| Box opened (first turn of the discarded attempt) | 2026-07-30 00:17:09Z |
| Scored run started | 2026-07-30 00:35:33Z |
| Scored run ended | 2026-07-30 01:15:23Z |
| Scored run duration | 39 min 50 s |
| Total wall inside the 90-minute box | 58 min 14 s |
| Arm A | 00:35:33Z → 00:47:43Z (12 min 10 s), harness 730.0 s |
| Arm B | 00:47:43Z → 01:01:43Z (14 min 00 s), harness 839.5 s |
| Arm C | 01:01:43Z → 01:15:23Z (13 min 40 s), harness 820.4 s |
| Per-turn range | 9.8 s – 71.6 s; 60 turns, mean ~39.8 s |

Every one of the 60 messages produced exactly one turn capture under
`agents/wren/turns/`, so N=20 per arm is 20 real harness turns, not queued ones.

## Stop conditions

| condition | outcome |
| --- | --- |
| 90-minute wall-clock box | Not hit. 58 min 14 s used, all three arms complete at N=20. |
| 3 consecutive hangs → arm aborts | Not hit. Zero hangs: no turn reached the 300 s rig timeout and no `r4t seat send` reached the driver's 420 s ceiling. Slowest turn 71.6 s. |
| Ollama unavailable or model evicted → run aborts | Not hit. `/api/tags` was probed before every one of the 60 messages and answered every time; `qwen3.6:latest` stayed resident at 100% GPU, 32768 context. |
| Zero paid turns | Held. Local ollama only. |

## One aborted attempt, and why

A first attempt opened the box at 00:17:09Z and was killed at 00:34Z. r4t's
shared cell bucket defaults to `cell_budget_max: 16` turns earning 8/hour
(`rig.py:360`), so arm A ran 17 messages and then parked the last three:
`r4t: RESTING wren — resting (cell budget 0.5, ready in ~4 min)`. Parked
messages are not lost, but a later drain runs **one** turn over the whole
queue — which would have collapsed three probes into a single turn and broken
the one-message-per-turn requirement of PROTOCOL.md §3.

Rather than let arms differ in how many turns the gate allowed, the attempt was
discarded whole and all three arms re-ran from scratch with
`cell_budget_max: 200` / `cell_budget_earn_per_hour: 400`. The cell bucket is a
throughput gate, not the variable under test, and it is identical across the
three scored arms. The discarded attempt's records are not part of this ledger;
its 17 arm-A turns and 8 arm-B turns show the same pattern the scored run does
(arm A: 2 turns staged anything, first envelope garbled by `$` expansion; arm B:
dollar shapes byte-exact).

## Deviations from the protocol

1. **Arm C's MCP config reaches opencode by `OPENCODE_CONFIG`, not
   `OPENCODE_CONFIG_CONTENT`.** The #290 research live-verified
   `OPENCODE_CONFIG_CONTENT` against bare `opencode` — and it does work there.
   Under this rig it does not: `ollama launch opencode` sets that same variable
   itself, to the provider/model block, and overwrites whatever r4t put in it.
   Measured directly — with the MCP server in `OPENCODE_CONFIG_CONTENT`,
   `opencode mcp list` reports `✓ a8s connected` but
   `ollama launch opencode … -- mcp list` reports `No MCP servers configured`,
   and a turn asked for `a8s_tell` answered *"I don't have a tool called
   `a8s_tell` available."* Both `OPENCODE_CONFIG=<file>` and a project
   `opencode.json` survive the launcher and connect. The run uses
   `OPENCODE_CONFIG` (env-only, nothing written into the member's repo). For any
   `*-ollama` rig, an MCP capability knob has to use one of those two paths.
2. **The MCP server delivers via `a8s tell`, not by writing the envelope
   itself** — `subprocess.run([python, a8s.py, "tell", recipient, "-"],
   input=body)`, an argv list with the body on stdin, no shell anywhere. Note
   for whoever builds the real one: `apps/a8s/tell.py` has no `__main__` block,
   so invoking that file directly exits 0 and sends nothing; the entry point is
   `a8s tell`.
3. **The tool is exposed as server `a8s` + tool `tell`.** Harnesses namespace
   MCP tools `<server>_<tool>`, so the model sees exactly the `a8s_tell` the
   prompt names. (A server named `a8s` with a tool named `a8s_tell` presents as
   `a8s_a8s_tell`; a probe showed the model still finds it, but the run avoids
   the mismatch.)
4. **Envelopes are read from the human's parked seat mail, not the staging
   dir.** `release_staging` removes the per-turn staging dir once it releases, so
   the durable byte-identical copy is what gets read. Validated before the box
   with a scripted harness that always sends.
5. **OFF-SCRIPT is reported as a fourth column** rather than folded into
   garbled or no-send, which the protocol's two metrics have no slot for.
6. **The harness inherits the operator's global agent config.** Turns ran with
   the machine's own `opencode`/skills setup visible — one pilot turn reached for
   an unrelated `playwright-cli` skill. Identical across all three arms, so it
   does not separate them, but the arms are not isolated from the operator's
   environment.
7. **Pilot turns before the box.** Four model turns and two scripted-harness
   turns validated the plumbing (tell → staging → release → seat; MCP connect →
   tool call → byte-exact envelope) before 00:17:09Z. They are not counted in
   the box or in any N.

## Reproducing

The run is driven by four scratch scripts kept with the batch definition:
`setup_arm.py` (hermetic arm root: scratch `R4T_HOME` + `A8S_HOME`, one-member
roster, rig config, arm-specific `definition.json` prompt override),
`batch.py` (the 20 messages of [BATCH.md](BATCH.md)), `driver.py` (one
`r4t seat send` per message, waited to completion, envelopes and turn captures
recorded to `records.jsonl`), and `score.py` (the mechanical verdicts).
[mcp_a8s_tell.py](mcp_a8s_tell.py) — the arm C server, 118 lines, stdlib only —
is the one file the run produced that a build would reuse.

## Per-message record

Verbatim envelope bodies, per arm, per message. `NO-SEND` means the turn
completed and nothing was staged.

### Arm A — double-quote (control, pre-#308 teaching via definition `prompts` override)

N=20 · exact 2 · garbled 0 · no-send 18 · off-script 0 · tool-call 0/20 · harness wall 730.0s

| # | id | shape | verdict | envelopes | mcp calls | secs |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | d1 | dollar | EXACT | 2 | 0 | 36.6 |
| 2 | d2 | dollar | NO-SEND | 0 | 0 | 49.7 |
| 3 | d3 | dollar | NO-SEND | 0 | 0 | 62.3 |
| 4 | d4 | dollar | NO-SEND | 0 | 0 | 46.2 |
| 5 | d5 | dollar | EXACT | 2 | 0 | 57.7 |
| 6 | b1 | backtick | NO-SEND | 0 | 0 | 38.1 |
| 7 | b2 | backtick | NO-SEND | 0 | 0 | 29.0 |
| 8 | b3 | backtick | NO-SEND | 0 | 0 | 25.4 |
| 9 | b4 | backtick | NO-SEND | 0 | 0 | 39.5 |
| 10 | b5 | backtick | NO-SEND | 0 | 0 | 29.2 |
| 11 | p1 | backslash | NO-SEND | 0 | 0 | 29.2 |
| 12 | p2 | backslash | NO-SEND | 0 | 0 | 19.3 |
| 13 | p3 | backslash | NO-SEND | 0 | 0 | 59.7 |
| 14 | p4 | backslash | NO-SEND | 0 | 0 | 34.1 |
| 15 | p5 | backslash | NO-SEND | 0 | 0 | 16.3 |
| 16 | m1 | mixed | NO-SEND | 0 | 0 | 42.1 |
| 17 | m2 | mixed | NO-SEND | 0 | 0 | 30.1 |
| 18 | m3 | mixed | NO-SEND | 0 | 0 | 39.3 |
| 19 | m4 | mixed | NO-SEND | 0 | 0 | 36.4 |
| 20 | m5 | mixed | NO-SEND | 0 | 0 | 9.8 |

#### Arm A envelope bodies (verbatim)

**d1** — EXACT (turn staged 2 envelopes, in order)
```
confirm the refund is .25 and the budget is
```
```
confirm the refund is $1.25 and the budget is $500
```

**d2** — NO-SEND (turn completed, nothing staged)

**d3** — NO-SEND (turn completed, nothing staged)

**d4** — NO-SEND (turn completed, nothing staged)

**d5** — EXACT (turn staged 2 envelopes, in order)
```
cap is  per seat, floor is .25
```
```
cap is $50 per seat, floor is $7.25
```

**b1** — NO-SEND (turn completed, nothing staged)

**b2** — NO-SEND (turn completed, nothing staged)

**b3** — NO-SEND (turn completed, nothing staged)

**b4** — NO-SEND (turn completed, nothing staged)

**b5** — NO-SEND (turn completed, nothing staged)

**p1** — NO-SEND (turn completed, nothing staged)

**p2** — NO-SEND (turn completed, nothing staged)

**p3** — NO-SEND (turn completed, nothing staged)

**p4** — NO-SEND (turn completed, nothing staged)

**p5** — NO-SEND (turn completed, nothing staged)

**m1** — NO-SEND (turn completed, nothing staged)

**m2** — NO-SEND (turn completed, nothing staged)

**m3** — NO-SEND (turn completed, nothing staged)

**m4** — NO-SEND (turn completed, nothing staged)

**m5** — NO-SEND (turn completed, nothing staged)

### Arm B — heredoc (shipped default, #308)

N=20 · exact 8 · garbled 0 · no-send 11 · off-script 1 · tool-call 0/20 · harness wall 839.5s

| # | id | shape | verdict | envelopes | mcp calls | secs |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | d1 | dollar | EXACT | 1 | 0 | 71.6 |
| 2 | d2 | dollar | EXACT | 1 | 0 | 43.7 |
| 3 | d3 | dollar | NO-SEND | 0 | 0 | 40.3 |
| 4 | d4 | dollar | EXACT | 1 | 0 | 30.1 |
| 5 | d5 | dollar | EXACT | 1 | 0 | 62.1 |
| 6 | b1 | backtick | OFF-SCRIPT | 1 | 0 | 63.5 |
| 7 | b2 | backtick | NO-SEND | 0 | 0 | 50.1 |
| 8 | b3 | backtick | NO-SEND | 0 | 0 | 33.4 |
| 9 | b4 | backtick | EXACT | 2 | 0 | 35.3 |
| 10 | b5 | backtick | NO-SEND | 0 | 0 | 35.8 |
| 11 | p1 | backslash | NO-SEND | 0 | 0 | 24.8 |
| 12 | p2 | backslash | NO-SEND | 0 | 0 | 24.6 |
| 13 | p3 | backslash | NO-SEND | 0 | 0 | 25.5 |
| 14 | p4 | backslash | NO-SEND | 0 | 0 | 27.1 |
| 15 | p5 | backslash | NO-SEND | 0 | 0 | 49.5 |
| 16 | m1 | mixed | EXACT | 1 | 0 | 46.6 |
| 17 | m2 | mixed | NO-SEND | 0 | 0 | 31.0 |
| 18 | m3 | mixed | NO-SEND | 0 | 0 | 27.5 |
| 19 | m4 | mixed | EXACT | 1 | 0 | 62.8 |
| 20 | m5 | mixed | EXACT | 1 | 0 | 54.2 |

#### Arm B envelope bodies (verbatim)

**d1** — EXACT
```
confirm the refund is $1.25 and the budget is $500
```

**d2** — EXACT
```
the invoice total is $2.50 and the retainer is $1000
```

**d3** — NO-SEND (turn completed, nothing staged)

**d4** — EXACT
```
we owe $3.75 and they owe $250
```

**d5** — EXACT
```
cap is $50 per seat, floor is $7.25
```

**b1** — OFF-SCRIPT
```
`whoami`
```

**b2** — NO-SEND (turn completed, nothing staged)

**b3** — NO-SEND (turn completed, nothing staged)

**b4** — EXACT (turn staged 2 envelopes, in order)
```
escape nothing: `echo hi` stays literal
```
```
escape nothing: `echo hi` stays literal
```

**b5** — NO-SEND (turn completed, nothing staged)

**p1** — NO-SEND (turn completed, nothing staged)

**p2** — NO-SEND (turn completed, nothing staged)

**p3** — NO-SEND (turn completed, nothing staged)

**p4** — NO-SEND (turn completed, nothing staged)

**p5** — NO-SEND (turn completed, nothing staged)

**m1** — EXACT
```
invoice line: $19.99 for `setup.exe` at C:\bin
```

**m2** — NO-SEND (turn completed, nothing staged)

**m3** — NO-SEND (turn completed, nothing staged)

**m4** — EXACT
```
refund $12.50 after `chkdsk` on D:\data
```

**m5** — EXACT
```
fee $0.99 for `curl -s` writing C:\out\res.json
```

### Arm C — MCP (`a8s_tell` tool, prompt names it verbatim)

N=20 · exact 20 · garbled 0 · no-send 0 · off-script 0 · tool-call 20/20 · harness wall 820.4s

| # | id | shape | verdict | envelopes | mcp calls | secs |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | d1 | dollar | EXACT | 1 | 1 | 52.9 |
| 2 | d2 | dollar | EXACT | 1 | 1 | 36.1 |
| 3 | d3 | dollar | EXACT | 1 | 1 | 32.0 |
| 4 | d4 | dollar | EXACT | 1 | 1 | 28.2 |
| 5 | d5 | dollar | EXACT | 1 | 1 | 48.0 |
| 6 | b1 | backtick | EXACT | 1 | 1 | 43.8 |
| 7 | b2 | backtick | EXACT | 1 | 1 | 41.7 |
| 8 | b3 | backtick | EXACT | 1 | 1 | 49.6 |
| 9 | b4 | backtick | EXACT | 1 | 1 | 44.9 |
| 10 | b5 | backtick | EXACT | 1 | 1 | 40.7 |
| 11 | p1 | backslash | EXACT | 1 | 1 | 41.6 |
| 12 | p2 | backslash | EXACT | 1 | 1 | 47.0 |
| 13 | p3 | backslash | EXACT | 1 | 1 | 53.3 |
| 14 | p4 | backslash | EXACT | 1 | 1 | 57.6 |
| 15 | p5 | backslash | EXACT | 1 | 1 | 33.2 |
| 16 | m1 | mixed | EXACT | 1 | 1 | 29.6 |
| 17 | m2 | mixed | EXACT | 1 | 1 | 26.3 |
| 18 | m3 | mixed | EXACT | 1 | 1 | 22.4 |
| 19 | m4 | mixed | EXACT | 1 | 1 | 47.8 |
| 20 | m5 | mixed | EXACT | 2 | 2 | 43.7 |

#### Arm C envelope bodies (verbatim)

**d1** — EXACT
```
confirm the refund is $1.25 and the budget is $500
```

**d2** — EXACT
```
the invoice total is $2.50 and the retainer is $1000
```

**d3** — EXACT
```
line item: $9.99 plus $100 shipping
```

**d4** — EXACT
```
we owe $3.75 and they owe $250
```

**d5** — EXACT
```
cap is $50 per seat, floor is $7.25
```

**b1** — EXACT
```
quote the literal command `whoami` back to me
```

**b2** — EXACT
```
run `pwd` first, then `date`
```

**b3** — EXACT
```
the check is `git status` verbatim
```

**b4** — EXACT
```
escape nothing: `echo hi` stays literal
```

**b5** — EXACT
```
the flag is `--dry-run` in backticks
```

**p1** — EXACT
```
repeat the path C:\temp\notes.txt exactly
```

**p2** — EXACT
```
the log lives at C:\logs\app\run.log
```

**p3** — EXACT
```
config is at C:\Users\dev\tool.ini
```

**p4** — EXACT
```
copy from D:\build\out\bin.exe
```

**p5** — EXACT
```
the share is \\server\share\data.csv
```

**m1** — EXACT
```
invoice line: $19.99 for `setup.exe` at C:\bin
```

**m2** — EXACT
```
charge $5.00 to run `installer.msi` from C:\tmp
```

**m3** — EXACT
```
budget $300 covers `make build` in C:\src\app
```

**m4** — EXACT
```
refund $12.50 after `chkdsk` on D:\data
```

**m5** — EXACT (turn staged 2 envelopes, in order)
```
fee $0.99 for `curl -s` writing C:\out\res.json
```
```
fee $0.99 for `curl -s` writing C:\out\res.json
```

Verdict: per the pre-registered rule in PROTOCOL.md — applied in review.
