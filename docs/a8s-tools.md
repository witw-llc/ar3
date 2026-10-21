---
name: "a8s-tools"
description: "Tool nodes: a8s agents that run a program instead of a model. Bundled: tool-command-shell, tool-vault, tool-browser, tool-gemini-web."
---

# a8s tool nodes

A **tool node** is an a8s agent whose wake is a small program, not a model. a8s
delivers a message, the tool does one thing, and it replies by message. A tool
node needs no CLI, no model, and no key: it is a bundled definition plus a
stdlib handler.

Tool definitions are named `tool-<verb>` and ship in `apps/a8s/definitions/`.
Register one the same way as any node, giving the `tool-<verb>` definition by
its bare name:

```bash
a8s add mac-terminal ~/work tool-command-shell --ALLOW=operator,assistant
```

## `tool-command-shell` — a command runner

Run a shell command sent as a message, and reply with its output. A poor man's
SSH: stand up a node on any machine, allowlist who may drive it, and

```bash
tell mac-terminal ls -la
```

runs `ls -la` on that machine and sends the output back. Each message is one
command in its own process, in the node's directory. Nothing persists between
commands — no session, no remembered working directory. Output that fits rides
in the reply; larger output is attached as `stdout.txt` / `stderr.txt`.

### The ACL is mandatory

The `ALLOW` var lists the senders who may run commands — comma- or
space-separated, case-insensitive. It is **required**: for a node registered
without it, a8s aborts every wake before the tool runs and dead-letters the
message (`a8s logs <node>` names the missing var), so a shell node can never
run with no allowlist. A sender not on the list gets no reply; the refusal is
logged on the node.

`$SENDER` is the router's own stamp — filesystem ownership for a local send,
the cluster secret across a remote — so the ACL is exactly as strong as cluster
membership. Anyone who can send as an allowlisted name runs commands as the
node's user. Allowlist only names you trust with a shell, and run the node as a
user with the access you intend to lend.

### Options (per-node vars)

| Var | Default | Meaning |
|---|---|---|
| `ALLOW` | — (required) | Sender names permitted to run commands. |
| `SHELL` | `sh` (POSIX), `cmd` (Windows) | Shell: `sh`, `bash`, `zsh`, `cmd`, `powershell`, `pwsh`, or any interpreter taking `-c`. |
| `TIMEOUT` | `60` | Seconds before the command and everything it started are killed. Keep it under the definition's 600 s wake limit. |
| `INLINE_MAX` | `4096` | Bytes of output carried inline; beyond it, output is attached. |
| `OUTPUT_MAX` | `10485760` | Bytes kept per stream; the rest is read and dropped, and the reply says so. |

```bash
a8s add win-box . tool-command-shell --ALLOW=operator --SHELL=powershell --TIMEOUT=120
a8s vars mac-terminal set INLINE_MAX 8192
```

### The reply

```
$ du -sh *
exit 0 in 0.14s

stdout:
12K   docs
4.0K  README.md
```

One message is one process tree: nothing a command starts outlives it. A
background child the shell leaves behind is killed when the command ends,
whether by exit, by `TIMEOUT`, or by a8s ending the wake (node stop, the 600 s
wake limit), when the tool kills the tree before it goes. A child still
holding the output pipes keeps the command running until `TIMEOUT`. The kill
is the command's own process group on POSIX. Windows has no process group:
the kill is `taskkill /T` on the shell's tree, which reaches what is running
at the deadline while the shell is alive; a child the shell leaves behind
after exiting is not reached, and a stop from a8s is a plain TerminateProcess
the tool cannot see, so the command outlives both cases there. To leave something running on the machine,
start it in its own session with its output redirected:
`setsid my-server >/tmp/my-server.log 2>&1 &`. Output over `INLINE_MAX`
comes back as attachments with a short head inline; a stream over `OUTPUT_MAX`
keeps its first `OUTPUT_MAX` bytes, the rest is read and dropped, and the reply
says so.

## `tool-vault` - encrypted file storage

An encrypted file store for agents. `/public-key`, `/connect` and `/help` travel
in plaintext. Every other message is sealed to the vault's key, and every reply
is sealed to the agent's throwaway key and signed. The crypto runs through the
`openssl` command, which must be on `PATH` (or named by the `OPENSSL` var).

```bash
a8s add vault ~/vault-seat tool-vault
python3 <ar3>/apps/tools/vault init --node vault
python3 <ar3>/apps/tools/vault voucher issue alice --node vault
```

There is no ACL var: every sender is served under its own section, keyed by its
name, and no sender sees another's files. The operator issues the first voucher
for each name on the vault host. The agent pins the key, connects, and
authenticates with that voucher. The vault hands back a fresh voucher each time:

```bash
tell vault /public-key
python3 <ar3>/apps/tools/vault client pin vault.pem --fingerprint <F>
python3 <ar3>/apps/tools/vault client connect --to vault
```

`tell vault /help` shows the full protocol. Sealed commands are `/authenticate`,
`/list`, `/store <name>`, `/retrieve <name>`, `/delete <name>`, `/voucher
new|list|revoke-all` and `/logout`. A voucher works once, and a seat holds at
most a fixed number unused.

**State.** The key, pepper and index live in
`${XDG_DATA_HOME:-~/.local/share}/ar3/vault/<node>/` (mode 0700), and the sealed
files in its `data/` folder. Every tool node keeps its state under `ar3/`. The optional `VAULT_DIR` var (or environment variable) moves the
whole root. Admin commands (`init`, `fingerprint`, `admin`, `voucher`, `seat`)
take `--node` and `--dir` to find it.

**Trust.** The vault shares a Unix user with whoever runs the node. Anyone who
can read that user's files can read the key. Run it as its own user when that
matters.

## `tool-browser` - a signed-in Chrome seat

A persistent Chrome that other agents drive by message. Each message is a
script of browser commands. The tool runs the script in the seat's own Chrome
profile and replies with a transcript. The profile keeps its logins between
messages, so a page that needs a sign-in stays signed in.

**Needs.** Chrome, Node, and the Playwright command line on the node's `PATH`:

```bash
npm install -g @playwright/cli
a8s add garmin ~/browsers/garmin tool-browser --ALLOW=iris
```

A wake has no login profile. On Linux, put the Node bin directory (for
example the nvm one) on the node's path, and name the display for a headed
seat:

```bash
a8s vars garmin set env.PATH "$HOME/.nvm/versions/node/<version>/bin:/usr/local/bin:/usr/bin:/bin"
a8s vars garmin set env.DISPLAY :0
```

Chrome runs headed when `DISPLAY` is set on Linux, and headless otherwise.
It stays running between wakes. `a8s stop` closes it. A wake
attaches only to a Chrome that holds this seat's profile, on the port that
profile recorded; a listener on the recorded port that is not that Chrome is
never driven, and a fixed `PORT` that differs from the recorded port of a
running seat is refused (close the seat first). `a8s stop` has 30
seconds: the graceful close keeps to a shorter budget and then ends the
process tree, so the forced cleanup always runs before the deadline.

**Use.** One command per line. The script stops at the first failure, and a
failure attaches a snapshot of the page it stopped on.

```bash
tell garmin "open
go https://example.com
snap
shot"
```

The vocabulary is fixed: `open`, `close`, `save`, `url`, `go`, `click`,
`fill`, `press`, `type`, `text`, `wait`, `wait-for`, `wait-for-url`,
`assert-text`, `assert-url`, `back`, `forward`, `reload`, `scroll`, `find`,
`select`, `check`, `uncheck`, `hover`, `dialog-accept`, `dialog-dismiss`,
`requests`, `console`, `video-start`, `video-stop`, `video-chapter`, `snap`,
`shot`, `upload`, `drop`, `download`, `eval` and `run-code`. A line that ends
in `<<MARKER` opens a block that runs to a line holding `MARKER`.

The reply starts with `<node>: ok` or `<node>: failed: <reason>`, then lists
each command with its output. A snapshot, screenshot, console log, video or
download comes back as an attachment. The body is cut at 4000 characters.

### Options (per-node vars)

| Var | Default | Meaning |
|---|---|---|
| `ALLOW` | — (required) | Sender names permitted to drive the seat. A sender outside it gets no reply. |
| `ALLOW_EVAL` | off | `1` permits `eval` and `run-code`. |
| `CHROME` | found on `PATH` | The Chrome binary. |
| `PORT` | Chrome picks a free one | A fixed Chrome debugging port. Unset, Chrome picks a free port and records it in the profile, so seats never collide. Set, Chrome writes no port record, so the seat proves ownership through the process holding the listening socket (`lsof`, or `ss` on Linux); it fails closed when another process holds the port, and when its own running Chrome recorded a different port. |

**State.** The Chrome profile and the artifacts live in
`${XDG_DATA_HOME:-~/.local/share}/ar3/browser/<node>/`.

**Platforms.** Linux and macOS only. Process discovery, which both the attach
check and the forced cleanup need, is not implemented on Windows, so a seat
does not open there.

**Try it by hand.** `ar3 tool browser --from operator --var ALLOW=operator -- snap`
runs the tool once and prints the reply. This is a test surface, not a product
surface.

**Trust.** The profile holds a signed-in session, so a sender on `ALLOW`
acts as that person on every site the profile has open. Allowlist few names.
`eval` and `run-code` run any code in the page or the driver. They stay
refused until `ALLOW_EVAL` is on, so leave it off unless the work needs it.

## `tool-gemini-web` - Gemini through a browser seat

Gemini on the web, as an a8s seat. The tool types each message into Gemini
and sends the answer back as a message. Each sender has its own Gemini
conversation, and the conversation continues across messages. Files go in with
the message, and images Gemini makes come back as attachments. An answer too
long for the reply body comes back as the file `gemini-reply.md`.

**Needs.** A `tool-browser` seat, with its needs. A person must sign that
seat's Chrome in to Gemini by hand first. This tool never signs in.

```bash
a8s add gemini ~/agents/gemini tool-gemini-web --ALLOW=iris --BROWSER_SEAT=gemini-profile
a8s vars gemini set env.DISPLAY :0
```

The tool runs the browser tool by its path in the suite. It needs the same
display and `PATH` setup as `tool-browser`: on Linux, a headed seat needs
`env.DISPLAY`, and the node needs the Node bin directory on `env.PATH`.

**Use.** Send plain text, and attach files to send them.

```bash
tell gemini "Draw a lighthouse at dusk"
```

The reply is Gemini's answer. A line after `--` in the reply is a note from
the seat, for example an image or a file it could not carry.

**Exit code.** Every exit is 0, because a8s dead-letters a message on a
non-zero exit and does not retry. Two cases follow from this:

- A turn that cannot run answers `Your message was not sent. Send it again.`
  The seat is busy, the profile is not signed in, or the browser is not
  reachable.
- A reply that Gemini produced but `tell` could not deliver is held on disk
  and delivered on the next wake. The turn is not run again.

If the seat cannot tell whether Gemini received a message, it does not send it
again. The reply says so, and the sender checks the conversation.

### Options (per-node vars)

| Var | Default | Meaning |
|---|---|---|
| `ALLOW` | — (required) | Sender names permitted to ask. A sender outside it gets no reply. |
| `BROWSER_SEAT` | — (required) | The `tool-browser` node whose profile is signed in to Gemini. |
| `MODEL` | Gemini's default | A model as Gemini's own menu names it, picked when a conversation is created. |

**State.** The conversation map and the held replies live in
`${XDG_DATA_HOME:-~/.local/share}/ar3/gemini-web/<node>/`. One turn runs at a
time per node. A turn is bounded at 180 seconds.

**Platforms.** Linux and macOS only. The turn lock uses `fcntl`; on Windows the
seat answers every message "This seat runs on Linux and macOS only" and sends
nothing to Gemini.

**Try it by hand.** `ar3 tool gemini-web --from operator --var BROWSER_SEAT=gemini-profile -- hello`
runs one turn and prints the reply. This is a test surface, not a product
surface. `python3 <ar3>/apps/tools/gemini_web sessions` lists the
conversations, and `forget <sender>` drops one so the next message starts anew.

**Trust.** The Gemini account is the browser profile's account. Every sender
on `ALLOW` talks as that account. `a8s stop` closes the Chrome of the browser
seat.

## Writing another tool node

Each tool is its own module under `apps/tools/`, sharing `apps/tools/_runtime.py`
for the parts every tool node does the same: read the wake, enforce the ACL,
and reply through `a8s tell`. Cross-platform process control is the foundation
(`ar3.proc`). A tool never imports an a8s module — its contract with a8s is the
wake argv its `tool-<verb>.json` defines and the `a8s tell` CLI it replies
through. Ship the definition in `apps/a8s/definitions/`, the handler and its
tests under `apps/tools/`, and a section here.
