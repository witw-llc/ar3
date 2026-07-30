# Distillation

Turning raw experience (notes, transcripts, command output, images) into
durable, deduplicated knowledge entries.

```bash
k7e distill notes.md
k7e distill ./transcripts/      # a whole directory
k7e distill notes.md --dry-run  # show candidates, store nothing
```

**Distillation requires `distill_command` (or `llm_command`).** The CLI fails
fast when neither is configured. There is no offline pattern-matching fallback.

## Pipeline

```
raw file ─┬─ text  ─ LLM via distill_command (chunked, stdin→stdout)
          │              │
          │         dedup across chunks
          │              │
          └─ media ─ distill_command (prompt includes file path)
                         │
                         ▼
                  diff vs existing store ─► store genuine deltas
```

### Text extraction

1. **LLM extraction** — the text is chunked (~3000 chars, 200 overlap) and each
   chunk is sent to the model with a strict "extract only genuinely novel
   knowledge" prompt (max 3 items/chunk). Output is parsed as a JSON array of
   `{title, content, tags}`.
2. **Dedup** — candidates are deduplicated across chunks before storage.

### Media extraction

Media goes through the same `distill_command`. The prompt includes the absolute
file path — your CLI must know how to handle images, audio, or video (e.g. a
multimodal wrapper). The binary is stored as a content-addressed asset when
extraction succeeds.

## Delta detection

Before storing, candidates are diffed against the existing store so distillation
is idempotent-ish: re-running over the same input doesn't pile up duplicates.
Genuinely new or changed knowledge becomes new entries.

## Related write operations

- `k7e consolidate [--dry-run]` — find and merge duplicate nodes by title
  similarity (uses `supersede` under the hood).
- `k7e compile <tag> [--dry-run]` — synthesize the active entries for a tag into
  a single `compiled` reference page (LLM).

See [cli.md](cli.md) for full command/flag reference.
