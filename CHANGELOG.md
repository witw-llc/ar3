# Changelog

Notable changes to The Ark, newest first, following
[Keep a Changelog](https://keepachangelog.com/). Versions are the suite semver
in `VERSION`; a merge to `main` bumps it and cuts a release.

This record starts at 0.1.52, the first version after release v0.1.51. Earlier
history is in git.

Add to `Unreleased` in the same PR as the change, and rename the heading to the
version when the batch is ready to merge.

## [Unreleased]

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
