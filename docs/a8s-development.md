# a8s — Development Notes

Historical decisions, hard constraints, and things that didn't work.
Read [a8s.md](a8s.md) first for concept and usage.

## Hard constraints when refactoring

- **`cmd_start` re-execs via `core.ENTRYPOINT`**, not `__file__`.
- **Argv interpolation** (`$SENDER`, `$RECIPIENT`, `$MESSAGE`, `$TIMESTAMP`,
  `$AGE`, `$A8S_DIR`, `$DEFINITION_PATH`, plus per-node a8s vars as `$KEY`)
  expands via `definitions._expand_argv`. Vars are registry-backed (`a8s vars`),
  not OS environment. Used-but-unset raises `UndefinedVarsError` before spawn.
  Per-message wakes use `invoke` via `build_command`; batch wakes use
  `batch.invoke` via `build_batch_command`, appending either a composed
  prose prompt (`batch.format` absent/`"prompt"`) or a JSON array of
  envelopes (`"envelopes"`).
- **`core.PRINT_LOCK` is the cross-module log lock.** Only set when
  `daemon.attached_loop` starts.
- **`run_with_prefix` uses `start_new_session=True`** — don't drop this.
- **Per-agent take-over via detach-request (no orphans).** Don't reintroduce
  process-level SIGTERM-and-wait in `acquire`.
- **Per-agent kill via kill-request + SIGUSR1.** Handler checks at iteration top.
- **File-proxy delivery is ungated.** `attached_loop` promotes a proxy's
  envelopes every iteration, including while another handled agent's wake is
  in flight — the move spawns nothing, and `tells` watches that inbox. Only
  subprocess wakes queue behind the single in-flight slot.
- **Agent-directory invariant — `.outbox/` is one-way.** a8s never reads or
  writes sidecars there. Ingest is atomic rename into `pending/`.
- **Remote routing publishes to all configured remotes.** Receivers dedupe by ULID.
- **Cross-cluster `FILE:` payloads ride storage services.** Configured under
  `network.json`'s `services` map (separate from `remotes`). Kinds that ship:
  `tempfile_org` (zero-signup, ephemeral) and `s3` (`s3://bucket[/prefix]`,
  any S3-compatible endpoint), `file_sync` (`file://` local sync folder plus
  `base_url`), and `webdav` (`webdav://` endpoint plus `base_url`). A new kind implements `StorageService` and
  registers in two places in `network.py` — `_build_service` and
  `detect_service_kind`.
- **`s3` returns presigned URLs, not `s3://` URIs.** The capability to fetch
  travels inside the envelope, so a receiving cluster needs no AWS credentials
  and there is one code path instead of a mode switch. boto3 is a lazy,
  optional import (`requirements/a8s-s3.txt`) chosen for its credential chain:
  an operator who grants a machine an IAM role gets working uploads with no
  a8s-side secret handling. Keys sit under a prefix (default `a8s`) so bucket
  lifecycle rules own expiry — nothing in a8s deletes objects.
- **WebDAV URLs use the `webdav://` scheme** at config time (`webdav+https` is not
  used — map to HTTPS internally).
- Inbound remote delivery **waits** (`storage_receive_wait_seconds`, default 900s)
  for attachment URLs to become fetchable before writing the inbox envelope —
  an agent woken for a file it cannot open burns tokens hunting for it.
  The sender never waits on publication — an unsent message would block for
  nothing, and pulling bytes is the receiver's job. Failures become
  `ATTACHMENT_UNAVAILABLE` in `files` and `ATTACHMENT UNAVAILABLE:` in wake text.
- **Attachment URLs are https.** A peer chooses the URL a node fetches, and
  presigned links carry authorization in the query string; `storage_allow_http`
  relaxes it for a LAN store without TLS. Read the knob with `get_setting`, not
  `get_int` — `get_int` clamps to a minimum of 1, so 0 would read as true.
- **`file_sync` needs a path-derivable public URL** (`base_url/<key>`). That
  covers a webserver or CDN over the synced dir, `rclone serve`, and Nextcloud
  public folders. It does not cover Google Drive, OneDrive or Dropbox, which
  mint an opaque per-file id at upload time — that is what the `rclone` kind is
  for.
- **`rclone` shells out; both calls are synchronous.** `copyto` then `link`, so
  the public URL exists when `store` returns and nothing waits on a sync
  daemon. `retrieve` returns False on purpose: the link is public https and the
  receiver's plain GET fetches it, so no receiver needs rclone. Only hosts in
  `_DIRECT_LINK_HOSTS` are accepted — `rclone link` on Drive returns a viewer
  URL that 307s, so it is rewritten to `drive.usercontent.google.com/download`
  — a receiver would follow the redirect, but the direct form costs one less
  round trip. An unknown host raises rather than
  passing through, because storing a preview page as the attachment is silent
  corruption. Verified against a live Drive remote: 110 KB round-tripped
  sha256-identical, fetched anonymously with no redirect and no credential.
- **The receive wait runs off the subscriber worker.** A transport delivers
  envelopes on one serial thread, so a retry loop that sleeps there holds every
  later message — including plain text from another sender — behind one
  unreachable URL. `receive_envelope` makes a single download attempt inline;
  only if bytes are missing does it hand that recipient's delivery to a bounded
  pool, which retries, writes the inbox, and sends the receipt. Ordering is
  preserved for everything that downloads first try.
- **Storage option names fold dashes to underscores** (`--base-url` and
  `--base_url` are one option), and `a8s storage` builds the service before
  writing config so a bad option fails at the CLI rather than as a silently
  skipped service at daemon start.
- **`--password` on a storage service goes to `secrets.json`** (mode 0600),
  never `network.json` — the same split `a8s remote` uses.
- **Storage services are stateless.** No start/stop lifecycle.
- **Absolute attachment paths in wake prompts.** Delivered messages append `ATTACHED FILE: <absolute-path>` lines (not bare `FILE:`). Path comes from definition `files_dir` (default `.files` under agent root) plus `<msg_id>/<filename>`.
- **Outbox attachments are staged.** Tell copies sources into `.outbox/<msg_id>/`; outbox envelopes carry `filename` only. Ingest moves the bundle with the JSON. Routing delivers into `<files_dir>/<msg_id>/`. Delivered wakes append `ATTACHED FILE:` lines (not bare `FILE:`).
- **Definition `outbox_dir`.** Optional; defaults to `.outbox` under agent root. Absolute paths allowed. Harness ingests from the resolved path; wakes inject `TELL_OUTBOX_DIR` into the invoke subprocess so tell writes there without the agent seeing the outbox in its workspace.
- **Tell outbox resolution.** `TELL_OUTBOX_DIR` when set (a8s injects it on wake). Else a unique configured outbox matched from CWD when the a8s state root (default `~/.config/a8s`) is readable (see [a8s-filedrop.md](a8s-filedrop.md)). System / agent-user installs without a readable registry need the env. No blind CWD tree-walk for a random `.outbox`.
- **Recipient validation follows the outbox, not the CWD.** `tell` validates against the registry only when the resolved outbox is a registered agent's own outbox. Any other outbox makes tell a staging writer and its consumer owns routing — r4t points a caged roster member's `TELL_OUTBOX_DIR` at a per-turn staging dir, and roster members are not a8s agents. Don't re-couple this to `sender_from_cwd()`: `from` stamping is a separate rule and stays CWD-driven.
- **Persistent MQTT sessions.** `clean_session=False` + QoS 1, hash-derived `client_id`.
- **`publish` waits for readiness event before raising.** Don't drop the
  disconnect handler.
- **Per-message backoff retry.** BACKOFF_SCHEDULE drives `.retry` sidecars.
- **Exit 0 is the only delivery ack.** Any other wake outcome — nonzero exit,
  timeout kill, failed spawn, unexpanded vars — moves the envelopes back into
  the inbox and arms the agent's `wake-retry` record, and after
  MAX_WAKE_ATTEMPTS they stay in trash as logged dead letters.
  `_wake_retry_ready` gates dispatch, so a permanently broken CLI backs off
  instead of spinning the handler. Delivery is at-least-once: a wake command
  must tolerate the same envelope twice. r4t dispatch already does — it
  enqueues durably and returns 0 before any turn runs, so a8s retries
  delivery without re-running turns.
- **Local routing claims the ULID in `seen-ids`** to prevent MQTT round-trip dupes.
- **`settings.json` is the stable operator config.** `a8s config set` persists
  machine-wide keys; `a8s config` (no args) catalogs every knob including
  definition, registry, and network fields. Env vars apply only when a key
  is absent from the file. `A8S_HOME` relocates the whole state dir.
- **`conversations.sqlite3` is machine-wide.** One routed row per logical
  message (alias fan-out is one row). Inserts do not prune. `a8s update`
  retains `convo_max_rows` (default 50000) during housekeeping. Queried by
  `a8s convo <agent>` — not per-agent storage; `--limit` only controls display.
- **`transactions.sqlite3` holds routing breadcrumbs, not bodies.** Several rows
  per message, written concurrently by the router, wake handlers, and receive
  loops. `txlog.log` never raises; `a8s trace <ULID>` and `a8s transactions`
  are the only readers, and `a8s update` retains `txlog_max_rows`. Both
  stores share the WAL/busy-retry discipline in `sqlite_store.py`.

## Per-tool quirks

- **Claude Code** — `--permission-mode dontAsk` + `--allowedTools "..."`. `--continue` for continuity.
- **Gemini CLI** — `--yolo` REQUIRED in headless mode. Policy Engine doesn't apply to `-p`.
- **Codex CLI** — `--full-auto`. `stdin=subprocess.DEVNULL` REQUIRED (hangs otherwise).
- **Copilot CLI** — `--allow-all-tools` REQUIRED. Marker is `.github/copilot-instructions.md`.
- **OpenCode** — `opencode run "<msg>"`. `--dangerously-skip-permissions` required. Model in agent's `opencode.json`.

## What didn't work

- Synchronous `a8s prompt` — raced with the loop. Queue into inbox instead.
- Mailboxes inside agent dirs — Gemini surfaced them to the model. Moved to `agents/` under the a8s state root.
- Headless tool-use without auto-approval — hangs silently. Always pass the flag.
- Singleton daemon — replaced with per-agent handlers.
- `says` broadcast verb — LLMs couldn't pick tell vs says consistently.
- Senderless `prompt`/`clear` commands — security hole over MQTT. Removed.
- `--unrestricted` global flag — retired. Use custom definition files instead.

## Active design threads

| # | State | Topic |
|---|---|---|
| #63 | partial | Multi-cluster routing. MQTT in, mini-MQTT/HTTPS/TCP/encryption still open. |
| #72 | open | Mailbox file format discussion. |
| #93 | open | Grok CLI as tool kind. |
