---
name: "a8s-tools"
description: "Set up and use tool nodes for shell commands, encrypted files, Chrome, and Gemini."
---

# a8s tool nodes

A tool node runs a program when it receives a message. It returns the result
as a message. The bundled tools are `command-shell`, `vault`, `browser`, and
`gemini-web`. Each has a public command under `ar3 tool`.

## Register and configure a tool

Use the bundled definition name `tool-NAME`:

```bash
a8s add terminal ~/work tool-command-shell --ALLOW=operator,assistant
a8s vars terminal set TIMEOUT 120
a8s start terminal
```

Registration creates the node settings. `a8s vars` changes a saved setting.
`a8s start` starts message processing. Read the tool's README for prerequisites
and its option defaults:

| Tool | Function | Reference |
|---|---|---|
| `command-shell` | Runs shell commands as the node's OS user. | [Shell tool](../apps/tools/command_shell/README.md) |
| `vault` | Stores encrypted files for authenticated senders. | [Vault tool](../apps/tools/vault/README.md) |
| `browser` | Runs browser scripts in a saved Chrome profile. | [Browser tool](../apps/tools/browser/README.md) |
| `gemini-web` | Sends prompts through a signed-in Gemini profile. | [Gemini tool](../apps/tools/gemini_web/README.md) |

## Use a local terminal

Use `--node NAME` to load a registered node's settings and root directory.
The registered definition must match the requested tool. Put wrapper options
before the command:

```bash
ar3 tool command-shell --node terminal -- "echo hello"
ar3 tool command-shell --node terminal --var TIMEOUT=10 -- "echo hello"
ar3 tool browser --node browser-seat status
ar3 tool gemini-web --node gemini sessions
ar3 tool vault --node vault fingerprint
```

`--var KEY=VALUE` overrides a setting for this run only. It is applied last.
The local operator passes the network `ALLOW` check unless a final
`--var ALLOW=...` prevents it.

Without registration, use `--seat NAME` to select local tool state. Without
either selector, the tool uses its default name and the current directory.
`--seat` does not load registry settings. Do not combine the selectors.

```bash
ar3 tool command-shell -- "echo hello"
ar3 tool browser --seat scratch status
ar3 tool gemini-web --seat gemini-test --var BROWSER_SEAT=browser-seat -- "Hello"
```

### Local commands and messages

| Tool | Local commands |
|---|---|
| `browser` | `doctor`, `open`, `close`, `status`, `go`, `snap`, `console`, `do`, `idle`, `secret` |
| `gemini-web` | `sessions`, `forget` |
| `vault` | `init`, `fingerprint`, `admin`, `voucher`, `seat`, `client` |
| `command-shell` | Uses shell messages; set options with `a8s vars` or `--var`. |

A local verb runs only from the command line. A message cannot run a local
administrative verb. Put `--` before a message to select the message path
explicitly. For example, `ar3 tool gemini-web --node gemini -- sessions` sends
`sessions` as a prompt. Without `--`, it lists local conversation records.

Use `ar3 tool NAME --help` to list the public commands and options. Use
`ar3 tool NAME COMMAND --help` for one local command. Help does not start a
browser, call Gemini, or perform a vault operation.

The vault client runs in the sender's directory and environment. It refuses
`--node` and `--seat`. Use `--to VAULT_NAME` to select its destination:

```bash
ar3 tool vault client --help
ar3 tool vault client send /list --to vault
```

The client requires a verified pinned key, a connection, and authentication.
Follow the vault README before the first file request.

### Scripts and sessions

For a message tool, supply a command to run one script. Supply `-` to read one
script from stdin. Omit the command to start a session:

```bash
ar3 tool command-shell --node terminal
ar3 tool browser --node browser-seat - <<'EOF_SCRIPT'
go https://example.com
snap
EOF_SCRIPT
```

A session reads one command per line. A line ending in `<<MARKER` starts a
block; a line containing only `MARKER` ends it. A failed command does not end
the session. Press Ctrl-D to end input. Piped input uses the same format
without prompts. The vault has no interactive message session.

Use `--attach PATH` before a message or `-` to attach a file. Local results
print in the terminal. Attachments appear as `artifact: <path>`.

## Send from another node

Open two terminals in your node's directory. Watch replies in the first:

```bash
tells -f
```

Send a message from the second:

```bash
tell terminal - <<'EOF_SCRIPT'
uname -a
EOF_SCRIPT
```

Attachments arrive in `.files/<msg-ulid>/` under your node's directory.
For shell, browser, and Gemini nodes, the sender must be on `ALLOW`.

## Message help

Send a single line containing `help`, `/help`, `--help`, or `/?`. Matching is
not case-sensitive. The tool returns the `## Usage` section of its README.
The reply identifies the tool, node, and README path.

```bash
tell terminal help
ar3 tool browser --node browser-seat -- help
```

Help does not run a shell command, browser command, or Gemini request.
`help me open a page` is an ordinary message. The ACL check occurs before help,
so an unauthorized sender gets no reply. Vault help is plaintext and does not
require authentication.

## Browser profiles, recipes, and secrets

A browser node keeps its Chrome profile between messages. `a8s stop NODE`
closes Chrome and cancels operations that started before the stop. A new
operation can open it again. `TIMEOUT` closes an idle browser; a running
script is not interrupted by this idle check.

Store secrets through the local command, with a hidden prompt or stdin:

```bash
ar3 tool browser --node browser-seat secret set PASSWORD --origin https://app.example.com
ar3 tool browser --node browser-seat secret list
```

A script uses `fill-secret TARGET PASSWORD`. The field's origin must be
allowed. Messages cannot change or list the store. Values are kept out of
process arguments and redacted from text replies and snapshots. Screenshots,
video, and downloaded contents are not redacted. Read the browser README's
Secrets section for the complete limits.

Recipes provide site-specific commands:

```bash
ar3 tool browser --node browser-seat -- recipe
ar3 tool browser --node browser-seat -- recipe gb list --limit 3
```

Only bundled code and recipes under the registered browser node's root are
loaded by the public CLI. Messages cannot install code. See the
[recipe reference](../apps/tools/browser/recipes/README.md) for configuration
and development. The bundled `givebacks` recipe has the alias `gb`.

## Gemini profile setup

Create a browser node first. Open `https://gemini.google.com` in its profile.
Sign in manually in that Chrome window. Then register the Gemini node with
`BROWSER_SEAT` set to the browser node's name. The Gemini node needs the same
display, executable path, OS user, and data home. Follow the ordered setup in
the [Gemini reference](../apps/tools/gemini_web/README.md#register).

Each sender has a separate conversation under the same signed-in account.
Use `sessions` to list local records. Use `forget SENDER` to start that
sender's next prompt in a new conversation. These local commands do not need
`BROWSER_SEAT` and do not call Gemini.

## Access control

`command-shell`, `browser`, and `gemini-web` require `ALLOW` for routed
messages. Separate sender names with commas or spaces. Names are not
case-sensitive. If the variable is missing, a8s stops the wake before the
tool runs. Read `a8s logs NODE` for the error. A sender outside the list gets
no reply; the node logs the refusal.

Local sender identity comes from filesystem ownership. Remote identity
depends on the cluster secret. Permit only senders you trust with the node's
capabilities. The vault uses sender-specific vouchers instead of `ALLOW`.

## Write another tool node

Put the tool under `apps/tools/`. Use `apps/tools/_runtime.py` to read wakes,
check access, and send replies. Use `ar3.proc` for process control. A tool must
not import an a8s module. Its integration points are the definition's wake
arguments, the `a8s tell` CLI, and foundation interfaces such as `ar3.nodes`.

Resolve registered settings by the explicit node name. Do not infer identity
from the current directory or `A8S_TURN_RECIPIENT`.

Include these files and routes:

- A `tool-NAME.json` definition in `apps/a8s/definitions/`.
- The handler and tests under `apps/tools/`.
- Public `ar3 tool NAME` routes for every user operation and option.
- A README with Needs, Register, Usage, State, Platforms, and Trust sections.
- A link from this page to that README.

The README is the only source of message help:

1. Use the exact heading `## Usage`. Include command syntax, reply format,
   and a `### Options` table. The runtime reads up to the next `## ` heading.
2. Check the ACL before help. Before parsing or executing a message, call
   `rt.is_help(message)`.
3. For help, send `rt.help_reply(tool, node, readme, suite_path)` through
   `rt.send_reply`. Then return 0. Resolve `readme` beside the handler.
4. Put dynamic data, such as a key fingerprint, in `rt.help_reply`'s `extra`
   argument. Do not duplicate static usage in code.
5. Return 0 from a routed wake. Report operation failures in its reply.
   Local commands must use the appropriate failure exit code.

Write instructions with short, active sentences. Use one action per step and
one term per concept. Preserve exact command and option names. User operations
must not require a Python module path or repository working directory.
