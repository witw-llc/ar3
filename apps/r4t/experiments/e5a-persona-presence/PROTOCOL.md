# Experiment protocol — e5a-persona-presence

*Filled-in copy of the [template](../PROTOCOL.md). This is the first r4t
experiment that makes a design ruling (E0 only validated the machinery and set
the noise floor). It asks whether a single domain-matched named persona, placed
in front of an otherwise identical judge rubric, makes the judge more robust to
paraphrased questions.*

*Pre-codification research gate (LAB-SPEC section 7): **PASSED**. The literature
survey and its synthesis live beside this file as `research-result.md` and
`research-SYNTHESIS.md`. The field explicitly lacks and recommends this exact
A/B ("We are not aware of published evidence comparing a named persona to an
equivalent generic instruction holding all else equal"); the design constraints
below are the gate's non-negotiable updates.*

---

## 0. Identity

- **Name:** `e5a-persona-presence`
- **Owner:** Neil (blesses the ruling; the runner never rules)
- **Runner:** `r4t lab` driven by an Opus agent (single command plus this
  protocol; no live-org supervision — posthoc class)
- **Launch time (declared before start):** stamped per trial in the ledger row
  (`environment` + `stamp`); the run is minutes of local inference, not a
  scheduled window.

## 1. Hypothesis

- **The one question:** Does fronting an identical judge rubric with one
  domain-matched named persona (Grace Hopper) raise paraphrase consistency
  versus the same rubric anonymous?
- **The variable:** ONLY the persona line. Arm A is the anonymous rubric; arm B
  is the byte-identical rubric with a single Grace Hopper persona line prepended
  (`arms/A/persona.md` is empty, `arms/B/persona.md` holds the persona; the
  rubric, transcript, questions, answer format, rig, and pinned model are one
  shared set of fixtures).
- **Held constant:** the transcript, the 12 questions, the ground truth, the
  rubric body, the answer format, the `local` rig, the `qwen3.6` pin, and
  trials per arm.
- **What confirms it:** arm B's paraphrase_consistency exceeds arm A's — the
  persona makes the judge answer a question and its reworded twin the same way
  more often.
- **What falsifies it:** arm B's paraphrase_consistency does NOT exceed arm A's
  (the sign test shows no B-over-A direction, or the delta is at or below zero).
  Per the research gate's sharpened null: if the persona does not raise
  paraphrase consistency, the packed-token theory is dead for judging and the
  style-only / attention-alignment explanation is the default. This is a
  concrete, runner-visible outcome: `r4t lab report` scores it mechanically.
- **Primary measurement:** `paraphrase_consistency` — per trial, the fraction of
  the 6 semantic pairs the judge answered identically (original twin equals
  paraphrase twin) — compared A versus B via the sign test, bootstrap CI, and
  the pre-registered `delta_gte` predictions.
- **Secondary measurements:** `within_arm_consistency` (repeatability across an
  arm's trials), `anchor_accuracy` (verdicts on the 2 ground-truth anchor pairs
  only), per-arm chance-corrected paraphrase kappa against the 0.6 floor, and
  wall-clock / exclusion count from the ledger.

## 2. Setup runbook

No org, no seeding — the posthoc chassis owns everything. The `judge` role binds
to the symbolic rig `local` (the manifest default), supplied from the local
rigs.json exactly as ROSTER.md Rig lines are satisfied.

**Judge model family must differ from the fixture-author family (research gate,
self-preference bias):** the fixtures are excerpted from a Gemini-authored team
run (team d5n ran on agy Gemini), so the judge runs on LOCAL qwen3.6 — a
different family. This also places E5a on the digest-pinned frozen tier: the
ledger records the exact ollama model digest per trial, so the ruling is
reproducible against that frozen local model, not a moving cloud target.

```bash
ollama pull qwen3.6
r4t rig add local ollama --model qwen3.6   # bind the judge role's default rig
r4t lab run e5a-persona-presence           # 6 trials per arm, arms alternated (12 total)
r4t lab report e5a-persona-presence
```

To judge with a different local model without touching the package, bind the
role at run time: `r4t lab run e5a-persona-presence --rig judge=other-rig`. If
that rig resolves outside the qwen3.6 series the trial still runs but its row is
flagged `pin_mismatch` and the report keeps it in its own column (model
resolutions are never pooled).

- **Single-shot judging only.** Each trial is exactly one judge invocation over
  the frozen fixtures — never a multi-turn conversation. This is deliberate: the
  research gate found 71% of injected personas drift by turn 80, so a persona
  judge is only trustworthy single-shot. Nobody may add multi-turn judging to
  this experiment without re-opening the design.
- **Isolation check:** each trial is a fresh `ollama run`; trials share no
  process, queue, or state. Arms are alternated so time-drift cannot confound
  one arm.
- **Hawthorne check:** the fixtures are a frozen transcript; nothing in either
  arm's prompt names the experiment, the other arm, or the variable under test.
  The only difference a reader would see is the persona line.

## 3. Observation schedule

N/A — this is a single non-interactive command, not a supervised live run. The
per-trial ledger rows and the aggregated report are the complete record. Re-run
`r4t lab report e5a-persona-presence` at any time; it is read-only and
reproducible (the bootstrap RNG is seeded).

One qualitative dimension to eyeball in the raw outputs under `reports/` (an
observation, not a scored metric — research gate item 5): whether the exacting
Grace Hopper persona under-credits the team's honest confession and retraction
(sycophancy research finds persona shifts compliance 1.7–19.3%). The debatable
pairs on bad faith, retraction adequacy, and integrity are where that would
show up.

## 4. Intervention decision table

N/A — a posthoc experiment has no live agents to nudge, message, or escalate
about. A trial whose judge output cannot be parsed for all 12 questions is
recorded as `excluded` with reason `parse_error` and never silently dropped; the
only "intervention" is re-running trials to accumulate more data. If qwen3.6
breaks the answer format on the harder fixture, the fix is to tighten the format
instructions IDENTICALLY in both arms (the persona line stays the only
difference) and note it in the run record — never to touch one arm alone.

## 5. Stop conditions

- **Wall-clock ceiling (required):** `box_seconds` = 1800 per trial, enforced by
  the chassis; a trial that exceeds it is excluded with reason `timeout`. The
  longer, harder fixture makes each trial slower than E0's; the box absorbs it.
- **Fixed N:** `trials_per_arm` = 6, `stopping_rule` = null. No sequential early
  stop — the count is fixed before the first trial so peeking cannot inflate the
  result.
- **Falsifier reached:** if the report scores the direction prediction
  `falsified` (B does not exceed A), the packed-token theory is dead for judging
  on this model; record the numbers and stop. That is the finding, not a
  failure — and it is the human's ruling to make, not the runner's.

## 6. Metrics ledger

Machine-recorded, not hand-kept: every trial appends one JSONL row to
`~/.config/r4t/lab/e5a-persona-presence/trials.jsonl` (trial ULID, stamp, arm,
captured environment including the resolved qwen3.6 digest, per-trial metrics,
exit reason, wall-clock, report path, and the sha256 of the raw judge output).
Raw outputs are kept verbatim under `reports/`. `r4t lab report
e5a-persona-presence` aggregates per arm and per resolved model, runs the sign
test and bootstrap CI on paraphrase_consistency, computes the per-arm paraphrase
kappa against the 0.6 floor, scores the three pre-registered predictions
(operator confidences, not the judge's self-reported confidence), and prints the
verdict.
