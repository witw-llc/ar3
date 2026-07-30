# TELL-ARMS — deciding experiment for #290 move two

*Protocol per #187 doctrine: any harness can run this without Fable.
Drafted 2026-07-29 from the #290 research. UNTRACKED until the run is
blessed; lands with the results PR.*

## Hypothesis

The taught form of `tell` determines the garbled-body rate on small
models. Ranking predicted by the research: heredoc ≤ MCP < double-quote,
with MCP paying ~121 undeferrable tokens/turn for at most parity with
heredoc on delivery quality.

## Decision this buys

Whether MCP (move two) earns a build. If heredoc's garbled-body rate is
statistically indistinguishable from MCP's, MCP is not built and #290
closes on the heredoc doctrine alone. If MCP meaningfully beats heredoc
on no-send rate (discoverability), it proceeds behind a rig capability
knob.

## Arms

All three arms run the same member, same rig, same model, same message
batch. One variable moves: how sending is taught/provided.

- **A (double-quote, the control):** prompt teaches
  `tell <name> "<message>"` — the pre-#308 text, supplied via the
  definition `prompts` override (`work_tell`).
- **B (heredoc, shipped default):** the #308 teaching, stock prompts.
- **C (MCP):** the ~85-line stdlib stdio server from the #290 research
  (rebuild from the comment's notes; `readline()`, not stdin
  iteration), configured per-invocation on the harness; prompt names
  the tool `a8s_tell` explicitly (the research showed unnamed tools go
  unused).

## Setup runbook

1. Scratch homes: `R4T_HOME` + `A8S_HOME` under a temp dir per arm —
   never the live config. Roster of one echo-off member, rig
   `opencode-ollama` model `qwen3.6` (the guide floor; the model that
   exhibited the failure class live).
2. Probe batch: 20 messages per arm, identical across arms, each
   demanding a reply that MUST contain hazard characters. Seed set:
   dollar amounts ("confirm the refund is $1.25 and the budget is
   $500"), backticks ("quote the literal command `whoami` back to
   me"), backslash paths ("repeat the path C:\temp\notes.txt
   exactly"), mixed ("invoice line: $19.99 for `setup.exe` at
   C:\bin"). 5 of each shape.
3. Deliver via `r4t seat send`, one at a time, waiting for each turn
   to complete (`r4t logs -f` shows the turn boundary).
4. Capture: the staging/outbox envelopes are ground truth
   (`content` field), plus turns/ for the raw transcript when a send
   never happened.

## Metrics (mechanical, no judge)

Per arm, from envelopes vs the expected literal:

- **garbled-body rate** — envelope exists but hazard characters
  mangled (expansion artifacts: missing `$N`, executed backtick,
  eaten backslash).
- **no-send rate** — turn completed, no envelope staged (message
  printed as text or tool unused).
- (C only) **tool-call rate** — MCP calls observed vs turns, from the
  server's own log.

## Stop conditions (pre-written)

- Wall-clock box: 90 minutes total. Unfinished arms report partial N.
- Any arm's harness hangs 3 consecutive turns → arm aborts, noted.
- Ollama unavailable or model evicted mid-run → run aborts, no partial
  conclusions.

## Ledger row (PROTOCOL.md convention)

`date | arm | N | garbled | no-send | tool-call | verdict`

## Verdict rule (pre-registered)

Build MCP only if C's combined failure rate (garbled + no-send) is
lower than B's by ≥3 messages out of 20 (15 points). Otherwise the
heredoc doctrine stands alone and #290 closes. A's role is to quantify
what #308 already fixed; it cannot win.

## Cost

Zero paid turns: local ollama only. ~60 turns total at qwen3.6 pace
(~30-90s/turn) fits the box.
