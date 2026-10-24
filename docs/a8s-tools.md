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

Each tool has its own README with the full command list, the reply shape, the
vars, the state and the trust notes.

## Drive a tool in real time

### From the node's machine

`ar3 tool <name> --node <node>` drives a tool node from one terminal. It reads
the node's vars from the a8s registry, runs in the node's directory, and prints
each reply. The person at the keyboard is the operator and passes the ACL.

```bash
ar3 tool command-shell --node mac-terminal                  # a session
ar3 tool command-shell --node mac-terminal -- uname -a      # one script
ar3 tool command-shell --node mac-terminal - <<'EOF'       # one script from stdin
uname -a
EOF
```

With no command, `ar3 tool` starts a session. Type one command per line. Each
reply prints when the command is done. A failed command prints its reply and
the session goes on. A line that ends in `<<MARKER` opens a block that runs to
a line holding `MARKER`. Press Ctrl-D to end the session. A session reads from
a pipe as well, with no prompt. `--var KEY=VALUE` overrides a node var for the
run. `--attach PATH` adds a file to a one-script run. Attachments print as
`artifact: <path>`.

A browser seat has one lock. `a8s stop` ends the script that holds the seat:
its next command fails with `seat closed by a8s stop`. Every path that opens Chrome
checks the stop, so a stop ends any operation that began before it. A new
command after the stop opens Chrome again.

`tool-vault` has no `--node` and no session. Its messages are sealed. Use the
vault client.

### From another node

Open two terminals. In the first, inside the directory of your own node, watch
for replies:

```bash
tells -f
```

In the second, send the tool a message. Send multi-line scripts on stdin so the
shell never touches them:

```bash
tell mac-terminal - <<'EOF'
uname -a
EOF
```

The reply arrives in the first terminal. Attachments land in your node's
`.files/<msg-ulid>/`.

### Try a tool with no node

`ar3 tool <name>` without `--node` runs the tool in the current directory with
the vars you give by `--var`, with no registry:

```bash
ar3 tool command-shell -- "echo hello"
ar3 tool command-shell -- help
```

## Ask a tool for help

Send `help` to any tool node. The message must be a single line that reads
`help`, `/help`, `--help` or `/?`, in any case. The node answers with its usage
text and runs nothing: no command, no browser step, no Gemini turn. The text is
the `## Usage` section of the tool's README, read when help is called. It lists
the commands and the vars that shape the tool. The reply names the node and the
tool and ends with the path of the README.

```bash
tell mac-terminal help
```

`help me open a page` is not help. It goes to the tool as a command. A sender
who is not on the node's `ALLOW` list gets no reply, to `help` as to anything
else. `tool-vault` has no ACL, so every sender gets its help in plaintext.

## The tools

### `tool-command-shell` - a command runner

Run a shell command sent as a message, and reply with its output. A small SSH:
each message is one command, in its own process, in the node's directory.
Nothing persists between messages. Output that fits rides in the reply, and
larger output is attached. Full reference:
[apps/tools/command_shell/README.md](../apps/tools/command_shell/README.md).

### `tool-vault` - encrypted file storage

An encrypted file store for agents. `help`, `/public-key` and `/connect` travel
in plaintext. Every other message is sealed to the vault's key, and every reply
is sealed and signed. There is no ACL var: every sender is served under its own
name and needs an operator-issued voucher. Full reference:
[apps/tools/vault/README.md](../apps/tools/vault/README.md).

### `tool-browser` - a signed-in Chrome seat

A persistent Chrome that other agents drive by message. Each message is a
script of browser commands, one per line. The reply is a transcript, with
snapshots, screenshots, console logs, videos and downloads attached. The
profile keeps its logins between messages. Full reference, with every command:
[apps/tools/browser/README.md](../apps/tools/browser/README.md).

### `tool-gemini-web` - Gemini through a browser seat

Gemini on the web, as an a8s seat. The tool types each message into Gemini in a
signed-in browser seat and sends the answer back. Each sender has its own
conversation. Files go in and generated images come back. Full reference:
[apps/tools/gemini_web/README.md](../apps/tools/gemini_web/README.md).

## The ACL

The tools that run something for a sender (`tool-command-shell`, `tool-browser`,
`tool-gemini-web`) take a required `ALLOW` var: the senders who may use the
node, comma- or space-separated, case-insensitive. For a node registered
without it, a8s aborts every wake before the tool runs and dead-letters the
message (`a8s logs <node>` names the missing var). A sender who is not on the
list gets no reply, and the refusal is logged on the node.

`$SENDER` is the router's own stamp: filesystem ownership for a local send, the
cluster secret across a remote. The ACL is exactly as strong as cluster
membership. Allowlist only names you trust with what the tool can do.

The person at the node's machine who runs `ar3 tool --node` is the operator
and passes the ACL. The ACL is for senders on the network.

## Writing another tool node

Each tool is its own module or package under `apps/tools/`, sharing
`apps/tools/_runtime.py` for the parts every tool node does the same: read the
wake, enforce the ACL, and reply through `a8s tell`. Cross-platform process
control is the foundation (`ar3.proc`). A tool never imports an a8s module. Its
contract with a8s is the wake argv its `tool-<verb>.json` defines and the
`a8s tell` CLI it replies through.

Ship these with the tool:

- The definition, in `apps/a8s/definitions/`.
- The handler and its tests, under `apps/tools/`.
- A `README.md` beside the handler, with the sections the other tools use:
  Needs, Register, Usage, State, Platforms, Try it by hand, Trust.
  The `## Usage` section is the tool's help text (see below). Link it from this page.

The help contract:

1. Write a `## Usage` section in the tool's README. It is the help text, and
   there is no other copy. It holds what an agent needs to drive the tool: one
   line on what it does, the real-time loop, the commands with their syntax
   (a table), the reply shape, and the vars (`### Options`). The wrapper reads
   from the line after the heading to the next `## ` heading. Keep the section
   readable as plain text, because it is sent as a message body.
2. In the handler, after the ACL check and before any parsing of the message,
   call `rt.is_help(message)`. When it is true, send
   `rt.help_reply(tool, node, readme, suite_path)` with `rt.send_reply` and
   return 0. `readme` is `Path(__file__).resolve().parent / "README.md"`.
   `suite_path` is the README path inside the suite. A refused sender never
   reaches this line, so it gets no reply. A README with no `## Usage` section
   gets a one-line reply that says so.
3. Anything dynamic, such as a key fingerprint, goes in the `extra` argument of
   `rt.help_reply`, not in the README.
4. Every exit is 0. a8s dead-letters a wake that exits with any other code.
