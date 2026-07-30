"""k7e — Knowledge accumulation engine.

Flat-file knowledge store with hybrid search (FTS5 + embeddings).
Atomic markdown entries, content-addressed assets, Maps of Content.

This file is the script entry point; functionality lives in sibling modules:

  engine.py       store, search, get, reindex, assets, MOCs
  distill.py  raw experience → knowledge extraction (LLM-powered)
  hygiene.py      structural audit
  cli.py          COMMANDS table, dispatch, main

Surface (CLI):
  search <query>              hybrid search (BM25 + semantic + metadata)
  get <id>                    read a full entry
  store <title> [--tags]      create a new knowledge entry
  append <id> --section <name>  append to existing entry
  asset <file>                store binary (content-addressed, deduped)
  distill <file|dir>      extract knowledge from raw experience
  reindex [--embeddings]      rebuild index from files
  stats                       diagnostics
  check [--fix]               structural integrity audit

State:
  K7E_HOME (env) or ~/.config/k7e   base directory for the knowledge store
  $K7E_HOME/nodes/            atomic markdown entries (source of truth)
  $K7E_HOME/mocs/             Maps of Content (mutable indexes)
  $K7E_HOME/assets/           content-addressed binaries
  $K7E_HOME/.index.db         SQLite FTS5 + embeddings (derived, rebuildable)
"""
from __future__ import annotations

import sys

from cli import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
