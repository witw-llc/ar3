# a8s — Development Notes

Historical decisions, hard constraints, and things that didn't work.
Read [a8s.md](a8s.md) first for concept and usage.

A resident wake can return after enqueue, before its detached engine finishes.
Consume its foundation-defined local failure records after the enqueue wake
settles, then record the local failure and affected envelope dead letters.
Never add a retry, sender notification, or reliable-delivery promise for this
accounting path. Completed turns stay completed if a later session step fails.

## Hard constraints when refactoring

- **Named node health is one live inspection.** `a8s health <node>` shows its definition, variables, existing runtime/activity/failure facts and applicable engine quota, using the node's effective account/environment. Preserve bare health. Never dereference secret bindings for display or infer remote/current health from last-heard age. Use existing probes and quota adapters with bounded waits; no monitor, inbox consumption or restart. Validate every invoke, idle and batch launch before probing; wrapper detection must not discard environment or working-directory effects. Refuse an unbound launch context without displaying assignments. Always tear down and reap owned probe children within bounds on success, timeout and interrupted waits. Node-health quota must be live: a failed query is unavailable, never a snapshot or inferred zero. Other automatic quota callers retain their spend policy. Keep engine quota buckets distinct from rig fuel. Use Muse's no-turn live `usage --json` only after verifying a supporting version; require a fresh mint allowance and suppress launcher auto-update. Never start a model turn for node health. If that command is unsupported or fails, report unavailable with a repair hint rather than starting an implicit turn or serving cached usage.

- **Reject unknown tell options before staging.** Before `--`, a token that
  starts with ASCII `-` is an option, except the standalone stdin marker `-`.
  Quotes do not escape that rule; use `--` or stdin for literal dash-leading
  text. Preserve supported options before and after the recipient.
- **Strict registry reads reject present non-object sections, including null.**
  Missing sections stay empty. Update and ps JSON must report unknown state
  before stopping or restarting a node.


- **Keep bare ls unchanged.** Local registrations override remembered remote names case-insensitively. Local/remote and recency filters change only the view, never the address book. Read the persistent last-heard table, not retained transaction rows. Last-heard is not remote health; ps remains local process state. Both listing commands provide help and reject unknown options.

- **a8s is best-effort messaging.** a8s is lightweight, best-effort messaging
  between agents, like UDP: each message is sent once with no delivery
  guarantee, and it may be picked up by several nodes or by none. Never add a
  delivery guarantee, a retry on a timeout, or a step that makes an agent check
  whether a message was received; an agent knows only that it sent.
- **A failed send or wake simply fails.** Keep decisions simple: the initiator
  is in control, and a sender that wants a message through sends again. Give a
  send one try per hop and never retry across passes: no backoff ladder, no
  parked send, no re-offer of a received envelope, no held receipt, no wake
  ladder, and no verb that returns a dead letter. Keep failure reports on the
  machine that failed — the agent log, one transaction row, a dead-letter
  count in `a8s ps` and `a8s health`. Never write a message to an agent about a
  failure, and send nothing about a failure across the network. Write no
  delivery promise in a doc: no at-least-once, and no claim that a dropped
  message can be recovered. Owner ruling, 2026-10-15.
- **`cmd_start` re-execs via `core.ENTRYPOINT`**, not `__file__`.
- **Argv interpolation** (`$SENDER`, `$RECIPIENT`, `$MESSAGE`, `$TIMESTAMP`,
  `$AGE`, `$A8S_DIR`, `$PYTHON`, `$DEFINITION_PATH`, plus per-node a8s vars as `$KEY`)
  expands via `definitions._expand_argv`. Vars are registry-backed (`a8s vars`),
  not OS environment. Used-but-unset raises `UndefinedVarsError` before spawn.
- **Every stop of a node runs its `stop.invoke` once.** `daemon.run_stop_hook`
  runs on the handler's final release and on a kill request, outside the wake
  slot. It names the hook process in the `stop-hook` mark right after it
  starts the hook, and marks it done after. The CLI clears the mark before it signals.
  For a handler that ended outright (`stop --force`, or a Windows handler that
  did not take its stop request) it
  waits for a named hook still running, and runs the hook itself only when
  the handler left no mark. A new path that ends a node does the same. A takeover by another
  handler is not a stop and runs no hook.
- **A node's own environment is `a8s vars <name> set env.<NAME> <value>`,** the
  grammar `r4t rig set <rig> env.<NAME>` uses. Only an `env.`-prefixed key
  reaches the child's environment, and `<NAME>` keeps its case; every other var
  stays argv interpolation and never reaches the environment. Do not export
  every var. Precedence,
  lowest first: the start shell, the PATH record (`wake_path`),
  `definition.env`, the node's `env.<NAME>` vars, then the routing variables a8s
  injects. `definitions.wake_env` composes the first four and `daemon._wake_env`
  adds the last; any new place that builds a wake or probe environment calls
  `wake_env(definition, load_agent_env(name))`.
  Per-message wakes use `invoke` via `build_command`; batch wakes use
  `batch.invoke` via `build_batch_command`, appending either a composed
  prose prompt (`batch.format` absent/`"prompt"`) or a JSON array of
  envelopes (`"envelopes"`).
- **`apps/a8s/ext/` never reaches the public mirror.** An extension verb moves
  into core, with its tests and a `docs/a8s.md` line, only after it has proved
  general. An extension claims a verb or a flag form by returning non-None from
  `handle`, and never changes core behaviour for arguments it does not claim.
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
- **`tell` never blocks, and stays minimal.** `tell` is asynchronous by
  nature; past a remote the envelope is gone the way a UDP packet is gone. No
  flag makes it wait, retry, confirm, or declare a wire, because a script that
  treats "no answer yet" as failure resends and doubles the message. A sender
  knows only that it sent; it reads replies with `tells`, and the router's
  transaction rows (`a8s tx --from NAME`) are for an operator debugging the network. `tell` is the one thing every
  harness and every remote seat shares, so every convenience goes on `tells`
  or on the roster, never on `tell`. Owner ruling, 2026-09-03.
- **Plain `tells` never times out by default.** It waits for the next
  arrival with no limit; only an explicit positive `--timeout` bounds it.
- **Agent-directory invariant — `.outbox/` is one-way.** a8s never reads or
  writes sidecars there. Ingest is atomic rename into `pending/`.
- **Remote routing publishes to all configured remotes.** Receivers dedupe by ULID.
- **Cross-cluster `FILE:` payloads ride storage services.** Configured under
  `network.json`'s `services` map (separate from `remotes`). Kinds that ship:
  `tempfile_org` (zero-signup, ephemeral) and `s3` (`s3://bucket[/prefix]`,
  any S3-compatible endpoint), `file_sync` (`file://` local sync folder plus
  `base_url`), `webdav` (`webdav://` endpoint plus `base_url`), `rclone`, and
  `sync_folder` (a bare path to a folder some sync client watches; publishes
  nothing). A new kind implements `StorageService` and
  registers in two places in `network.py` — `_build_service` and
  `detect_service_kind`.
- **Upload is any-of.** Every send attempts every service once; a message
  publishes once each file landed somewhere, and only services that accepted a
  file contribute a URL. Do not require all-of: a second storage service must
  add a path, not a second way to fail. The floor is one, not zero: a file no
  service took fails the publish to every remote, and the message drops unless
  local delivery took it.
- **A stored secret is bound to the destination it was typed for.** `secrets.json`
  keeps beside each secret a digest of the entry's non-secret fields
  (`network.spec_binding`), written by every writer that stores a secret. A
  reader uses a secret only when the digest matches the spec it loaded, because
  `network.json` and `secrets.json` are two files and a reader between a
  writer's two writes would otherwise send the new password to the old
  destination. A secret with no digest or a different one is dropped and
  reported on stderr, so `--json` output stays JSON. A secret stored before
  0.1.102 has no digest and is entered again once; every shape since then is
  read for good (next bullet). A rewrite without `--pass` keeps the stored
  password only when its digest matches the entry as it stood before the
  rewrite; otherwise the rewrite drops it and says so on stderr.
- **A stored secret survives every update.** A release reads every `secrets.json`
  that any earlier release since 0.1.102 wrote, and a stored password keeps
  working after `ar3 update` with no action from the operator. A change to the
  secret file's shape, its digest input, or the fields a writer stores carries
  a reader for the old shape, and `apps/a8s/tests/test_secrets_compat.py` reads
  the frozen fixtures of every shape ever shipped. Breaking this takes the
  owner's explicit exception, recorded in the Decisions ledger before the
  change is built (ruled 2026-10-08, after 0.1.102 cost the operator a
  password re-entry on every seat).
- **Services resolve at use time, not daemon start.** `load_services` rebuilds
  only when `network.json` or `secrets.json` changes, so both the routing pass
  and the receive callback can call it per use. A daemon runs for days, and one
  that captured its list at startup ignored every service configured
  afterwards while `a8s storage` listed them as configured.
- **`s3` returns presigned URLs, not `s3://` URIs.** The capability to fetch
  travels inside the envelope, so a receiving cluster needs no AWS credentials
  and there is one code path instead of a mode switch. boto3 is a lazy,
  optional import (`requirements/a8s-s3.txt`) chosen for its credential chain:
  an operator who grants a machine an IAM role gets working uploads with no
  a8s-side secret handling. Keys sit under a prefix (default `a8s`) so bucket
  lifecycle rules own expiry, and the only objects a8s deletes are a health
  probe and what a `retain_days` sweep reaps: the bundles of a `sync_folder`
  service and the envelopes of a folder or s3 remote.
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
- **`sync_folder` publishes nothing**, which is why it is a separate kind from
  `file_sync` rather than a mode of it. It claims **bare paths** at config time,
  so it never collides with a scheme somebody else owns. Its marker
  (`a8s+sync:<ULID>/<name>`) carries no host, no path and no service name, so
  each configured folder resolves it against its own root and the existing
  first-to-answer download loop makes two folders race. Keys are the message
  ULID rather than a random token — a sync folder is private, so unguessability
  buys nothing, while grouping a message's attachments gives the retention
  sweep something to act on. Published kinds keep their random token for the
  opposite reason: a ULID is time-ordered and therefore guessable, which matters
  on a public URL.
- **A sync folder is written by another program, so presence is not arrival.**
  Bytes go to a `.part` name and are renamed, and `manifest.json` records the
  size a receiver must see before accepting a copy. Anything short is treated
  as absent — `retrieve` returns False and the receive wait keeps polling —
  never as a failure. This is what defends against a mid-sync file and against
  a OneDrive Files On-Demand placeholder.
- **Sync-folder health separates local access from peer evidence.** Label the
  probe as a local round-trip. Observe timestamped, opaque per-home activity
  records, exclude this home's writes, and warn when peer evidence is absent
  or older than `peer_stale_seconds` (default 86400). A fresh peer write is not
  end-to-end delivery proof. Invalid or future records never count; file mtime
  is not the freshness clock. Activity recording is best effort and cannot
  fail an otherwise successful attachment copy. Do not add a background
  scheduler or put host/account names in the shared metadata.
- **`store` takes an optional `msg_id`.** `a8s health` has no envelope, so a
  service that needs one mints its own rather than growing a second code path.
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
- **An s3 remote fans out, and only time removes its mail.** Every node whose
  registry holds a name polls `<prefix>/<name>/`, and a name may live on
  several machines; each one gets a copy, as each MQTT subscriber does. Never
  delete an envelope on receive: record its ULID in this machine's ledger
  (`core.s3_ledger_path`, `transports/ledger.py`) and leave the object for the
  other machines. Publish every envelope, including one addressed to a name
  this node holds — the routing pass has already delivered it and written
  the `seen` table, as it does before an MQTT publish, and the receive path collapses
  the echo. Delete an object only in the retention reap, and only when both its
  ULID mint time and its `LastModified` clear `retain_days`. Keep the refusal
  in `a8s remote` and `a8s storage` of a prefix the other uses at the same
  endpoint: the reap deletes under the transport's prefix, and
  `services/s3.py` never deletes at all. Do not default the transport's prefix;
  a default would land on the storage service's.
- **A transport offers each envelope once and consumes it.** The receive
  callback answers nothing. After it returns or raises, MQTT acknowledges the
  message, and folder and s3 record the ULID in the consumed ledger. Never add
  an answer that withholds the acknowledgement or leaves an envelope for the
  next poll: a8s makes no delivery promise, and a re-offer delivers twice.
- **The seen-table insert names the one receiver.** Several daemons on one
  machine subscribe to the same remote and resolve recipients from the same
  registry. `receive_envelope` runs `INSERT OR IGNORE` on the ULID before it
  delivers; the call whose insert adds the row delivers, and every other call
  drops the envelope. A store that cannot be written answers as the winner, so
  a broken store costs a possible duplicate and never every message. An
  envelope for a name this node does not hold is recorded the same way, so the
  machine logs the miss once.
- **A failed delivery drops, once.** The seen row is written before delivery,
  so a failed inbox write is never tried again. It is one `WARN dropped` line
  in the agent log and one `DROPPED` row. Do not move the insert after the
  delivery to make a failure retryable.
- **The attachment wait is bounded and holds nothing.** The first fetch runs on
  the subscriber worker and does not wait. A file worth waiting for moves that
  recipient to a four-worker pool, which waits up to
  `storage_receive_wait_seconds`. The seen row is written and the envelope is
  consumed when the wait starts, so a restart during the wait loses that
  message. A wait that raises or cannot start is a `DROPPED` row.
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
- **MQTT acknowledges every message after the callback** (`manual_ack`). Never
  ack a message from an earlier connection: a packet id belongs to its session,
  so the broker redelivers it instead, and the seen table drops the copy.
  `stop()` drains: new arrivals stay unacknowledged for the broker to replay,
  the worker finishes its queue within a bounded join, and only then does the
  client disconnect. Don't move `disconnect()` ahead of the worker join.
- **A receipt is sent once and never waits.** `publish_control` publishes when
  the link is up and raises when it is down, and the receipt is dropped with a
  `WARN` line. It does not wait for the PUBACK. Receipts are for the operator;
  nothing waits for one.
- **The agent log shows delivery outcomes only.** One line per message for
  `inbox_write`, and one for attachments that failed.
- **A node that owns no recipient sends nothing back.** a8s is best effort: a
  message may be picked up by several nodes or by none. Never add a receipt,
  acknowledgement or report for a message a node does not deliver; the miss is
  a rate-limited `NOT_LOCAL` row in that node's own transaction log.
- **A publish has three outcomes, and none is tried again.**
  `make_publish_remotes` gives each remote one publish per send. `SENT` is a
  remote that took it. `FAILED` is a definite failure, one `DROPPED` row for
  that remote when another path took the message. `AMBIGUOUS` is a
  `PublishAmbiguous` from the transport — no PUBACK within `ack_timeout_s`, or
  paho's `MQTT_ERR_NO_CONN` after the publish call, which paho keeps queued and
  sends after the reconnect, or an S3 put that ends in botocore's
  `ReadTimeoutError` or `ConnectionClosedError` (matched by class name; a
  connect, DNS or endpoint error and an error response stay `FAILED`): count it as sent, write one `PUBLISH_AMBIGUOUS`
  row, and never retry it. If any path took the message it is sent; if none
  did, it moves to trash with one `DISCARDED` row and is never sent again.
- **A remote's own resend stays, and so do bounded waits inside one try.**
  Leave paho's QoS 1 resend after a reconnect and the S3 SDK's `max_attempts`
  as configured. The waits that stay: the MQTT reconnect wait before a publish
  (once per remote per pass; the pass's `down` set fails the rest at once),
  the folder rename retry, the attachment fetch wait, and the SQLite busy
  retry. Do not add another.
- **Exit 0 is the only delivery ack, and a failed wake is a dead letter at
  once.** Any other wake outcome — nonzero exit, a kill by the timeout, the
  watchdog or `stop --force`, a failed spawn, unexpanded vars — leaves the
  envelopes in trash, marks each one in the agent's `dead-letters/` directory
  (one empty file named as its trash file), writes one `WAKE_FAILED` row for the wake, one `DROPPED` row each and
  one agent-log line naming the ids. Never requeue a wake's envelopes, never
  arm a wake backoff, and never add a verb that returns a dead letter: the
  sender is in control and sends again. Never write a message to an agent
  about a failure, and send nothing about it across the network. `a8s health`
  counts the markers whose mtime falls in the last 24 hours, `a8s ps` counts
  the `WAKE_FAILED` rows of the last 24 hours, and the count deletes markers older than 7 days. An inbox file that will not
  parse and a definition that will not load are dead letters too. An idle
  wake has no envelope and leaves no dead letter, but when it exits nonzero,
  cannot start, aborts on an unexpanded var or is killed, it is one failed
  wake with its `WAKE_FAILED` row the same way.
- **A routing pass claims a pending message before it sends it.** Rename it
  from `pending/` into `sending/` first, and settle it from there. A file
  that will not move is not sent; a settle step that fails leaves the file in
  `sending/`, which no pass scans, so nothing is sent twice. Every message the
  router drops writes one `DROPPED` or `DISCARDED` row, and a fan-out that
  reached some members is delivered, with one `DROPPED` row per member it
  missed.
  Trash-after-spawn stays: the envelopes move to trash once the wake starts.
- **`stop --force` ends in seconds, and plain `stop` never kills.** A plain stop
  sends one SIGTERM and waits for the wake as long as it runs. `--force` sends a
  second SIGTERM, which makes the handler kill its wake group; a handler that has
  not detached `STOP_FORCE_GRACE_S` later cannot act on a signal, and `--force`
  then kills it with SIGKILL. A handler blocked in a call never runs a Python
  signal handler, so do not lengthen the wait in place of the kill. The wake's
  pid comes from the agent's `wake-pid` record, written when the wake spawns and
  removed when it settles; a reader checks the pid's start token before it
  signals, because a record outlives a handler killed outright. A claim (pid 0)
  precedes the pid: the handler writes the record with the wake's envelopes
  just before it spawns the wake, and the spawn replaces it atomically
  (`atomic_write_text`), so a kill during the replace leaves one whole record.
  The envelopes leave the inbox earlier, while `wake_once` builds the command
  and environment; a handler killed in that window leaves no record that names
  them. That window predates the claim.

  **Killing.** Freeze the handler first (`SIGSTOP`, confirmed by the stopped
  state in `/proc` or `ps` within about a second; `NtSuspendProcess` returning
  status 0 on Windows). A frozen handler starts no wake and keeps every pipe
  open, so the wake's leader is still alive when its record is read. Kill that
  wake (its group on POSIX; its process tree on Windows, which has no group),
  then every direct child of the frozen handler the record did not name, with
  its descendants (the stop hook the handler started is spared: the stop waits
  for it), then the handler. Take each child's start token when it is
  listed and check it again before every kill: a pid is never killed on its
  parent link alone. A child that leads its own group takes the group;
  another POSIX child shares the handler's group, so it is frozen, its
  descendants are listed the same way, and each dies alone; Windows ends the
  child's tree. A handler that cannot be frozen, or whose freeze is not
  confirmed, is reported on stderr and torn down in a bounded order: the
  wake its record names, the handler, then whatever wake its record names
  after that. Every exit leaves no handler stopped: it is killed, or resumed
  (`SIGCONT`, `NtResumeProcess`) when the stop fails first. Killing the handler
  before the wake loses a wake that writes to the handler's pipe: it exits on
  the broken pipe, its record reads as no wake, and its children live on.

  **The limit.** A wake leader can exit on its own between the stop's lookup
  and its kill, and before the freeze. Its record then names a pid that is
  gone, and the stop kills no pid it cannot prove it owns, so the leader's
  children run on. Freezing the handler does not prevent this; it only stops
  the handler's own death from causing it.

  **Settling is separate from killing.** A `wake-pid` record that outlives the
  handler proves the wake never settled, whether or not the stop found its
  processes. The stop dead-letters the envelopes the record names (a claim
  names them even when the wake has no pid), and a departed leader never
  means the mail was settled or the processes ended. A child started by a
  handler from before the claim has no unit, and nothing names its mail.

  Never `killpg` the handler: `a8s run` in a terminal shares its group with
  the shell. After a kill, clear the pid files and any request files, and
  write a `RUN_STOP` row, so the node starts again at once. Either way the
  stop prints the ids of the killed wake's dead letters.
- **On Windows a stop is a request file, never `os.kill`.** `os.kill` there is
  `TerminateProcess`: the handler ends with no chance to end its wake, which
  keeps running in its own process group with its mail never settled.
  `a8s stop`, `restart`, `exit` and the `kill` fallback write `stop-request`
  (`<requester pid> <level>`: 1 is the first SIGTERM, 2 the force) and the
  handler's listener thread takes it as that many signals and deletes it. The
  deletion is the stop's proof the handler heard it. A handler that has not
  taken it `STOP_FORCE_GRACE_S` later cannot act on one, so even a plain stop
  kills it outright, as `--force` does: frozen, then its wake's tree, then
  its other children, then the handler. POSIX keeps signals.
- **A detach routes the outbox once.** `attached_loop`'s `finally:` runs one
  `route_outboxes` pass over the agents whose pid file still names this
  process, before `stop_remotes`. Reuse the loop's own routing call; write no
  second routing path. A failure in it is logged to the agent log and never
  holds the detach. `a8s step` and `--drain` add no final pass, and an agent
  released to a take-over or a kill is left to its new holder. `tell` stays
  non-blocking: the final pass lives in the stop, not in the sender.
- **Local routing claims the ULID in the `seen` table before remote publication**
  to prevent synchronous or later broker echoes. Match the exact message ID,
  never a sender name or namespace that another machine can legitimately use.
- **Wake environment is declared and learned, not only inherited from the start shell.**
  Without a knob a node's `PATH` is whatever shell ran `a8s start`, forever —
  fine from an interactive login shell, wrong from `ssh host -- 'a8s start x'`,
  cron or CI, where the first wake fails hours later while the operator's own
  shell still resolves the binary fine. `wake_env` composes the declared layer
  (`definition.env` over the machine-wide `wake_path`) and `daemon._wake_env`
  puts the routing variables on top of it, so a node can fix its own `PATH` and
  still cannot move its own outbox.
- **The operator never maintains a `PATH`.** `wake_path` is a8s's own record,
  and `learn_wake_path` adds to it from every terminal the operator types a
  node-starting verb in (`cli.LEARNS_PATH`). Learn only at a terminal and
  never inside a wake: a wake's `PATH` is a8s's own composition. Record a
  whole `PATH`, not one resolved binary: an agent turn calls tools nobody
  listed. Do not make a login shell the default source of the `PATH`; rc
  files are programs, and `wake_shell` stays opt-in. Every message about a
  program that was not found names one command, `a8s restart <name>`.
- **Remember only what lasts.** `durable_path` drops a directory that does
  not exist and stores a per-shell fnm directory as the alias it links to.
- **`wake_path` adds to the handler's `PATH` and never removes from it.** A
  recording is right on the day it is made and wrong after the next tool
  install, so `merge_paths` keeps the handler's directories in the handler's
  order and places each recorded-only directory where the recording had it.
  Do not make `wake_path` replace the handler's `PATH`: a working shell then
  loses the directories it has and the recording lacks. `definition.env` is
  the knob that replaces, and it is per node.
- **A harness on `PATH` is half of a launch.** A script resolves and still
  exits 127 when the interpreter on its `#!` line does not, and every npm
  global install is such a script. `ar3.proc.missing_interpreter` reads that
  line; `r4t engine <id> check` reports it as `runnable: false`, and a wake
  that exits 127 names it.
  `_warn_unresolvable_harnesses` probes against the composed environment, so a
  node the knob fixed stops warning. It resolves the harness through wrappers
  (`harness_program` unwraps `flock`, `timeout`, `env`, `nice` and friends)
  because the `FileNotFoundError` guard around the spawn only ever sees
  `argv[0]` and a wrapped harness fails *inside* the wrapper. It declines to
  guess inside `sh -c`, skips an unexpanded `$VAR`, and stays quiet for a
  `wake_shell` node whose `PATH` an rc file decides. It warns and never
  refuses: the harness may be installed later, and an attached node that
  cannot wake is still worth having.
- **`wake_shell: "login"` is opt-in for a reason.** No flag string is correct
  on every box — `zsh -lc` and `bash -lc` read different startup files, and
  `-ilc` is the only complete form on some accounts — so the wrap uses `$SHELL`
  and the operator asks for it per node. `-c` goes last: `bash -c -l "cmd"`
  runs `-l` as the command and makes `cmd` `$0`. The rc runs inside the wrapped
  shell, after a8s hands over the environment, so an unguarded rc `export` beats
  injection and rc stdout lands in the wake log ahead of the harness output.
  Windows has no login-shell convention, so `a8s start` refuses rather than
  ignoring the field.
- **Receive dedup remembers by age, never by count.** The `seen` table in
  `transactions.sqlite3` keeps a message id for `dedup_days` and a receipt id
  for one day, each a day longer than its admission window so a sender's
  clock running up to a day ahead cannot slip a replay through the edge,
  expired in batches; an inbound envelope older than `dedup_days`
  is refused without a receipt, with an `EXPIRED` row only when the recipient
  is an agent this node holds, so the memory always outlasts what any wire
  can replay. Never put receipts and messages under one count budget.
- **One stale-breaking cross-process mutex idiom remains: the consumed ledger's sidecar.**
  `transports/ledger.py` takes `<ledger>.lock` with one `O_CREAT | O_EXCL` and
  breaks a holder dead longer than `LEDGER_LOCK_STALE_SECONDS` by its mtime.
  Appends never wait for it; only compaction does, and compaction may give up.
  The receive winner is not a lock: it is the seen table's `INSERT OR IGNORE`,
  which SQLite makes atomic across processes. Do not add a receive claim file,
  a second stale-breaking lock shape, or a takeover. Process-liveness claims
  and the foundation's OS advisory locks are separate contracts. Sync-folder
  writer identity creation reuses `ar3.locking.file_lock` with bounded contention
  and publishes its complete value with `atomic_write_text`.
- **`settings.json` is the stable operator config.** `a8s config set` persists
  machine-wide keys; `a8s config` (no args) catalogs every knob including
  definition, registry, and network fields. Env vars apply only when a key
  is absent from the file. `A8S_HOME` relocates the whole state dir.
- **`conversations.sqlite3` is machine-wide.** One routed row per logical
  message (alias fan-out is one row). Inserts do not prune. `a8s update`
  retains `convo_max_rows` (default 50000) during housekeeping. Queried by
  `a8s convo <agent>` — not per-agent storage; `--limit` only controls display.
- **Attachment outcomes belong to actual recipients.** Keep one message row
  with immutable first-committed outcomes keyed by canonical recipient in its
  JSON entry. Record each actual local or remote routed envelope. Project only
  the requested recipient's outcome on reads. A remote publication is not a
  receipt; absent evidence, including legacy rows, renders explicitly unrecorded.
  Preserve historical data; do not migrate or delete the archive. Row insertion
  order and sequence-cursor semantics remain unchanged.
- **`transactions.sqlite3` holds routing breadcrumbs, not bodies.** Several rows
  per message, written concurrently by the router, wake handlers, and receive
  loops. `txlog.log` never raises; `a8s trace <ULID>` and `a8s transactions`
  read it, and `a8s update` retains `txlog_max_rows`. The remote address book
  `a8s ls` reads is the `remote_last_heard` table in the same file, one row per
  folded name, written by `txlog.log` in the same transaction as each
  `RECEIVED_REMOTE` row. Retention never touches it; do not derive the book
  from the event rows again. Both stores share the WAL/busy-retry discipline
  in `sqlite_store.py`.
- **A reader never reports "no rows" for a store it could not open, and never
  creates one.** Missing and unreadable are both errors naming the path
  (`a8s: cannot read <path>: <reason>`, `no conversation store at <path>`),
  exit 1; a readable store with no rows prints nothing and exits 0. Go through
  `convo.open_for_read` / `txlog.open_for_read`, never `_connect`, which
  creates the file and the schema. Those helpers open through
  `sqlite_store.connect_read_only`, which cannot write and refuses a file
  lacking the store's own table, so a zero-byte or unrelated database is an
  error rather than a store the reader just initialized. The one exception is
  `txlog.last_heard`,
  whose caller answers from the registry and treats the heard remotes as a
  supplement.
- **A running node holds one connection to each store for its lifetime; a
  read-only reader is only as available as that.** Both stores are WAL, and a
  `mode=ro` open needs `-wal` and `-shm` to exist or the directory to be
  writable, so a reader with read+execute on the a8s home can read only while
  something holds the pair open. `attached_loop` opens `convo.hold_open()` and
  `txlog.hold_open()` at start and closes them at shutdown — idle connections
  of their own, taking no lock and blocking no `wal_checkpoint(TRUNCATE)`.
  When the open fails with neither side file present, the reader keeps
  SQLite's own words and adds the condition they can hide (`no WAL side files
  sit beside the store, and a reader that cannot create them cannot open it
  until a node holds the store open; is a node running on this machine?`).
  The sentence is a possibility, not a diagnosis: the same codes are raised
  by an unreadable main file, and no production reader writes a probe to tell
  the two apart.
- **A wake's stdout is read by a dedicated reader thread, never the main
  loop.** The main loop only ever drains that thread's queue with a bounded
  deadline (`wake_drain_grace_seconds`) — never an unbounded `for line in
  proc.stdout:` EOF wait. An inherited write end (a grandchild that keeps the
  pipe open after the wake itself exits) must not wedge the runner.
- **An update stops or restarts a node only on strict reads.** `a8s update`,
  `a8s ps --json` and `ar3 update` read the registry, each handler pid file
  and its `pid.start` (`read_handler_pid_strict`), each wake record
  (`read_wake_pid(strict=True)`) and each status probe (`run_status_probe`)
  without reaping or deleting anything, and validate every value they
  decide on: a pid is a positive decimal int, a start token has the exact
  form `process_start_token` writes on this platform (`valid_start_token`),
  a wake record's fields have their written types, a status block is an
  object with a non-empty `invoke` list of strings, a probe answers `busy`,
  `resident`, `idle` or nothing, and a resident state's `status` and
  `turns` (an int of 0 or more) are what the bridge writes. An absent file
  means "not running" or "no wake"; any other failure (an OSError, content
  that does not parse, a value of the wrong type or form) is unknown, never
  absent, recycled or idle. Only a valid start token that differs from the
  live process's shows a recycled pid. Unknown holds the node's whole
  handler group, and an unreadable membership (registry, pid file) holds the
  whole update.
  Reaping stale files stays on the normal paths (`a8s ps`, `start`, `stop`).
- **The watchdog's kill boundary is the wake's own process group, nothing
  else.** Recovery closes the runner's end of the current wake's stdout and,
  if that process is still alive, terminates its process group — never a
  detached process (its own session) or anything outside the in-flight wake.
- **On Windows each wake runs in its own job object, the stand-in for its
  process group.** The handler starts the wake suspended, assigns it to a
  new job, then resumes it (`ar3.proc.start_in_job`), so nothing the wake
  starts — the real program behind a launcher such as a venv's python.exe
  or a `.cmd` shim included — begins outside the job. It holds the handle
  until the wake settles. A job that cannot be made leaves the wake
  running with no job; a resume that fails ends the suspended process and,
  once its exit is confirmed, starts it again with no job; an exit that
  cannot be confirmed fails the wake. Each case writes one line to the
  agent's log. A packaged (MSIX, for example Microsoft Store) program
  activated through its app execution alias under
  `%LOCALAPPDATA%\Microsoft\WindowsApps`, or a venv launcher whose base is
  a packaged Python, starts the real program outside the job even so, and
  everything it starts with it; the packaged interpreter run directly stays
  in the job. The handler logs one line when the wake starts through such a
  launcher or `IsProcessInJob` says the leader is not in its job
  (`windows_job_escape_note`). Recommend a python.org interpreter on Windows. A
  forced kill or a drain timeout ends the job (`terminate_job`), then the
  leader by pid, so a descendant whose parent has exited dies too. It does
  not run `taskkill /T`, which follows parent links into a child that left
  the job; the tree walk runs only when the wake has no job or ending the
  job fails. A settled
  wake's job is released with its kill on close cleared (`release_job`), so
  a settle kills nothing, as on POSIX. The job kills on close, so a handler
  killed outright takes its wake's tree with it. A child started with
  `CREATE_BREAKAWAY_FROM_JOB` may leave the job, as a POSIX child may leave
  its group with `setsid`. r4t's resident bridge does, so it survives a
  forced stop of its wake that the handler carries out, whether or not the
  process that launched it still runs. A handler that does not take the
  stop is killed from the CLI, which ends the wake's tree with
  `taskkill /T`; that reaches a bridge whose launching process still runs.
- **Native autostart remains experimental until owner-run basic OS checks.**
  Do not require disposable machines for this acceptance. The owner tests
  startup after boot/login and removal of the owned startup entry on existing
  Linux, macOS and Windows machines. Record results without claiming broader
  native verification.
- **Autostart restores a node at startup and adds no supervisor.** `--autostart`
  keeps a per-node preference and arms one entry in the current user's native
  startup facility: Linux cron `@reboot`, a macOS LaunchAgent (RunAtLoad, no
  KeepAlive) at login, a Windows logon task with an interactive token and no
  saved password. There is no crash restart and no pre-login start on macOS or
  Windows. State lives in `<a8s home>/autostart.json` under `autostart.lock`;
  an entry carries the epoch of its arming, and the claim at the pid file
  refuses a startup whose epoch is no longer the armed one, so a queued event
  never revives a stopped, removed or re-created node. `stop`, `kill` and
  `exit` disarm (a stop also disarms unnamed siblings of a shared handler);
  `restart` and `update` keep the arming; a plain `start` rearms a remembered
  preference; `remove` deletes the owned entry or fails and keeps the node.
  Entries are owned by account, config-home id and node, and never carry
  credentials: proxy and CA settings sit in a mode-0600 file in the config home.
  `ls --autostart` reports configuration, not node health.
  If stopped-intent storage cannot be read, locked or written, `stop`, `kill` and
  `exit` still attempt current handler shutdown and return failure. Report
  that queued or future startup may remain enabled; never claim durable
  stopped intent when its state is unreadable, malformed or cannot be saved.
  Proxy and CA settings are saved per node, only when `--autostart` is given
  explicitly (`start` or `add`) and only after the entry is written; a shell
  without them leaves a node's saved values alone, and a rearm, `restart` or
  `update` never recaptures. `ar3 update` stops with the internal
  `stop --keep-autostart`, so the arming survives the update. The Windows
  logon task names the account by SID taken from the OS (no SID, no task) and
  runs `pythonw.exe` so no console opens; neither has been run on Windows.
  `remove` purges autostart before its last steps, so a failure after that
  point leaves a registered node with its preference cleared: it fails closed,
  never leaves an entry that can start the node, and a repeated `remove`
  finishes the job.
- **A queued explicit start obeys later intent.** `start` returns before its
  detached child attaches, so each start reserves a fresh manual claim token per
  concrete node in `autostart.json`, passes it on the child's internal argv,
  and the child's pid claim rechecks it under `autostart.lock`, spending it.
  A later `start`, `stop`, `kill`, `exit` or `remove` replaces or clears the
  token, with or without a pid file or an autostart preference, so a child that
  was spawned and had not attached cannot start a node the operator stopped.
  `restart` and `update` keep the preference and issue a new token; an
  alias binds the token to each member; a removed and re-created node refuses
  an old token. OS startup still uses the armed epoch, and public `run` takes
  over with no token.
- **`start` never takes over; `run` always does.** `start` on a running node
  leaves its process alone, and the child it spawns claims the pid file
  atomically with `--no-takeover`. `restart` is stop then start. Public `run`
  takes over into the console. Startup entries use the no-takeover claim.
  Native OS behavior has been exercised only through Linux cron fixtures; the
  launchd and Task Scheduler backends are generated-file and fake-command
  tested, and macOS and Windows runs remain to do.

## Tool CLI and documentation

Give every bundled tool a public `ar3 tool` entry point for its user and
configuration options. Do not require source-relative Python commands in user
instructions. Preserve registered-node variables and keep local administration
separate from network messages. Write tool documentation in ASD-STE100 language.
Keep each README's `## Usage` section as the single source for message help.

## Browser control boundary

Mark second-user isolation as experimental until basic native checks are
recorded. Use an ordinary second OS account on the same machine to test the
live browser connection. Windows browser startup remains unsupported; a
refusal to launch is not proof of cross-user isolation.

A browser seat uses a driver-owned debugging pipe, with no DevTools TCP fallback.
Driver IPC lives in a verified private OS-user directory; an inherited socket
path cannot redirect it. Hash the exact seat name for launch, discovery and
cleanup. Read browser targets from nonce-bound structured driver responses,
never from page titles or console output. Refuse Windows browser startup until
its process ownership and IPC boundary are implemented and verified.

### Browser tab commands

Expose existing tabs through `tabs`, select one through `tab N`, and capture
one through `shot [N]`. Use zero-based indices, matching the driver and recipe
API. Keep selection after an indexed screenshot. Reject invalid or excess
arguments instead of ignoring them. Read tab identities from structured
driver results; page titles and console output must not control selection.
Keep local-file refusal, secret scrubbing and sensitive-step capture guards
in force before capturing the selected page.

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

`bin#N` is an issue in the pre-carve repository; a bare `#N` is this one. The
numbering overlaps, so the prefix is what tells them apart.

| # | State | Topic |
|---|---|---|
| bin#63 | partial | Multi-cluster routing. MQTT, folder and s3 in; mini-MQTT/TCP/encryption still open. |
| bin#72 | open | Mailbox file format discussion. |
| bin#93 | open | Grok CLI as tool kind. |
