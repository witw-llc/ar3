# CLI reference

`k7e <command> [args]`. Run `k7e` with no args for the command list.

## Read

### `search <query>`
Hybrid search (BM25 + metadata + semantic), fused and ranked.

```
--limit N              max results (default 5)
--json                 JSON output
--ids                  IDs only, one per line
--rerank               LLM rerank the candidate pool
--include-superseded   include retired entries
```

When the semantic track runs, search prints `embed <N>ms` to **stderr** — the
query-embedding cost alone, so a caller on a latency budget can price it. The
line gains `(semantic track unavailable)` when ollama did not answer in time and
FTS5 carried the search by itself. stdout is untouched either way.

### `get <id> [<id> ...] [--no-track] [--json]`
Print full entries. Counts as a "use" (bumps ranking signals) unless
`--no-track` is given — for a caller that reads an entry only to size it
before deciding whether to use it (r4t's knowledge packer), and wants
`touch` to be the thing that actually counts as a recall.

One id prints the entry alone. Several print them in the order asked,
separated by a `--- k7e:<id> ---` line before each entry after the first;
`--json` emits `[{"id", "text"}]` instead and is the form to parse. Both
flags apply to the whole batch.

A batch is one interpreter startup rather than one per id, which is most of
what a small local read costs: eight entries take ~60ms batched against
~375ms fetched singly. An id that does not exist is reported on stderr and
skipped — a caller sizing a pool would rather pack the rest than pack
nothing. The exit code is 1 only when nothing at all was found.

### `touch <id> [<id> ...]`
Bump the usage ranking signal (`use_count`, `last_used_at`) for one or more
entries without reading them — the other half of `get --no-track`.

### `recall <text> [--limit N]`
RAG: retrieve relevant entries for a topic or pasted conversation and synthesize
an answer (LLM, reranker on by default). Accepts text as an arg or via stdin.

### `list [--tag X] [--status active] [--ids]`
List entries with optional filters.

### `stats [--json]`
Store statistics (entry counts, tags, confidence).

## Write

### `store <title>`
Create a new entry. Content from `--content` or stdin.

```
--tags a,b,c       comma-separated tags
--aliases x,y      comma-separated aliases
--content "..."    inline content (else read stdin)
```

### `append <id> --section <name>`
Append content (arg or stdin) to a named section of an existing entry.

### `supersede <old_id> <new_id>`
Mark `old_id` as superseded by `new_id`. Preserves the audit trail; hides the
old entry from default search.

### `asset <file>`
Store a binary content-addressed (SHA256, deduped). Prints the stored path.

### `distill <file|dir> [--dry-run]`
Extract knowledge from raw files. See [k7e-distillation.md](k7e-distillation.md).

### `consolidate [--dry-run]`
Find and merge duplicate nodes by title similarity.

### `compile <tag> [--dry-run]`
Synthesize active entries for a tag into a `compiled` reference page (LLM).

## Maintenance

### `reindex [--embeddings]`
Rebuild `.index.db` from the markdown files. `--embeddings` recomputes vectors.
Resets the `use_count`/`last_used_at` ranking signals (by design).

### `embed-pending [--json]`
Embed the backlog: every entry queued by `store`/`append`/`distill` gets its
vector. `--json` reports `{"embedded": N, "pending": M, "seconds": S}` — a
non-zero `pending` means ollama did not answer and those entries wait for the
next run. Run it where latency is free (r4t drives it from idle dreaming), never
on a path someone is waiting on.

### `rebuild-mocs`
Regenerate all Maps of Content from entry tags.

### `check [--fix]`
Audit structural integrity; `--fix` repairs what it safely can.

## System

### `status`
Show capabilities, the resolved LLM/embedding models, and recommendations.

### `config <key> [value]`
Get/set configuration. See [k7e-configuration.md](k7e-configuration.md).
