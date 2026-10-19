# Field sync

Copies what a field machine holds about its agents into a local corpus, so
agents with memory on and off can be compared over time on real turns. Only
read commands run on the far end; nothing there is ever modified.

## What it copies

- The agent's a8s conversation archive, through `a8s convo <agent> --json`.
- While the agent's memory is on, the r4t engine-memory turn captures
  (`turns/<ULID>.json`: input, output, the knowledge ids injected), through
  `rsync --ignore-existing`. Nothing is deleted locally. The script reads
  `rsync --version` once: openrsync (macOS) gets the memory home shell-quoted,
  because it hands the path to the far end's shell; rsync 3 gets the raw path
  with `-s`, which sends it protected. Both pull a home with a space in it.

## Run

    apps/k7e/tests/eval/field/sync.py --dest ~/k7e-corpus --host user@box --agent alice --agent bob

Give `--host` the name that resolves over the private network (a Tailscale
DNS name, or the address): a bare hostname can be caught by another
network's search domains and point at a stranger. The host label, and so
the corpus directory, follows the name used, so keep using the same one.

With no `--agent`, the agent list comes from `a8s ls -q` on the far end. That
list includes names a node only heard over the broker; name the agents to
leave those out. `--a8s` (default `~/.ar3/a8s`) and `--r4t-home` (default
`~/.config/r4t`) are far-end paths. `--timeout` (default 60 s) limits every
far-end command. `--dry-run` prints what would run and pulls nothing. `--json`
prints one summary object per agent.

Exit 0 when every agent verified, 1 when any did not or the host is
unreachable, 2 on a usage or boundary refusal.

## Layout

    <dest>/<host-label>/<agent>/convo.jsonl    append-only, one row per ulid
    <dest>/<host-label>/<agent>/turns/         the far end's turn captures
    <dest>/<host-label>/<agent>/sync.json      the manifest

`host-label` is the host with `@`, `:` and `/` replaced by `-`.

The first pull runs `a8s convo <agent> --json --since 1970-01-01`: a cursor
makes an unset `--limit` drain every row, where a pull without one stops at
ten. Later pulls pass the newest stored ulid as the cursor, so the far end
returns only newer rows. A row already stored is skipped.

## Manifest

`sync.json` holds `agent`, `host`, `cursor` (the newest ulid stored) and a
`pulls` list. Each pull appends `at`, `memory`, `cursor_before`,
`cursor_after`, `rows_added`, `rows_total`, `turns_added`, `turns_total`,
`verified` and `note`. The file is replaced atomically.

## Verification

Each pull checks two counts and sets `verified` only when both hold:

- Rows: the far end's `a8s convo --json --since <cursor_before>` row count
  equals the rows appended. A message that arrives between the two reads
  shows as a mismatch; the next pull clears it.
- Turns (memory not off): local turn files are at least the far end's count.
  A local count above it means the far end pruned.

A failed agent prints both numbers, the other agents are still pulled, and the
exit code is 1.

## The memory stamp

Each pull records the agent's `MEMORY` var (`on`, `off`, `small`, `medium`,
`large`; absent means `off`). The engine captures turns only while memory is
on, so a pull during `off` has no turns by design and skips them. The stamp
lets an analysis split the corpus into memory-on and memory-off spans.

## Boundary

`--dest` is resolved and must not resolve inside the repo checkout. Each agent
directory must be a direct child of `<dest>/<host-label>`, and agent names
match `[A-Za-z0-9][A-Za-z0-9_.-]*`. A refusal happens before anything is
created. The script never removes files.

## Tests

`apps/k7e/tests/test_field_sync.py` points `--ssh` at a `fake-ssh` shim that
drops the host argument and runs the appended command locally, and `--a8s` at
a fake launcher, so no network and no real ssh run. `--rsync` swaps the rsync
command the same way. The rsync tests skip when rsync is not installed.
