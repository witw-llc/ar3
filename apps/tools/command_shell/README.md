# tool-command-shell

Run a shell command and return its output. Each message starts a separate
process in the node's directory. The tool does not keep a shell session or a
working directory between messages.

Use `ar3 tool command-shell` from a terminal. Use `tell` from another node.
See [Tool nodes](../../../docs/a8s-tools.md).

## Needs

Install AR3. The tool uses `sh` on POSIX and `cmd` on Windows by default.
Set `SHELL` to select another shell.

## Register

```bash
a8s add terminal ~/work tool-command-shell --ALLOW=operator,assistant
a8s start terminal
```

`ALLOW` is required. If it is missing, a8s stops the wake before the tool runs.
It drops the message. Use `a8s logs terminal` to read the error. A sender outside `ALLOW` gets no reply. The node logs the refusal.

## Usage

Run one shell command per message and return its output.

On the node's machine:

```bash
ar3 tool command-shell --node terminal
ar3 tool command-shell --node terminal -- ls -la
ar3 tool command-shell --node terminal - <<'EOF_SCRIPT'
cd docs && du -sh *
EOF_SCRIPT
```

The first command starts an interactive session. The second runs one command.
The third reads one script from stdin. These script examples use a POSIX shell.
Use commands for your selected shell on Windows.

`--node` loads the node's settings and directory. The local operator passes
`ALLOW` unless `--var ALLOW=...` overrides that setting.

Session rules:

- Enter one command per line. Each line starts a separate process in the
  node's directory. A `cd` command does not affect the next line.
- To enter a script on several lines, end the first line with `<<MARKER`.
  End the block with a line that contains only `MARKER`.
- A failed command does not end the session.
- Press Ctrl-C to stop the current command. Press Ctrl-D to end the session.
- Attached files appear as `artifact: <path>`.

The tool has no seat lock. Local commands and routed messages can run at the
same time. They can access the same files.

From another node, open two terminals in that node's directory. Run this in
the first terminal:

```bash
tells -f
```

Send a command from the second terminal:

```bash
tell terminal ls -la
tell terminal - <<'EOF_SCRIPT'
cd docs && du -sh *
EOF_SCRIPT
```

The sender must be on `ALLOW`. Replies appear in the first terminal.
Attachments go to `.files/<msg-ulid>/` in the sender's directory.

### Commands

| Message | Result |
|---|---|
| `<command>` | Runs the message through the shell after attachment lines are removed. An empty message returns `no command given`. |
| `help` | Returns this usage text. Also accepts `/help`, `--help`, and `/?`. The message must contain only that one line. No shell command runs. |

`help me` and `echo help` are shell commands. They do not request tool help.

### Reply

```text
$ du -sh *
exit 0 in 0.14s

stdout:
12K   docs
4.0K  README.md
```

The reply shows the command, its exit code, its duration, stdout, and stderr.
It shows `(no output)` when both output streams are empty. A timeout returns
`timed out after <n>s, process group killed`.

The tool attaches output over `INLINE_MAX` as `stdout.txt` or `stderr.txt`.
The reply includes the start of the output. For each stream, the tool keeps at
most `OUTPUT_MAX` bytes. It discards the remaining bytes and reports this.

On POSIX, the tool ends the command's process group when the command exits,
times out, or receives an a8s stop. This also applies during startup. A child
that keeps an output pipe open keeps the command active until `TIMEOUT`.
To keep a service running independently on POSIX, start it in a separate
session and redirect its output:

```bash
setsid my-server >/tmp/my-server.log 2>&1 &
```

See Platforms for Windows process limits.

### Options

Set an option at registration with `a8s add ... --KEY=VALUE`. To change a
registered option, use `a8s vars <node> set KEY VALUE`. Use
`--var KEY=VALUE` before `--` for a change that applies to one local run.

| Variable | Default | Function |
|---|---|---|
| `ALLOW` | Required | Lists permitted sender names. Separate names with commas or spaces. Names are not case-sensitive. |
| `SHELL` | `sh` on POSIX; `cmd` on Windows | Selects `sh`, `bash`, `zsh`, `cmd`, `powershell`, `pwsh`, or another interpreter that accepts `-c`. |
| `TIMEOUT` | `60` | Sets the command time limit in seconds. Keep it below the 600-second wake limit. |
| `INLINE_MAX` | `4096` | Sets the output size in bytes above which the tool attaches a file. |
| `OUTPUT_MAX` | `10485760` | Sets the maximum number of bytes kept for each output stream. |

```bash
a8s vars terminal set INLINE_MAX 8192
a8s vars terminal set SHELL powershell
a8s vars terminal set TIMEOUT 120
ar3 tool command-shell --node terminal --var TIMEOUT=10 -- "echo hello"
```

## State

The tool keeps no session state. A command can change files in the node's
directory or anywhere else the node's OS user can write.

## Platforms

The tool supports POSIX and Windows. On POSIX, it ends the command's process
group. On Windows, it uses `taskkill /T` on the shell's process tree. That
command reaches children only while the shell is alive. It cannot reach a
child after the shell exits.

An a8s stop uses `TerminateProcess` on Windows. The tool cannot handle that
stop, so a shell command can continue after it.

## Try it by hand

Use a local directory without registration:

```bash
ar3 tool command-shell -- "echo hello"
ar3 tool command-shell -- help
ar3 tool command-shell --help
```

Use `--help` before `--` for local command-line help. Use `-- help` for the
message usage text. Configuration uses `a8s vars` or `--var`; no source-file
path is needed.

## Errors

| Condition | Action |
|---|---|
| Missing `ALLOW` | Run `a8s vars terminal set ALLOW operator,assistant`. |
| Shell not found | Install the shell or set `SHELL` to an available shell. |
| Command times out | Check the command. Increase `TIMEOUT` only if the work needs more time. |
| Output is incomplete | Read the attachments. Increase `OUTPUT_MAX` if the reply reports discarded bytes. |

## Trust

A permitted sender runs commands as the node's OS user. Give access only to
senders you trust with that user's files and programs.

For local messages, a8s sets the sender from filesystem ownership. Remote
sender identity depends on cluster membership and its shared secret. Use a
separate OS user when the node needs restricted access.
