# tool-command-shell

A command runner. Each message is one shell command. The node runs it in its
own directory and replies with the output. It is a small SSH: stand up a node
on any machine, allowlist who may drive it, and send commands. Each message is
one command in its own process. Nothing persists between messages: no session,
no remembered working directory.

This page is the reference for the node. The tool overview is in
[docs/a8s-tools.md](../../../docs/a8s-tools.md).

## Needs

Nothing beyond the suite. The node runs commands with the shell its `SHELL`
var names (`sh` on POSIX, `cmd` on Windows by default).

## Register

```bash
a8s add mac-terminal ~/work tool-command-shell --ALLOW=operator,assistant
```

`ALLOW` is required. For a node registered without it, a8s aborts every wake
before the tool runs and dead-letters the message (`a8s logs <node>` names the
missing var). A shell node can never run with no allowlist. A sender not on the
list gets no reply. The refusal is logged on the node.

## Usage

Run one shell command per message and reply with its output.

On the node's machine, `ar3 tool` drives the node from one terminal. It reads
the node's vars from the a8s registry and runs in the node's directory. You
are the operator, so you pass `ALLOW`:

```bash
ar3 tool command-shell --node mac-terminal                 # a session
ar3 tool command-shell --node mac-terminal -- ls -la       # one command
ar3 tool command-shell --node mac-terminal - <<'EOF'       # one command from stdin
cd docs && du -sh *
EOF
```

A session takes one command per line. Each line runs in its own process in the
node's directory, and its reply prints when it is done. `cd` does not carry
from one line to the next. A line that ends in `<<EOF` (or another marker)
opens a block. The block goes to the shell whole, up to the line that holds the
marker. A failed command prints its reply and the session goes on. Ctrl-C stops
the running command. Ctrl-D ends the session. Attachments print as
`artifact: <path>`.

The tool takes no seat lock. Each message is its own process in its own
session, so nothing is shared. A session command and a wake run side by side.

From another node, work in real time with two terminals. In the first, inside
the directory of your own node (the sender must be on `ALLOW`):

```bash
tells -f
```

In the second:

```bash
tell mac-terminal ls -la
tell mac-terminal - <<'EOF'
cd docs && du -sh *
EOF
```

The reply arrives in the first terminal. Output over `INLINE_MAX` is attached;
attachments land under your node's `.files/<msg-ulid>/`.

### Commands

| Message | Meaning |
|---|---|
| `<command>` | The whole message (attachment lines removed) is run by the shell. An empty message answers `no command given`. |
| `help` | The usage text. Also `/help`, `--help`, `/?`. The message must be that one line. Nothing runs. `help me` or `echo help` are commands, and run. |

### Reply

```
$ du -sh *
exit 0 in 0.14s

stdout:
12K   docs
4.0K  README.md
```

The reply names the command, then `exit <code> in <seconds>s`, then stdout and
stderr. When both streams are empty it says `(no output)`. When the deadline
passes it says `timed out after <n>s, process group killed`. Output over
`INLINE_MAX` comes back as `stdout.txt` and `stderr.txt` attachments, with a
short head inline. A stream over `OUTPUT_MAX` keeps its first `OUTPUT_MAX`
bytes. The rest is read and dropped, and the reply says so.

One message is one process tree. Nothing a command starts outlives it. A
background child the shell leaves behind is killed when the command ends,
whether by exit, by `TIMEOUT`, or by a8s ending the wake (node stop, the 600
second wake limit). A child that still holds the output pipes keeps the command
running until `TIMEOUT`. To leave something running on the machine, start it in
its own session with its output redirected:
`setsid my-server >/tmp/my-server.log 2>&1 &`.

### Options

Set with `a8s add ... --KEY=VALUE` or `a8s vars <node> set KEY VALUE`.

| Var | Default | Meaning |
|---|---|---|
| `ALLOW` | none (required) | Sender names permitted to run commands. Comma or space separated, case-insensitive. |
| `SHELL` | `sh` (POSIX), `cmd` (Windows) | Shell: `sh`, `bash`, `zsh`, `cmd`, `powershell`, `pwsh`, or any interpreter that takes `-c`. |
| `TIMEOUT` | `60` | Seconds before the command and everything it started are killed. Keep it under the definition's 600 second wake limit. |
| `INLINE_MAX` | `4096` | Bytes of output carried inline. Beyond it, output is attached. |
| `OUTPUT_MAX` | `10485760` | Bytes kept per stream. |

```bash
a8s add win-box . tool-command-shell --ALLOW=operator --SHELL=powershell --TIMEOUT=120
a8s vars mac-terminal set INLINE_MAX 8192
```

## State

None. Each message runs in the node's directory. The tool keeps nothing
between messages.

## Platforms

POSIX and Windows. On POSIX the kill is the command's own process group. On
Windows there is no process group: the kill is `taskkill /T` on the shell's
tree. It reaches what is running at the deadline while the shell is alive. A
child the shell leaves behind after it exits is not reached. A stop from a8s is
a plain TerminateProcess that the tool cannot see, so the command outlives that
stop on Windows.

## Try it by hand

On the node's machine, see Usage: `ar3 tool command-shell --node <node>`.
For a directory that is not a node, run one wake and print the reply:

```bash
ar3 tool command-shell --var ALLOW=operator -- "echo hello"
ar3 tool command-shell --var ALLOW=operator -- help
```

The tool has no other CLI. Its wake entry is
`python3 apps/tools/command_shell --from <sender> --allow <names> -- <message>`.

## Trust

`$SENDER` is the router's own stamp: filesystem ownership for a local send, the
cluster secret across a remote. The ACL is exactly as strong as cluster
membership. Anyone who can send as an allowlisted name runs commands as the
node's user. Allowlist only names you trust with a shell, and run the node as a
user with the access you intend to lend.
