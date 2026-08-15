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

import io
import sys

from cli import main


if __name__ == "__main__":
    # stderr already defaults to backslashreplace, so only stdout needs the
    # floor — an unencodable glyph (e.g. on a redirected Windows console)
    # gets a lossless, reversible escape instead of crashing the process.
    # The isinstance/errors=="strict" guard is mypy's own (PR 18292): it
    # never fires once a caller has set a deliberate error handler, and
    # skips a replaced sys.stdout (e.g. io.StringIO under embedding) cleanly
    # instead of raising AttributeError. Every --json path in the suite is
    # ensure_ascii, so machine-readable output is unaffected either way.
    if isinstance(sys.stdout, io.TextIOWrapper) and sys.stdout.errors == "strict":
        sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main(sys.argv[1:]))
