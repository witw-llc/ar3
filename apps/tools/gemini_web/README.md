# tool-gemini-web

Gemini on the web, as an a8s seat. The node types each message into Gemini in
a signed-in browser seat and sends Gemini's answer back as a message. Each
sender has its own Gemini conversation, and the conversation continues across
messages. Files go in with the message. Images that Gemini makes come back as
attachments. An answer too long for the reply body comes back as the file
`gemini-reply.md`.

This page is the reference for the node. The tool overview is in
[docs/a8s-tools.md](../../../docs/a8s-tools.md).

## Needs

A [tool-browser](../browser/README.md) seat, with its needs. A person must sign
that seat's Chrome in to Gemini by hand first. This node never signs in.

The node runs the browser tool by its path in the suite. It needs the same
display and `PATH` setup as `tool-browser`. On Linux, a headed seat needs
`env.DISPLAY`, and the node needs the Node bin directory on `env.PATH`.

## Register

```bash
a8s add gemini ~/agents/gemini tool-gemini-web --ALLOW=iris --BROWSER_SEAT=gemini-profile
a8s vars gemini set env.DISPLAY :0
```

`ALLOW` and `BROWSER_SEAT` are required. A sender outside `ALLOW` gets no reply.

## Usage

Type each message into Gemini on the web and send its answer back.

Work in real time with two terminals. In the first, inside the directory of
your own node (the sender must be on `ALLOW`):

```bash
tells -f
```

In the second:

```bash
tell gemini "Draw a lighthouse at dusk"
tell gemini --attach notes.pdf "Summarize this file"
```

The answer arrives in the first terminal. Attachments land under your node's
`.files/<msg-ulid>/`. A turn can take up to 180 seconds.

### Commands

| Message | Meaning |
|---|---|
| `<text>` | The message goes to Gemini, in the sender's own conversation. Attached files go in with it. |
| `help` | The usage text. Also `/help`, `--help`, `/?`. The message must be that one line. Gemini is not asked. |

### Reply

The reply is Gemini's answer. A line after `--` in the reply is a note from the
seat, for example an image or a file it could not carry.

Every exit is 0, because a8s dead-letters a message on a non-zero exit and does
not retry. Two cases follow from this:

- A turn that cannot run answers `Your message was not sent. Send it again.`
  The seat is busy, the profile is not signed in, or the browser is not
  reachable.
- A reply that Gemini produced but `tell` could not deliver is held on disk and
  delivered on the next wake. The turn is not run again.

If the seat cannot tell whether Gemini received a message, it does not send it
again. The reply says so, and the sender checks the conversation.

### Options

Set with `a8s add ... --KEY=VALUE` or `a8s vars <node> set KEY VALUE`.

| Var | Default | Meaning |
|---|---|---|
| `ALLOW` | none (required) | Sender names permitted to ask. A sender outside it gets no reply. |
| `BROWSER_SEAT` | none (required) | The `tool-browser` node whose profile is signed in to Gemini. |
| `MODEL` | Gemini's default | A model as Gemini's own menu names it, picked when a conversation is created. |

## State

The conversation map and the held replies live in
`${XDG_DATA_HOME:-~/.local/share}/ar3/gemini-web/<node>/`. One turn runs at a
time per node. A turn is bounded at 180 seconds.

## Platforms

Linux and macOS only. The turn lock uses `fcntl`. On Windows the seat answers
every message "This seat runs on Linux and macOS only" and sends nothing to
Gemini.

## Try it by hand

On the node's machine, run one turn and print the reply. `--node` reads the
node's vars, `BROWSER_SEAT` included, and you pass `ALLOW` as the operator:

```bash
ar3 tool gemini-web --node gemini -- hello
ar3 tool gemini-web --node gemini -- help
```

The tool has its own CLI:

```bash
python3 apps/tools/gemini_web ask --browser-seat gemini-profile "hello"
python3 apps/tools/gemini_web sessions        # list the conversations
python3 apps/tools/gemini_web forget <sender> # the next message starts anew
```

## Trust

The Gemini account is the browser profile's account. Every sender on `ALLOW`
talks as that account. `a8s stop` closes the Chrome of the browser seat.
