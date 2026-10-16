# Development: constraints, testing, layout

## Hard constraints

- **Every node distilled from a turn names the turn; a node born from a file
  the member read names the file.** An operator answers "where did that come
  from" from the capture and the node, never from journals.
- **The member's knowledge pack carries no node ids and no store vocabulary;
  ids live in the turn capture.** A member that can name its memory's
  machinery narrates it to the person correcting it.
- **Distill never reads a turn capture's `## Prompt`; it reads the routed
  messages, the human messages and the output.** The prompt is standing
  context the member sees every turn, and distilling it re-extracts the
  persona and the store's own injected entries.
- **A capture whose distill times out at the full limit `DREAM_ATTEMPTS`
  times in a row is set aside, never retried forever.** One always-failing
  capture otherwise spends every sweep's budget.
- **A message body never writes a capture heading.** r4t escapes a `#` that
  opens a body line; k7e reads the capture's sections by those headings.
- **The correction pass reads only messages from senders the roster's
  `People:` line names; absent, it is off.** The envelope's `class` never
  decides it alone — a8s stamps none, so an unnamed seat's mail is `human` on
  the wire's silence.
- **A turn runs once. A turn that exits nonzero, times out or never finishes
  makes its batch a dead letter, and each sender gets one notice that the
  member may have partly acted.** The notice never asks for a resend, and no
  breaker or requeue runs the batch again; the sender decides.
- **A wake starts no turn that its remaining `max_wake_seconds` (read from the
  a8s definition) cannot cover at the rig's `timeout_seconds`.** a8s kills a
  wake at that limit and dead-letters its mail.
- **Only a rig that fits a fresh wake can end a wake.** A rig whose
  `timeout_seconds` exceeds `max_wake_seconds` less `WAKE_MARGIN_SECONDS`
  skips its member alone (`NEVER-FITS`), and `r4t status` reports it as a
  red verdict.

## Testing

- **Unit + fake sandbox (plumbing):** `r4t sandbox --fake` runs a bundled
  three-agent roster (Lead/Dev/Tester building a tiny battleship game)
  against deterministic scripted agents — no LLM calls — inside a
  throwaway `A8S_HOME`/`R4T_HOME`, then emits a self-contained report on
  **stdout** (progress on **stderr**). MECHANICAL CHECKS are computed
  (program built and runs, leader answered the originator, turns within
  budget, zero orphan processes, dead-letter counts). The pytest suite
  runs it end to end.
- **Live sandbox (acceptance / eval):** `r4t sandbox` (no `--fake`) runs
  the same scenario with a real harness. Pick any named preset:
  `r4t sandbox --preset opencode` (default), or local models via Ollama:
  `r4t sandbox --preset ollama-opencode --model qwen2.5-coder:7b`.
  Other presets (`claude`, `codex`, `cursor`, `agy`, …) work the same
  way — see `r4t rig presets`. `live-agent.py` prepends explicit
  per-role steps and stages protocol tells if the model skips them.
  Save the report: `r4t sandbox --preset agy > report.md`

```bash
apps/r4t/tests/run     # from the repo root; builds tests/.venv on first run
```

### Failure scenarios

`r4t sandbox --fake --break MEMBER[:SHAPE]` breaks one member on purpose and
grades the recovery path. Each shape is a way real harnesses fail, and each
lands somewhere different in dispatch:

| SHAPE | The member | r4t must |
|---|---|---|
| `exit` (default) | exits nonzero every turn | dead-letter the batch, tell the sender once that it may have partly acted |
| `hang` | sleeps past its rig timeout | kill the process group at the timeout, dead-letter the batch, tell the sender it ran out of time |
| `silent` | does the work, then answers on stdout without ever calling `tell` | stage the cleaned stdout as one reply to the sender, no dead letter, deliverable intact |
| `mute` | prints tool chrome, stages nothing, exits 0 | nudge nobody — the turn is spent and the message is gone; the mission-review heartbeat is what re-engages the org |

Every shape also checks the turn was charged to the member's budget: a member
that keeps failing pays for its attempts. Each has a pytest in
`tests/test_sandbox.py`, and the checks are written so a governance regression
reads as FAIL rather than a quieter run.

### What the sandbox fakes

Every line here is a place a green sandbox run says nothing about a real
roster:

- **Roles are scripted.** `fake-agent.py` picks its action from a regex over
  the prompt, so the pipeline completes however well or badly the prompt
  teaches. Nothing here tests whether a model would follow it.
- **Sends bypass `tell`.** Fake and live sandbox agents both write staged
  envelopes into `$TELL_OUTBOX_DIR` from Python. Staging release, routing and
  header stamping are the real thing; the member's own `tell` invocation —
  shell quoting, outbox discovery — is not.
- **No `a8s_tell` tool.** Sandbox rigs carry no preset, so the `mcp` knob is
  off in both modes and the per-turn MCP injection never runs.
- **Live mode still scripts two turns.** `live-agent.py` skips the LLM
  entirely for the Tester role and for the Lead's post-VERIFIED turn.
- **Live mode covers for the model.** After every turn it stages the tell the
  model should have sent, and seeds `battleship.py` when Dev leaves none. A
  green live report proves the pipeline ran, not that the harness followed
  protocol.
- **VERIFIED is a regex.** Both modes read `VERIFIED:` out of the incoming
  text; a model that phrases its verdict any other way reads as unverified.
- **Fake turns are instant.** Fake mode drops the cadence gate and every turn
  returns in milliseconds, so throttling and budget resting under real latency
  go untested.
- **Broken members break cleanly.** The `--break` shapes are a nonzero exit, a
  `sleep`, a `print` and chrome-only output. Real harnesses fail messier:
  partial output, tool loops, a CLI that blocks on stdin.
- **Nobody continues.** The sandbox roster sets no `Continue:`, so refounds,
  dump turns and the cold-start retry never run.

## Layout

`r4t.py` (CLI) · `dispatch.py` (enqueue, batch turns, staging release, flush,
mission-review) · `state.py` (all on-disk state under `$R4T_HOME`) · `rig.py`
(rig config, presets, model resolution, continuation grades) · `roster.py` ·
`org.py` (org dirs + settings) · `check.py` (verification sweep) · `judge.py`
(post-hoc MAST judge) · `verdict.py` (health verdicts + dead-letter rollup,
behind `r4t status`) · `notify.py` (the outward `tell` seam and the a8s names
one host can see) · `sandbox.py` + `sandbox/` (the end-to-end harness).
Observability rides on a8s: traffic in the a8s txlog/convo, r4t decision
lines in the node log via dispatch stdout, r4t-only state via `r4t status`.
