# Experiment protocol — e0-noise-floor

*Filled-in copy of the [template](../PROTOCOL.md). This experiment makes NO
design ruling — it validates the lab machinery and measures the judging noise
floor that every future rung's sign test must beat. The pre-codification
research gate (LAB-SPEC section 7) is therefore N/A: there is no doctrine
question to survey the literature on before committing this package.*

---

## 0. Identity

- **Name:** `e0-noise-floor`
- **Owner:** Neil
- **Runner:** `r4t lab` (single command; no live harness supervision)
- **Launch time (declared before start):** stamped per trial in the ledger row
  (`environment` + `stamp`); the run is a few minutes, not a scheduled window.

## 1. Hypothesis

- **The one question:** With the judging task held identical, how much do a
  local model's yes/no verdicts wobble across repeats and across paraphrased
  wording?
- **The variable:** ONLY the phrasing of the questions. Arm A asks the original
  wording; arm B asks semantic paraphrases of the same four questions.
- **Held constant:** the transcript, the ground truth, the judge prompt
  instructions, the rig, the pinned model, and the answer format.
- **What confirms it:** high within-arm consistency and high cross-arm
  agreement — the judge is a low-noise instrument on this task.
- **What falsifies it:** `cross_arm_agreement` below 75 percent (paraphrase
  wording flips verdicts) or `within_arm_consistency` below 75 percent in
  either arm (the judge is unstable even repeating itself). These are the
  pre-registered predictions, scored mechanically by `r4t lab report`.
- **Primary measurement:** `cross_arm_agreement` and `within_arm_consistency`
  from the report.
- **Secondary measurements:** `accuracy` versus ground truth, wall-clock and
  exclusion count from the ledger.

## 2. Setup runbook

No org, no seeding — the posthoc chassis owns everything. The `judge` role
binds to a symbolic rig named `local` (the manifest default); the operator
supplies that rig from the local rigs.json, exactly as ROSTER.md Rig lines are
satisfied. Create it once against the pinned model, then run:

```bash
ollama pull qwen3.6
r4t rig add local ollama --model qwen3.6   # bind the `judge` role's default rig
r4t lab run e0-noise-floor -n 4            # 4 trials per arm, arms alternated (8 total)
r4t lab report e0-noise-floor
```

To judge with a different local model without touching the package, bind the
role at run time: `r4t lab run e0-noise-floor --rig judge=some-other-rig`. If
that rig resolves outside the `qwen3.6` series the trial still runs, but its
row is flagged `pin_mismatch` and the report keeps it in its own column
(model resolutions are never pooled).

- **Isolation check:** each trial is a fresh `ollama run` over the frozen
  fixtures; trials share no process, queue, or state.
- **Hawthorne check:** the fixtures are a fictional team transcript; nothing in
  the prompt names the experiment, the other arm, or the variable under test.

## 3. Observation schedule

N/A — this is a single non-interactive command, not a supervised live run.
There is nothing to sweep: the ledger row per trial and the aggregated report
are the complete record. Re-run `r4t lab report e0-noise-floor` at any time; it
is read-only and reproducible (the bootstrap RNG is seeded).

## 4. Intervention decision table

N/A — a posthoc experiment has no live agents to nudge, message, or escalate
about. A trial whose judge output cannot be parsed for every question is
recorded as `excluded` with reason `parse_error` and never silently dropped;
the only "intervention" is the operator re-running trials to accumulate more
data.

## 5. Stop conditions

- **Wall-clock ceiling (required):** `box_seconds` = 600 per trial, enforced by
  the chassis; a trial that exceeds it is excluded with reason `timeout`.
- **Fixed N:** `trials_per_arm` = 4, `stopping_rule` = null. No sequential
  early stop — the count is fixed before the first trial so peeking cannot
  inflate the result.
- **Falsifier reached:** if the report scores either prediction `falsified`,
  the noise floor is higher than hoped; record the number and stop. That is the
  finding, not a failure.

## 6. Metrics ledger

Machine-recorded, not hand-kept: every trial appends one JSONL row to
`~/.config/r4t/lab/e0-noise-floor/trials.jsonl` (trial ULID, stamp, arm,
captured environment including the resolved model digest, metrics, exit reason,
wall-clock, report path, and the sha256 of the raw judge output). Raw outputs
are kept verbatim under `reports/`. `r4t lab report e0-noise-floor` aggregates
per arm and per resolved model, scores the predictions, and prints the verdict.
