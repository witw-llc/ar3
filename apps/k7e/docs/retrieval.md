# Retrieval & ranking

How `k7e search` and `k7e recall` turn a query into the right entries.

## Pipeline

```
query
  │
  ├─ BM25 (SQLite FTS5)        ┐
  ├─ metadata (title/alias/tag)├─ Reciprocal Rank Fusion (RRF)
  └─ embeddings (ollama)       ┘            │
                                            ▼
                         score × confidence × recency-decay × use-boost
                                            │
                                            ▼
                              (optional) LLM reranker
                                            │
                                            ▼
                                        top-K results
```

### 1. Candidate retrieval (three signals)

- **BM25** — keyword relevance via FTS5. Always available (stdlib sqlite3).
- **Metadata** — exact-ish matches against title, aliases, and tags.
- **Embeddings** — semantic similarity via ollama vectors. Optional; linear
  scan, automatically skipped past ~10k nodes (k7e is single-user scale, not a
  vector-DB).

All three filter to `status='active'` unless `include_superseded` is set.

### 2. Fusion (RRF)

The three ranked lists are merged with Reciprocal Rank Fusion — robust to the
fact that BM25 scores and cosine similarities aren't on the same scale. Search
over-fetches (`limit × 3`) so later stages have a real pool to work with.

### 3. Score multipliers

Each fused score is multiplied by:

- **Confidence** — `0.7 + 0.3 × confidence` (static prior, 0.85..1.0).
- **Recency decay** — a Gaussian on age: flat for `decay_offset_days`, then
  `exp(-(age − offset)² / 2s²)` with `s` chosen so the multiplier hits 0.5 at
  `decay_scale_days` past the flat zone. Basis date is `last_used_at` if set,
  else `last_updated`. **This is relevance decay, not truth decay** — fading
  only affects ranking, never deletes anything. Disabled when `scale <= 0`.
- **Use-count boost** — `1 + log10(1 + use_count) × use_count_weight`
  (~1.2× at 10 uses, ~1.4× at 100). Facts you keep retrieving stay near the top.

Entries earn freshness when consumed: synthesized by `recall` or read by
`k7e get`. Appearing in a `search` listing does not count — the caller may
never open the entry. Because `last_used_at`/`use_count` are index-only, this
signal resets on `reindex`.

### 4. LLM reranker (optional)

A cross-encoder-style pass: the top ~15 candidates (id, title, snippet) and the
query go to the LLM, which returns a ranked ID list used to reorder the pool.
Attacks "sibling collisions" (several true facts on one topic) that fusion alone
can't separate. Degrades gracefully to the fused order when no LLM is available
or the response can't be parsed.

- `k7e search` — off by default; enable with `--rerank`.
- `k7e recall` — on by default (recall is already LLM-heavy).

## search vs recall

| | `k7e search` | `k7e recall` |
|---|---|---|
| Returns | ranked entries | LLM-synthesized answer over retrieved entries (RAG) |
| Reranker | opt-in (`--rerank`) | on by default |
| Needs LLM | no | yes (fails fast without it) |
| Use when | you want the source entries | you want a synthesized answer for a topic/conversation |

## Tuning

All tunable via `k7e config` or env (see [configuration.md](configuration.md)):

| Knob | Default | Effect |
|------|---------|--------|
| `decay_offset_days` | 30 | flat (no decay) window |
| `decay_scale_days` | 365 | half-life past the flat window; `<=0` disables decay |
| `use_count_weight` | 0.2 | strength of the use-count boost |
| `rerank` | off | LLM rerank by default in `search` |

## Measuring quality: the eval harness

Tuning ranking blind is guesswork, so retrieval quality is measured with a
checked-in **Recall@K** harness (`apps/k7e/tests/`):

- `tests/eval/corpus.json` — hand-curated dev-knowledge facts, including
  deliberate sibling-collision clusters.
- `tests/eval/questions.json` — paraphrased questions mapped to the expected
  entry.
- `tests/test_eval_recall.py`:
  - **Tier A** (deterministic, no ollama) — FTS + metadata + RRF + decay;
    asserts baseline R@1/R@3/R@5/R@10. Runs in the always-on CI job.
  - **Tier B** (`@llm`, needs ollama) — full hybrid + reranker; higher
    thresholds; prints an R@K summary. Runs in the `llm` CI job.

Recall@K = fraction of questions whose expected entry appears in the top K.
Raise the floors as ranking improves; never let them regress.
