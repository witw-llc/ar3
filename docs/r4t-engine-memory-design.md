# Engine memory design

This release implements #157's roster-free memory path, #280/#283's store
integrity work, and #104's retrieval evaluation. It excludes MCP, shared
memory tiers, alternative backends, and general contradiction discovery.

## Identity and authority

A named `--agent` owns one store independent of its engine and working
directory. Names are case-insensitive, matching a8s. Without `--agent`, the
canonical working directory is the identity. Stores live under r4t's state
home, outside the working tree. An explicit home pins a portable store to its
identity; another named agent cannot silently adopt that home. Identity
metadata travels with the directory. Physical directories separate retrieval
scopes; agents sharing an OS account still need OS isolation for access control.

Direct operator input can correct recalled facts. Routed messages carry sender
metadata separately from their prose, and only explicitly configured people
qualify for correction. A message containing an apparent sender, capture
header, or human-message section cannot manufacture authority. Batch delivery
preserves per-message attribution. Existing roster `People:` rules remain.

## Turn lifecycle

`--memory on|off|small|medium|large` is opt-in. Engine and rig execution use
the same memory lifecycle. Bundled a8s definitions forward `MEMORY` on message,
batch, and idle invocations. Memory off leaves prompt and execution unchanged.

Before execution, search active entries and pack age-stamped snippets within
the existing byte budgets, without generative synthesis. Query and candidate
depth defaults are selected by the retrieval comparison. Roster and engine
paths share the retrieval/packing implementation and count only injected
entries as uses. Operator diagnostics name the home, bytes, latency and queue.

Capture the original input separately from the injected prompt, output,
recalled IDs, sender authority, engine/model configuration, and outcome. Keep
stdout/stderr streaming and preserve engine exit codes. Only successful turns
enter the durable queue. Distillation reads original experience, never the
injected memory section. STATUS.md remains immediate working state.

## Worker and recovery

A successful standalone turn starts a bounded background worker; no permanent
daemon or manual maintenance command is required. a8s idle also kicks pending
work, including when its engine idle latch suppresses a model turn. One OS
lock per store coordinates workers and is released on process death. Queue
records and progress are atomic files. New turns can enqueue while a worker
runs. Pending work survives interruption and is retried by the next run/idle.

The writer defaults to the selected engine/model with a configured rig override
available. Distillation is an unscaffolded, memory-disabled invocation, so it
cannot recursively enqueue itself. Work has bounded time and batch size;
failures leave pending records and operator-visible diagnostics.

Extraction and correction decisions are journaled before applying writes.
Replaying an interrupted job applies each decided mutation idempotently,
including appends and supersession, rather than asking the model again and
duplicating partial work. Completion is recorded only after durable writes.
Markdown is authoritative and its index can be rebuilt without losing
supersession. Per-store write coordination protects IDs and read-modify-write
operations across workers and CLI writers.

## Evidence and acceptance

Standing wiki decisions require private stores, bounded snippets, asynchronous
distillation, descriptive notes, and externally verified corrections. K3's
message-only result supersedes the research's broader priming-query proposal;
the current hybrid retriever still receives a fresh comparison. Existing
packing, embeddings, age and poisoning results are historical evidence, not
new measurements for this release.

Compare message-only and expanded queries at depths 8/16/32, FTS-only and real
hybrid where available, under fixed budgets. Report evidence coverage, bytes,
latency and limitations. Test fresh-invocation learning/correction, engine
switching, agent separation and portability, interrupted and concurrent jobs,
unavailable embeddings, off-mode compatibility, streaming and exit semantics.
Separate deterministic process fixtures from live-engine results in the PR.
