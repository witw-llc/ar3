# tool-gemini-web

Send a message to Gemini through a browser profile. The tool returns Gemini's
answer. Each sender has a separate conversation. Later messages from that
sender use the same conversation. You can attach files to a message. The tool
returns generated images as attachments. It puts long answers in
`gemini-reply.md`.

Use `ar3 tool gemini-web` for local commands. Use `tell` to send a message from
another node. See [Tool nodes](../../../docs/a8s-tools.md).

## Needs

Use Linux or macOS. Install the requirements for
[tool-browser](../browser/README.md#needs). The Gemini node needs access to the
same browser profile, display, and executable path as the browser node.

A person must sign in to Gemini in that browser profile before the first
message. The tool does not sign in. A sign-in in your normal Chrome profile
does not sign in the tool's profile.

## Register

1. Register a browser node:

   ```bash
   a8s add gemini-profile ~/browsers/gemini-profile tool-browser --ALLOW=assistant
   ```

2. On Linux, set the display for the browser node:

   ```bash
   a8s vars gemini-profile set env.DISPLAY :0
   ```

   Use the display of your desktop. Set `env.XAUTHORITY` if your display needs
   it. See the browser requirements for `env.PATH`.

3. Check the browser requirements:

   ```bash
   ar3 tool browser --node gemini-profile doctor
   ```

4. Open Gemini in the tool's profile:

   ```bash
   ar3 tool browser --node gemini-profile go https://gemini.google.com
   ```

5. Sign in in that Chrome window. Confirm that you can enter a prompt.

6. Register the Gemini node:

   ```bash
   a8s add gemini ~/agents/gemini tool-gemini-web --ALLOW=assistant --BROWSER_SEAT=gemini-profile
   ```

7. On Linux, give the Gemini node the same display settings:

   ```bash
   a8s vars gemini set env.DISPLAY :0
   ```

   Set the same `env.PATH` and `env.XAUTHORITY` when needed. The Gemini node
   starts the browser tool itself. It needs these settings on its own node.

8. Start the Gemini node to receive messages:

   ```bash
   a8s start gemini
   ```

`BROWSER_SEAT` is the browser node name, `gemini-profile` in this example. It is
not a path or the Gemini node name. Keep both nodes under the same OS user and
use the same `XDG_DATA_HOME`. Otherwise they do not use the same profile.

## Usage

Send a prompt to Gemini and receive its answer.

On the node's machine:

```bash
ar3 tool gemini-web --node gemini -- "Summarize the purpose of a unit test."
ar3 tool gemini-web --node gemini --attach notes.pdf -- "Summarize this file."
ar3 tool gemini-web --node gemini -- help
```

Put `--` before the prompt. It makes every following word part of the message.
For example, `-- sessions` sends the word `sessions` to Gemini. Without `--`,
`sessions` is a local command that lists conversations.

From another node, open two terminals in that node's directory. Run this in
the first terminal:

```bash
tells -f
```

Send the message from the second terminal:

```bash
tell gemini "Draw a lighthouse at dusk"
tell gemini --attach notes.pdf "Summarize this file"
```

The sender must be on `ALLOW`. Replies appear in the first terminal.
Attachments go to `.files/<msg-ulid>/` in the sender's directory.
A turn can take up to 180 seconds.

### Commands

| Message | Result |
|---|---|
| `<text>` | Sends the text and attached files to the sender's conversation. |
| `help` | Returns this usage text without a Gemini request. Also accepts `/help`, `--help`, and `/?`. The message must contain only that one line. |

### Reply

The reply contains Gemini's answer. Text after a line of `--` is a tool note.
For example, the note can identify an image that the tool could not attach.

A turn that cannot start returns `Your message was not sent. Send it again.`
The browser can be busy, signed out, or unavailable. Correct the problem
before you send the message again.

If the tool cannot confirm that Gemini received the prompt, it does not send
the prompt again. Read the reply and check the conversation.

If Gemini answered but the tool could not send the reply, the tool saves it.
It tries to deliver that reply on the next wake. It does not repeat the Gemini
request. A wake exits with code 0. Read the reply for the turn result.

### Options

Set an option at registration with `a8s add ... --KEY=VALUE`. To change a
registered option, use `a8s vars <node> set KEY VALUE`. Use
`--var KEY=VALUE` before the local command for a change that applies to one run.

| Variable | Default | Function |
|---|---|---|
| `ALLOW` | Required | Lists permitted sender names. Separate names with commas or spaces. Names are not case-sensitive. Other senders get no reply. |
| `BROWSER_SEAT` | Required | Names the browser node with the signed-in Gemini profile. |
| `MODEL` | Gemini's default | Selects a model when the tool creates a conversation. Use the label from Gemini's model menu. |
| `TIMEOUT` | Unset | Sets the browser idle time in seconds. Chrome closes after this time without a script. The next turn opens it. Without this setting, Chrome stays open until a stop. |

```bash
a8s vars gemini set BROWSER_SEAT gemini-profile
a8s vars gemini set TIMEOUT 900
```

The `BROWSER_SEAT` option is required for prompts. The local `sessions` and
`forget` commands do not need it.

## Local commands

Run these commands on the node's machine:

```bash
ar3 tool gemini-web --help
ar3 tool gemini-web --node gemini sessions
ar3 tool gemini-web --node gemini forget operator
ar3 tool gemini-web --node gemini forget --help
```

`sessions` lists sender names, conversation URLs, and creation times. `forget`
removes the local conversation record for the named sender. The sender's next
prompt starts a new conversation. It does not delete the conversation from
the Gemini website. Local prompts use sender `operator` unless you set
`--from NAME` before `--`.

To use a local seat without registration:

```bash
ar3 tool gemini-web --seat gemini-test --var BROWSER_SEAT=gemini-profile -- "Hello"
ar3 tool gemini-web --seat gemini-test sessions
```

Use `--seat` for a local state name. Use `--node` to load a registered node's
settings. Do not combine them. Local administrative commands are not message
commands. A routed message that says `forget operator` is a Gemini prompt.

## State

The tool stores conversation records and pending replies in
`${XDG_DATA_HOME:-~/.local/share}/ar3/gemini-web/<node>/`. It runs one turn at a
time per node. Each turn has a 180-second limit.

## Platforms

The tool supports Linux and macOS. Its turn lock requires `fcntl`. On Windows,
it replies `This seat runs on Linux and macOS only` and sends nothing to Gemini.

## Errors

| Condition | Action |
|---|---|
| Missing `ALLOW` or `BROWSER_SEAT` | Set the variable with `a8s vars gemini set KEY VALUE`. |
| Browser requirements missing | Run `ar3 tool browser --node gemini-profile doctor`. |
| Profile signed out | Open Gemini with the browser command in Register. Sign in in that window. |
| Browser busy | Let the current script finish. Then send a new request if the reply says the prompt was not sent. |
| Wrong conversation | Run `sessions`. Use `forget SENDER` to start that sender's next prompt in a new conversation. |
| No reply to a routed request | Check `ALLOW` and `a8s logs gemini`. |

## Trust

Every sender on `ALLOW` uses the Gemini account in the browser profile. Separate
conversations do not create separate accounts. `a8s stop gemini` closes the
browser named by `BROWSER_SEAT`. A configured `TIMEOUT` also closes that browser
when it is idle.
