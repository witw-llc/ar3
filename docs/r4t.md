# r4t — the roster

An unsupervised agent roster once burned 40% of a monthly AI plan thanking
each other for thanking each other. The quieter waste is the opposite one:
a subscription costs the same idle or busy, so every unspent prompt is money
already paid and thrown away. r4t exists to end both — the plan you pay for
stays earning, and no roster can ever blow it. The spend underneath both is
attention: every sharp edge a model mishandles pulls you out of the vision
seat and into the trenches, so the rule here is that the harness holds the
edges — defaults do the right thing, prompts remind, skills instruct — and
neither you nor the model is trusted to be careful.

AI CLI agents — Claude Code, Codex, OpenCode, Copilot, Antigravity, local
Ollama models — already message each other over [a8s](a8s.md).
But a8s is deliberately dumb: it delivers messages and files, nothing more.
No budgets, no retries, no queue. r4t is the layer any AI CLI connects to
a8s **through**: name your members in a `ROSTER.md`, map each to a real CLI
in an out-of-repo rig config, and every turn is dispatched, budgeted,
throttled, queued, and audited — no agent polices itself, and nothing ever
waits on a human. Even a roster of ONE pays off: a single agent behind r4t
gets spend budgets, one-command rig swaps, quota-aware retries, and a
durable queue that never drops a message.

## Quick start

Prove the pipeline first — no LLM, no API keys, all state in a throwaway dir:

```bash
r4t sandbox --fake
```

Three scripted agents build and test a tiny game; a report lands on stdout
(`Program runs and exits 0 | PASS`, dead letters 0, ...).

Now a real roster on your repo:

```bash
cd ~/your-repo
r4t init          # writes ROSTER.md (owner + Lead + Dev) and rigs.json
r4t roster check  # -> ".../ROSTER.md: OK (3 member(s), leader Lead)"
r4t rig ls        # each rig's model, limits, and roster resolution
```

The roster names members and symbolic rigs; `~/.config/r4t/rigs.json` maps
rigs to CLIs (default: OpenCode). Swap any rig in one command —
`r4t rig swap leader claude` — see `r4t rig presets` for the full list.

Register the roster on a8s (`r4t init` prints these with your paths):

```bash
a8s add your-repo-node ~/your-repo r4t
a8s namespace your-repo your-repo-node
a8s start your-repo-node
```

Give yourself an address and say hello:

```bash
a8s add me ~/a8s-me && a8s start me
export TELL_OUTBOX_DIR=~/a8s-me/.outbox
tell your-repo "Introduce yourselves."
```

Watch it work (from inside the repo):

```bash
r4t status   # health verdicts, member budgets, queues, open threads
r4t logs -f  # every governance decision and turn as it happens
```

The roster's reply arrives in `a8s convo me`. Full walkthrough, including
what fails closed when the roster and rig config disagree:
[r4t-tutorial.md](r4t-tutorial.md).

## How it works

External mail always enters at the roster leader; inside the walls, members
message each other by first name with the ordinary `tell`, delegate, and end
their turn — nobody blocks waiting. Every turn costs budget; a member out of
budget rests while its queue holds, and refill is the retry, so the machine's
one shared subscription never idles while any project has work. A member that
answers in prose instead of sending gets its output delivered as the reply
anyway — weak local models do this routinely, and strong models have done it
in production too (`- **Fallback:** off` in the roster mutes this per member).
Full flow: [r4t-message-flow.md](r4t-message-flow.md).

## Learn more

- [The Ark Raising](../guide/README.md) — the suite build-along; [chapter 2](../guide/02-the-founding.md) founds a governed roster of one
- [Tutorial](r4t-tutorial.md) — first roster, step by step, fail-closed rules
- [Rigs](r4t-rigs.md) — presets, `--model`, settings, the governance knob table
- [Message flow](r4t-message-flow.md) — threads, queues, the stdout fallback
- [Operations](r4t-operations.md) — `status`, `logs`, `chat`, the human seat
- [Org design](r4t-org.md) — cells and leads, `MISSION.md`, portable orgs
- [Knowledge](r4t-knowledge.md) — a member's private k7e memory (experimental)
- [Verification](r4t-verification.md) — `r4t check`, checklists, doorbell gate, the post-hoc judge, `r4t task trace`
- [Governance](r4t-governance.md) — why each layer exists, with prior art
- [Security model](r4t-security.md) — what a repo edit can never change
- [Isolation](r4t-isolation.md) — run an org behind a Unix user or a container
- [Development](r4t-development.md) — sandbox testing, module layout
- Harness notes: [agy](r4t-harness-agy.md) ·
  [ollama launch](r4t-harness-ollama-launch.md)
