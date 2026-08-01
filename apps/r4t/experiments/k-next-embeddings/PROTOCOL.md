# Experiment protocol — k-next-embeddings

*Filled-in copy of the [template](../PROTOCOL.md). Research gate: issue
[#61](https://github.com/witw-llc/ar3-private/issues/61), owner blessing
2026-07-31 — the embeddings track is the next K experiment and a k7e need.
K3/K6 already established the lexical-gap wall (best-recipe retrieval misses
5-6/10 on hard conditions); this package reruns both under k7e's existing
semantic track (RRF fusion + ollama `nomic-embed-text`) to measure whether it
closes the gap. Neither `r4t lab run` (org/posthoc classes) nor an r4t
roster fits this shape — retrieval scoring against a k7e store, no members,
no judge — so this package follows k0-knowledge-inject's precedent: bespoke
drivers that own their own results.*

---

## 0. Identity

- **Name:** `k-next-embeddings`
- **Owner:** Neil (blesses budget/default rulings; the runner never rules)
- **Runner:** `k3e-run.py` / `k6e-run.py`, driven by the resident seat (Ares)
  or any harness with a Python 3 + local ollama
- **Launch time:** stamped into every result file by the driver (`run` field)

## 1. Hypothesis

- **The one question:** Does k7e's semantic track (RRF-fused BM25 + ollama
  embeddings) close the lexical gap that BM25-only retrieval hits when a
  query shares no vocabulary with its gold entry?
- **The variable:** ONLY the semantic track. Arm OFF = FTS5/BM25 only
  (embeddings table empty, `OLLAMA_URL` pointed at a dead port so the
  semantic query-time call fails and contributes nothing). Arm ON = the same
  store, reindexed with embeddings populated, real RRF fusion of BM25 +
  metadata + semantic tracks. Store contents and query text are identical
  across arms.
- **Held constant:** store contents, query wording (bare user message only —
  K3/K6 proved additions never help), search limit (10), RRF-k (60, k7e
  default), decay/use-count config (k7e defaults).
- **What confirms it:** hit@5 on the K3 `hard` and `hard-neutral` conditions
  is materially higher with embeddings ON than OFF, and the K6 subset's
  per-type hit@5 does not regress on any of the six question types.
- **What falsifies it:** hard/hard-neutral hit@5 with embeddings ON is not
  materially better than OFF (the semantic track doesn't earn its keep), OR
  `easy`-condition (K3) / any question-type (K6) hit@5 regresses when
  embeddings turn on (RRF fusion hurts cases BM25 already had).
- **Primary measurement:** hit@5 on the K3 `hard` and `hard-neutral`
  conditions, both arms.
- **Secondary measurements:** K3 `easy`-condition hit@5 (regression check),
  K6 per-question-type hit@1/3/5 both arms, per-query/per-store embed
  latency, embedding reindex wall-clock (K3: one 31-entry store; K6: 50
  stores of ~250 entries each).

## 2. Setup runbook

```bash
# Prerequisite: ollama running locally with the embed model pulled.
ollama list | grep nomic-embed-text || ollama pull nomic-embed-text

# Phase 1 — K3 rerun. Builds one 31-entry store, ~seconds total.
python3 apps/r4t/experiments/k-next-embeddings/k3e-run.py

# Phase 2 — K6 subset. Downloads LongMemEval_S (cleaned) to
# ~/.cache/k-next-longmemeval/ on first run (not committed to the repo, MIT
# license, ~270MB, no auth required — direct HF resolve URL). Builds 50
# fresh stores of ~250 entries each; full sweep embeds ~12.5k turn-pairs.
python3 apps/r4t/experiments/k-next-embeddings/k6e-run.py

# Smoke / cheap reruns
python3 apps/r4t/experiments/k-next-embeddings/k6e-run.py --limit 3
```

Both drivers write a timestamped `<run>.json` (full rows + summary) and
`<run>.md` (summary table) to `~/.config/r4t/lab/k-next-embeddings/{k3,k6}/`
by default (override with `--out`), mirroring k0-knowledge-inject's
out-of-repo ledger convention — results are not committed here; they land
with the PR once the owner runs the full sweep.

- **Isolation check:** each store is a fresh temp `K7E_HOME`; K3 builds one
  store reused for both arms (index rebuilt via `reindex()` between arms,
  file contents unchanged), K6 builds one store per question. No shared
  process or state between arms or between questions.
- **Hawthorne check:** N/A — this is a mechanical retrieval measurement, no
  member or LLM being observed for behavior.

## 3. Fixtures

- **K3** (`fixtures/k3/store.json`, `fixtures/k3/queries.json`): 31 ops
  notes (10 gold + 21 distractors) styled as an agent's distilled dream
  notes — incidents, configs, decisions, roles. 30 queries (10 gold entries
  x easy/hard/hard-neutral). `easy` queries share the gold note's own
  vocabulary (verified: 4-11 shared content words per pair). `hard` queries
  share zero content words with their own gold note. `hard-neutral` queries
  additionally share zero content words with *any* store entry (the
  control — no incidental corpus-wide vocabulary to latch onto). Overlap
  verified mechanically with a stopword-filtered word-set checker during
  fixture construction; not re-verified at run time.
- **K6** (`fixtures/k6/subset.json`): 50 frozen `question_id`s from
  LongMemEval_S (cleaned), stratified by question type with a fixed seed
  (42) and proportional allocation (knowledge-update 7, multi-session 12,
  single-session-assistant 6, single-session-preference 3,
  single-session-user 6, temporal-reasoning 13, abstention 3 — abstention
  pooled across types by `_abs` question-id suffix, sums to 50 of 500). The
  stratification algorithm is reproduced in `k6e-run.py:build_subset()` for
  audit; the committed `subset.json` is the frozen source of truth the
  driver actually reads.
- **Abstention handling (K6):** an abstention question's `answer_session_ids`
  points to a plausible-looking distractor session with no `has_answer: true`
  turn anywhere in it — there is no genuine evidence to retrieve. hit@k is
  therefore not computed for abstention questions; they are reported as a
  separate row (count only) rather than folded into the six real
  question-type rows.

## 4. Intervention table

| Trigger | Action | Who |
|---|---|---|
| ollama unreachable / embed model missing | `ollama pull nomic-embed-text`; if still down, stop — no substitute embed model without blessing | runner |
| K6 dataset download fails (network/HF outage) | retry once; if still failing, run with `--dataset <manual path>` after fetching by hand (URL is in the driver's `DATASET_URL` constant and this file's setup runbook) | runner |
| A K6 store build errors on a specific question (malformed session data) | skip that question, log its id, continue — do not abort the sweep for one bad row | runner |
| Arm OFF shows any hit (embeddings contributing when they should be dark) | stop, investigate — the `OLLAMA_URL` dead-port isolation has a leak | runner |

## 5. Stopping rule

Fixed workload: K3 is one pass over 30 queries (2 arms), done in seconds. K6
is one pass over 50 questions (2 arms), each building its own store — no
early stop; `--limit N` exists for smoke runs, not for cutting the real
sweep short. Report once both drivers complete.

## 6. Ruling authority

Neil. The runner records and reports; whether embeddings become a k7e
default (and any budget/latency tradeoff at wake time — the inject path
must not stall turns) is the owner's call on the numbers.

## 7. Research-gate checklist (done)

- [x] Prior campaign (K3, K6) established the lexical-gap wall this package
      reruns against
- [x] Issue #61 scope + owner blessing (2026-07-30/31) recorded
- [x] Protocol placeholders resolved before launch
- [x] Fixtures built and lexical-gap/control constraints verified mechanically
- [x] Both drivers smoke-tested locally (K3 full run; K6 with `--limit 3`)
