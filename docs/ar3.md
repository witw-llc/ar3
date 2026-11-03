---
name: "ar3"
description: "Show whether AR3 is set up and working on this machine."
---

# AR3

The front door to AR3: **a8s** (agent message router), **r4t**
(the roster), **k7e** (knowledge engine).

`ar3` never mutates product state; it owns and maintains the suite's own
substrate instead. It reads a8s/r4t/k7e state and probes prerequisites, and it
never runs another product's commands for you (`ar3 tool` is the one exception: it drives a tool node on this machine, below) — when something is missing,
`ar3` names the real command to fix it. What `ar3` writes is its own substrate
and never a product's state: `ar3 deps` fetches on-demand heavy dependencies
into `~/.local/share/ar3/deps`, `ar3 update` maintains the suite install
itself, and `ar3 update --auto` keeps its state under `~/.config/ar3/update`
and one scheduler entry. The verb that creates the need installs the dependency on its
own — `a8s storage mybucket s3://my-bucket` fetches boto3 the moment that
service is registered; `ar3 deps` is the explicit form, for scripts and for
`ar3 doctor`.

```
ar3            # where the suite stands right now
ar3 doctor     # are the harnesses and tools it runs on actually working?
ar3 update     # pull this install forward to the latest release
ar3 update --auto # nightly auto-update: show it; `on` and `off` turn it on and off
ar3 tool command-shell --node mac-terminal  # drive a tool node from this terminal
ar3 deps       # list on-demand heavy dependency groups and their status
ar3 deps a8s-s3 # fetch one group by hand (here, boto3 for a8s's S3 storage)
ar3 --version  # the suite semver (every ar3 CLI answers this)
```

## `ar3` — the greeter

```
A R K
8 4 7
S T E
```

Then one panel per product, each line marked ✓ or ✗ with a short fact, and
every ✗ followed by the command that fixes it:

```
a8s — agent message router  (~/.config/a8s)
  ✓ cli       a8s -> /path/to/a8s
  ✓ registry  3 agent(s), 1 alias(es), 0 namespace(s)
  ✗ router    no agent attached   (try: a8s start <agent>)

r4t — the roster  (~/.config/r4t)
  ✓ cli    r4t -> /path/to/r4t
  ✓ rigs   2 rig(s): leader, worker
  ✗ rosters  none under ~/.config/r4t/rosters   (try: r4t add <dir> [<runbook>])

k7e — knowledge engine  (~/.config/k7e)
  ✓ cli    k7e -> /path/to/k7e
  ✓ store  41 entr(ies) under ~/.config/k7e/nodes
  ✗ index  no search index   (try: k7e reindex)
```

A product that is not installed at all degrades to its ✗ rows and hints; `ar3`
still prints the rest.

State locations follow each product exactly, including the environment
overrides: `A8S_HOME`, `R4T_HOME` (and `XDG_CONFIG_HOME`), `K7E_HOME`.

## `ar3 tool`

Use a bundled tool from the terminal:

```text
ar3 tool NAME [--node NAME | --seat NAME] [--from NAME] [--var KEY=VALUE]... [--attach PATH]... [--] [command... | -]
```

Tool names are `browser`, `gemini-web`, `command-shell`, and `vault`.
Put wrapper options before the command. Use `ar3 tool NAME --help` for public
commands and settings. Use `ar3 tool NAME COMMAND --help` for one local verb.
Help performs no tool operation.

`--node NAME` loads a registered node's variables, environment settings, and
root directory. Its definition must match the tool name. `--seat NAME` selects
local state without loading registered settings. Do not combine them.
Without either selector, the tool uses its default name and the current
directory. `--var KEY=VALUE` overrides a setting for one run and is applied
last. Saved configuration uses `a8s vars NODE set KEY VALUE`.

The local operator passes `ALLOW` by default. `--from NAME` selects the local
message sender, otherwise `operator`. A final `--var ALLOW=...` can still
refuse that sender. Network access rules remain unchanged.

### Local commands

| Tool | Local verbs |
|---|---|
| `browser` | `doctor`, `open`, `close`, `status`, `go`, `snap`, `console`, `do`, `idle`, `secret` |
| `gemini-web` | `sessions`, `forget` |
| `vault` | `init`, `fingerprint`, `admin`, `voucher`, `seat`, `client` |
| `command-shell` | Uses shell messages. Configure it with `a8s vars` or `--var`. |

```bash
ar3 tool browser --node browser-seat doctor
ar3 tool browser --node browser-seat secret set PASSWORD --origin https://app.example.com
ar3 tool gemini-web --node gemini sessions
ar3 tool gemini-web --node gemini forget operator
ar3 tool vault --node vault init
ar3 tool vault client --help
```

Secret values come from a hidden prompt or stdin, never an argument. Secret
commands require an explicit `--node` or `--seat`. They start no Chrome.

The vault client runs from the sender's directory and environment. It rejects
`--node` and `--seat`. Use `--to NAME` after the client action to select a vault.
The browser's public `do` command loads bundled recipes and the registered
node's private recipes. It does not accept arbitrary recipe directories.

### Messages and sessions

Put `--` before the message to bypass local verb selection. For example,
`ar3 tool gemini-web --node gemini -- sessions` sends a Gemini prompt. Without
`--`, `sessions` lists local conversation records.

```bash
ar3 tool command-shell --node terminal -- "echo hello"
ar3 tool gemini-web --node gemini -- "Hello"
ar3 tool browser --node browser-seat -- help
ar3 tool browser --node browser-seat - <<'EOF_SCRIPT'
go https://example.com
snap
EOF_SCRIPT
```

With a message, the tool runs once and prints the reply. With `-`, it reads
one message from stdin. With no command, it starts a session. A session reads
one command per line and continues after failures. End a line with
`<<MARKER` to start a block. A line containing only `MARKER` ends that block.
Press Ctrl-D to end the session. Piped input has no prompt. The vault has no
interactive message session.

`--attach PATH` adds a file to a message or stdin run. Local replies print in
the terminal. Attachments are copied to a temporary directory named on stderr
and listed as `artifact: <path>`. Local command exit codes report the tool's
result. Routed wakes report their result in the reply.

See [Tool nodes](a8s-tools.md) for registration, access control, profile setup,
and links to each tool's complete reference.

## `ar3 doctor`

Functional probes for the CLIs the suite runs on — every one is read-only and
time-bounded, so `doctor` never hangs and never installs, starts, or
configures anything.

- **Suite** — the version this copy is running, and whether a newer one is
  published on the public mirror. This is the one probe that reaches the
  network, so it lives here and not in bare `ar3`; when GitHub cannot be
  reached it says so rather than claiming you are current.
- **Harnesses** — `claude`, `agent` (Cursor), `codex`, `copilot`, `opencode`,
  `agy`, `ollama`: on PATH, and does a version probe actually answer?
- **Services** — the ollama server (reachable? which models are pulled?),
  docker (binary present *and* daemon reachable — they fail differently), and
  `a8s secrets`: a password that a8s stores for a remote or storage entry but
  does not use, because an earlier release stored it or it belongs to another
  destination. The row names each entry and the command that stores it again.
  It never shows a password.
- **Tooling** — git present with `user.name` and `user.email` configured, `openssl` on PATH (the vault tool node needs it), and
  `utf-8 output`: on Windows, whether this child runs in UTF-8 mode or carries
  a `PYTHONIOENCODING`, and what its stdout encoding actually is — a cp1252
  console is what makes the suite's arrows and em dashes crash a command that
  was only printing. On macOS and Linux the line reads *not applicable*.

Exit code is 0 when the core prerequisites hold (git configured, at least
one agent harness answering, and no unused a8s password), 1 otherwise — so it
can gate a setup script.

## `AR3_PYTHON`

Every launcher — `ar3`, `a8s`, `tell`, `tells`, `r4t`, `k7e`, and their `.cmd`
and `.ps1` siblings — tries `AR3_PYTHON` before `python3`, `python` and
`py -3`. Set it to the full path of an interpreter when a harness bundles its
own and will not put it on `PATH`. It is probed like any other candidate: a
path that does not run is named on stderr and the launcher carries on down
`PATH`, so a stale variable never takes the suite down.

## `ar3 deps`

A handful of features depend on a heavy package most installs never need —
boto3 for a8s's S3 storage service. Those packages are not vendored and not
required at install time; the feature that needs one calls
`ar3.deps.use_group` and degrades to a WARN naming the fix when the group is
not there.

`ar3 deps` lists every group defined under `requirements/*.txt` with its
installed/missing status for the running interpreter. `ar3 deps <group>`
fetches that one group with `uv pip install --target` (or plain `pip` when
`uv` is not on PATH) into `~/.local/share/ar3/deps/<interpreter>/<group>` —
one directory per Python build, so an interpreter upgrade or a machine move
never half-loads an incompatible install. Besides the install tree itself and
the auto-update state and scheduler entry (below), this is the only place `ar3`
ever writes.

## `ar3 update`

Pulls this install forward and restarts running a8s nodes so their handlers
re-exec the new code. It runs `get.sh` — the same installer the one-liner
fetches — from beside the copy you invoked, with `AR3_DIR` pointed at that
copy, so the install you updated is the one you ran. `AR3_VERSION` pins a
release and `AR3_CHANNEL` picks stable or beta, exactly as at install time.
On Windows it runs `get.sh` through Git for Windows' `sh` — the one on PATH,
or the one beside `git` when PowerShell or cmd.exe carries only Git's `cmd\`
directory — so the verb works from any shell, not only Git Bash.

It reports the version it moved from and to, or says nothing moved.

**`--engine NAME [NAME ...]` and `--all-engines` update engine binaries as
part of the same run** — the agent-machine refresh in one line. With either
flag the sequence is: every running a8s node is stopped (`a8s stop` waits
for the current wake, so nothing is mid-turn while its binary is
replaced), the selected engines update, the suite update runs, and exactly
the nodes that were stopped start again. Nodes come back even when an
update fails. The two flags contradict.

**A busy node is waited for, never stopped mid-turn.** A stop would end a
resident session's turn and drop the messages it holds. `--wait SECONDS`
(default 600) is how long `ar3 update` waits for every node `a8s ps --json`
reports as not idle (busy, or a status probe that failed). Each stop is then
decided on a fresh `a8s ps --json` snapshot: a node that is not idle is left
running on its current handler with every node that shares its handler, the
output names it, and when no snapshot can be read no node is stopped. Run
`ar3 update` again once `a8s ps` shows it idle. Without the engine flags the
wait is handed to the installer's `a8s update`, which applies the same rule.

Each engine is updated by its own known method — a self-update verb
(`claude update`, `agent update`, `agy update`, `devin update`,
`opencode upgrade`), its package manager when the binary resolves to one
(`brew upgrade` for Cellar installs, `brew upgrade --cask` for Caskroom,
`npm install -g <pkg>@latest` for npm globals, with `sudo` when the global
root is not writable), or a forced check (muse's launcher updates itself on
any invocation under `MUSE_SYNC_UPDATE=1`). Package-managed installs are
updated by the manager, never the engine's own verb — a self-updater would
lay a second copy beside the managed one. Names are the binaries `ar3
doctor` probes (`claude`, `agent`, `codex`, `copilot`, `opencode`, `agy`,
`muse`, `devin`, `ollama`); engine ids like `cursor` or `ollama-codex` are
accepted and resolve to their binary. Command names are resolved to their
paths before they run, so npm `.cmd` shims and version-manager installs
work on Windows, WSL, Ubuntu and macOS alike; a platform without `sudo`,
or a brew/npm-managed binary whose manager is not on PATH, gets a named
refusal rather than a failed command. An engine with no known method, or
an explicitly named engine that is not installed, fails the run after the
rest have been tried.

**It refuses on a working checkout.** `get.sh` reaches the tree with
`git pull --ff-only`, and on a pinned version `git checkout`. Run against a
clone somebody is developing in, that ranges from a confusing failure to
discarded work, and nothing printed afterwards undoes it. So an uncommitted
change to a tracked file, or a branch that is not the remote's default, stops
the update before it starts and says which it found, naming the paths. A
detached HEAD does not: that is what an `AR3_VERSION` pin leaves behind, and
`get.sh` rejoins the branch itself. An untracked file does not either: a state
file some tool left beside the install is not work in progress, and git stops
a pull or a checkout by itself, naming the path, when an incoming file would
land on one. The installer never forces a checkout, so that stop holds on a
pinned update too.

## Auto-update

A per-user install updates itself at night, and only when no seat is busy.
`ar3 update --auto on` is the switch; a fresh install and the first manual
`ar3 update` turn it on, `ar3 update --auto off` turns it off for good, and
`AR3_AUTO_UPDATE=0` at install time records it off. A machine-wide install
(`AR3_SYSTEM=1`), or any tree this account cannot write, is never armed: the
account that owns it updates it. `ar3` and `ar3 doctor` print `auto-update: not
armed` until it is on.

```
ar3 update --auto                  # on or off, window, trigger, tonight's slot, last result, next try
ar3 update --auto --json           # the same, for scripts
ar3 update --auto on|off           # arm or remove the scheduler entry
ar3 update --auto --window 01:00-04:00   # when a night's try may start, local time (default 02:00-05:00)
ar3 update --auto --tell owner --from lead   # tell a seat what happened (--tell none stops it)
ar3 update --auto run --now        # one attempt now, outside the window
```

**The trigger** is one entry per install that runs every few minutes: a cron
line on Linux (it shares the account crontab lock with `a8s` autostart), a
LaunchAgent on macOS (with no graphical login the status says it arms at next
login), a scheduled task with an interactive token on Windows. macOS and
Windows run it only while the user is logged in. The entry starts a small
launcher kept in the ar3 home (`~/.config/ar3/update/<id>.launch.py`), outside
the install, so a tick still starts when an unfinished update left the install
moved aside as `<install>.prev`. The launcher names the install and the ar3
home that armed it, and the run uses the `PATH`, proxy settings and suite homes
(`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `A8S_HOME`, `R4T_HOME`, `K7E_HOME`) of the
shell that armed it, because a scheduler starts it with almost no environment.
Re-arm with `ar3 update --auto on` after you change one of them.

**Each tick decides in code.** Outside the window, before tonight's slot (a
start spread by install and night, so a fleet does not fetch at once) or after
the night is done, a tick exits without touching the network. After the slot,
every tick retries until the window closes; no attempt starts after it, and a
running one finishes. A second tick while one runs exits at once.

**It skips, and says why.** `ar3 update --auto` shows the last result: `noop`
(already current), `skip-busy` (a seat is not idle, or shares a handler with
one that is), `skip-unknown` (no `a8s`, `a8s ps` cannot say, the current
commit or release tag cannot be read, or the tree is not one auto-update
made), `skip-network` (also when the remote's commit cannot be read), `skip-refused`
(a working checkout, as for `ar3 update`), `skip-pinned` (an `AR3_VERSION` pin),
`skip-channel` (a beta clone: auto-update follows the public releases only),
`skip-bad-release`, and `skip-input` (the pull asked for a password, a key or a
host check, or hung; it runs with no terminal and no prompts, so run `ar3
update` by hand once). The log is `~/.config/ar3/update/<id>.log`, one line per
change; the installer's output is `<id>.last-run.txt`.

**It records before it changes anything.** Before the installer runs, the
run saves the current commit or release tag, the commit the remote is at (or
the release tag it tells the installer to fetch), and the seats that are
running. When the installer fails or hangs after it has changed the tree, or
the run ends under it, the tree is put back from that record (the seats never
left the old code). Every tick reads the tree on disk first and finishes what
an earlier run left (putting a tree back, a restart, a rollback) before it
checks the network or installs anything. It changes only a tree that is
exactly one it recorded: the same commit with no change to a tracked file, or
the same release tag. Any other tree, such as a commit a person made at the
same version, is never put back: the record is dropped, the result is
`skip-unknown`, and nothing changes. Right before each step that changes the
install or restarts seats onto it, both trees are read again: the install must
still be exactly the recorded tree, and a put-back or rollback runs only when
the tree it restores (the recorded commit, or `<install>.prev` with the
recorded tag and VERSION) is exactly the one recorded; otherwise nothing
changes. For a rollback that is `rollback-blocked` (the recorded commit is
missing, or `<install>.prev` is missing or holds another release): the seats
may still be on the failed release, so `ar3 update --auto`, `ar3` and `ar3
doctor` say so, and `--tell` sends it once a night until a person runs `ar3
update`. Anything else is `skip-unknown`. Nothing is put back, restarted or rolled
back while a seat is busy or cannot be asked about. A manual `ar3 update`
waits for a running tick and ends the record before it starts, so even an
interrupted one is never undone. When it cannot take the lock, or cannot read
or write the state in `~/.config/ar3/update`, it changes nothing, exits
nonzero and names the file: fix its permissions, or remove it to discard
auto-update recovery. A tick never writes over a state it cannot read, and
`ar3` and `ar3 doctor` say so; `get.sh` run by hand ends it when it
finishes.

**It checks its own work.** The pull leaves the seats on their old code. The
restart is `a8s update --wait 0` from the new tree, which looks at each handler
group again; a seat that turned busy in between stays on its old code
(`partial`) and a later idle tick restarts it. Then `ar3`, `a8s`, `r4t` and
`k7e` must each answer `--version` with the new version and every seat that was
running before the update must be running again (polled for 90 seconds); a
later tick checks the same seats. If not, the old tree is
restored (a git install resets to the recorded commit; a release unpack swaps
back `<install>.prev`, also when git has replaced a release unpack with a
clone), the seats restart onto it, the release is skipped from then on, and the
result is `rolled-back` (or `rollback-failed`). A rollback that waits for a busy
seat, or whose restart failed or left a busy seat on the failed release, is
`rollback-partial`: no busy seat is stopped, and the next idle tick finishes
it before anything else. The health
check is local; it asks no remote service.

**`--tell`** sends one line on `updated`, `rolled-back`, `rollback-partial`,
`rollback-failed`, `rollback-blocked` (once a night), `failed` and `skip-input`, from the registered node `--from`. A message that
cannot be sent is logged and changes nothing else.

## What `ar3` is not

`ar3` does not wrap, alias, or pass through the products' own verbs: sending a
message is still `tell`, running a roster is still `r4t dispatch`, storing
knowledge is still `k7e`. Those commands appear in `ar3` output only as hints
pointing you at the tool that owns them. `ar3 deps` and `ar3 update` are the
exceptions to "AR3 never mutates", and only in the same narrow way: they write
`ar3`'s own substrate — the dependency cache, the install tree, and the
auto-update state and scheduler entry — never a product's state. `ar3 update` runs the suite's own installer; it does not
reimplement it, and it is not a wrapper around `a8s update`, which means
something else entirely (restart running nodes so handlers re-exec).
