# Security model

What keeps a repo edit, or a lying message, from changing what runs.

- **Symbolic rigs.** The in-repo roster may only name a rig
  (`leader`, `member`, ...). Argv, timeouts, and limits live exclusively in
  the out-of-repo config — a repo edit can never change what runs. An
  unknown rig fails closed: that member does not run.
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
[message-flow.md](message-flow.md#no-wire-header-inside-the-walls). The
verification round keeps its findings on a surface agents cannot read — see
[verification.md](verification.md). A rig can also run behind a real OS
boundary — a Unix user or a container — so the harness sandbox stops being
load-bearing; see [isolation.md](isolation.md).
