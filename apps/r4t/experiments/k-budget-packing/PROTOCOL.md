# Experiment protocol -- k-budget-packing

*Filled-in copy of the [template](../PROTOCOL.md). Issues
[#52](https://github.com/witw-llc/ar3-private/issues/52) (Knowledge
mini-config) and [#12](https://github.com/witw-llc/ar3-private/issues/12).
The K memory campaign (private wiki, `Experiments-k-memory-campaign-2026-07-30`,
K6 section) decomposed 45 failed LongMemEval questions into 3 retrieval
misses, 4 budget starvations, 28 reader failures, 10 correct -- and found
that injecting at 2 KiB admits a **median of one entry** (median block
2231 B), which structurally zeroes multi-session (0/12) and
temporal-reasoning (0/12): question types that by construction need evidence
from more than one session. Retrieval found the evidence; the packer cut it.
This package asks whether a different packer, at the same budget, keeps it.*

*Like k0-knowledge-inject, k-next-embeddings, k4e-poisoning and
k-age-presentation, this is a bespoke-driver package: no r4t roster, no
member, no dispatch process, no `r4t lab run`.*

---

## 0. Identity

- **Name:** `k-budget-packing`
- **Owner:** Neil (rules on which strategy ships and at what default; the
  runner never rules)
- **Runner:** `k-budget-run.py` (mechanical) and `k-budget-agy-run.py`
  (LLM arm), driven by the resident seat (Ares) or any harness with
  Python 3, a local ollama serving `nomic-embed-text`, and `agy`
- **Launch time:** stamped into every result file by the driver (`run` field)

## 1. Hypothesis

- **The one question:** At a fixed inject budget, does the *packing strategy*
  -- not retrieval, not the budget number -- decide whether a question's
  evidence reaches the reader?
- **The variable:** the packer, and only the packer. Four strategies over the
  identical retrieval result:
  - `s1-greedy-whole` -- the shipped control
    (`knowledge.knowledge_section`): whole entries in rank order, stop at the
    first overflow, byte-slice only a lone oversized first entry.
  - `s2-per-entry-cap` -- cap every entry at `budget // 3`, keep walking the
    rank order with what the caps left over.
  - `s3-head-then-fill` -- guarantee the top entries a 256 B head each, then
    spend the remainder deepening them from the top down.
  - `s4-rank-proportional` -- split the budget by a `1/(rank+1)` weight over
    the top 8 hits, then sweep unspent slack back down the ranks.
- **Held constant:** the store (one k7e entry per user/assistant turn-pair,
  built exactly as `k-next-embeddings/k6e-run.py` builds it), the embedding
  model and `reindex(embeddings=True)` pass, the seed query (the bare
  question text), `SEARCH_LIMIT` (imported from `apps/r4t/knowledge.py`, 8),
  the snippet extraction (`knowledge._entry_snippet`, imported not copied),
  the provenance stamp format, and the question subset. **One store per
  question, one search per question, sixteen packs from that one result** --
  no strategy ever sees a different retrieval.
- **What confirms it:** at 2048 and/or 8192 bytes, at least one challenger
  strategy raises full evidence coverage over `s1-greedy-whole` on
  multi-session and temporal-reasoning, without lowering it on the
  single-session types.
- **What falsifies it:** no strategy beats `s1-greedy-whole` on full coverage
  at any budget below 32768 -- packing is not the lever, and the K6 budget-
  starvation count was a description of retrieval quality, not of the packer.
  A second falsifier for the *budget* question: if `s1-greedy-whole` at 2048
  already covers everything retrieval found, the T-shirt small tier is not
  the constraint.
- **Primary measurement:** **full evidence coverage rate** -- the fraction of
  scored questions whose *every* gold turn text survives into the packed
  prompt. Mechanical, no LLM: LongMemEval marks its answer-bearing turns
  (`has_answer: true`), and the driver records at store-build time which
  entry swallowed each one, so coverage is a normalized substring test
  against the packed text, not a judgment.
- **Secondary measurements:** partial coverage (>=1 gold turn but not all),
  the *retrieval ceiling* (all gold entries present in the top-8 hits at all
  -- the number no packer can beat), entries per prompt (mean/median), bytes
  used, mean gold-prefix fraction, per-question store build and embed
  wall-clock. Then the LLM arm: `contains_answer` and answer token recall
  from `agy`.

The falsifier is visible in the driver's own tables -- `full` per
strategy x budget, per stratum -- with no judge involved.

## 2. Setup runbook

```bash
# Prerequisites
ollama list | grep nomic-embed-text || ollama pull nomic-embed-text
agy --version   # LLM arm only; auth is out of scope here

# Dataset: LongMemEval_S cleaned (MIT, xiaowu0162). Not committed. The
# k-next-embeddings package downloads and caches it; reuse that cache.
ls ~/.cache/k-next-longmemeval/longmemeval_s_cleaned.json || \
  python3 apps/r4t/experiments/k-next-embeddings/k6e-run.py --limit 1

# Smoke -- two questions, all 16 strategy x budget cells (~20s).
python3 apps/r4t/experiments/k-budget-packing/k-budget-run.py --limit 2

# Full mechanical sweep -- 50 questions x 4 strategies x 4 budgets.
# One store + one search per question; the 16 packs are free. ~7 min.
python3 apps/r4t/experiments/k-budget-packing/k-budget-run.py

# LLM arm (n-small) -- the theory-predicted movers only, control vs the
# winning challengers, at the two budgets a real member actually gets. Which
# challenger wins is budget-dependent, so both leaders ride along.
python3 apps/r4t/experiments/k-budget-packing/k-budget-agy-run.py \
  --packs   ~/.config/r4t/lab/k-budget-packing/mechanical/<slug>-packs.json \
  --coverage ~/.config/r4t/lab/k-budget-packing/mechanical/<slug>.json \
  --budgets 2048,8192
```

- **Isolation check:** every question builds its own `K7E_HOME` under a fresh
  `TemporaryDirectory`; no store, index, or embedding table outlives its
  question. `OLLAMA_URL`/`EMBED_MODEL`/`K7E_HOME` are restored around every
  question so the driver cannot leak into a caller's environment.
- **Hawthorne check:** N/A -- no member, no live agent being observed. The
  mechanical arm makes no LLM call at all; the LLM arm's prompt names neither
  the experiment nor the strategy.

## 3. Budgets under test

2048 (`small`), 8192 (`medium`), 32768 (`large`) -- the #52 T-shirt sizes
verbatim from `roster.KNOWLEDGE_SIZES` -- plus **4096** as a mid point the
suite does not currently offer. The run therefore also reads on whether the
T-shirt values are set right: if `small` is where coverage collapses and
4096 recovers most of it, `small` is mis-set independently of any packer.

## 4. Fixtures

This package ships **no fixtures of its own** -- deliberately. It reuses
`../k-next-embeddings/fixtures/k6/subset.json`, the frozen 50-question
LongMemEval_S subset (stratified by question type, seed 42: 13
temporal-reasoning, 12 multi-session, 7 knowledge-update, 6
single-session-assistant, 6 single-session-user, 3 single-session-preference,
3 abstention). Sharing the subset is what makes this package's coverage
numbers directly comparable to K6's hit@k numbers -- a second sample would
have made the comparison an argument instead of a subtraction.

Abstention questions carry no genuine gold turn and are excluded from every
coverage rate (they are counted, never scored).

## 5. Intervention table

| Trigger | Action | Who |
| --- | --- | --- |
| ollama unreachable / `nomic-embed-text` missing | `ollama pull nomic-embed-text`; if still down, stop -- an FTS-only sweep answers a different question than the one on the tin | runner |
| Dataset cache missing | re-run the k6e downloader (Section 2); never hand-edit the subset | runner |
| A question's store build or search raises | let it raise -- a partial sweep with a visible traceback beats a silent hole in a coverage rate. Completed questions are already flushed to disk. | runner |
| An `agy` cell errors or times out | record the row with `error` set and continue; report the error count alongside the rates | runner |
| A strategy wins only on the gold-prefix fraction and not on full coverage | report both, rule neither -- escalate | runner |

## 6. Stopping rule

Fixed workload. Mechanical: 50 questions x 4 strategies x 4 budgets = 800
packs, zero LLM calls; no early stop. LLM arm: 25 questions x 3 strategies x
2 budgets = 150 `agy` calls; `--limit` caps it for budget-capped partial runs
only. Cutting the real sweep short is the owner's call, made explicitly.

## 7. Ruling authority

Neil. The runner reports the tables; whether `knowledge.knowledge_section`
adopts a challenger packer, and whether `roster.KNOWLEDGE_SIZES` moves, is
the owner's call on the numbers.

## 8. Result ledger

`~/.config/r4t/lab/k-budget-packing/` -- `mechanical/<stamp>.json`
(per-question metadata + 800 rows + summary), `mechanical/<stamp>-packs.json`
(the rendered `## Knowledge` sections, the LLM arm's input),
`mechanical/<stamp>.md` (the tables), and `agy/<stamp>.{json,md}`. Results are
not committed -- the package is the reproducer, the ledger is the run.
