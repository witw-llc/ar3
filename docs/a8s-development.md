# a8s — Development Notes

Historical decisions, hard constraints, and things that didn't work.
Read [a8s.md](a8s.md) first for concept and usage.

## Hard constraints when refactoring

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
  (one empty file named as its trash file), writes one `DROPPED` row each and
  one agent-log line naming the ids. Never requeue a wake's envelopes, never
  arm a wake backoff, and never add a verb that returns a dead letter: the
  sender is in control and sends again. Never write a message to an agent
  about a failure, and send nothing about it across the network. `a8s ps` and
  `a8s health` count the markers whose mtime falls in the last 24 hours, and
  the count deletes markers older than 7 days. An inbox file that will not
  parse and a definition that will not load are dead letters too.
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
  signals, because a record outlives a handler killed outright. Kill the wake
  first (its group on POSIX; the process on Windows, which has no group), then
  the handler. Never `killpg` the handler: `a8s run` in a terminal shares its
  group with the shell. After a kill, clear the pid files and any request files,
  dead-letter the wake's envelopes when its `wake-pid` record shows it never
  settled (nothing else will), and write a `RUN_STOP` row, so the node starts
  again at once. Either way the stop prints the ids of the killed wake's dead
  letters.
- **A detach routes the outbox once.** `attached_loop`'s `finally:` runs one
  `route_outboxes` pass over the agents whose pid file still names this
  process, before `stop_remotes`. Reuse the loop's own routing call; write no
  second routing path. A failure in it is logged to the agent log and never
  holds the detach. `a8s step` and `--drain` add no final pass, and an agent
  released to a take-over or a kill is left to its new holder. `tell` stays
  non-blocking: the final pass lives in the stop, not in the sender.
- **Local routing claims the ULID in the `seen` table** to prevent MQTT round-trip dupes.
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
- **One cross-process mutex idiom remains: the consumed ledger's sidecar.**
  `transports/ledger.py` takes `<ledger>.lock` with one `O_CREAT | O_EXCL` and
  breaks a holder dead longer than `LEDGER_LOCK_STALE_SECONDS` by its mtime.
  Appends never wait for it; only compaction does, and compaction may give up.
  The receive winner is not a lock: it is the seen table's `INSERT OR IGNORE`,
  which SQLite makes atomic across processes. Do not add a claim file, a
  second lock shape, or a takeover.
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
- **The watchdog's kill boundary is the wake's own process group, nothing
  else.** Recovery closes the runner's end of the current wake's stdout and,
  if that process is still alive, terminates its process group — never a
  detached process (its own session) or anything outside the in-flight wake.

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
