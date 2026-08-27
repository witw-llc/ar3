# Changelog

Notable changes to ar3, newest first, following
[Keep a Changelog](https://keepachangelog.com/). Versions are the suite semver
in `VERSION`; a merge to `main` bumps it and cuts a release.

This record starts at 0.1.52, the first version after release v0.1.51. Earlier
history is in git.

Add to `Unreleased` in the same PR as the change, and rename the heading to the
version when the batch is ready to merge.

## 0.1.76

### Added
- **`a8s_tell` can send files.** The MCP tool took `recipient` and `body` and
  nothing else, so a member on a rig with `mcp on` could not attach a file at
  all — a capability the shell form has always had. That gap was a reason to
  keep a member on the shell rather than a cost worth paying, which is exactly
  the call it forced on a live seat. The tool now takes `attachments`, a list
  of absolute paths, and the wake prompt names the argument: a tool argument
  the prompt does not mention goes unused for the same reason a generically
  described tool does. Delivery reuses `tell --attach`, and the `=` form
  specifically — the separate-argument form keeps consuming arguments while
  they name existing files, so a recipient that matched a filename in the
  working directory would be swallowed as an attachment. Path validation stays
  in `tell`, where the size cap and the resolved path in the error already
  live.
- **`ar3 update` updates the install in place.** Updating used to start in a
  browser: open the public mirror, copy the install one-liner, paste it back.
  The installer was already sitting in every install as `get.sh`, and the
  README mentioned it once, in a commented-out line inside a cron example —
  which is to say it was undiscoverable. The verb runs that same script
  against the copy you invoked (`AR3_DIR` is passed, so it never updates some
  other install by default), honours `AR3_VERSION` and `AR3_CHANNEL` exactly
  as at install time, and reports the version it moved from and to. It is
  **refused on a working checkout** — uncommitted changes, or a branch that is
  not the remote's default — because `get.sh` reaches the tree with
  `git pull --ff-only` and `git checkout -f`, and no message printed after
  that undoes what it did. A detached HEAD sitting exactly on a release tag is
  allowed: that is what an `AR3_VERSION` pin leaves behind, and the tag must
  match the `v[0-9]*` grammar `get.sh` itself accepts, since that is the only
  detached state the installer can have produced. Detached anywhere else is
  refused — on an ordinary commit, or on a tag named `wip` — because a
  developer parked on an unpushed commit reports no branch in precisely the
  same way a pin does.
  Silence from git is refused too: every clearance is read out of git's own
  answers, so a `.git` that git will not confirm as a work tree — missing
  binary, timeout, dubious ownership — leaves this knowing nothing about the
  tree, and a clean-looking `None` was previously read as permission to
  overwrite it. This and `ar3 deps` are the only things ar3 writes, and both
  write ar3's own substrate rather than a product's state.
- **`a8s ls` lists the names you can reach, not only the ones that run here.**
  A node only ever heard over a remote had no row at all, so the command
  answered "what runs here" while reading as "what can I reach" — and an agent
  told to message a name it could not find went looking for a fault instead of
  sending. Remote names now list after the local ones with DEFINITION `remote`
  and the local time each last reached this node; `-q` includes them, since
  that is the form a script uses to answer "can I reach X". A name that is
  both registered and a remote sender appears once, as its registry row.
  "Heard" means arrival — `RECEIVED_REMOTE` — and nothing else: publishing to
  a name records that the transport took the message, not that anything on the
  far side read it, so a remote that is down looks identical to one that is
  fine. Names fold case-insensitively, matching how the registry resolves
  them, and the newest arrival supplies the spelling. No age cutoff is
  applied, and the stamp says how fresh the address is — but the list is read
  out of the transaction log, so a remote whose rows have aged out of
  `txlog_max_rows` drops off until it speaks again. The log is an event
  record, not a roster.

### Fixed
- **`a8s convo` no longer renders a lost attachment as a delivered one.** A
  file the transfer could not deliver arrives with `error` and `detail` on its
  entry, and the conversation archive threw both away, keeping only the
  filename. The line it printed — `- attachment: notes.md` — differed from a
  delivered file only in being a bare name rather than an absolute path, so a
  failure was reported in the vocabulary of success and the reader went
  looking for a file that was never there. The failure now reads
  `- ATTACHMENT UNAVAILABLE: <name>: <why>`, matching what the wake prompt has
  always told an agent. The bare-name line stays as it is and is *not* treated
  as evidence of loss: a message this agent sent keeps its files in an outbox
  bundle that lookup never searches, and an inbound bundle is reaped after its
  retention window, so only an entry that actually arrived carrying an error
  is reported lost. Normalization is where the fix belongs — the archive is
  written once, so anything dropped there is gone for good. The receive path
  was carrying the other half of the bug: the per-recipient download returns a
  *new* envelope holding the error, and both the immediate and deferred paths
  went on to record the original storage-bearing one, so a corrected renderer
  would still never have been handed a failure to render. The archive now gets
  the envelope the recipient actually received. Because that download runs per
  recipient, an alias fan-out can end with one recipient holding bytes and
  another holding an error, and one message id is one archive row — so the row
  reports a file as lost when any recipient's copy was lost, and a failure
  arriving later is folded into a row already written clean. The direction is
  deliberate: a lost file described as delivered sends a reader after something
  that was never written, while the reverse only sends them to check a file
  they already hold. Per-recipient outcomes are the real model and are #225.
- **The conversation archive no longer drops a recipient that arrives second.**
  `messages.message_id` is UNIQUE and the insert was `INSERT OR IGNORE`, so a
  second write for the same id was discarded whole — the row and its
  `message_agents` rows together. Any recipient recorded after the first
  therefore had no conversation at all, which is what a deferred attachment
  delivery has always been: `a8s convo <name>` was empty for the recipient
  whose file took the slow path. A second write now attaches its recipients to
  the existing row instead of vanishing.
- **`r4t engine cursor quota` finds a Windows-side IDE from WSL.** A seat that
  runs the CLI on Linux while Cursor itself is installed on Windows now has
  every `/mnt/c/Users/<profile>` checked after the Linux path — measured
  working from such a seat, where `R4T_CURSOR_STATE_DB` had been the only way
  in. Selection also stops at the first database that actually holds a token
  rather than the first that exists, because a machine can carry several
  Windows profiles and a Cursor that was never signed in has a database with
  nothing in it. A locally installed IDE still outranks any of them.
- **`r4t engine codex quota` says which sign-in carries a quota.** A CLI
  authenticated with an API key is signed in perfectly well, but a rate limit
  belongs to a subscription and an API key has none — so the server answers
  "chatgpt authentication required", which reads as a broken login and sends
  the reader off to fix something that is not wrong. The failure now names the
  distinction and points at `codex login status`.

## 0.1.75

### Added
- **The `mcp` knob now serves agy.** An agy rig can take the `a8s_tell` tool
  instead of the shell `tell` heredoc: `r4t rig set <rig> mcp on`. agy has no
  per-invocation MCP flag in any released version — it reads
  `$HOME/.gemini/config/mcp_config.json` and nothing else — but under an org
  with `run_as` isolation that home is the member's own, because sudoers
  `env_reset` drops the router's environment and the turn's login shell
  expands `$HOME` to the agent user's. r4t writes the file through that same
  sudo grant at turn start, merging the a8s server into whatever is already
  there and leaving every other server and every field it does not recognise
  intact. The knob stays **opt-in** (like cursor's): r4t is writing into a
  directory it does not own. It is refused, by name and with the fix, on a rig
  with no isolation (the home is the router's), on a container org, and where
  a second agy member shares the Unix user — one config names one staging
  outbox, so their sends would cross. `roster check` reaches the same verdict
  before a wake pays for it. That file is global to the Unix user across every
  org and node, which no roster scan can see, so it carries its own ownership
  record too: a turn that finds an a8s entry naming a different staging outbox
  refuses by that path instead of overwriting it. `mcp off` on an agy rig takes
  r4t's own entry back out — agy reads that file whether the knob is on or not,
  so a member left holding `a8s_tell` while its prompt teaches the shell command
  would send into whichever outbox the stale entry names. An entry owned by
  another outbox is never removed — but it is reported, because that member
  loads the tool anyway and no turn of its own will ever clear it. A removal
  that cannot happen is logged rather than made the turn's verdict.

### Fixed
- **`r4t engine codex quota` works on current codex.** The probe sent
  `-a untrusted`, an approval policy the CLI dropped somewhere before 0.149;
  a codex that new refuses the argv and exits before the handshake, which read
  as a 15-second login timeout because the child's stderr was discarded. It
  now sends `-a never` — accepted by every codex in the field, and stricter
  than what it replaces — and quotes the CLI's own complaint whether the probe
  comes back empty or the CLI exits fast enough to break the pipe under the
  first write.
- **`r4t engine cursor quota` works off macOS.** The Cursor state database was
  looked for at a hardcoded macOS path, so the engine could never find a token
  on Linux or Windows and blamed the login for it. The path now resolves per
  platform, the failure names every path it searched, and it says that the
  token comes from the Cursor IDE — which installing the `cursor-agent` CLI
  alone never produces. `R4T_CURSOR_STATE_DB` points at the database outright
  for the case no rule can guess, such as a WSL shell whose Cursor is
  installed Windows-side.

## 0.1.74

### Changed
- **History and day-log headings speak delegator-local time.** A history
  entry's `## <stamp> from|to <party>`, a day log's `## <stamp> dispatch ...`
  line, the sandbox report header, and the turn-capture meta block now stamp
  with the machine's local zone (`2026-08-20 16:41:55 PDT (UTC-07:00)`)
  instead of UTC — a model reads these every turn, and UTC-only stamps read
  as living in the wrong day. The parenthetical offset keeps the instant
  reversible on any machine. Filenames, the retention window, and JSON
  payload stamps stay UTC; the sandbox conversation table sorts entries by
  resolved UTC instant, not by the display string.

## 0.1.73

### Changed
- **Folder remotes and sync-folder storage reap by default.** A shared
  folder nobody swept only grew: `--retain-days` on both the `folder`
  remote and the `sync_folder` storage service now defaults to 3 (72h) —
  enough to outlast the broker's ~24h retention and cover a 3-day weekend
  outage — instead of off. `--retain-days 0` keeps everything forever, same
  as before. The folder is a wire, not an archive: delivered mail and
  attachments are already archived per-machine (`conversations.sqlite3`,
  `.files/`), so what stays behind in the shared folder is only ever a
  spare copy. A file is reaped only when both its ULID mint time and its
  mtime clear the window: mtime alone can be pushed forward by a resync,
  which only delays the reap, and mint time alone would let a sender
  resuming from a long outage delete its own just-published message while
  reporting success. The sweep now rides the receive side too — both
  transports previously swept only on publish, so a machine that only
  receives never reaped — throttled to once an hour so it costs one
  directory pass per hour rather than one per publish or poll.

### Fixed
- **Dreaming no longer reports a store that never happened.** A live roster
  ran nineteen days taking nothing into its knowledge store while logging a
  successful dream on every idle pass. Three faults in a row, each of which
  would have caught it alone:
  - r4t built the distill bridge from the member's turn rig and handed it to
    k7e with no isolation wrapper. Under `run_as` the harness is installed in
    the agent user's home and authenticated as that user, so the router user
    cannot invoke it — `agy` was not even on its PATH. `Rig.distill_command`
    now takes the org's `run_as` and wraps the bridge in the member's own
    cage, using a login shell because an invoke names its harness bare.
    Container isolation, which the bridge cannot cross, no longer gets a
    rig-derived command at all — the store's own `distill_command` answers
    there, because an operator who configured one has said how to cross.
  - k7e's `distill` exits 0 whether the LLM produced nothing or never ran.
    It now separates the two: a pass whose every LLM call failed exits 1 and
    says why, and a pass that lost only some calls warns. A bridge that ran
    and found nothing novel still exits 0. This is what stops the data loss —
    r4t already declines to advance its watermark on a non-zero exit.
  - r4t's own log line claimed captures went "into the knowledge store" on
    the strength of that zero exit. It now counts what k7e reported and says
    `no new knowledge` when that is the answer.

  Turn captures keep only the most recent 50, so on the affected deployment
  everything before the last three days was consumed and discarded. The
  watermark advancing past captures nothing ever read is the shape to watch
  for elsewhere.
- **A slow capture no longer wedges dreaming.** With the bridge repaired, the
  same roster hit the next failure along: the batch is counted in captures but
  the cost is in bytes, and five captures ran past the 600s call timeout. The
  whole-batch call committed the 48 entries k7e had stored on the way and
  advanced the watermark by nothing, so the following pass redrew the same
  batch and timed out again — dreaming wedged on those captures permanently
  while re-paying for work already in the store, and each attempt held the
  node's only wake slot for ten minutes. Captures are now distilled one per
  call with the watermark advancing after each, so a slow one costs its own
  progress and no more. The timeout is now named (`DISTILL_TIMEOUT_SECONDS`)
  and applies per capture, under a `DREAM_BUDGET_SECONDS` wall-clock budget for
  the whole sweep — per-capture limits multiply, and an idle pass that outlives
  the definition's `max_wake_seconds` is killed from outside, costing the sole
  wake slot and the rest of the pass.
- **A distill that lost part of its input no longer reports success.** Three
  ways a broken bridge still read as a quiet one: a capture is chunked, so one
  failed call among many exited 0 and let the caller watermark the whole
  capture, dropping that chunk for good; a bridge that printed an auth error to
  stdout and exited 0 looked like an answer, because at that layer any
  non-empty stdout is one — the required JSON array's absence is now the tell;
  and the failure ledger was global, so a dead reranker (reached through
  `diff_against_store`'s own search) failed a distillation that had worked,
  retrying that capture forever. Failures are now recorded per purpose and any
  distill-purpose failure fails the run.
- **The distill bridge stops pointing the harness at the store.** `{workdir}`
  is the harness's working directory, and under `run_as` the documented store
  posture is a 0700 router-owned dir — an opencode-class rig was being sent
  exactly where the cage excludes. It now receives the member's own workdir;
  store I/O stays router-side, where only the model call crosses the boundary.
  The `{workdir}` placeholder only reaches two built-in rig classes, though —
  every bridge also inherits its *process* cwd from k7e, which launches it
  inside that same store — so the `run_as` wrapper now `cd`s to the member's
  workdir before the harness starts, covering every rig class the same way —
  and the `cd` runs in a startup-free `sh` that then `exec`s the login shell,
  because a login shell sources profiles before its `-c` string, still inside
  the unreadable store.
- **Every unreadable capture gets its `DREAM-SKIPPED` line**, not just whichever
  one happened to be last in the batch. Candidates k7e parsed but could not use
  get a `DREAM-DROPPED` line rather than dying in stderr.
- **Only a payload the parser can use counts as an answer.** A bracket-shaped
  string is not one: `Error: token [expired]` and `Error code [401]` both passed
  a shape check, and neither was ever read. The parse decides now — no array, an
  unparseable one, a non-array payload, or an array with nothing usable in it
  all record a failed call. A valid `[]` stays a real answer. The boundary is
  whether the model *attempted* the schema, judged separately from whether
  anything validated: an array carrying even one candidate-shaped item — a dict
  with title and content keys, whatever their types — proves the chunk was
  read, so every discarded item (wrong types, missing keys, bare non-objects)
  prints its own drop line and the run stays exit 0; only a payload with no
  candidate-shaped answer at all (`[401]`) fails. Failing an attempted payload
  would re-offer input against a model that shapes it identically every time.
- **A media bridge cannot store its own error as knowledge.** A response with no
  JSON object anywhere fell through to a raw-text fallback that made
  `Error: authentication expired; sign in again` a knowledge entry — worse than
  losing the file, since the store then recalls it as a fact. Unparseable
  brace-bearing prose and well-formed objects with no `content` field both
  record a failure and store nothing; the raw-text fallback survives only for a
  parsed object whose `content` is a truthy list or dict — the model that
  plainly tried, which is the case it was built for. Key presence alone was
  still too weak: a structured error object carrying `"content": null` (or
  empty) is a failed call, not an attempt — and so is an explicit error
  envelope, whatever its `content` carries. A truthy `error`, an explicit
  `success: false` or `ok: false`, a numeric `status`/`code` outside 2xx, or
  a serialized form of the same, is rejected before content handling. The
  `success`/`ok` flags follow the same positive-recognition rule as
  `status`/`code`: serialized false forms (`"false"`, `0`) and any
  unrecognized flag value now reject too, closing the gap where a literal-
  `False` check let `success: "false"` and `ok: "false"` ride straight
  through. A serialized `status`/`code` string is tokenized and judged by
  positive recognition, not a failure-word blocklist: it passes only when
  every token is benign — a stable success word (`ok`, `success`, `succeeded`,
  `complete`, `completed`, `done`), a 2xx 3-digit token, or `http`/`https`
  filler — and at least one token is positively successful, so `"200 OK"`,
  `"HTTP 204"`, `"SUCCESS"`, and `"COMPLETED"` pass while any non-2xx 3-digit
  token or unrecognized word (`"401 Unauthorized"`,
  `"AUTHENTICATION_ERROR"`) rejects the value. The rule is now complete by
  type, not just by value: a boolean `status`/`code` means what it says
  (`false` rejects, `true` passes) instead of being skipped as if only a
  numeric value could reach the 2xx test, and a container value (`dict`,
  `list`) is unrecognized envelope state and rejects rather than falling
  through an isinstance chain that had nothing left to check it against. The
  failure-word blocklist
  (`error`, `err`, `failed`, `unauthorized`, `denied`, `invalid`, …) is gone:
  it let `RATE_LIMITED`, `THROTTLED`, and `RESOURCE_EXHAUSTED` — none of
  which matched any listed
  component — ride straight through as content, because failure vocabulary is
  open-ended (there is always a next provider's next code) while success
  vocabulary is small and stable. The bar is still deliberately narrow in the
  other direction (`"error": null` and `"status": 200`, numeric or string,
  beside real content still pass; an empty tokenizable value carries no
  signal and passes), because a missed envelope and an over-broad match are
  both permanent, not one cheap and one costly: the former poisons the store,
  the latter wedges the capture in retry forever, since r4t re-offers the
  same capture on every idle pass. The fallback itself no longer admits
  arbitrary truthy containers — only the
  recognized fragments shape, a non-empty list of strings, which is the one
  the #70 case pinned. Whitespace-only content is the unnormalized spelling
  of empty on both paths: a scalar `content` string is failed (and otherwise
  stripped before use) when nothing survives `.strip()`, and an all-whitespace
  fragments list is a failed call while one real fragment still opens the raw
  fallback.
- **The sweep budget covers embedding.** Bounding distill alone left one
  `EMBED_TIMEOUT` per knowledge member running after the budget was spent, which
  on six stores reaches past the wake ceiling on its own.

## 0.1.72

### Fixed
- **The a8s test suite can no longer touch live state, on any platform.**
  A native-Windows verification run caught the suite overwriting the
  developer's real registry and publishing fixture envelopes to the real
  brokers: four e2e sites redirected `HOME` alone, and `ntpath.expanduser`
  never consults `HOME` — resolution fell through `USERPROFILE` to the real
  home. Every home redirect now goes through one conftest helper
  (`set_home`) that pins all four resolution variables, and a session-wide
  `A8S_HOME` floor under pytest's tmp root turns any future bypass into a
  write to a throwaway directory instead of a live one.
- **The rotation fix got the test its arithmetic deserved.** 0.1.71's
  strict-alternation test never exercises a skip — its inboxes stay full,
  so the old and new counter arithmetic agree at every step it takes. A new
  deterministic transient-skip test makes the preferred agent unready
  exactly once and asserts the counter lands one past the *woken* position
  (the skipped agent gets the very next turn); it fails on the pre-fix
  arithmetic, which is the property a regression test is for.

### Changed
- `CLAUDE.md`'s per-suite test counts caught up with reality (a8s ~1440,
  r4t ~1450, ar3 ~175, k7e ~190).

## 0.1.71

### Fixed
- **Wake rotation advances past the agent that woke, not by one.** When the
  rotation's preferred agent was transiently unready and a later sibling took
  the slot, `wake_rr += 1` parked the counter on the agent that had just
  woken — handing it a double turn. The counter now advances by position
  (`start + woke_index + 1`), the way the idle rotation always has, matching
  the stated contract: next after the one that woke.
- **The strict-rotation test stops racing real subprocesses.** 0.1.70's
  release run failed on both platforms because the wake-fairness test
  asserted strict `A,B,A,B` alternation against real wake subprocesses,
  whose completion timing on a loaded runner can legitimately hand a sibling
  two turns. The test now stubs the wake to a deterministic consume — it
  asserts the rotation arithmetic, which is what it names — while the
  starvation-bound test keeps covering fairness under real timing.

## 0.1.70

### Changed
- **Time is local where a human or a model reads it, UTC where it sorts.**
  Every timestamp the suite showed was UTC, so agents concluded they live in
  UTC and every relative word they wrote — *today*, *tomorrow*, *this
  morning* — resolved in the wrong day. Display now reads in the machine's own
  zone and always names it: `a8s logs`, `a8s convo`, the wake ticker lines,
  the remote `joined` display, `r4t status` (a new `time:` line) and `r4t logs`
  (a new day header) all show `2026-08-16 13:22:04 PDT`, or `UTC-07:00` in
  place of the abbreviation on a platform that has none — the Windows seat
  spells zone names the long way. **The prompts are the real fix**, because the
  reader that matters is the model: a8s's composed batch prompt opens with
  *Local time is 2026-08-16 13:22 PDT. Every date and time you read or write
  is this zone unless it carries an explicit offset.*, r4t's member intro
  closes with the same statement about relative words, and a new `$NOW`
  built-in puts the anchor in a single-message wake — every bundled definition's
  prompt now leads with `[$NOW]`. **Storage did not move.** Agent log-line
  prefixes, envelope `date`, the conversation archive, r4t's day-log filenames
  and its retention window are all still UTC, byte for byte, because those are
  sort keys: `a8s logs A B` merges two files by that prefix, `prune_day_logs`
  string-compares day names, and a portable org's log directory is shared
  between machines that are not in one zone. `a8s convo --heading-*` gains a
  `{utc}` placeholder for the stored value. The zone is the machine's, so `TZ`
  is the only knob — a caged r4t member takes `"env": {"TZ": "..."}` on its rig
  — and there is no new zone field anywhere. New shared module `ark/clock.py`.

### Fixed
- **A wake's stdout can no longer wedge the runner** (the alive-but-deaf
  incident: a runner sat 8 hours with 34 routed messages queued because a
  grandchild of an exited wake held the stdout pipe's write end open, and the
  runner's read-to-EOF never returned). A dedicated reader thread now owns
  each wake's stdout; after the wake exits the runner drains it for a bounded
  grace (`wake_drain_grace_seconds`, default 5) and then closes its own end,
  logging the suspected inherited handle. Side effect: wake output now
  streams live on Windows, where the old `select`-on-a-pipe path could not.
- **An alive-but-deaf runner recovers itself.** A watchdog thread on every
  resident `a8s run` watches three clocks — the dispatch loop's own beat, the
  in-flight wake, and the oldest undispatched inbox message. When the beat is
  stale and addressed mail has waited longer than `watchdog_wedge_seconds`
  (default 120, 0 disables), it closes the wake's stdout, terminates the
  wake's own process group — never anything detached from it — and logs a
  `WEDGE` row recording recovery or, failing that, a loud alert. An active
  turn is never touched: a wake alive and inside its `max_wake_seconds`
  budget means the stall is elsewhere (a slow storage upload can hold an
  iteration past the threshold), so the watchdog logs and stands down. On a
  drain timeout the exited wake's surviving group members are terminated —
  the grandchild holding the pipe dies with the turn it leaked from.
- **`a8s tx` no longer logs `DROPPED` for messages that deliver.** On a
  shared broker topic every node sees every envelope; one addressed to an
  agent this node does not host now logs `NOT_LOCAL` (console `REMOTE_SKIP`),
  and a malformed control envelope logs `DISCARDED`. `DROPPED` is reserved
  for terminal paths — grep for it and every hit is real.
- **`tell` stops warning on every send from a deliberately exported
  `TELL_OUTBOX_DIR`.** The hijack note now fires only when the CWD sits
  inside a *different* registered agent's root — the one shape where
  misattribution is real. A CWD no agent owns is explicit configuration, and
  a sibling sharing the owner's repo root is not a hijacker.
- **The locale codepage can neither crash output nor corrupt input.** The
  stdout `backslashreplace` floor is now `core.harden_stdio()` and runs for
  `tell`/`tells` too, with a cp1252 regression test pinning `a8s tx`. The
  same call re-pins stdin to UTF-8: a Windows seat's cp1252 decode was
  storing every piped non-ASCII body as permanent mojibake with nothing
  telling the sender — the envelope store's contract is UTF-8, so stdin
  decodes as UTF-8 no matter what the locale (or `PYTHONIOENCODING`) says,
  invalid bytes escaping reversibly. The floor covers both raising stdout
  modes — strict and surrogateescape (the interpreter's own default under a
  C locale and on some Windows pipe setups), whose difference cost a field
  seat a crash *after* the send had committed: the exit code lied and a
  retrying caller would double-send. A seat needs no `PYTHONUTF8=1` for
  either direction.

### Added
- **Runner lifecycle in the transaction log.** `RUN_START`, `RUN_STOP`,
  `WAKE_START`/`WAKE_RETURN` per envelope, a throttled `HEARTBEAT`
  (`txlog_heartbeat_seconds`, default 300, 0 disables), and `WEDGE` — so a
  dead or deaf dispatcher is visible from `a8s tx` in minutes, not
  reconstructed from per-seat logs after the fact.
- **`r4t add <dir> [<runbook>]` — one name registers everything.** One command
  validates a runbook and puts a directory on the network: the a8s agent, the
  namespace prefix, the node directory and the address you type are all the
  same word, the directory's own. The runbook resolves the way an a8s
  definition does — a built-in by bare name (`r4t add ~/proj triforce`), an
  explicit path, or the `r4t.md` already at the directory — and it is validated
  fully, loudly, with the file:line errors the loader already produces, before
  anything is registered. Registering nothing on a failure is the point: a
  node that exists is a node that runs. Re-adding a registered name refuses
  with the remedy rather than duplicating it, matching `a8s add`, and it names
  the reason you rarely need it — the runbook is re-read every turn, so a
  changed roster needs no re-add. **`r4t init` retargets to match**: it writes
  a starter `r4t.md` extending `triforce` and stops there. Two verbs, one job
  each — init writes the file, add registers the node — and the stale advice
  that a namespace prefix cannot share its agent's name is gone with the
  `-node` suffix it produced.
- **The node is the namespace, and `:name` is the way out.** Three address
  forms and no fourth: `node` is the roster leader, `node:member` is that
  member, and a leading colon means the global a8s space — `:bob` is the
  outside node named bob even when this roster has a member called bob. That
  is the one case in the grammar where the colon is mandatory, and it is
  stripped on resolution, so the recipient replies to a plain name without
  knowing a marker was typed. Qualifying your own node is a no-op
  (`acme:bob` means the same thing inside `acme` as outside it), so runbook and
  charter text is portable verbatim, while `:acme:bob` deliberately leaves the
  walls and comes back at the ingress gate. Colons are for namespacing only:
  a member, cell or node name is refused if one appears inside it, and
  `r4t roster check` now points a shadowed name at `:name` instead of
  shrugging.
- **`Ingress:` is enforced, and a walled member is refused rather than
  redirected.** External mail addressed to `node:member` reaches that member
  when it carries `- **Ingress:** yes` — on by default for the leader, off for
  everyone else — and is otherwise dead-lettered with the reason, one
  `REFUSED` line on the ticker, and a message naming both remedies. Silently
  landing it on the leader would have the leader answer for a member that
  never saw it, and the sender would never learn its address was ignored. An
  unknown sub-address and a cell address are refused the same way; one post
  forked to a whole cell is deferred and says so by name.
- **The trust ceiling — a repo cannot raise its own permissions.** A runbook is
  checked in and its `## Rigs` blocks name permission stances, so the runbook
  proposes and the machine caps: an out-of-repo ceiling, `auto` by default,
  fails any repo-declared rig that asks above it, with the remedy. `r4t add
  <dir> --trust` raises it for that node, once, knowingly, and records it
  machine-side where nothing in the repo can reach it. The check runs wherever
  the roster loads, which is every turn, so editing `r4t.md` to `bypass` after
  an untrusted `add` fails closed at the next wake rather than the next `add`.
  A machine `rigs.json` rig is untouched — that stance is the operator's own.
- **The runbook — one `r4t.md` that says what the team is.** A node's whole
  configuration is now one markdown file at the node directory: YAML
  frontmatter plus six closed H2 sections (`Mission`, `Charter`, `Roster`,
  `Cells`, `Rigs`, `Rituals`), replacing `ROSTER.md` + `MISSION.md` +
  `CHARTER.md` + `rigs.json` + `r4t-org.json`. One block grammar serves every
  collection — a leading run of `- **Key:** value` bullets, then prose the
  model reads verbatim — and the bold is optional, so `engine: claude --model
  opus` parses exactly as a person would write it. A member is complete with
  that one line: the engine line is a `r4t engine <id> run` invocation minus
  the prompt, and promoting it to a `## Rigs` block is cut, paste, name it.
  **A rig declared in the runbook shadows a machine rig of the same name,
  whole-block** — never field-merged, so a runbook can never inherit a
  permission stance you cannot see. `extends:` names a base (the shipped
  `triforce` and `ark-suite`, or a path); frontmatter merges per key and an H2
  section replaces whole, which is also how a runbook splits across files.
  `${VAR}` / `${VAR:-default}` / `${VAR:?message}` resolve from the node's a8s
  vars in field values and prose — never a heading, never frontmatter — and an
  unset variable with no default is a hard error rather than an empty string; a
  node var named `MISSION` replaces the `## Mission` section outright, which is
  how one runbook serves two projects. `r4t runbook show --resolved
  [--sources]` prints the merged, interpolated truth and names the layer every
  section came from, and `r4t runbook check` lints it with every error naming
  the line, the token and the closed set it should have come from. Colons are
  refused inside member and cell names, an unknown or repeated `##` is a loud
  error naming the six, and `## Charter` reaches every member's prompt where
  the mission reaches only leads. A `r4t.md` wins over a legacy `ROSTER.md` in
  the same directory, which is named as ignored rather than blended. v1 does
  not carry H3-level merge, `Remove:` tombstones, or an `r4t/` directory
  convention — the `extends:` chain is the split, and a runbook using one of
  them is refused as deferred rather than unknown.
- **The leader is the node's door, and a roster without one is refused.** Mail
  addressed to the node with nothing past the colon lands on the roster leader
  — the apex takes the node's mail, so it queues, threads, batches and narrates
  the ticker exactly like member-addressed mail. What is new is the guarantee:
  a ROSTER.md that marks no leader, or marks two, now fails when the roster
  loads, naming the remedy, instead of loading fine and dead-lettering the
  first bare message to arrive.
  `r4t roster check` still reads a broken roster and reports every problem in
  one pass — the tool that diagnoses a roster has to be able to load a wrong
  one — and a roster that will not load now says so on the node log instead of
  draining silently.
- **Per-node a8s vars reach the mailbox path fields, so two agents can share
  one repo.** `outbox_dir`, `inbox_dir` and `files_dir` now interpolate the
  node's vars plus a new path-field built-in `$NODE` (the registered name), so
  a definition carrying `".outbox-$NODE"` gives every node rooted at the same
  directory a mailbox of its own — where before both resolved `<root>/.outbox`,
  one handler won the scan race, and the router stamped the winner's name on
  the loser's mail. A path field naming an unset var makes the node
  *unresolved* rather than falling back to the default: it is skipped by
  routing, `a8s ls` shows `unresolved: $KEY`, `a8s start` refuses it, and
  `a8s health` names it — a fallback would silently re-create the collision.
  Per-message placeholders (`$SENDER` and friends) are refused in a path field,
  and `definition.env` stays literal: a var reaches argv and a mailbox path,
  never the child's environment. Re-pointing a mailbox var is refused while a
  handler is attached, and the carry-over is transactional when it is allowed:
  a destination collision or unreadable source aborts the whole switch with
  nothing moved and nothing saved, and a mid-move failure rolls back — the
  registry never records a path the mail did not fully reach. Every path the
  tool un-points is remembered on the node's `retired_mailboxes` list, and
  `a8s health` walks that list — nested and absolute paths included — plus
  any non-empty `.outbox*` / `.inbox*` / `.files*` directory under a
  registered root that no node owns, pruning retired entries once they empty.
- **The node log narrates the roster — one line per dispatch lifecycle
  event.** `a8s logs <node> -f` used to show a wake starting and a wake
  exiting and nothing in between: r4t wrote its narration only to its own day
  log. Every lifecycle event now also goes to stdout, flushed as it happens,
  which is what a8s pumps into the node's log — so one `-f` on one node is the
  roster running, in order: `QUEUED` (a message joined a member's queue),
  `TURN` (a turn started, with its batch size, rig and conversation path),
  `DONE` (exit and duration), and `RESTING` / `BREAKER` / `DEFERRED` for a
  member with mail that did not run, carrying the reason. Never a message body
  and never transcript text — those stay in the day log, where `r4t logs`
  scopes them.
- **`r4t tell --as <member>` — speak into the roster as another member.**
  The owner's impersonation verb, for jumpstarting a member's queue or
  diagnosing where a message lands without waiting for a real sender. Routes
  through the same ingest path a real member-to-member send takes, stamped
  `from` the impersonated member, and narrates the ticker (`r4t: QUEUED ...`)
  like any other arrival. Refuses an unknown `--as` or `--to` name loudly.
- **`r4t logs` scopes to several members or a whole cell.** `--agent` is now
  repeatable, and a new `--cell <cell>` follows every member in a roster
  cell — both work with `-f`.
- **The rotation, and one place that prints it (#179).** `r4t status` opens
  with a Now / Next / Then / Held block above Health: who is running and for
  how long against its timeout, who goes next **and why**, and every held
  member with its blocker and its next verb. "Why" is words before numbers —
  `ingress + passed over 2   score 3` — because a bare number is a symptom
  with no cause. The selection is `schedule.next_up`, the same call the drain
  loop makes: status never re-derives the ranking, so the printed answer and
  the taken one cannot drift.
  Two tiers. **Tier 1**: a member holding mail from a priority sender goes
  next, always — `priority_senders` in the org config, `fnmatch` globs,
  default `["neil*"]`, `[]` to empty the tier. It never preempts; a running
  turn always finishes, so the promise is *next*, not *now*. **Tier 2**:
  `score = 2*ask + 1*ingress + passes`, ties broken by oldest message then by
  name. `ask` is the future r4t-only verb and contributes 0 by construction
  today; the term is visible so the ladder does not change shape when it
  lands. `ingress` is mail from outside the roster, stamped `origin` on the
  envelope at the wall. `passes` is aging counted in turns, not seconds:
  it rises for every ready member a selection skips and resets when the member
  runs, and a member the budget held back does not age — the scheduler did not
  pass it over, the budget did. Because the classes can add at most 3, a
  member passed over 4 times outranks anything freshly arrived mail can carry.
  The run queue itself is derived, never stored: a member is queued when its
  inbox holds a message, and there is no scheduler state file to disagree with
  the inboxes.
- **A killed turn no longer loses its batch.** A turn claims by MOVING its
  envelopes into `agents/<member>/queue/.inflight/` rather than deleting them.
  A clean end drops them; a failed turn moves them back under their original
  filenames, so they keep their ids and their place in arrival order instead
  of being minted afresh behind whatever arrived meanwhile. A `SIGKILL`, an
  OOM or a closed lid simply leaves them there, and every idle wake returns
  each in-flight batch whose member holds no live lock — the PID lock is the
  liveness test, so a turn genuinely running is left alone.
- **A member whose harness cannot start parks, instead of failing forever
  (#138).** An exec that never started — the binary is not on `PATH` — fails
  identically on every retry, and the breaker's answer was a probe turn every
  ten minutes with a fresh error to the sender each time, forever. The FIRST
  such failure now parks the member: one `PARKED` ticker line, one day-log
  line, then silence. Its queue holds untouched, which is what makes the
  silence safe, and it leaves the rotation entirely. It returns when a probe
  that costs nothing says the cause is gone — `shutil.which` on every idle
  wake, no subprocess and no tokens — or when `r4t resume <member>` /
  `r4t resume --all` says so by hand. A timeout, a nonzero exit with output, a
  network error and an exhausted quota stay transient and keep the ordinary
  breaker.

### Changed
- **ar3's own roster runs on a runbook — the format's acceptance test.**
  `org/AR3/` collapses from six files in three formats (`ROSTER.md`,
  `MISSION.md`, `CHARTER.md`, `rigs.json`, `definition.json`,
  `r4t-org.json` — 429 lines) to one 382-line `r4t.md`, and a five-member
  roster with cells, rigs and rituals loads with no error and no warning.
  Nothing load-bearing moved: each `Engine:` line rebuilds the deleted
  `invoke` array byte for byte, the widened `Allowed tools:` and the rig
  budgets are fields, and the `_notes` strings that carried the reason for
  each rig's shape are readable prose under the block instead of escaped JSON.
  The workplace repo, `comms` and `egress` are frontmatter, and the node's
  hand-copied `definition.json` is gone: `r4t add` registers against the
  bundled `r4t` definition, and idle resolves its node from the directory it
  wakes in.
- **The product is named `ar3` (owner ruling, 2026-08-16).** The suite takes
  the name of its front-door command, styled like `a8s`, `r4t` and `k7e`:
  lowercase always, "the ar3 suite" where the longer form reads better. Every
  user-visible surface follows — `--version` prints `(ar3, python …)` on all
  four CLIs, the bare `ar3` tagline, `--help`, the installed skill
  descriptions, the installer messages, and the mirror's release titles and
  commit subjects. The `ark/` package, `arkver`, the `ar3`/`a8s`/`r4t`/`k7e`
  commands and the `AR3` node are unchanged, as is the build-along guide's
  title, *The Ark Raising*.

### Changed
- **Continuation is gated by the label, the clock, the last exit, and the
  engine.** `Continue:` is still the opt-in and still time-valued
  (`Continue: 15m`), and a turn now founds a fresh conversation rather than
  resuming one whenever the previous turn did not exit clean (`CONTINUE-DIRTY`)
  or the conversation sat idle past the member's window (`CONTINUE-STALE`) —
  the idle sweep spends a dump turn on the graceful path, and these are the
  backstop for a roster whose idle pass has not run. Each measured engine
  preset also carries a continuation grade: **cursor** is good (resume is keyed
  on an MD5 of the absolute working directory, verified), **codex** is moderate
  and its preset now passes `--include-non-interactive` so `resume --last` can
  see the roster's own `codex exec` turns, and **claude** is poor — resuming
  `claude -p` across a process boundary re-wrote the whole conversation 40.6%
  of the time against a 2.5% same-process baseline, and every roster turn is a
  new process. `Continue:` on a poor-graded preset is now a config error naming
  the measurement rather than a quiet cost. Engines the research has not
  measured are ungraded and continue exactly as before, and
  `r4t engine run --continue` is untouched: one process, and the operator's
  own choice. `r4t rig presets` prints each preset's grade beside its
  continuation tokens, so the refusal is visible where a preset is chosen.
- **A stalled org is re-engaged by the mission-review heartbeat alone.** With
  no ledger to ask, a stall is now "the drain ran nothing, every queue is
  empty, no turn is live, and no member has finished a turn since the last
  tick" — the turn-completion stamp is what keeps a stall distinguishable from
  a lull across idle passes. One general mechanism replaces the watchdog that
  used to chase individual unanswered threads.
- **`r4t clear` prunes locks, drains, and applies log retention.** It no longer
  expires thread ledgers, and its `--older-than` flag is gone with them.
- **One turn at a time is a contract, not a setting.** A node runs exactly one
  member turn, start to finish, and only then asks who goes next. That is what
  makes `a8s logs <node> -f` followable by a person: nothing interleaves
  because nothing is concurrent. The rotation is always arithmetic and never a
  model call — a queue whose order came out of a model is a queue nobody can
  explain. The drain loop re-selects after every turn rather than sweeping
  members alphabetically. Parallelism is a **second node**, on another machine
  or this one; the rig spend buckets are machine-global, so two nodes on one
  machine cannot double your burn behind your back. What you give up is the
  single watchable stream, and one hung member stalling the roster until its
  per-turn timeout ends it — which is why the running row prints
  `1m12s of 45m`.
- **The cadence throttle is off by default** (`throttle.min_seconds_between_
  turn_starts`, 15 -> 0). Under serialization there is no pile-up left to
  defend against, so a standing gap between turns is dead air that also makes
  "who is next" stop being immediate. It stays as opt-in slow motion for
  watching a rotation go by.
- **The idle pass runs every 60 seconds, in a strict order, and says nothing
  when nothing happened.** `idle.timeout` in the r4t node definition drops from
  300 to 60. A pass now recovers orphaned in-flight batches first (everything
  after it reads the queue and has to read a true one), prunes stale locks,
  probes parked members, drains, dreams, runs the stall heartbeat, and retires
  idle conversations last. A pass that finds nothing prints nothing at all: at
  a one-minute cadence a heartbeat line would be over a thousand lines a day in
  the one stream the roster is meant to be watchable in — `r4t status` carries
  the idle time instead. Because the heartbeat's backoff ladder counts idle
  WAKES, a wall-clock floor of 30 minutes now bounds it too, so shortening the
  wake interval cannot multiply a paid leader turn five-fold without anyone
  editing a policy.
- **The guide teaches the runbook path, and its transcripts were re-captured
  against this build.** *The Ark Raising* chapter 2 founds a roster with
  `r4t init` → edit `r4t.md` → `r4t add` in place of a hand-written `ROSTER.md`
  and the three-command a8s registration, and it reads the ticker and the
  rotation instead of a seat inbox; chapters 3–6 lose the retired `r4t seat`,
  the task layer and the chat TUI, and speak to the roster from the chapter 1
  a8s seat with `tell`. Every "You should see:" block on the reworked path was
  re-run on a throwaway HOME rather than edited by hand. The roster templates
  under `guide/templates/` are `r4t.md` runbooks now, and a new
  `apps/r4t/tests/test_guide.py` pins each receipt the chapters quote to the
  source that prints it, so a reworded line fails a suite instead of rotting a
  tutorial.

### Removed
- **`throttle.max_concurrent` and the per-rig `concurrency` (owner ruling,
  2026-08-16).** Both are deleted outright with no lineage: no explanatory
  error, no pin-to-1 that ignores the value. A knob that can only hold one
  value invites the question "what if 2?", and the answer — "nothing, it is
  ignored" — is worse than the question. Leaving them parseable would also have
  silently opted every existing config *out* of the contract, since
  `max_concurrent: 0` meant unlimited. Nothing the repo ships set either to
  anything but 1; `r4t rig set <rig> concurrency` now fails with the ordinary
  unknown-setting error, and a leftover key in a hand-written config is
  ignored like any other unknown key. `r4t status` and `r4t rig list` print
  `contract: one turn at a time (cadence 0s)` where the throttle line was, and
  the `CONC` column is gone.
- **The `r4t chat` TUI (owner ruling, 2026-08-16).** The interactive chat
  window — Textual front end and line-UI fallback alike — is gone: a scoped
  `r4t logs` is the window onto a running roster, and `r4t tell --as` is the
  door back in. `chat_tui.py` is deleted and `textual` drops out of
  `requirements/r4t.txt`.
- **The human seat, and the task layer behind it (owner ruling, 2026-08-16).**
  Fire-and-forget is now the rule inside the walls: a message carries no task
  and demands no answer.
  - **The seat.** No `r4t seat` verb, no seat mailbox, no parking, no
    `Address:` doorbell and no `doorbell_check` org setting. `chat.py` is
    deleted. The operator's way into a roster is `r4t tell --as <member>` and
    their way to watch it is `r4t logs`.
  - **The `Human:` and `Address:` roster fields.** A roster is members that
    take turns, so every member carries a `Rig:` and the operator has no row.
    Both fields now name themselves as retired and disable the member that
    carries one. The Ark's own roster keeps Ares as prose about the PR gate
    rather than a member entry.
  - **The task ledger and every answer obligation.** `tasks.py`,
    `tasktrace.py`, the `r4t task` verb, the `quiet_task_seconds` knob and the
    quiet-thread sweep are gone, along with thread status, thread closure, the
    `ANSWERED` line and the ingress/inside distinction that existed to decide
    what was owed. **Thread ids stay**: they are message lineage, and the
    ticker, the day log, the dead-letter record and reply attribution all still
    carry them. This resolves #58 by removing the code the bug lived in.

## [0.1.69]

### Added
- **Every bundled engine node ships an `-unrestricted` variant.**
  `a8s add amos ~/agents/amos engine-cursor-unrestricted` is the same node as
  `engine-cursor` with `--permissions bypass` on all three wakes — for an
  agent on its own machine and its own account. The stance is chosen by
  definition name at `add` time and lives visibly on the invoke lines; the
  base variants never grow it.
- **`a8s remote <name> <folder>` — a folder remote: messages cross through a
  directory a sync client already watches (#169).** One `<ULID>.json` file
  per envelope, the same bytes MQTT carries, beside the `<ULID>/` attachment
  bundles — no broker, port, host or account, because the user's own
  iCloud/OneDrive/Drive/Dropbox is the wire. The same command registers the
  folder as a `sync_folder` storage service under the same name, so a
  message's attachments travel with it. Options: `--poll-seconds` (15),
  `--prefix` (none), `--retain-days` (off). Nothing is deleted on receive:
  each machine keeps its own consumed-ULID ledger under
  `~/.config/a8s/folder-remotes/`, registration stamps a `joined` cutoff so a
  machine joining an old folder is not owed its backlog (with a one-hour skew
  allowance so a slow-clock peer's new mail still lands), and `a8s health`
  probes reachability without consuming a single waiting envelope.
- **`r4t rig fuel <rig>` — how much tank a rig has left, as one number in
  0..1 (#152).** `r4t engine <id> quota` reports every dial an account
  carries; fuel keeps the ones the rig's own model burns and reports the
  binding one, so an Opus rig and a Fable rig on the same subscription read
  different numbers. A rig on a local engine reads 1.00, an unlimited seat
  reads `null`, and `--json` carries the number, a `state` naming why it is or
  is not one (`gauged` / `unlimited` / `unconstrained`), and the buckets it
  came from. Nothing runs and no budget moves.

### Changed
- **The Ark Raising, chapter 1, is written around Claude Code.** The harness
  choice is now the question "what software are you using?" — `claude` is the
  spine, `cursor`/`codex`/`agy` are one-word swaps, and the ollama/OpenCode
  free path runs alongside as before. The reader no longer hand-writes
  `solo.json`: the hookup is `a8s add solo ~/ark/solo engine-claude`, the
  bundled definition, read rather than typed. Every pasted output is a fresh
  live capture on the claude spine, and a new `templates/01-solo-claude/`
  carries the persona with no definition file, because none is needed.

### Fixed
- **A spaced sync path can no longer register as a broken broker remote.**
  The broker form requires `mqtt://` or `mqtts://`, so an unquoted
  `a8s remote box G:/My Drive/A8S` — which Git Bash splits into a "broker"
  and a "topic" — is refused with the quoting fix echoed back instead of
  saying "added" about a remote that can never connect.
- **File writes wait out Windows file-holds.** OneDrive and Defender open
  new files in watched folders immediately, which made the atomic
  write-then-rename fail `WinError 32`. `ark.fsio.replace_with_retry`
  (10 attempts, backoff to 250 ms) now rides under the folder remote's
  publish, `atomic_write_text`, and the sync_folder service's writes.
- **A folder remote that filters backlog says so.** One warning per process
  names the count, the `joined` cutoff with its human UTC time, and the
  re-join remedy — a clock that was ahead at registration used to make a
  node silently deaf; now the condition reads in `a8s logs`, and
  `a8s remote <name>` renders the cutoff as a timestamp.
- **A claude engine node can no longer talk itself mute.** The claude and
  ollama-claude presets' tool allowlist named `tell` but not `a8s`, so the
  `--agent` scaffold's `a8s convo` step was silently denied under `dontAsk`;
  claude concluded Bash itself was off and wrote that into LESSONS.md, where
  every later wake inherited it and stopped replying. `Bash(a8s convo:*)` is
  now on the allowlist — that one subcommand, since the cold boot needs
  exactly it and a broad `a8s` grant would hand untrusted inbound mail the
  router's control verbs. Found live during the chapter-1 recapture.
- **The prompt parses after the flags on every Python the suite deploys to.**
  `r4t engine codex run --permissions bypass --agent amos "hi"` died
  "unrecognized arguments" on Python 3.10/3.11 (the interpreter Ubuntu 22.04
  ships) while working on 3.12+: older argparse abandons a positional that
  trails the options once the optional positionals matched empty. r4t now
  adopts what old argparse abandoned — `engine … run`, `rig run`, `seat send`,
  `task show`/`trace` and `rig get` take their trailing positionals on either
  side of the flags, on every interpreter. Found live on a
  deployed VM; `--version` also names the running Python
  (`r4t 0.1.69 (The Ark, python 3.10.20)`) across all four CLIs, so the next
  interpreter-dependent field report answers the question in its own paste.
- **MQTT client identity is per node, and `a8s health` probes anonymously
  (#168).** A remote's default client id hashes the host, the handler's
  attached agent set, and the remote name, so two nodes on one machine hold
  two broker sessions instead of taking turns evicting each other, and the id
  stays the same across restarts so the broker replays each node's queued
  QoS-1 messages. `a8s health` connects with a random one-shot id and a clean
  session, so a connectivity check can neither inherit a node's session nor
  consume the mail waiting in it. `A8S_CLIENT_TAG` replaces the node tag when
  set, and a `client_id` in a remote's `network.json` spec still wins.
- **Running the a8s test suite on Windows no longer writes to the real config
  home.** The `fake_home` fixture set `HOME`, which Windows path expansion
  never reads; it now points `USERPROFILE` at the per-test directory and
  clears the `HOMEDRIVE`/`HOMEPATH` fallback, so an "isolated" test can no
  longer clobber a developer's live `~/.a8s`.

## [0.1.68]

### Added
- **`r4t rig run <rig> PROMPT` — one headless turn as a named rig (#157).**
  The same turn `r4t engine <id> run` composes, with the rig's own preset,
  model, permission stance, tool allowlist, timeout and `env` map already
  applied, and gated on the rig's machine-global spend bucket. An engine is
  bare metal; a rig is that engine plus its tuning plus its budget. Precedence
  is flag > rig > preset, and every engine-layer flag (`--dir`, `--agent`,
  `--idle`, `--echo`, `--no-scaffold`, `--lessons-cap`, `--continue`,
  `--permissions`, `--allowed-tools`) means what it means there. A rig with no
  budget keys runs with no gate; an exhausted one refuses by default naming
  the wait, holds for the refill under `--wait`, or spends to its floor under
  `--now`. `--json` reports the turn, the reason and the budget on stderr, so
  stdout stays the engine's own reply stream. There is no rig-level continue
  key: the preset declares capability and a roster member declares policy, and
  a third setting between them would lose to the member every time it mattered.
  See [`docs/r4t-rigs.md`](docs/r4t-rigs.md).
- **One vocabulary for permissions, tools and continuation (#159, #160,
  #136).** `r4t engine <id> run` takes `--continue`, `--permissions
  ask|auto|bypass` and `--allowed-tools SPEC`, and a rig takes the last two as
  the keys `permissions` and `allowed_tools` (`r4t rig set <rig> permissions
  bypass`). Each is unset by default, and unset means the preset's own flags,
  byte for byte. r4t translates the word into each CLI's own spelling from one
  table: a mode below an engine's floor is a hard error naming the reason, one
  above its ceiling proceeds with a note, and `--echo` prints exactly what will
  run. `--allowed-tools` is the answer to the claude preset's narrow allowlist,
  and as a rig key it survives `rig swap`. The stance is a rig key and never a
  roster field, so an in-repo edit cannot raise a member's permissions.
  `--idle --continue` is refused: an idle wake is a cold start.
  See [`docs/r4t-engine.md`](docs/r4t-engine.md).
- **`r4t engine check` — the argv probe that spends no turn.** `r4t engine
  check` (or `r4t engine <id> check`) composes the argv `run` would spend a
  turn on and asks the installed CLI whether it still parses, reporting each
  engine's binary, version and verdict, and exiting 1 if any composed argv is
  rejected. It drives only `--help` and `--version` — never a turn, never a
  token. This is the durable answer to the drift that broke two shipped presets
  at once.
- **The bare engine node wakes three ways.**
  `apps/a8s/definitions/engine-claude.json` now carries a `batch` block, so a
  burst of N messages is one invocation and one context load instead of N,
  and an `idle` block, so a quiet node runs one consolidation pass per quiet
  period and no more. [`docs/r4t-engine.md`](docs/r4t-engine.md) documents
  how a definition's parameters map onto `engine run`.
- **All nine `RUN_ENGINES` ship a bundled, add-and-go engine node.** Every
  engine `r4t engine run` supports now has its own
  `apps/a8s/definitions/engine-<id>.json` — `a8s add name ./dir engine-cursor`
  works unedited, no copy-and-tweak required. Each single-message wake states
  the sender (`$SENDER tells $RECIPIENT ($AGE): $MESSAGE`), the same shape
  every other bundled definition already uses, so a bare node can tell who it
  is answering — the gap #157's chapter-1 field test found in a bare
  `$MESSAGE`. The `ollama-*` engines additionally require the a8s var `MODEL`.
  Built-ins in `apps/a8s/definitions/` stay usable as shipped; a custom
  definition belongs to `a8s defs add`, not an edit of the hidden directory.

### Changed
- **The Ark Raising, chapter 1, now opens on the engine turn (#157).** The
  first win is supercharging the agent instructions a reader already has —
  their `AGENTS.md` or `CLAUDE.md` run through `r4t engine <id> run`, with
  the smart cold-boot scaffold (`STATUS.md` / `LESSONS.md`) giving a
  stateless CLI a memory that survives the process. The a8s hookup follows
  the win instead of preceding it: one definition with `invoke` / `batch` /
  `idle` pointed at the same engine command, so `tell` wakes the same
  softened agent. The hand-rolled `reply.sh` wrapper is gone from the
  chapter and its templates; `--echo` and `r4t engine <id> check` are taught
  as the see-what-runs tools. Rosters, rigs and budgets stay in chapter 2.

### Fixed
- **`get.sh` on Windows adds the suite to the Windows user Path.** The rc line
  only reaches Git Bash shells, so the `.cmd`/`.ps1` shims were unreachable
  from PowerShell and cmd.exe until the user edited Path by hand. The
  installer detects a Windows uname and writes it into `HKCU\Environment`
  directly: read raw with `DoNotExpandEnvironmentNames`, appended, and
  written back as `REG_EXPAND_SZ`, broadcasting `WM_SETTINGCHANGE` itself —
  the pattern rustup, scoop, uv/cargo-dist and winget all converged on
  independently. Never `setx` (1024-char truncation, flattens the
  user/system split), and never a plain `[Environment]::…` round-trip
  either — that reads Path already *expanded* and writes it back as
  `REG_SZ`, permanently flattening every `%USERPROFILE%`-style entry and
  downgrading the value's type on the very first install; new terminal
  windows pick up the change either way.
- **Three Windows bugs found live on Windows 11, Git Bash, Microsoft Store
  Python 3.13 (#2).** `a8s`'s `_pid_alive` used `os.kill(pid, 0)` to probe a
  pid file — signal 0 on Windows **is** `CTRL_C_EVENT`, and a thirteen-year-
  old CPython bug (bpo-14484/gh-128932, fixed 2025-01-17, backported to
  3.12/3.13) let a failed `GenerateConsoleCtrlEvent` fall through into
  `TerminateProcess` instead of returning, so probing a live holder on an
  unpatched interpreter could kill it; probing a dead one raised `OSError`
  instead of `ProcessLookupError` either way, crashing every command that
  reads a pid file (`a8s ls`, acquire). It now asks
  `OpenProcess`/`WaitForSingleObject` via ctypes on Windows — immune to the
  `STILL_ACTIVE` (259) exit-code collision `GetExitCodeProcess` alone can't
  rule out — with an access-denied probe reading as alive, mirroring the
  POSIX branch's own `PermissionError` handling; the POSIX path is
  unchanged. `apps/a8s/daemon.py`'s `attached_loop` registered
  `signal.SIGUSR1`, which doesn't exist on Windows — `AttributeError` at
  startup killed every `a8s start` child silently (stderr is discarded).
  Registration, restoration, and `cmd_kill`'s signal send are now guarded by
  `hasattr(signal, "SIGUSR1")`, demoted to a latency optimisation: the
  iteration-top kill-request branch that used to wait for it now kills the
  in-flight wake's subprocess group itself, on every platform, so `a8s kill`
  can no longer ack success while the woken CLI keeps running. Windows
  Python's console is UTF-8 by default; the crash is on **redirected**
  streams (`> file`, `| tee`, CI capture), where `ar3`'s checkmarks raised
  `UnicodeEncodeError`. `ar3`, `a8s`, `r4t` and `k7e`'s entry points now
  guard `sys.stdout` with `errors="backslashreplace"` — mypy's own pattern,
  `isinstance(..., TextIOWrapper) and .errors == "strict"` — so an
  unencodable glyph is escaped losslessly instead of crashing the process;
  stderr is untouched, since its default handler is already
  `backslashreplace`. Confirmed against a full a8s filedrop loop on the
  reporting machine.
- **`codex exec resume` takes no `--sandbox`.** Every codex continuation — a
  roster member with `Continue: on` and `r4t engine codex run --continue`
  alike — composed an argv codex refuses to parse ("unexpected argument
  '--sandbox' found", codex-cli 0.147.0). The flag now comes out when the turn
  resumes.
- **Shipped presets drifted from the installed CLIs (#162).** `codex exec
  --full-auto` no longer parses on codex-cli 0.147.0 — `r4t engine codex run`
  and any `codex`/`ollama-codex` rig now pass `--sandbox workspace-write`
  instead, the argv `codex exec` needs since it hard-codes approval policy to
  `never`. `apps/a8s/definitions/opencode.json` and `ollama-opencode.json`
  carried `--dangerously-skip-permissions`, a claude-only flag opencode's
  lenient parser silently ignores — those agents ran with auto-approval off;
  both now pass opencode's own `--auto`. `apps/a8s/definitions/copilot.json`
  dropped the machine-global `--continue` that #17 ruled against. Five bundled
  a8s definitions (`codex.json`, `cursor.json`, `agy.json`, `opencode.json`,
  `ollama-opencode.json`) resumed the previous conversation unconditionally on
  every wake, against #155's fresh-session ruling; none of the bundled
  definitions resume now.

### Changed
- **The opacity principle is stated in [`docs/a8s.md`](docs/a8s.md).** A
  recipient name is a claim, not a lookup: only a local-only cluster rejects
  an unknown name at send time; with any remote configured, no sender can know
  whether a name resolves anywhere. Exit 0 means the envelope entered the
  network — evidence of delivery is a reply or `a8s trace`.

## [0.1.67]

### Added
- **The suite doctrine lands as [`docs/ark.md`](docs/ark.md).** One doctrine for
  every app built on The Ark — dependencies, filesystem, CLI feel, processes,
  integration, docs and release — stated as rules, so a user who has never
  opened a new Ark app can navigate it because it feels like the ones already
  learned. k7e's separate zero-dependency doctrine dissolves into it: the
  suite-wide rule is stdlib at the core with anything more arriving only through
  the foundation's two tiers, and k7e importing nothing beyond stdlib becomes a
  fact about k7e rather than a doctrine k7e maintains alone. k7e's behavior is
  unchanged.
- **The `ark/` foundation package vendors paho-mqtt.** `ark/_vendor/paho`
  ships an unmodified, sha256-verified copy of paho-mqtt 2.1.0
  (`ark/_vendor/vendor.txt` carries the pin); `ark.vendor.ensure_vendor()`
  prepends it to `sys.path` so `apps/a8s/transports/mqtt.py` resolves the
  vendored copy first. a8s's MQTT transport now works under any `python3`
  with zero `pip` — set `ARK_NO_VENDOR=1` to opt back into a system or venv
  install instead.
- **`r4t engine <id> run --echo`.** Prints the composed argv (shell-quoted)
  and the exact prompt sent to the CLI — scaffold prelude included — to
  stderr before spawning, so stdout stays only the engine's own reply
  stream. The turn still runs.
- **`ark/deps.py` and `ar3 deps` — tier 2 of the foundation's dependency
  doctrine.** Tier 1 (`ark/vendor.py`) ships small pins inside the repo;
  tier 2 fetches heavy, optional ones (boto3, textual) on demand into
  `~/.local/share/ark/deps/<interpreter>/<group>`, one directory per Python
  build so an interpreter upgrade misses cleanly instead of half-loading an
  incompatible install. `ar3 deps` lists groups and their installed/missing
  status; `ar3 deps <group>` installs one with `uv pip install --target` (or
  plain `pip` without uv), swapping a tmp dir into place only once the
  install succeeds so a failure never clobbers a working install. This is
  the amendment to `ar3`'s charter: it still never mutates product state,
  but it now owns and maintains this one piece of the suite's own substrate.
  a8s's S3 storage and r4t's chat TUI both switch to it — replacing the
  `pip install -r requirements/*.txt` instructions they shipped before,
  which were broken on any PEP 668 (externally-managed) Python.

### Changed
- **One `ark/` foundation module per piece of duplicated code, shared by
  every app.** `ark.ulid` (moved verbatim from `apps/a8s/ulid.py`), `ark.home`
  (one `app_home()` resolver), `ark.fsio` (one `atomic_write_text()`), `ark.proc`
  (one `spawn()` + `terminate_group()`), and `ark.envseam` (the
  `TELL_OUTBOX_DIR` / `TELL_FILE_MAX` reserved-env contract a8s routing owns)
  replace six-plus hand-rolled copies across a8s, r4t, and k7e. r4t's own
  `apps/r4t/ulid.py` shadowing `apps/a8s/ulid.py` on `sys.path` — the reason
  k7e has always been driven as a subprocess rather than imported in-process
  — is gone for good: there is now exactly one `ulid` binding in the suite.
- **a8s adopts `XDG_CONFIG_HOME` for its state root.** `resolve_a8s_home()`
  now honors `XDG_CONFIG_HOME/a8s` the same way r4t and k7e always have,
  ahead of the legacy `~/.a8s` fallback — a deliberate unification, not a
  bug; pre-1.0 scorch-the-earth applies, so there is no migration path to
  preserve for an operator who was relying on the old resolution order.
- **`r4t engine <id> run`'s timeout kill and `r4t sandbox`'s orphan-process
  cleanup now escalate SIGTERM before SIGKILL, with a grace period, instead
  of killing immediately.** Both now share `ark.proc.terminate_group`, a
  strict improvement: a harness that traps SIGTERM gets the chance to exit
  cleanly before the group is force-killed.
- **A rig's static `env` map can no longer name `TELL_FILE_MAX`, alongside
  the existing `TELL_OUTBOX_DIR` refusal.** `rig.TURN_OWNED_ENV` now derives
  from `ark.envseam.ROUTING_OWNED` — both env vars a8s routing computes and
  injects on wake — instead of hand-listing only one of the two.
- **`r4t engine <id> run` rotates an oversized `LESSONS.md` instead of just
  warning.** Past the line cap (`--lessons-cap`, default 200), the oldest
  whole lines move out to `LESSONS-ARCHIVE.md` — created if absent, appended
  to in order — so the live file lands at exactly the cap. No model ever
  touches either file; both writes are atomic (temp file + `os.replace`).
- **The `ollama launch` presets rename to an `ollama-` prefix.**
  `opencode-ollama` / `claude-ollama` / `codex-ollama` / `copilot-ollama` are
  now
  `ollama-opencode` / `ollama-claude` / `ollama-codex` / `ollama-copilot`, so
  the local-model family sorts together in `r4t rig presets` and `r4t engine
  list` next to the bare `ollama` preset.
- **`r4t engine <id> run` supports `opencode` and three of the four
  `ollama-*` local launchers.** The five original engines (`claude`, `codex`,
  `agy`, `copilot`, `cursor`) are joined by `opencode`, `ollama-claude`,
  `ollama-codex` and `ollama-opencode`, whose headless invocation is now
  verified. Bare `ollama` stays excluded (no file tools for the run
  scaffold to use), and so does `ollama-copilot` — every file write it makes
  lands in copilot's session-state mirror rather than the real working
  directory, which the scaffold's `STATUS.md` contract cannot survive; cloud
  `copilot` is unaffected.

## [0.1.66]

### Added
- **`r4t engine <id> run` — one headless turn of an engine CLI as a bare
  stateless agent, invoked directly by an a8s node with no roster or
  dispatcher.** Supports the five engines with a verified unattended
  invocation (`claude`, `codex`, `agy`, `copilot`, `cursor`); argv composition
  reuses `rig.build_preset_invoke`, the one source of preset argv truth. A
  cache-stable "smart cold boot" scaffold (default on, `--no-scaffold` to
  skip) points the CLI at `STATUS.md`/`LESSONS.md`/`AGENTS.md` as its only
  memory across the fresh session every wake brings; `--idle` adds a
  Cody-pattern debounce so a timer-woken node skips a wasted consolidation
  turn when nothing changed since the last one. `apps/a8s/definitions/
  engine-claude.json` wires the pattern into a8s. See `docs/r4t-engine.md`.

### Changed
- **r4t continuation is off unless the roster says otherwise — the warm/size
  gate is gone.** Measured production telemetry (ar3-private#155) shows the
  cache miss is a process-boundary phenomenon no warmth window or size cap can
  prevent: a resume seconds into an alive task re-writes the conversation ~16×
  as often as staying in-process. The `Continue:` flag (`on`, `off`, or an
  idle duration) is now the only switch and writing it is an explicit
  acceptance of the miss risk; the `continue_warm_seconds` /
  `continue_max_context_tokens` / `continue_max_transcript_bytes` preset knobs
  and the `CONTINUE-CHILL` founding path are removed. The cache telemetry
  hardens instead: the probe skips synthetic zero-usage tail records, and a
  continued turn that re-created its own history logs `CACHE-MISS` loudly.

### Fixed
- **Shared filedrop outboxes honor a co-registered `from`.** When several
  agents share one physical `.outbox/` (GAS-style dual `a8s add … filedrop` on
  one mount), ingest attributes each envelope to the claimed peer on that path
  instead of whoever emptied the directory first; unbacked claims still
  force-stamp. (#150)
- **MQTT PUBACK wait no longer shares the 5s connect timeout.** Production
  evidence across residential-network machines showed ~3-5% of publishes
  failing with "publish not acknowledged" — the broker had already accepted
  the message, but the 5s connect timeout was too short for the ack to catch
  up on a residential latency tail, so the retry duplicated a publish the
  receiver's ULID dedup then had to absorb. `MqttTransport` gains a separate
  `ack_timeout_s` option (default 30.0) for the PUBACK wait; connection waits
  are unchanged. Publish successes now also reach the global log (previously
  per-agent log only), and the delivery-receipt publish-failure warning
  states plainly that receipts are fire-and-forget and not retried.

## [0.1.65]

### Added
- **`r4t engine <id> quota` — ask an engine how much subscription is left and
  when it resets, without spending a turn.** One component per engine under
  `apps/r4t/engines/`; accepts an engine id or any rig preset id, `--json`
  for scripts, and `r4t engine list` to see both. Codex answers over its own
  app-server protocol, Copilot over the entitlement endpoint the IDE
  extensions use, Antigravity over its local language-server API, Cursor over
  the dashboard's own call, Claude over the endpoint behind `/usage`;
  OpenCode delegates to whichever provider backs it, and local ollama models
  report no cloud quota at all. Live answers persist as snapshots that serve,
  age-stamped, when the live check cannot. (#148)

### Fixed
- **The wake-PATH warning no longer misjudges relative harness paths.** A
  definition whose invoke is `./name` is resolved the way a wake resolves it —
  against the node's registered root, where the wake sets its CWD — instead of
  against whatever directory `a8s start` happened to run from. Starting a node
  from anywhere no longer warns about a harness that resolves fine.

## [0.1.64]

### Removed
- **The repo-root `ROSTER.md` / `MISSION.md` draft is gone.** The Ark's own
  roster lives in a private org directory now; the 0.1.55 draft at the repo
  root was superseded, shipped to the public mirror, and would dispatch a
  stale seven-member roster to anyone registering an r4t node at the repo
  root. (#122)

### Added
- **`org/AR3/` — the Ark's own roster, in the repo.** The AR3 org (roster,
  mission, charter, node definition, rig snapshot) moves from a config
  directory into the repo so it is reviewed like code and deployed by
  `git pull`; `r4t-org.json` names the workplace as `../..`, so the enclosing
  checkout is the workplace on any machine. The directory is excluded from the
  public mirror alongside the experiments — the roster's internals are the
  project's operations, not the product. (#122)
- **`tools/wiki-gardener.py` checks the private wiki against its gardening
  charter.** Point it at a wiki checkout and it reports six defect classes —
  pages with no category or no state, banners carrying neither a reason nor a
  date, pages unreachable from `_Sidebar.md` within two link hops, sidebar
  entries that are not index pages, and internal links to pages that do
  not exist — one line each, or `--json` for a machine. It is stdlib-only,
  reads nothing but the directory it is given, and runs on demand at no Actions
  cost; the release workflow runs it over the wiki and reports without blocking
  while the wiki is being gardened into shape. (#133)
- **A node declares the environment its wakes get, instead of inheriting the
  start shell's.** A wake used to run with whatever `PATH` the shell that ran
  `a8s start` happened to have, permanently — right from an interactive login
  shell, wrong from ssh, cron, launchd or CI, where the harness goes
  unresolvable hours later at the first wake and the operator's own shell still
  finds it. Three knobs: `definition.env` for literal `NAME: value` pairs a node
  needs, machine-wide `wake_path` as the fallback `PATH` for every node that
  does not name one, and `definition.wake_shell: "login"` to run the invoke
  through `$SHELL -ilc` for the `PATH` that cannot be written down. `a8s add`
  records the operator's own `PATH` into `wake_path` the first time, since that
  shell is correct by construction at that moment, and never overwrites it. a8s
  injects `TELL_OUTBOX_DIR` and `TELL_FILE_MAX` last, so a node can fix its own
  `PATH` and still cannot move its own outbox. (#121)
- **`r4t roster check` warns when a member's knowledge store sits inside the
  workplace.** A container keeps the stores out by never mounting `R4T_HOME`,
  and it mounts the workplace read-write at its real path — so an `R4T_HOME`
  under the workplace rides into the cage and hands the member every store,
  its own and its siblings'. The check names the member, the store path, the
  workplace, and the consequence. It warns and blocks nothing: on a bare org
  the placement costs nothing. (#54)

### Fixed
- **The `a8s start` harness warning names the fix.** It said "start from a login
  shell", which was the only remedy that existed and the wrong one on half the
  boxes the suite runs on. It now probes against the environment the wake will
  actually get — `definition.env` and `wake_path` applied — and names both
  knobs, so a node fixed by either stops warning. (#121)
- **The reference `run_as` provisioning seals r4t's own state, and the docker
  test proves it.** `R4T_HOME` was left at the umask, so on the reference
  deployment the agent user could list it and read any member's k7e store —
  the modes r4t re-asserts each turn cover staging and the delivered bundle
  and nothing else. The provisioning now makes `R4T_HOME` router-owned and
  search-only for the shared work group (it has to stay traversable: staging
  and the delivered bundle live under it) and each store dir `0700`, and the
  boundary test asserts from inside the cage that the agent can do neither.
  (#54)

### Documentation
- **What `run_as` gives a knowledge store is another user's files, not a
  mode.** The knowledge and isolation pages said the cage holds under either
  isolation mode; under `run_as` that is true only where the operator set a
  mode r4t does not set. Both pages now say which modes r4t re-asserts, which
  one is the operator's, and what to provision. (#54)

## [0.1.63]

### Added
- **`r4t roster check` flags a member name that shadows something outside the
  wall.** The leader addresses roster members and registered a8s nodes with the
  same verb, and a name inside the roster wins — so an outside node sharing a
  member's name is unreachable from that leader, by precedence rather than by
  a block, and nothing said so. The check now names the overlap (node, alias,
  or namespace) and the message flow page states the precedence rule. It warns
  and blocks nothing: on a single-owner network there usually is no collision,
  and where there is one the operator may mean it. (#40)
- **`tell` names the one shape a leaked `TELL_OUTBOX_DIR` makes.** A variable
  inherited from a live seat into an unrelated shell passes every check the
  send path makes — the directory exists, it is writable, it belongs to a real
  agent — and the mail simply leaves under that agent's name. What gives it
  away is the pair: the outbox is a registered agent's own, and the working
  directory is nowhere near that agent. `tell` now warns on stderr and sends
  anyway, and `tell --check` reports the same line. A refusal would be wrong;
  an operator may mean it. r4t's staging outbox is not registered and a seat
  working in its own root matches its owner, so neither warns. (#92)

### Documentation
- **Per-member knowledge stores are separated by convention until an OS
  boundary enforces it.** One store per member bounds what a member is given,
  not what it can take: the stores are files under `R4T_HOME`, `R4T_HOME` is
  in the turn environment, and a tool-capable rig has a shell. On a bare org
  there is no boundary between such a member and any store, its own or a
  sibling's — observed live in the K1 rig matrix, where a full-tool preset
  stated a codeword present only on disk. Stated in the knowledge and
  isolation pages, because "each member has its own store" reads like a
  guarantee. Partial: the lab-driver mitigation and the #51 read-path
  consequence are still open on #54. (#54)
- **Sequential entry ids are a recency signal, and models read them.** With
  every date stripped from the injected entries, a 4B model resolved a
  fact-supersession conflict off the id ordinals alone. Nothing tells a reader
  that `K7E-BBB-NNNNN` is allocated in order; it works it out. In production
  the signal is free and usually correct, and silently wrong after a bulk
  import, a store merge, or a rebuild that re-numbers. An experiment testing
  "no temporal information" has to shuffle or mask ids or it under-measures
  the penalty. Written down in the k7e architecture and r4t knowledge pages so
  it is not rediscovered as a mystery. (#69)

### Changed
- **`k7e get` takes several ids, and the knowledge pass stops paying for
  startup.** Rank-proportional packing has to read every entry in its
  weighting pool before it can weigh any of them, and it was spending one
  interpreter startup per entry to do it — 8 entries measured at 379ms, of
  which the reads themselves were almost none. `k7e get` now accepts many ids
  (`--json` for the parseable form, both flags applying to the whole batch),
  and r4t's sizing pass makes one call: **375ms → 60ms measured on 8 entries**.
  A missing id is reported on stderr and skipped rather than failing the
  batch, since a caller sizing a pool would rather pack the rest. Single-id
  behaviour is unchanged. (#111)
- **One issue-number namespace in the source.** Code and docs carried in from
  the pre-carve repository quote that repository's issue numbers, and the two
  namespaces overlap — `#90` is a real issue in both, on different subjects.
  GitHub linked every legacy reference to whatever this repo happens to number
  the same, so a reader following one landed on unrelated work. 98 references
  are now written `bin#N`; a bare `#N` means this repo and nothing else. Each
  was classified by comparing when the line was written against when this
  repo's issue of that number was opened, not by eye. (#73)

### Fixed
- **A shared handler's idle pass rotates, and a quiet stretch no longer
  shuffles the wake order.** Two fairness gaps left over from the #20
  wake-rotation review. The wake counter advanced on every free-slot
  iteration whether or not anyone had mail, so a short interval spun it
  through each idle pass and which of two agents mailed at the same moment
  went first depended on how long the lull happened to be — fair on average,
  unreproducible in the particular. It now advances only when a wake actually
  started. The idle pass had the wake loop's original bug untouched: it began
  at index 0 and stopped at the first started invoke, so an agent whose clock
  keeps expiring first took every idle slot and its siblings were never
  checked. It now rotates on its own counter. (#74)
- **`a8s start` says so when a node cannot see its harness.** `a8s start`
  hands its own environment to the handler, which hands it to every wake, so a
  node's `PATH` is whatever the shell that started it happened to have —
  permanently, until restart. Start from a login shell and everything works;
  start from `ssh host -- 'a8s start x'`, cron or CI and the harness is
  unresolvable at the first wake, hours later, while the operator's own shell
  still resolves it fine. That gap is why the failure reads as intermittent
  rather than as a `PATH` problem. `a8s start` now probes each node's harness
  in exactly the environment the node will inherit, and looks *through*
  wrappers — the old guard saw only `argv[0]`, so a definition wrapping its
  harness in `flock` or `timeout` failed inside the wrapper and reported the
  wrong program. It declines to guess inside `sh -c`, skips an unexpanded
  `$VAR`, and warns rather than refuses. `ar3 doctor` now names the same
  consequence when a harness is not on `PATH`, so the two symptoms tell one
  story. (#121)
- **A media file with a surprising response no longer takes the distill batch
  with it.** `_parse_llm_response` was hardened against non-string content by
  #57, but `_parse_multimodal_response` is a separate path reached only through
  a media file, so the fix never covered it. The wider problem was the batch
  itself: `dream_sweep` reads a nonzero exit as a failed dream and re-runs the
  same directory, so one undecodable byte in one capture wedged distillation
  permanently. `distill()` now skips the file, records why, and keeps going.
  (#70)
- **A skipped capture is now visible to the operator who was asleep for it.**
  k7e wrote its skip notes to stderr; r4t captures stderr and prints it only
  when the exit code is nonzero, so on a *successful* dream the note was
  captured and discarded. The watermark advances past the skipped file
  regardless, so nobody ever learned a capture went unread. `k7e distill` now
  prints skipped files on stdout alongside what it stored, and a successful
  dream logs each one as `DREAM-SKIPPED`. (#71)
- **The CLI stopped pointing at a directory new installs do not have.** 36
  user-facing strings and docstrings still named `~/.a8s`; `a8s config` printed
  it directly. Swept to `~/.config/a8s`, keeping the three places that describe
  the legacy fallback itself. (#72)
- **`--opt=value` works everywhere it should have.** The three commands that
  take open-ended options disagreed with each other: `a8s add` demanded
  `--KEY=value` and rejected the spaced form, while `a8s remote` and
  `a8s storage` demanded `--opt value` and rejected `=`. The disagreement
  failed silently in the worst way — `a8s storage fm webdav://… --base_url=…
  --user=… --password=…` parsed as *two* options literally named
  `base_url=…` and `user=…`, each swallowing the following flag as its value,
  so the error named options nobody had typed and the password never reached
  the config at all. All three now share one parser: both spellings work, `-`
  and `_` in an option name are equivalent, `--pass` still aliases to
  `--password`, and a spaced value that looks like another option is refused
  with a message that says to use `--opt=<value>` if the value really starts
  with a dash. A single-dash option says which long form to type instead.
  `a8s tell --attach` and `a8s logs --tail` already took both spellings; now
  the rest of the CLI agrees with them.

## [0.1.62]

### Fixed
- **One message, one delivery.** A machine running more than one a8s daemon
  delivered every inbound message once per daemon: each runs its own subscriber
  and resolves recipients from the same registry, so each one saw the envelope
  and each one wrote it to the inbox. The `seen-ids` ring could not arbitrate
  it — the ring is read when the envelope arrives and written only after
  delivery finishes, and downloading a sync-folder attachment puts seconds
  between the two. Observed as one send producing two delivery receipts, and
  with an attachment, two inbox writes 7.3 seconds apart. A receiver now claims
  the message ULID before it starts, with a single atomic filesystem
  operation; the others drop the envelope as the duplicate it is. Claims
  expire after five minutes and are swept at daemon startup, so a receiver
  killed mid-delivery releases the message rather than stranding it.

## [0.1.61]

### Added
- **`sync_folder` storage — attachments through a folder your sync client
  already watches.** Point it at a bare path (`a8s storage onedrive
  "~/OneDrive - Contoso/A8S"`), point a second machine at the same folder, and
  the bytes cross by themselves. Nothing is published: no host, no credential,
  and no URL that resolves for anyone outside the folder. The marker in the
  envelope names neither the service nor the path, so two machines need not
  agree on what to call it, and configuring two folders makes them race —
  whichever syncs first delivers. Attachments are keyed by message ULID, so one
  message's files stay together. `--retain_days` sweeps old bundles and is off
  by default, because deleting from a shared folder deletes from every machine
  sharing it. Use `rclone` on headless and VM machines, which have no sync
  client to ride along with.
- A file is staged under a `.part` name and renamed, and a `manifest.json`
  records the size a receiver must see before it accepts the copy. A sync
  client publishes a name before the bytes behind it land, and OneDrive's
  Files On-Demand shows a placeholder that only materializes when read — both
  now read as "not here yet" rather than as a delivered file.

### Fixed
- **One dead storage service no longer blocks the others.** Upload required
  every file to reach every configured service, so a single unreachable remote
  pushed the whole message through the backoff schedule and into the trash
  while a working remote held a copy the entire time — configuring a second
  service was a second way to lose mail. A message now publishes once each file
  landed somewhere. Every send still attempts every service, and only services
  that accepted a file contribute a URL, so a failed one publishes nothing
  rather than a link that cannot resolve. A file no service accepted still
  keeps the retry.
- **A daemon picks up storage services configured after it started.** Services
  were loaded once per daemon lifetime, so a node running since before a
  service was added failed every attachment through it and said nothing, while
  `a8s storage` listed the service as configured. Both the routing pass and the
  receive callback now resolve at use time; the built list is reused until
  `network.json` or `secrets.json` changes.

## [0.1.60]

### Added
- **r4t prices a continuation before it pays for one.** `Continue: on` is only
  cheap while the provider still holds the conversation's prefix in cache. Two
  independent things end that, and only one is about time: the cache window
  slides shut, and a conversation grows big enough that it is re-written even
  while in use. r4t now checks both before each turn. Past either limit it logs
  `r4t: CONTINUE-CHILL` with the reason and founds a fresh conversation instead
  of re-sending the old one. The turn still runs.
- **Per-turn cache telemetry.** Each dispatched turn logs `r4t: CACHE` with the
  context carried, how much of it was read from cache, and how much was
  written. This is what the limits above get tuned against.
- **Conversation probes, one per harness** (`apps/r4t/transcript.py`). The
  `claude` preset reads its JSONL session log and reports what the next
  continuation would carry. A harness with no probe is never gated — an
  unmeasured window is a guess, and a guess costs money on a schedule nobody
  chose. The knobs are `continue_warm_seconds`,
  `continue_max_context_tokens` and `continue_max_transcript_bytes`.

### Changed
- **The `claude` rigs stabilize their prompt prefix.** The `claude` and
  `claude-ollama` presets pass `--exclude-dynamic-system-prompt-sections`,
  which moves cwd, environment info, memory paths and git status out of the
  system prompt. Those change without anyone editing anything — a commit
  between two turns used to invalidate the largest cached block.
- **The a8s `claude` definition runs one fresh session per wake.** a8s wakes an
  agent when mail arrives, minutes to hours apart, which is almost never inside
  a cache window. It dropped `--continue`, so a wake no longer pays to re-send
  a conversation it cannot reuse. Continuation stays in r4t, which dispatches
  turns close enough together for it to pay.

## [0.1.59]

### Added
- **Every CLI answers `--version`** — `ar3`, `a8s`, `r4t`, `k7e`, `tell` and
  `tells` all print the suite semver from `VERSION`. `ar3 doctor` prints it too
  and says whether a newer version is published on the public mirror. The check
  is only in `doctor`, has a short timeout, and stays quiet when GitHub cannot
  be reached: bare `ar3` remains offline and instant.
- **`a8s health` removes the probe object it uploads.** Storage services gained
  an optional `delete`, implemented for `webdav`, `file_sync`, `s3` and
  `rclone`. Attachments are never deleted — the receiver decides how long it
  needs them. A store that expires on its own says so and health stays quiet
  about it; anything else that survives is reported with its URL.

### Fixed
- **WebDAV uploads work against a real server.** Every object key carries a
  fresh random directory, and WebDAV `PUT` does not create parent collections,
  so every upload answered `409 Conflict`. Collections are now created first.
  The in-process test server accepted any `PUT`, which is why the suite stayed
  green against a client that could not upload anything anywhere; it now
  enforces the rule a real server enforces.
- **A WebDAV filename with a space now uploads.** The object key went into the
  request target unescaped, so voice memos and screenshots — the files most
  likely to carry spaces — failed before leaving the machine.
- `--prefix ""` means no prefix. An empty value fell back to `a8s`, so an
  operator pointing a service at a folder already dedicated to a8s could not
  avoid a redundant level below it.
- `a8s storage` accepts `--pass` as well as `--password`. `a8s remote` takes
  `--pass`, and the two surfaces disagreeing was a trap.

## [0.1.58] — 2026-08-05

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
