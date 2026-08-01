# k7e — Knowledge accumulation engine

A durable, local memory for knowledge that compounds. Flat markdown files +
hybrid search, zero-dependency core.

## Mission

k7e exists so that an agent — or the human working alongside it — **never has to
re-learn the same thing twice**. Hard-won facts, corrections, procedures, and
decisions are captured once, kept as plain markdown you can read and edit by
hand, and surfaced again at the moment they're relevant.

Three commitments shape every design decision:

- **Files are truth.** Knowledge lives as flat markdown on your disk. The search
  index (SQLite FTS5 + optional embeddings) is a cache you can delete and
  rebuild anytime. No database lock-in, no opaque blobs, no cloud.
- **The core is dependency-free.** Storage and keyword search (FTS5) need
  nothing but Python's standard library and work fully offline. Semantic search
  uses ollama embeddings; knowledge *capture* (distill/recall/compile) uses
  **explicit stdin→stdout CLI commands** you configure — no auto-detection.
- **Relevance is earned, not assumed.** Knowledge you keep using stays fresh;
  knowledge you never touch fades in *ranking* — never in storage. The store
  accumulates; retrieval forgets.

k7e is deliberately **personal, single-user, and local** — the inverse of
multi-tenant cloud memory layers. It optimizes for one person's (and their
agents') accumulated expertise, portable as a folder of text files.

## Cheat sheet

```bash
# Read
k7e search "expose local service remotely"   # hybrid search (BM25+semantic+meta)
k7e search "port forwarding" --rerank --ids  # LLM rerank, IDs only
k7e get K7E-000-00001                         # full entry (counts as a "use")
k7e recall "SSH bastion access"               # RAG answer over the store
echo "we were discussing tunnels" | k7e recall
k7e list --tag ssh --ids
k7e stats

# Write
k7e store "SSH Local Forwarding" --tags ssh,net --content "ssh -L 8080:host:80 ..."
echo "..." | k7e store "Title" --tags x,y      # content via stdin
k7e append K7E-000-00001 --section "Edge Cases" --content "Fails if port in use."
k7e supersede K7E-000-00001 K7E-000-00009      # retire old, keep audit trail
k7e asset screenshot.png                       # store binary (deduped)
k7e distill notes.md [--dry-run]               # extract knowledge from raw files
k7e consolidate [--dry-run]                    # merge duplicates
k7e compile networking [--dry-run]             # synthesize a tag into a page

# Maintain / inspect
k7e embed-pending [--json]                      # give queued entries their vectors
k7e reindex [--embeddings]                      # rebuild index from markdown
k7e check [--fix]                               # audit integrity
k7e status                                      # capabilities + LLM commands
k7e config llm_command 'your-stdin-stdout-cli'  # required for distill/recall/compile
k7e config summarize_command '...'              # optional recall override
```

Full flags and every command: **[k7e-cli.md](k7e-cli.md)**.

## Install

`k7e` is on `PATH` with the rest of The Ark — see the
[install section](../README.md#install). From a clone, without installing:

```bash
python3 apps/k7e/k7e.py status
```

Required: Python 3.10+ (sqlite3 bundled). Optional: **ollama** for semantic search
embeddings; an **LLM CLI** you configure via `llm_command` for distill/recall/compile.

## How it works (30 seconds)

- Every fact is a markdown file under `$K7E_HOME/nodes/` (default `~/.config/k7e`).
  `.index.db` is a derived cache — delete it and `k7e reindex` rebuilds.
- **Search** fuses BM25 + metadata + embeddings (RRF), then weights by
  confidence, recency decay, and use-count, with an optional LLM reranker.
- **The semantic track rides ollama when it answers.** Storing an entry only
  *queues* it for a vector; `k7e embed-pending` embeds the backlog whenever the
  caller has time to spare. A search embeds the query and nothing else, on a
  two-second budget — when ollama is absent or slow, the query is the only cost
  and the search falls back to FTS5. Nothing to configure either way; set
  `embeddings` to `none` to turn the track off outright.
- **Recall** is RAG: retrieve + synthesize an answer (reranker on by default).
- **Distill** extracts knowledge from raw files (LLM), dedupes, and stores only
  genuine deltas. Requires `distill_command` or `llm_command`.
- **LLM backend** is whatever you configure — stdin in, stdout out. No
  auto-detection, no bundled ollama generation path.

## Documentation

| Doc | What's in it |
|-----|--------------|
| [k7e-architecture.md](k7e-architecture.md) | Storage model, entry format, schema, lifecycle |
| [k7e-retrieval.md](k7e-retrieval.md) | Search/recall pipeline, ranking, reranker, eval harness |
| [k7e-distillation.md](k7e-distillation.md) | Extracting knowledge from raw experience |
| [k7e-configuration.md](k7e-configuration.md) | Config keys, env, LLM/embedding backends |
| [k7e-cli.md](k7e-cli.md) | Full command + flag reference |

## Tests

```bash
cd apps/k7e
tests/run                 # deterministic suite (~15s)
tests/run -m "not llm"    # skip live-LLM tests
tests/run -k eval         # the Recall@K harness
```

The `@llm` tests need a running ollama; everything else is deterministic.

## Integration

Any agent or harness can call `k7e search`/`k7e recall` (read) and
`k7e store`/`k7e distill` (write). Separation of concerns: the agent reads, the
harness/curator writes.

## Status

Pre-v1. The on-disk format (markdown + frontmatter) is the stable contract; the
derived index schema may change (just `reindex`).
