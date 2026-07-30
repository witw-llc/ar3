# Experiment protocol — k0-knowledge-inject

*Research gate (LAB-SPEC section 7): PASSED 2026-07-30 — survey, synthesis,
and hook map live on the wiki (Plans: K Memory Layers Gate / Deep Research).
This is the first runnable slice; `lab run` refuses org-class experiments, so
the bundled `k0-run.py` is the runner and owns its ledger.*

---

## 0. Identity

- **Name:** `k0-knowledge-inject`
- **Owner:** Neil (blesses budget/default rulings; the runner never rules)
- **Runner:** `k0-run.py` driven by the resident seat (Ares) or any harness
- **Launch time:** stamped into every ledger row by the driver (`run` field)

## 1. Hypothesis

- **The one question:** Does the budgeted `## Knowledge` inject deliver a
  fact that exists only in the member's k7e store — nowhere in prompt,
  history, or message — through a small model, without any other path
  leaking it?
- **The variable:** the `- **Knowledge:**` roster line, absent (arm A) vs
  `on` = 2 KiB (arm B). One line, nothing else.
- **Held constant:** roster shape, member, rig invoke, model, probe wording,
  store contents (codeword entry + 5 distractors, seeded identically per
  trial), budgets, throttle, seed.
- **Distill policy (frozen per the draft's open decision):** OFF for both
  arms. Stores are operator-seeded; dreaming is K2's variable, not K0's.
- **What confirms it:** arm B states the trial's codeword in ≥ 2/3 of
  trials while arm A stays ≤ 1/6.
- **What falsifies it:** arm B at or near arm A's floor — the inject block
  does not deliver usable facts at this budget/model; default stays off.
- **Primary measurement:** codeword present in the raw turn output
  (mechanical, case-insensitive; no judge).
- **Secondary measurements:** `r4t: PROMPT` composition line per trial
  (#39 — knowledge section bytes), wall-clock per turn, arm A leak check
  (codeword grepped from the assembled prompt: must be absent).

## 2. Setup runbook

```bash
# Chassis check first — no LLM, fake member greps its own prompt.
# Expect: arm A 0/N, arm B N/N. Anything else is a chassis bug, stop.
python3 apps/r4t/experiments/k0-knowledge-inject/k0-run.py --fake --trials 3

# Live run — requires ollama with the pinned model present.
ollama list | grep qwen3:1.7b
python3 apps/r4t/experiments/k0-knowledge-inject/k0-run.py --trials 6

# Variations the owner may bless: --model qwen3:0.6b (floor), --budget 4k
```

- **Isolation check:** every trial builds its own temp `R4T_HOME` and repo;
  stores are seeded fresh per trial with a trial-unique codeword; arms never
  share state. The ledger is the only thing that persists.
- **Hawthorne check:** the roster and probe never mention the experiment or
  the Knowledge field; the member is asked a plain question.

## 3. Observation schedule

The driver is synchronous — one probe turn per trial, per-trial line to
stderr as it lands (`A0 miss …` / `B0 HIT …`). No mid-run intervention;
read the ledger after: `~/.config/r4t/lab/k0-knowledge-inject/ledger.jsonl`.

## 4. Intervention table

| Trigger | Action | Who |
|---|---|---|
| `--fake` chassis check not A=0/B=all | stop, fix chassis, rerun | runner |
| ollama model missing/unresponsive | `ollama pull` the pin; if still down, stop — no substitute model without blessing | runner |
| arm A states a codeword (leak warning) | stop the run, investigate the leak path before any more trials | runner |
| turn timeout (300 s) on repeat | record the trial as a miss, note it, continue | runner |

## 5. Stopping rule

Fixed trials: 6 per arm per run (`--trials`), then report. No early stop —
the run is minutes, not hours.

## 6. Ruling authority

Neil. The runner records and reports; budget/default changes (K1: budget
sweep; K2: distill-on) are the owner's call on the numbers.

## 7. Research-gate checklist (done)

- [x] Field survey + synthesis + deep research banked (wiki, Plans pages)
- [x] Hook map matched what got built (#39 stats, Knowledge flag PR)
- [x] Protocol placeholders resolved before launch
