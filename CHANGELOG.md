# Changelog

Notable changes to The Ark, newest first, following
[Keep a Changelog](https://keepachangelog.com/). Versions are the suite semver
in `VERSION`; a merge to `main` bumps it and cuts a release.

This record starts at 0.1.52, the first version after release v0.1.51. Earlier
history is in git.

Add to `Unreleased` in the same PR as the change, and rename the heading to the
version when the batch is ready to merge.

## [Unreleased]

## [0.1.55] — 2026-08-02

### Changed
- **Mail from outside a roster is owed no reply** (#58). The quiet-thread sweep
  now skips every ingress thread, whoever sent it and whatever `meta.class` it
  carried, and watches intra-roster threads only. Beyond the wall a8s posts
  messages to nodes and carries no notion of an expected reply; acquiring one
  would put a decision point on every node of a network r4t does not own. The
  roster's own human counts as inside — their doorbell mail keeps its backstop.
  This ends the nudge loop where an unmarked filedrop opened threads the sweep
  chased forever. The ledger's `relay` flag is now `ingress`, stamped from
  which side of the wall the sender is on.
- `quiet_task_seconds: 0` disables the sweep instead of failing config
  validation. The sweep always read `<= 0` as off while the loader rejected it,
  so the obvious off switch was a config error — and a config error fails the
  whole dispatch path, which is an outage.
- Batch `pause` is a trailing-edge quiet period (wake when no new inbox message
  arrives for N seconds), defaulting to 3s when `batch.invoke` is declared.
  Explicit `0` disables it; an inbox at `batch.limit` wakes immediately so a
  steady trickle cannot wait forever.

### Removed
- The lab's experiments (`apps/r4t/experiments/`) no longer ship in the public
  mirror snapshot. The `r4t lab` chassis stays public; the protocols, arms and
  batch scripts are the owner's research.
- **`close_without_reply` and the `Ack:` roster knob.** With ingress owed
  nothing structurally, the verb had nothing left to close: its allow-list was
  machine-originated threads, which the sweep now ignores outright, and
  dispatcher-opened threads, which never got a production ledger writer (#86).
  What remained was a doctrine bullet on every wake prompt teaching a protocol
  with no live case. A roster still carrying `- **Ack:** off` is unaffected —
  the line is ignored, not an error.

### Added
- Structured batch ingress for a8s → r4t: `batch.format` (`"prompt"` default,
  `"envelopes"` for routers) so a blast of N messages to an r4t node becomes
  one wake, N enqueues, and one drain turn instead of N sequential wakes.
- `ROSTER.md` and `MISSION.md` at the repo root — the Ark's own roster, one
  member per domain (transport, roster, memory, guide, process) with the
  resident agent holding the seat. Rigs are `ark-` prefixed so tuning this
  roster never disturbs the lab's own, and members are spread across claude,
  agy and cursor so no single subscription's quota is the ceiling. Drafted, not
  registered or started.
- This changelog.
- Release and deploy procedures documented end to end on the private wiki.
- The doc taxonomy is named in `CLAUDE.md`: tutorial in `guide/`, how-to and
  reference in `docs/`, explanation on the wiki.

### Changed
- **A merge to `main` is the release.** `release.yml` runs on push to `main`,
  so the owner's merge is the only switch; `workflow_dispatch` remains for
  re-runs and backfills. A push that leaves `VERSION` unchanged short-circuits
  the run rather than re-tagging what already shipped.

## [0.1.54] — 2026-08-02

### Added
- `a8s transactions` (alias `tx`) — read the transaction log without opening
  SQLite, with `--follow`, and filters for events, senders, recipients and
  message id.
- `s3` storage service for attachments: presigned URLs so receivers need no
  credentials, with `boto3` imported lazily and pinned in
  `requirements/a8s-s3.txt`.

### Changed
- Work batches onto a version-named branch and only the owner merges to `main`.

## [0.1.53] — 2026-08-01

### Changed
- Every suite runs its tests from its own venv under `apps/<app>/tests/.venv`,
  built from that suite's requirements. Nothing installs into the system or
  Homebrew python.

## [0.1.52] — 2026-08-01

### Added
- `a8s convo --from NAME` — filter the conversation archive by sender, with or
  without `--follow`, repeatable for several senders. The limit counts matches
  rather than rows scanned.
