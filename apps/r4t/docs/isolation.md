# Isolation — a real OS boundary per org

Harness sandbox flags are not a security boundary: a CLI's `--sandbox` is
model-discretionary and drifts between versions. The boundary that holds is the
one the operating system already enforces — a Unix user with no sudo, or a
container. r4t makes either a per-**org** property. Inside the boundary the
agent runs fully permissive; the OS, not a flag, is what contains it.

## Why per-org, not per-rig

Isolation is a per-project decision; rigs span the system. A rig maps to a real
subscription and is machine-global — the same `leader` rig backs every org on
the box. So the containment choice cannot ride the rig: an org with ten rigs
must need **one** Unix user (or one image), not ten. A production box runs one
agent user whose `$HOME` holds the credentials for every CLI, and every rig
runs as that one user.

The border sits at the turn invoke and nowhere else. r4t and a8s are
deterministic machinery — routing, staging, budgeting — and run as the
operator/a8s user; the probabilistic thing, the roster member's harness turn,
is what gets wrapped. **Machinery outside, hands inside.** One user serves an
arbitrary roster.

## Setting the boundary

Two mutually exclusive org keys in `r4t-org.json` (both set is a config error,
reported by `r4t roster check`; the org degrades to no isolation):

- `run_as: "<username>"` — wrap every member turn in `sudo -u <username>`.
- `container: "<image>"` — run every member turn under `docker run --rm`.

r4t **verifies** the prerequisites before every turn and fails closed with an
action-first error; it never creates users, sudoers grants, workplace
permissions, or images. Provisioning is the operator's, once, below. A failed
check is an ordinary failed turn: the batch stays queued, the breaker counts
it, and `r4t status` shows the member unhealthy with the real error.

Set the boundary by editing the org's `r4t-org.json` (the same file that carries
`comms`, `egress`, and `doorbell_check`):

```json
{
  "run_as": "r4t-agent"
}
```

`r4t status` then prints one org-level line — `isolation: [user:r4t-agent]` /
`[container:<image>]` — so the boundary that covers the whole roster is visible
at a glance.

## Posture: most-permissive preset behind the boundary

Isolation composes with any harness preset and does not touch its flags. The
intended posture is the inverse of the flag-juggling it replaces: **behind
`run_as`/`container`, choose the preset's most-permissive flags.** The point of
the OS boundary is that the harness no longer has to police itself — a
half-trusted `--sandbox` inside an untrusted user buys nothing but breakage
(it was `--sandbox` that silently broke message staging for a whole org). Let
the agent be free inside the cell; let the cell be the wall.

## `run_as` — prerequisites (operator-provisioned, once)

1. **Create the agent user** — no sudo group, no sudoers entry of its own:

   ```bash
   sudo useradd -m -s /bin/bash r4t-leader
   ```

2. **Grant the router passwordless sudo to that user.** The scoped shape below
   limits the grant to the exact wake command; a blanket `NOPASSWD: ALL` for
   the target user also works but grants more than needed. Replace `router`
   with the user the r4t daemon runs as, and edit with `sudo visudo`:

   ```sudoers
   # /etc/sudoers.d/r4t-leader  (chmod 0440)
   Cmnd_Alias R4T_LEADER = /usr/bin/bash --login -c *
   router ALL=(r4t-leader) NOPASSWD: R4T_LEADER
   ```

   Verify the grant the same way r4t probes it:

   ```bash
   sudo -n -u r4t-leader true && echo "grant OK"
   ```

3. **Make the org workplace writable by the agent user.** r4t members work in
   the shared repo (cwd = the workplace), so the agent user needs write there.
   A shared group is the least-surprising arrangement:

   ```bash
   sudo groupadd r4t-work
   sudo usermod -aG r4t-work router
   sudo usermod -aG r4t-work r4t-leader
   sudo chgrp -R r4t-work /path/to/workplace
   sudo chmod -R g+ws /path/to/workplace     # g+s so new files stay group-owned
   ```

r4t re-probes 2 and 3 before every turn (`sudo -n -u <user> true`, then a touch
in the workplace as the agent user) and fails the turn if either regresses.

### What r4t does own

The message channel — the per-member staging dir that `TELL_OUTBOX_DIR` points
at — is r4t's own state, not the operator's repo. r4t re-asserts it to
`router:<agent-group>`, mode `2770` setgid, before every turn, so the agent
writes envelopes into it and everything the agent creates stays group-owned and
reachable by the router. Turn transcripts live under the router-owned team
directory and are written from the subprocess pipe, so the agent user cannot
read, alter, or erase the audit trail.

### The wrapper

```
sudo -u <user> bash --login -c \
  'export TELL_OUTBOX_DIR="$1"; cd "$2"; n="$3"; shift 3;
   while [ "$n" -gt 0 ]; do export "$1"; shift; n=$((n - 1)); done; exec "$@"' \
  _ <staging-dir> <workplace> <count> <NAME=value...> <argv...>
```

`bash --login` so the agent user's own profile resolves its per-user tool
installs. The environment cannot survive sudoers `env_reset`, so
`TELL_OUTBOX_DIR`, the workplace, and any further variables the turn needs ride
as positional arguments — `<count>` says how many `NAME=value` words follow, and
each is one word, so a value with spaces cannot re-split. `exec "$@"` hands the
harness argv through untouched, so quoting bugs are impossible. The rig's
wall-clock timeout wraps the whole `sudo`.

## `container` — prerequisites and contract

The image contract is minimal: **the harness CLI must be on the image's PATH.**
r4t never builds, pulls, or inspects the image — a missing image is an ordinary
turn failure with docker's own error on the human surface. The turn runs:

```
docker run --rm --name r4t-<node>-<member>-<ts> \
  -v <workplace>:<workplace> -w <workplace> \
  -v <staging>:<staging> -e TELL_OUTBOX_DIR=<staging> \
  -v <a8s-client>:<a8s-client>:ro -e PATH=<a8s-client>:<standard-path> \
  <container_args...> <image> <argv...>
```

- The workplace is bind-mounted read-write at the same absolute path and used
  as the workdir.
- The staging dir is bind-mounted read-write with `TELL_OUTBOX_DIR` injected
  via `-e` (no `env_reset` inside a container).
- The a8s client dir is mounted read-only with a PATH shim so the unmodified
  `tell` resolves inside the container.
- Network stays on (agents talk to model APIs). Turn it off in `container_args`
  if you want it off.

### `container_args` — credentials and everything else

An org-level `container_args` list is appended to `docker run` verbatim (the
option-passthrough principle). Cloud-CLI credentials ride here as
operator-chosen read-only mounts:

```json
{
  "container": "r4t/claude:latest",
  "container_args": [
    "-v", "/home/router/.config/anthropic:/root/.config/anthropic:ro",
    "--network", "host"
  ]
}
```

Interactive and browser-based auth flows are an explicit non-goal: a harness
that cannot authenticate headlessly from mounted files does not belong in a
container rig. On rig-timeout expiry r4t kills the container by its
deterministic name and lets `--rm` reap it.

## The `a8s_tell` tool behind the boundary

The `mcp` knob ([rigs](rigs.md#the-a8s_tell-tool-mcp)) — on by default on most
presets — injects the a8s MCP server into every turn, and each harness takes it
a different way:
claude, codex and copilot read a flag, so it rides argv through both wrappers
untouched; opencode reads a file named by `OPENCODE_CONFIG`; cursor reads
`.cursor/mcp.json` in the workdir. A boundary keeps only what it is told to keep,
so r4t carries each idiom across:

- **`run_as`** re-exports `OPENCODE_CONFIG` through the wrapper's counted
  positionals and writes the config into `agents/<member>/mcp/`, beside the
  staging outbox and readable by the agent user. cursor's file lands in the
  workplace, which the agent user already has.
- **`container`** mounts that one config dir read-only, passes the matching
  `-e`, and names the image's own `python3` for the server — the router's
  interpreter path is not in the image, while the a8s client dir is mounted at
  its real path. So the image contract gains one line when the knob is on:
  `python3` on PATH, which the `tell` shim already wants.
- **Before a `run_as` turn** r4t probes that the agent user can read the server
  script, its interpreter, and the config. If it cannot, the turn fails closed
  with the fix (`chmod -R a+rX` on the checkout, or the knob off). A member whose
  prompt teaches `a8s_tell` against a server that never starts has no way to send
  at all, so that outcome is worth refusing rather than degrading into.

Behind either boundary the server is told the outbox and nothing else: the
router's `HOME` and `A8S_HOME` are another user's home, or absent from the image,
and `tell` writes the envelope from `TELL_OUTBOX_DIR` alone.

## What this boundary does not do

The boundary controls filesystem and privilege, not the network or the
secrets you hand across it. An isolated rig can still reach any endpoint
(the field's converging answer is default-deny egress through an
allowlisting proxy), and a credential mounted read-only is readable by
everything that runs inside the boundary for as long as it is mounted
(the stronger pattern is short-lived, per-run secret injection). Both are
candidates for a later isolation round; today, mount only credentials the
rig's job actually needs, and treat "isolated" as meaning *it cannot
change what runs* — not *it cannot phone out*.

## Whole-node containment (a deployment choice, not a feature)

The org boundary wraps each member's turn. Because r4t anchors a team on a
directory and runs as ordinary machinery, an operator who wants a coarser wall
can simply run the **entire node** — the r4t daemon, a8s, everything — under one
Unix user or inside one container, as a deployment choice. Nothing in r4t needs
to know: it is just `sudo -u node-user r4t ...` or a node-wide container image.
Per-org `run_as`/`container` and whole-node containment compose; pick the grain
your threat model wants.

## Testing the real boundary

`tests/docker/run-as.sh` is the reference provisioning sequence: it builds an
`ubuntu:24.04` container and, as root inside it, creates a sudo-less agent user,
a shared work group, a scoped `visudo`-validated sudoers drop-in, and setgid
`2770` staging/workplace dirs — then runs a real org-level `run_as` dispatch turn
and asserts the boundary holds from inside (the turn's effective user is the
agent user, `TELL_OUTBOX_DIR` survived `env_reset`, staging writes are
group-writable, the agent cannot sudo, and it cannot read the router's home).
Run it directly (`apps/r4t/tests/docker/run-as.sh`); CI runs it on r4t changes.
It doubles as a copy-pasteable provisioning checklist for a real deployment.

## Not automated (by design)

Auto-provisioning users, sudoers, or workplace group bits; a forbidden-paths
sweep of the agent user's home; GUI/desktop OAuth flows; supervising the router
daemon itself. See [the security model](security.md) for what a repo edit can
never change,.
