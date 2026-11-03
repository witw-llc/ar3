# Security model

What keeps a repo edit, or a lying message, from changing what runs.

- **Symbolic rigs.** The in-repo roster may only name a rig
  (`leader`, `member`, ...). Argv, timeouts, and limits live exclusively in
  the out-of-repo config — a repo edit can never change what runs. An
  unknown rig fails closed: that member does not run.
- **The trust ceiling.** A runbook's `## Rigs` block *may* name a permission
  stance, and the machine caps which one: `auto` until `r4t add <dir> --trust`
  raises it for that node, re-checked every turn. Cloning a repo and
  registering it is not a code-execution decision made silently.
- **Access knobs.** `add_dirs` and `allow_tools` widen what an engine may
  touch, so a runbook may set them only on a node whose ceiling is `bypass`;
  `deny_tools` only narrows and is always accepted. The node's `deny_floor` (a
  reserved `rigs.json` key) is added to every turn of its engine, and neither a
  stance, `allowed_tools`, a runbook rig that shadows a machine rig, nor
  `--resident` can drop it: a launch that cannot carry the floor is refused.
  The `ADD_DIRS`, `ALLOW_TOOLS` and `DENY_TOOLS` a8s vars are the operator's
  dials, but an agent that can run `a8s vars` on its own node can set them, so
  mail that steers such an agent reaches `ADD_DIRS` and `ALLOW_TOOLS` at its
  next wake; keep `a8s vars` out of a seat's allowed tools, and put what must
  never happen in the floor. An access flag is the engine's own and not an OS
  sandbox; `COPILOT_ALLOW_ALL=true` in a copilot seat's environment trusts the
  folder the CLI starts in.
- **Copilot denials are logged, never told.** After a copilot turn r4t reads
  the session's own events and writes a bounded denial summary to the a8s log,
  the day log and `rig run --json`: counts, a fixed tool set, a kind slug, at
  most ten event ids and a count of the deny rules named. It holds no
  argument, command, path, URL, rule or error text, it is never sent to the owner or any
  seat, and it never changes an exit code. A denial count is not an audit: it
  says "complete" only when coverage is proven (see `r4t-engine.md`).
- **Pins.** `"pins": {"gerry": "leader"}` overrides the roster's Harness
  line silently — an in-repo edit can't upgrade a pinned agent.
- **Out-of-repo state.** All r4t state lives under `~/.config/r4t/` (relocate
  with `R4T_HOME`, mirroring `A8S_HOME`); the repo working tree is touched
  only by the harness subprocesses themselves.
- **No shell.** `{prompt}` substitutes into a single argv element; harness
  invocation never goes through a shell.
- **Attribution by filesystem.** Staged envelopes are attributed to the
  turn that owned the staging dir; a8s's router force-stamps `from` by
  outbox ownership. Neither trusts message content.

Related: external ingress is untrusted by design — see
[r4t-message-flow.md](r4t-message-flow.md#no-wire-header-inside-the-walls). The
verification round keeps its findings on a surface agents cannot read — see
[r4t-verification.md](r4t-verification.md). A rig can also run behind a real OS
boundary — a Unix user or a container — so the harness sandbox stops being
load-bearing; see [r4t-isolation.md](r4t-isolation.md).
