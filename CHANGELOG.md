# Changelog

Notable changes to The Ark, newest first, following
[Keep a Changelog](https://keepachangelog.com/). Versions are the suite semver
in `VERSION`; a merge to `main` bumps it and cuts a release.

This record starts at 0.1.52, the first version after release v0.1.51. Earlier
history is in git.

Add to `Unreleased` in the same PR as the change, and rename the heading to the
version when the batch is ready to merge.

## [0.1.58]

### Fixed
- `a8s health` follows the same path a receiver does. A service may decline
  its own URL on purpose — `rclone` returns a public https link and leaves the
  fetch to the receiver, which needs no rclone and no credentials — and health
  reported that correct behaviour as `FAIL (retrieve returned False)`. It now
  falls back to the public GET and says which route verified the round trip.
- `a8s health` names the remote you configured instead of its Python class. It
  read `.name` off a transport that exposes `.id`, the same slip the storage
  loop had.

## [0.1.57] — 2026-08-05

### Added
- Two storage kinds that need no account of their own. **`file_sync`** copies
  into a folder something else already syncs and hands out the public URL the
  object lands at; a8s does no syncing itself. It needs a store whose public
  URL is derivable from the path — a webserver or CDN over the synced
  directory, `rclone serve`, a Nextcloud public folder. **`webdav`** PUTs
  directly, for stores whose upload host and public host differ. Both take
  `--base-url`, the public prefix a receiver downloads from. Bring what you
  already have rather than standing up a bucket.
- **`rclone` storage** — upload through a remote you already configured, and
  let rclone hand back the public URL. This is the answer for Google Drive,
  which mints an opaque per-file id at upload so no path can predict the
  download URL; `file_sync` cannot address it and never could. `rclone copyto`
  and `rclone link` are both synchronous, so nothing waits on a background sync
  daemon. The uploader needs rclone; the receiver still needs nothing, because
  the result is an ordinary public https URL. Only backends with a known
  direct-download form are accepted — Drive today — since storing a backend's
  preview page as the attachment would be silent corruption.
- Attachment delivery waits for bytes instead of promising them. Sync-backed
  uploads take a moment to reach the cloud, so the **receiver** retries the
  download for up to `storage_receive_wait_seconds` (15m) and holds the message
  out of the inbox until its files land — an agent woken for a file it cannot
  open burns tokens hunting for it. The sender never waits: a message may sit
  unsent for minutes, so blocking on publication buys nothing, and pulling the
  bytes is the receiving node's job. When the wait is exhausted the message
  arrives with the failure named: `ATTACHMENT UNAVAILABLE: <file>: <reason>` in
  the wake text, and `error: ATTACHMENT_UNAVAILABLE` on the `files` entry.
- **Attachment URLs must be https.** A peer chooses the URL a node downloads
  from, and presigned links carry their own authorization in the query string,
  so plaintext is refused and redirects are not followed. `storage_allow_http=1`
  relaxes it for a store on your own network with no certificate.
- `a8s storage --help` (and `-h`) prints every kind with its options and
  examples. The text existed; no argument reached it.
- `a8s.md` documents storage services — the command table, the five kinds, the
  fan-out redundancy, the wait knobs. The page previously told readers
  cross-cluster file transfer did not exist.

### Fixed
- S3 attachment downloads use a plain HTTP GET on presigned `https` URLs so
  receiving clusters need no AWS credentials or `s3` storage entry. A generic
  http(s) fallback runs when no configured service claims the URL.
- **A stalled attachment no longer holds up unrelated mail.** A transport hands
  envelopes to one serial worker, so the receive retry loop blocked every later
  message — including plain text from another sender — behind a single
  unreachable URL, for the full 15-minute default. Delivery attempts the
  download once inline and defers only the retry, to a bounded pool.
- **An inbound attachment cannot write outside its message bundle.** The
  receive path took `filename` off the wire and joined it to the destination
  directory unchecked, while the send path already rejected non-basenames; a
  peer could write an arbitrary file as the a8s user. Both directions now share
  one guard.
- Downloads are capped by `max_file_bytes` — on the presigned-URL path as well
  as the generic fallback — instead of streaming whatever the peer serves.
- Attachment downloads follow at most three redirects, and every hop obeys the
  same https rule as the first URL. Object stores redirect a share URL to the
  host that holds the bytes, so refusing redirects outright breaks those links;
  following them without a limit would let the sender of an envelope choose
  where a receiver goes.
- `a8s storage` builds the service before writing config, so a typo'd or
  missing option fails at the CLI instead of silently skipping that service at
  daemon start. Option names fold dashes to underscores, so the documented
  `--base-url` works.
- A storage `--password` is written to `secrets.json` (mode 0600) rather than
  `network.json`, matching `a8s remote`.
- `a8s health` names the storage service that failed rather than its Python
  class.

## [0.1.56]

### Added
- `docs/r4t-idle.md` — reference for one idle pass: what runs `r4t idle`, the
  five mechanisms in order (watchdog, drain, flush, dream, heartbeat), costs,
  knobs, and the heartbeat's backoff numbers.

### Changed
- Idle-pass vernacular adopted across the r4t docs and guide: **watchdog**
  (`QUIET`), **heartbeat** (`MISSION-REVIEW`), **flush**, **dream**, and
  **drain**, with cross-links to the new page.

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
