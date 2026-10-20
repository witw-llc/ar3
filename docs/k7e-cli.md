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
--no-rerank            disable reranking even when configured globally
--include-superseded   include retired entries
--include-archive      include exploratory source claims
--as-of T              what the store held at time T (record clock)
--valid-at T           what was true at time T (validity window)
```

T is `YYYY-MM-DD` (the end of that local day), a local `YYYY-MM-DDTHH:MM:SS`,
or a UTC stamp ending in `Z`. A slice includes retired entries that the slice
time still covers. JSON hits carry `status`, `recorded_at`, `superseded_at`,
`valid_from`, `valid_to` and `replaces`, the ids of the entries each replaced.

When the semantic track runs, search prints `embed <N>ms` to **stderr** — the
query-embedding cost alone, so a caller on a latency budget can price it. The
line gains `(semantic track unavailable)` when ollama did not answer in time and
FTS5 carried the search by itself. stdout is untouched either way.

A node id (`K7E-BBB-NNNNN`) named in the query is a lookup, not a search: that
active node comes first, ahead of the ranked results. Retired nodes require
`--include-superseded` even when named by ID; use `get` for a direct audit read.

### `graph <id> [--json]`
One entry's two clocks and its edges: `->` for the edges its own file states
(`superseded_by`, `compiled_from`, `mentions`), `<-` for the edges other
entries state about it. See [k7e-architecture.md](k7e-architecture.md#two-clocks-and-the-graph).

### `get <id> [<id> ...] [--no-track] [--json]`
Print full entries. Counts as a "use" (bumps ranking signals) unless
`--no-track` is given — for a caller that reads an entry only to size it
before deciding whether to use it (r4t's knowledge packer), and wants
`touch` to be the thing that actually counts as a recall.

One id prints the entry alone. Several print them in the order asked,
separated by a `--- k7e:<id> ---` line before each entry after the first;
`--json` emits `[{"id", "text", "kind"}]` instead and is the form to parse.
`kind` is the archive record's kind as the file holds it, or `null` for an
operational entry; it is k7e's own typed-record test, so a reader need not
parse the frontmatter. Both flags apply to the whole batch.

A batch is one interpreter startup rather than one per id, which is most of
what a small local read costs: eight entries take ~60ms batched against
~375ms fetched singly. An id that does not exist is reported on stderr and
skipped — a caller sizing a pool would rather pack the rest than pack
nothing. The exit code is 1 only when nothing at all was found.

### `touch <id> [<id> ...]`
Bump the usage ranking signal (`use_count`, `last_used_at`) for one or more
entries without reading them — the other half of `get --no-track`.

### `recall <text> [--limit N] [--include-superseded] [--include-archive]`
RAG: retrieve relevant entries for a topic or pasted conversation and synthesize
an answer (LLM, reranker on by default). Accepts text as an arg or via stdin.

### `list [--tag X] [--status active] [--limit N] [--ids]`

Newest updated entries first, with newer IDs breaking same-day ties. `--limit`
bounds the output; zero returns no entries and negative values are rejected.
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

Repeated calls replace the frontmatter pointer in place. Reindex preserves it.
Do not supersede a general standing rule with a narrower scoped reinforcement;
store the scoped fact beside the general rule unless it actually replaces it.
Mark `old_id` as superseded by `new_id`, and record when. Preserves the audit
trail; hides the old entry from default search.

### `asset <file>`
Store a binary content-addressed (SHA256, deduped). Prints the stored path.

### `distill <file|dir> [--dry-run] [--archive]`

`--job ID` gives one immutable capture file a stable processing identity. Extraction decisions
and mutations are journaled for recovery, including appends and supersession.
Engine workers provide this automatically. Keep the identity stable on retries.
Extract knowledge from raw files, and over an r4t turn capture supersede the
entries that turn's people contradicted. See
[k7e-distillation.md](k7e-distillation.md).

### `consolidate [--dry-run]`
Find and merge duplicate nodes by title similarity.

### `compile <tag> [--dry-run]` · `compile --due` · `compile --revert <page>`
Fold the active entries for a tag into one reference page that supersedes them
(LLM). The page carries the tag first, then `compiled`, then up to ten of its
sources' own tags. A page folds again only into a compile of its first tag.

The page is what the model prints. The model runs in a scratch directory that
is removed after the call, so a file it writes is never the page and never
lands in the store. The reply is checked before anything is written: a reply
that narrates in the first person, has no section heading, or is shorter than
the smallest entry it would replace is rejected, the compile exits 1, and the
sources stay active. A call that times out (`COMPILE_TIMEOUT`, 120 s) is
retried on half the entries, down to three, so a tag too large for the model's
bound compiles in parts; a timeout at three entries fails the compile.

`--revert <page>` puts a compiled page's sources back in its place: every
source the page superseded is active again, and the page is retired with no
successor and stays on disk for audit. A source a correction retired meanwhile
keeps the correction. A page a later compile folded is refused until that
later page is reverted, which makes the earlier one active again; a page
already reverted restores nothing.

`--due` compiles the tag with the most entries no page has folded, when that
tag has at least five, and prints `compile: nothing` otherwise. It runs about
once a day per store (`COMPILE_DUE_HOURS`, marked in
`$K7E_HOME/.compile-due`); inside that window it prints `compile: not-due`. A failed compile exits 1 and also waits
out the window, and the next pass passes over the tag that failed, so a
compile that fails every time costs one attempt a day and holds no other tag
back. A pass that finds nothing ready needs no model command. One compile
folds at most 30 entries (`COMPILE_MAX_ENTRIES`), the tag's own page first;
the rest wait. Entries that expire (`valid_to`) are never folded or counted.

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

Reports duplicate standard sections and duplicate replacement pointers.
`--fix` merges section content, repairs malformed pointers using the surviving
index value when available, and rebuilds inconsistent indexes from Markdown.
A valid single file pointer remains authoritative over a disagreeing index.
Audit structural integrity; `--fix` repairs what it safely can. The `Index:`
line names any gap between the files and the SQLite index — how many entries
each holds, and every entry whose file status and indexed status disagree.
Search reads only the index, so a file that says `superseded` against a row
that says `active` is a retired claim still ranking. `k7e reindex` resolves
both.

## System

### `status`
Show capabilities, the resolved LLM/embedding models, and recommendations.

### `config <key> [value]`
Get/set configuration. See [k7e-configuration.md](k7e-configuration.md).
