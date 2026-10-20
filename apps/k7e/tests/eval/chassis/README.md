# Consolidation chassis

A 30-day scripted-model lab for k7e's memory. No network, no real model: a fake
model answers the extract, triage, correction and compile prompts from a plan.

The plan is 240 turns (8 a day) carrying 10 durable rules, 4 updates, 2
corrections, 2 peer contradictions, 2 poison lines and 40 telemetry items.
Each run distills every day, runs `compile --due`, and probes recall on days 7,
14 and 30.

Arms: `base`, `frozen` (clock stuck on day 1), `nocompile`, `alldurable`,
`obedient` (a poison-writing model), `oracle` (ground truth written straight
into the store, which proves the probes), `narration` (the compile model
answers every compile with its turn transcript instead of a page; the summary
reports how many sources a page folded, which must be 0).

Every `compile --due` that compiles is also checked for invariance: for each
durable key and each simulated day T, the top hit of `--valid-at T` and of
`--as-of T` must be the same before and after the compile.

## Run

    apps/k7e/tests/eval/chassis/all.sh [runs-dir]

`runs-dir` defaults to `$K7E_CHASSIS_RUNS`, then a temp dir. A run refuses to
start when its directory would resolve inside the repo or outside the runs
dir, or when its name is not a plain name, before it creates or removes
anything. One arm: `K7E_CHASSIS_RUNS=<dir> python3 sim.py <name> [flags]`;
`summ.py <name>...` prints the metrics and `dump.py <name> [pattern]` lists a
run's entries.
