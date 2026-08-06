# Architecture

> Files are truth. The index is a rebuildable cache.

k7e stores knowledge as flat markdown files on disk and derives a SQLite search
index from them. If the index is ever lost, corrupted, or schema-changed, delete
it and run `k7e reindex` — nothing is lost, because the markdown *is* the
database.

## Storage layout

`K7E_HOME` (default `~/.config/k7e`, honoring `XDG_CONFIG_HOME`):

```
$K7E_HOME/
├── nodes/BBB/        # atomic knowledge entries (source of truth)
│   └── K7E-000-00001.md
├── mocs/             # Maps of Content — mutable per-tag index pages
│   └── networking.md
├── assets/XX/        # content-addressed binaries (SHA256, deduped)
├── config.json       # configuration (see k7e-configuration.md)
└── .index.db         # SQLite FTS5 + embeddings (DERIVED, rebuildable)
```

- **nodes/** — the canonical store. One markdown file per fact, bucketed into
  `BBB/` subdirectories to keep directory sizes sane.
- **mocs/** — Maps of Content. Auto-generated topic index pages, one per tag.
  Mutable and rebuildable (`k7e rebuild-mocs`).
- **assets/** — binaries (images, audio, etc.) stored by content hash so the
  same file is never stored twice. `k7e asset <file>` returns the stored path.
- **.index.db** — the only non-authoritative artifact. Holds the FTS5 keyword
  index, embedding vectors, and the ranking-signal columns. Safe to delete.

## Entry format

Each node is YAML frontmatter + markdown sections:

```markdown
---
id: K7E-000-00001
title: SSH Local Forwarding
aliases: [ssh-tunnel, port-forward]
status: active
confidence: 0.5
verification_count: 0
last_updated: 2026-05-20
tags: [ssh, networking]
---

## Verified Protocol
ssh -L 8080:target:80 bastion — forwards local:8080 to target:80 via bastion

## Edge Cases
## False Paths
## History
* 2026-05-20: Initial entry.
```

- `id` — `K7E-BBB-NNNNN`, stable for the life of the entry. Sequential, and
  therefore a **recency signal a reader can use without being told to** — see
  below.
- `status` — `active` (default), `superseded`, or `compiled`. Only `active`
  entries appear in default search (see [k7e-retrieval.md](k7e-retrieval.md)).
- `confidence` — 0..1, a static prior folded into ranking.
- `aliases` — alternate names matched by metadata search.
- Sections (`Verified Protocol`, `Edge Cases`, `False Paths`, `History`) are
  conventional; `k7e append` adds to a named section.

## Ids leak write order

`K7E-BBB-NNNNN` is allocated in sequence, so a higher ordinal means written
later. Nothing documents that to a model, and models use it anyway: in the
age-presentation arms, with every date stripped from the injected entries, a
4B model resolved a fact-supersession conflict straight off the ordinals —
*"the safer bet on recency (ID 122 > 121)"*.

Two consequences, and neither calls for a code change:

- **In production the signal is usually free and usually right**, because write
  order does track recency. It is silently wrong whenever it stops doing so:
  bulk imports, merged stores, and the pre-v1 rebuilds that re-number
  everything. Nothing warns, because nothing knows it is being read.
- **An experiment that means to test "no temporal information" must shuffle or
  mask ids**, or it under-measures the penalty for withholding dates — the
  model still has a clock, just a coarse one.

The date stamp is what carries recency deliberately, and it must be present
wherever recency matters. If ids are ever randomized or hashed, this crutch
disappears without notice.

## Derived index schema

The `nodes` table in `.index.db` mirrors the frontmatter plus two
**index-only** ranking columns that are *not* written back to markdown:

- `last_used_at` — last time the entry was consumed: synthesized by `recall`
  or read by `k7e get`. A `search` listing does not count.
- `use_count` — how many times it has been used.

These reset on `reindex` by design: ranking is *re-earned from usage*, not
frozen forever. See [k7e-retrieval.md](k7e-retrieval.md) for how they feed scoring.

## Lifecycle

- **Create** — `k7e store` (manual) or `k7e distill` (extracted from raw files).
- **Grow** — `k7e append` adds detail to a section.
- **Retire** — `k7e supersede <old> <new>` flips `old` to `status: superseded`
  and records `superseded_by`. The audit trail is preserved (queryable with
  `--include-superseded`) but hidden from default search.
- **Synthesize** — `k7e compile <tag>` writes a `compiled` reference page from
  the active entries for a tag.
- **Rebuild** — `k7e reindex` regenerates `.index.db` from the markdown.

## Why this shape

k7e is the inverse of a multi-tenant cloud memory service. It optimizes for a
single person (and their agents): portable as a folder of text files,
greppable, diffable, git-friendly, and never hostage to a running database or a
remote endpoint. The index exists only to make retrieval fast.
