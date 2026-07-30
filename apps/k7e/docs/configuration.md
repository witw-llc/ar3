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
| `embeddings` | `K7E_EMBEDDINGS` | `ollama` | `ollama` or `none` |
| `embed_model` | `EMBED_MODEL` | `nomic-embed-text` | embedding model |
| `ollama_url` | `OLLAMA_URL` | `http://localhost:11434` | ollama endpoint (embeddings) |
| `rerank` | `K7E_RERANK` | off | LLM rerank in `search` by default |
| `decay_offset_days` | `K7E_DECAY_OFFSET` | 30 | flat (no-decay) window |
| `decay_scale_days` | `K7E_DECAY_SCALE` | 365 | decay half-life; `<=0` disables decay |
| `use_count_weight` | `K7E_USE_WEIGHT` | 0.2 | strength of use-count boost |

See [retrieval.md](retrieval.md#tuning) for what the ranking knobs do.

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

Semantic search uses ollama's `/api/embed` separately from LLM commands:

```bash
ollama pull nomic-embed-text
```

Without it (or with `embeddings none`), k7e runs FTS5-only — still effective for
keyword recall.

## What needs what

| Missing | Still works | Unavailable (fails fast) |
|---------|-------------|--------------------------|
| `llm_command` (and no purpose overrides) | store, FTS5 search, get, list, stats | `distill`, `recall`, `compile` |
| ollama / embed model | everything except semantic search | vector recall |
| purpose override only | other purposes via `llm_command` | that specific purpose if fallback also unset |

`k7e status` always reports exactly what's configured.
