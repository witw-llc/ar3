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

### `get <id>`
Print a full entry. Counts as a "use" (bumps ranking signals).

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
Extract knowledge from raw files. See [distillation.md](distillation.md).

### `consolidate [--dry-run]`
Find and merge duplicate nodes by title similarity.

### `compile <tag> [--dry-run]`
Synthesize active entries for a tag into a `compiled` reference page (LLM).

## Maintenance

### `reindex [--embeddings]`
Rebuild `.index.db` from the markdown files. `--embeddings` recomputes vectors.
Resets the `use_count`/`last_used_at` ranking signals (by design).

### `embed-pending`
Process queued embeddings.

### `rebuild-mocs`
Regenerate all Maps of Content from entry tags.

### `check [--fix]`
Audit structural integrity; `--fix` repairs what it safely can.

## System

### `status`
Show capabilities, the resolved LLM/embedding models, and recommendations.

### `config <key> [value]`
Get/set configuration. See [configuration.md](configuration.md).
