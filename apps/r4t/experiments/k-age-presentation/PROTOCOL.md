# Experiment protocol -- k-age-presentation

*Filled-in copy of the [template](../PROTOCOL.md). Issue
[#62](https://github.com/witw-llc/ar3-private/issues/62) part 3 -- the age-
presentation arm of the `## Knowledge` chrome campaign. The research gate is
done: the commissioned deep-research report (private wiki,
`Plans-Age-Presentation-Research`) found no controlled study of qualitative
age labels like "very old" -- that gap is exactly this package's job. Its
top recommendation: expose absolute time + computed age + a calibrated
status/action line, with the strongest expected effect from the explicit
status/action, not the qualitative label alone. Neither `r4t lab run` nor an
r4t roster fits this shape -- one hand-built prompt per trial, two bare LLM
readers, no member, no dispatch process -- so this package follows
k0-knowledge-inject's, k-next-embeddings', and k4e-poisoning's precedent:
bespoke drivers that own their own results.*

---

## 0. Identity

- **Name:** `k-age-presentation`
- **Owner:** Neil (blesses budget/default rulings; the runner never rules)
- **Runner:** `k-age-run.py`, driven by the resident seat (Ares) or any
  harness with Python 3, local ollama, and `agy`
- **Launch time:** stamped into every result file by the driver (`run` field)

## 1. Hypothesis

- **The one question:** Which age presentation on a `## Knowledge` provenance
  stamp actually keys a reader into staleness -- none, absolute date,
  bare relative age, a qualitative T-shirt label, or an explicit status/
  action line?
- **The variable:** the age presentation on every entry's stamp -- `none`
  (`(id)`), `absolute` (`(id, 2026-06-25)`, today's shipped inject format),
  `relative` (`(id, 36d old)`), `qualitative` (`(id, 36d old — very old)`,
  T-shirt thresholds from `fixtures/presentation.json`), `status` (stamp
  stays `(id, 36d old)`, entries older than 30d gain a
  `Status: possibly superseded...` line). Applied uniformly to every entry
  in the store, both tasks, same five conditions.
- **Held constant:** store contents and order per task (10 entries, fixed
  positions), the `CURRENT DATE: 2026-07-31` line (present in every prompt,
  every condition -- the report says absolute dates are uninterpretable
  without it), the mission line, the `## Knowledge` header and framing line
  (imported from `apps/r4t/knowledge.py`, never copied), the `## Messages`
  shape, the task question, per-task detector, reader model/version, trial
  count per cell.
- **What confirms it:** on the 4B floor (qwen3:4b), `qualitative` and/or
  `status` beat `none` and `absolute` on T1's correct-rate and/or T2's
  hedge-rate, replicated in direction (not necessarily magnitude) on agy --
  with `status` showing the larger effect, per the research report's Tier-B
  lean toward an action-bearing line over a bare adjective.
- **What falsifies it:** no condition beats `none`/`absolute` on either
  task's primary metric, on either reader -- age presentation does not
  matter at this scale, and the research report's "no located study"
  caveat holds for our own battery too. State this per-reader: a result
  that only replicates on agy and not qwen is a capability-gated effect,
  not a falsified hypothesis.
- **Primary measurement:** T1 `correct` rate + T2 `hedged` rate per
  condition, on both readers.
- **Secondary measurements:** T1 `stale_following` rate (does a richer
  stamp actively suppress the old fact, not just fail to state it); T2
  `asserted_unqualified` rate (the failure mode's raw count); per-cell
  wall-clock and error counts; prompt byte size per condition (does `status`
  cost enough tokens to matter on the 4B floor).

If you cannot write the falsifier as something the runner can *see*, the
experiment is not ready to launch. It can: `correct` / `stale_following` /
`hedged` / `asserted_unqualified` are mechanical, case-insensitive phrase
matches in the driver, no judge involved.

## 2. Setup runbook

```bash
# Prerequisites
ollama list | grep qwen3:4b || ollama pull qwen3:4b
agy --version   # confirm the CLI is on PATH; auth is out of scope here

# Chassis check -- no LLM calls, validates prompt assembly and I/O plumbing.
python3 apps/r4t/experiments/k-age-presentation/k-age-run.py --trials 0

# Smoke -- 1 trial x both tasks x all five conditions, qwen only.
python3 apps/r4t/experiments/k-age-presentation/k-age-run.py --trials 1 --reader qwen

# Smoke -- prove the agy wrapper end to end (costs one real agy call).
python3 apps/r4t/experiments/k-age-presentation/k-age-run.py --trials 1 --reader agy --limit 1

# Full sweep (the orchestrator's job, not this package's) -- 2 tasks x
# 5 conditions x 2 readers x 8 trials = 160 calls. Sharding by reader keeps
# any one invocation's wall-clock bounded (agy calls can run up to 240s
# each); results accumulate under --out regardless of how the sweep is
# sharded.
python3 apps/r4t/experiments/k-age-presentation/k-age-run.py --reader qwen
python3 apps/r4t/experiments/k-age-presentation/k-age-run.py --reader agy

# Filters exist for partial/targeted reruns, not just smoke:
python3 apps/r4t/experiments/k-age-presentation/k-age-run.py --task t1 --condition status --reader qwen
```

- **Isolation check:** every trial builds its own run-tagged prompt (no
  shared state between calls). No two readers or trials share a process.
- **Hawthorne check:** N/A -- this is a mechanical staleness-keying
  measurement against a fixed prompt, no member or live agent being
  observed for behavior. Nothing in the prompt names the experiment or the
  condition under test.

## 3. Fixtures

- **`fixtures/presentation.json`** -- the ONE variable, as data: the fixed
  `current_date` anchor (`2026-07-31`, never `datetime.now()` -- ages
  recompute identically on every future run), the qualitative label
  thresholds (`<7d` omitted, `7-30d` "old", `>30d` "very old"), and the
  status line text + its 30d threshold. Changing this file changes what all
  five conditions render; the driver has no hardcoded label strings.
- **`fixtures/t1_store.json`** -- update-conflict task. 10 entries: `KG-00003`
  (old_target, 36d, 2026-06-25) asserts deploys go out weekly on Fridays
  from main; `KG-00006` (new_target, 2d, 2026-07-29) asserts the current
  truth -- nightly at 2am from the release branch. 8 unrelated filler notes
  (standup cadence, invoicing, feature flags, postmortems, build cache,
  secrets rotation, docs style, staging refresh) hold the store at 10
  entries and never mention deploys, so the conflict is isolated to the
  target pair. Filler ages span all three qualitative buckets (fresh/old/
  very old) so the qualitative and status conditions aren't trivially
  identifiable by "only the target entries have labels."
- **`fixtures/t2_store.json`** -- stale-alone task. 10 entries: `KG-00003`
  (target, 90d, 2026-05-02) is the only entry about on-call ownership --
  "The on-call rotation owner is Priya." -- with no fresher entry to
  contradict or update it. 9 unrelated filler notes (deploy schedule,
  invoicing, feature flags, secrets rotation, standups, build cache, docs
  style, postmortems, staging refresh) hold the store at 10 entries and
  never mention on-call ownership.
- Both stores share `mission_line` and `task_question`, and entry order is
  fixed per store -- only the stamp presentation changes across conditions,
  never the store contents or position.

## 4. Intervention table

| Trigger | Action | Who |
| --- | --- | --- |
| ollama unreachable / qwen3:4b missing | `ollama pull qwen3:4b`; if still down, stop -- no substitute model without blessing | runner |
| `agy` call errors or times out (240s wrapper + 260s subprocess ceiling) | record the row with `error` set; continue -- one bad cell does not abort the sweep | runner |
| A qwen cell errors (HTTP/timeout) | same as above -- record and continue | runner |
| Any row's raw response looks corrupted/truncated in a way that would flip a scoring call | flag it in the report, do not hand-edit the detector or the row -- escalate to the owner | runner |

## 5. Stopping rule

Fixed workload: 2 tasks x 5 conditions x 2 readers x `--trials` (default 8) =
160 calls at the default trial count. No early stop. `--limit` exists for
smoke/budget-capped partial runs, not for cutting the real sweep short --
that is the owner's call, made explicitly via `--trials`.

## 6. Ruling authority

Neil. The runner records and reports; whether any richer presentation
replaces the shipped `absolute` stamp in `apps/r4t/knowledge.py`'s inject
(and whether the same verdict extends to a8s's `$AGE`/`$TIMESTAMP` wake
templates, per the owner's note on issue #62) is the owner's call on the
numbers, made only after the tests run.

## 7. Research-gate checklist (done)

- [x] Commissioned deep-research report banked on the private wiki
      (`Plans-Age-Presentation-Research`) before any arm ran
- [x] Report's key finding recorded: no controlled study of qualitative age
      labels exists; the strongest evidence favors an action-bearing status
      line over a bare adjective -- this package tests both, not just the
      issue's original T-shirt-label sketch
- [x] Protocol placeholders resolved before launch
- [x] Fixtures built: two task stores (10 entries each), presentation
      thresholds as data
- [x] Driver smoke-tested locally: `k-age-run.py --trials 1` (qwen, all 10
      task x condition cells; agy, 1 cell via `--limit 1`) -- numbers in the
      handoff report, not committed here
