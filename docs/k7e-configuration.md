# Configuration

Config lives in `$K7E_HOME/config.json`. Every key has an environment-variable
override (env wins over file). Inspect effective state with `k7e status`.

```bash
k7e config <key>          # read (purpose commands show llm_command fallback)
k7e config <key> <value>  # write to config.json
k7e status                # what's active + recommendations
```

## Keys

| Key | Env override | Default | Meaning |
|-----|--------------|---------|---------|
| `llm_command` | `K7E_LLM_COMMAND` | *(unset)* | fallback stdin→stdout CLI for all LLM uses |
| `summarize_command` | `K7E_SUMMARIZE_COMMAND` | *(fallback)* | recall synthesis |
| `decompose_command` | `K7E_DECOMPOSE_COMMAND` | *(fallback)* | long-text query extraction |
| `distill_command` | `K7E_DISTILL_COMMAND` | *(fallback)* | knowledge extraction |
| `compile_command` | `K7E_COMPILE_COMMAND` | *(fallback)* | tag synthesis |
| `rerank_command` | `K7E_RERANK_COMMAND` | *(fallback)* | search/recall reranking |
| `embeddings` | `K7E_EMBEDDINGS` | `ollama` | `ollama` (default), opt-in `openai`, or `none`/`off` |
| `embed_model` | `EMBED_MODEL` | provider-specific | `nomic-embed-text` for Ollama; `text-embedding-3-small` for OpenAI |
| `embed_dimensions` | `K7E_EMBED_DIMENSIONS` | model default | positive output size; OpenAI small defaults to 1536, large to 3072; Ollama must match its native vector size |
| `embed_query_timeout` | `K7E_EMBED_QUERY_TIMEOUT` | 2.0 | seconds a *search* waits on the query embedding |
| `ollama_url` | `OLLAMA_URL` | `http://localhost:11434` | ollama endpoint (embeddings) |
| `rerank` | `K7E_RERANK` | off | LLM rerank in `search` by default |
| `decay_offset_days` | `K7E_DECAY_OFFSET` | 30 | flat (no-decay) window |
| `decay_scale_days` | `K7E_DECAY_SCALE` | 365 | decay half-life; `<=0` disables decay |
| `use_count_weight` | `K7E_USE_WEIGHT` | 0.2 | strength of use-count boost |

See [k7e-retrieval.md](k7e-retrieval.md#tuning) for what the ranking knobs do.

## LLM commands (stdin → stdout)

k7e does **not** auto-detect LLMs and does **not** call ollama for generation.
You define explicit shell commands; k7e writes the prompt to **stdin** and reads
the response from **stdout**.

```bash
k7e config llm_command 'ollama run qwen3'    # fallback for all purposes
k7e config summarize_command 'my-sum-cli'    # optional recall override
k7e config rerank_command 'my-rank-cli'      # optional rerank override
```

Purpose-specific commands fall back to `llm_command` when unset. `k7e status`
lists each purpose and which command it resolves to.

Pick commands you control — a stateless ollama wrapper, a cloud CLI, whatever
fits your setup. k7e stays agnostic as long as the interface is stdin/stdout.

## Embeddings

Semantic search defaults to Ollama's `/api/embed`, separately from LLM commands:

```bash
ollama pull nomic-embed-text
```

That is the whole setup. With ollama and the model present, the semantic track
is live; without them (or with `embeddings none`), k7e runs FTS5-only — still
effective for keyword recall.

### Opt-in OpenAI provider

```bash
k7e config embeddings openai
k7e config embed_model text-embedding-3-small
# Optional: k7e config embed_dimensions 1536
# Supply OPENAI_API_KEY to the process through your runtime secret mechanism.
k7e embed-pending
k7e status
```

Selecting OpenAI sends each operational entry’s title and first 500 normalized
body characters to OpenAI when its vector is generated. The embedding view
omits exact reserved template heading lines and blank lines outside code
fences before the cut; section content, custom headings and fenced code remain
(see [embedding cache](k7e-architecture.md#embedding-cache)). Every semantic
search sends the query text; roster pack queries can contain incoming message text. These
requests incur provider charges. Typed archive records are not embedded.
It is never selected automatically. The adapter uses the fixed
`https://api.openai.com/v1/embeddings` endpoint, the runtime environment's
`OPENAI_API_KEY`, float responses and explicit dimensions. Credentials are not
read from config or persisted by the adapter; the CLI refuses the
`openai_api_key` config key. HTTP redirects are refused. `status` reports local
configuration only and never probes paid API access.

The provider supports `text-embedding-3-small` (1–1536 dimensions) and
`text-embedding-3-large` (1–3072), including shortened vectors. Explicit
`embed_model` settings take precedence: set the OpenAI model when changing providers.
Invalid provider/model/dimensions, missing keys, API errors, timeouts or invalid
vectors leave the write backlog pending and query retrieval uses keyword tracks.
There are no automatic retries, SDK dependency or endpoint override.
The request format follows the [OpenAI embeddings API](https://developers.openai.com/api/reference/resources/embeddings/methods/create).

### Vector coverage

`k7e status` reports how many active operational entries have a current vector
and how many are pending. `k7e embed-pending` derives missing or incompatible
vectors, including after a provider, model, dimension or input-format change.
Read-only status and check commands never generate vectors. Search remains
keyword-only for entries without compatible vectors.

A plain `k7e reindex` makes no provider calls. `k7e reindex --embeddings`
regenerates vectors and can incur charges: one request per operational entry,
without request batching. Failed inputs remain pending. When a version
changes the index schema, k7e rebuilds `.index.db` by itself on the first verb
that opens it; this re-derivation preserves Markdown, assets and source
snapshots, and resets index-only usage ranking. It drops cached vectors only
when the vector table itself changed shape; `k7e status` shows the pending
vectors. See [architecture](k7e-architecture.md#embedding-cache).

The two sides of the track cost differently, so they get different budgets:

- **Entries** are queued at write time and embedded one at a time by
  `k7e embed-pending`. Nobody waits on that path, so it uses the full 10s
  per-call timeout, and an unavailable provider leaves the queue for later.
- **Queries** are embedded inline by `search`, on `embed_query_timeout`
  (2s by default). A caller with a tighter budget lowers it; when the call
  misses, the search returns FTS5 results rather than nothing.

## What needs what

| Missing | Still works | Unavailable (fails fast) |
|---------|-------------|--------------------------|
| `llm_command` (and no purpose overrides) | store, FTS5 search, get, list, stats | `distill`, `recall`, `compile` |
| embedding provider / model / runtime key | everything except semantic search | vector recall |
| purpose override only | other purposes via `llm_command` | that specific purpose if fallback also unset |

`k7e status` always reports exactly what's configured.
