# tool-browser

A persistent Chrome that other agents drive by message. Each message is a
script: one command per line. The node runs the script in its own Chrome
profile and replies with a transcript. The profile keeps its logins between
messages, so a page that needs a sign-in stays signed in. Snapshots,
screenshots, console logs, videos and downloads come back as attachments.

This page is the reference for the node. The tool overview is in
[docs/a8s-tools.md](../../../docs/a8s-tools.md).

## Needs

Chrome, Node, and the Playwright command line on the node's `PATH`:

```bash
npm install -g @playwright/cli
python3 apps/tools/browser doctor     # checks all three
```

A wake has no login profile. On Linux, put the Node bin directory (for
example the nvm one) on the node's path, and name the display for a headed
seat:

```bash
a8s vars garmin set env.PATH "$HOME/.nvm/versions/node/<version>/bin:/usr/local/bin:/usr/bin:/bin"
a8s vars garmin set env.DISPLAY :0
```

Chrome runs headed when `DISPLAY` is set on Linux, and headless otherwise.
With no display, your view of the page is `shot` and `snap`.

## Register

```bash
a8s add garmin ~/browsers/garmin tool-browser --ALLOW=iris
```

`ALLOW` is required. A sender outside it gets no reply.

## Usage

Drive this node's signed-in Chrome. Each message is a script: one command per
line.

On the node's machine, `ar3 tool` drives the node from one terminal. It reads
the node's vars from the a8s registry. You are the operator, so you pass
`ALLOW`:

```bash
ar3 tool browser --node garmin                          # a session
ar3 tool browser --node garmin -- go https://example.com   # one script
ar3 tool browser --node garmin - <<'EOF'                # one script from stdin
go https://example.com
snap
EOF
```

In a session, type one command per line. Each reply prints when the command
is done. Session rules:

- One command per line. A line that ends in `<<MARKER` opens a block that runs
  over several lines, up to the line that holds `MARKER`.
- A failed command prints its reply, and the session goes on.
- Ctrl-C stops the running command. Ctrl-D ends the session.
- `close` quits Chrome. The next command opens it again.
- Attachments print as `artifact: <path>`.
- `help` prints this text.
- `--node` reads the node's vars. `--var KEY=VALUE` overrides one for the run.

From another node, work in real time with two terminals. In the first, inside
the directory of your own node (the sender must be on `ALLOW`):

```bash
tells -f
```

In the second, send a script on stdin so the shell never touches it:

```bash
tell garmin - <<'EOF'
open
go https://example.com
snap
shot
EOF
```

The reply arrives in the first terminal. Attachments land under your node's
`.files/<msg-ulid>/` (`screen.png`, `snapshot.txt`, `console.log`, and so on).
`a8s convo <you> --json` shows the files of each message.

Rules of the script:

- The whole script is parsed first, then run. It stops at the first failure
  and attaches a `*-failure.txt` snapshot of the page it stopped on.
- Blank lines and lines that start with `#` are ignored.
- A line that ends in `<<MARKER` opens a block that runs to a line holding
  `MARKER`. The line carries only the command word. The block is the whole
  argument (used for `run-code` and for long text).
- Every command except `close` and `video-stop` opens Chrome if it is not open.
- A target is a `snap` ref such as `e5`, else a CSS selector, else visible text
  (case-insensitive). Zero matches, or several, is an error that lists the
  candidates. So send `snap` first, then `click e12`.
- The wake is capped at 600 seconds. The reply body is cut at 4000 characters.
  A session reply is not cut.
- One script runs on a seat at a time. A wake, a session and the browser CLI
  take the same seat lock for each script. A script that finds the seat busy
  waits up to 60 seconds. Then the reply is
  `<node>: busy: seat held by <who> since <when>. Nothing ran.` A session holds
  the seat for one command at a time, so between two session lines a wake can
  run. Check the URL.

Example: a sign-in form, one message at a time.

```bash
tell garmin - <<'EOF'
go https://site.example/login
snap
EOF
# read the refs in snapshot.txt, then:
tell garmin - <<'EOF'
fill e7 "me@example.com"
fill e9 "a-password"
click e11
wait-for-url /home 20
shot
EOF
```

### Commands

| Command | Syntax | Notes |
|---|---|---|
| `open` | `open` | Starts Chrome if needed. Returns the URL. |
| `close` | `close` | Saves state and quits Chrome. |
| `save` | `save` | Writes the profile's `state.json`. |
| `url` | `url` | The current URL. |
| `go` | `go <url>` | `http`, `https`, `file` and `about` only. |
| `click` | `click <ref\|selector\|text>` | |
| `fill` | `fill <label\|selector> <text>` | Quote text that has spaces. |
| `select` | `select <label\|selector> <value>` | |
| `press` | `press <key>` | `Enter`, `Tab`, `Escape`, and so on. |
| `type` | `type <text>` | The rest of the line, verbatim. |
| `check`, `uncheck`, `hover` | `<verb> <ref\|selector\|text>` | |
| `text` | `text <selector>` | Inner text of the first match. |
| `find` | `find <text>` | The rest of the line, verbatim. |
| `scroll` | `scroll <dy>` | Pixels. |
| `wait` | `wait <seconds>` | A number over 300 is read as milliseconds. At most 300 seconds. |
| `wait-for` | `wait-for <selector> [seconds]` | Waits for a visible match. Default 10. |
| `wait-for-url` | `wait-for-url <substring> [seconds]` | Default 10. |
| `assert-text` | `assert-text <text>` | The rest of the line, verbatim. Fails if the text is not on the page. |
| `assert-url` | `assert-url <substring>` | |
| `back`, `forward`, `reload` | no arguments | Return the URL. |
| `dialog-accept` | `dialog-accept [text]` | |
| `dialog-dismiss` | no arguments | |
| `requests` | no arguments | The network log. |
| `console` | no arguments | The console log, inline and attached as `console.log`. |
| `snap` | no arguments | Accessibility snapshot with refs. Attached as `snapshot.txt`. |
| `shot` | no arguments | Screenshot. Attached as `screen.png`. |
| `upload` | `upload <abs path>` | Answers a file chooser the page has opened. One file. |
| `drop` | `drop <target> <abs path>...` | Drops files onto an element. Prefer it to `upload`. |
| `download` | `download <target> [seconds]` | Clicks the target, waits for a finished file (default 30 seconds), attaches it. |
| `video-start` | `video-start [slug]` | One recording at a time. |
| `video-chapter` | `video-chapter <title>` | Needs a recording. |
| `video-stop` | `video-stop` | Attaches the `.webm`. |
| `eval` | `eval <js expression>` | Needs `ALLOW_EVAL=1`. |
| `run-code` | `run-code <<END` ... `END` | Playwright statements with `page` in scope. Needs `ALLOW_EVAL=1`. |
| `help` | `help` | The usage text. Also `/help`, `--help`, `/?`. The message must be that one line. Nothing else runs. |

`upload` and `drop` paths are paths on the node's machine and must be
absolute.

### Reply

The reply starts with `<node>: ok` or `<node>: failed: <reason>`. A blank line
follows, then one line per command: two spaces on success, `! ` on failure,
with the output indented under it.

```
garmin: ok

  open
      https://example.com/
  snap
      /home/you/.local/share/ar3/browser/garmin/artifacts/20261023T091500-snapshot.txt
```

If the seat is busy, the reply is one line and nothing ran:

```
garmin: busy: seat held by a8s wake from iris (pid 4242) since 2026-10-23 09:15:00 PDT. Nothing ran.
```

### Options

Set with `a8s add ... --KEY=VALUE` or `a8s vars <node> set KEY VALUE`.

| Var | Default | Meaning |
|---|---|---|
| `ALLOW` | none (required) | Sender names permitted to drive the seat. A sender outside it gets no reply. |
| `ALLOW_EVAL` | off | `1` permits `eval` and `run-code`. |
| `CHROME` | found on `PATH` | The Chrome binary. |
| `PORT` | Chrome picks a free one | A fixed Chrome debugging port. Unset, Chrome picks a free port and records it in the profile, so seats never collide. Set, the seat proves ownership through the process that holds the listening socket (`lsof`, or `ss` on Linux). It fails closed when another process holds the port, and when its own running Chrome recorded a different port. |

A wake attaches only to a Chrome that holds this seat's profile, on the port
that profile recorded. A listener on the recorded port that is not that Chrome
is never driven.

## State

The Chrome profile, artifacts, downloads and scratch files live in
`${XDG_DATA_HOME:-~/.local/share}/ar3/browser/<node>/`. The browser stays
running between wakes. `a8s stop <node>` closes it. The stop has 30 seconds:
the graceful close keeps to a shorter budget and then ends the process tree.

The `lock` file in the seat directory is the seat lock. While a script runs, it
holds the pid, the kind and the start time of the holder. The OS frees the lock
when the holder dies. `a8s stop` does not wait for the seat: it closes Chrome
even when a session holds it. `a8s stop` ends the script that holds the seat:
its next command fails with `seat closed by a8s stop`. Every path that opens Chrome checks
the stop epoch, read when the operation began and before any wait for the seat.
So an operation that began before the stop never opens Chrome after it, even
when it was waiting for the seat. An operation started after the stop opens
Chrome again. The `stop-epoch` file holds the token of the last stop.
The `launch-lock` file is a short lock around a Chrome launch and around the
stop. The stop waits at most 3 seconds for it, then closes anyway. The stop
always fits its 30 seconds.

The a8s daemon and the terminal must use the same `XDG_DATA_HOME`. If they
differ, they use different lock files and different profiles for the same node
name.

## Platforms

Linux and macOS only. Process discovery, which the attach check and the
forced cleanup need, is not implemented on Windows, so a seat does not open
there.

## Try it by hand

On the node's machine, see Usage: `ar3 tool browser --node <node>`.

For a seat that is not a node, the tool has its own CLI. It uses the same
profile, the same lock and the same Chrome:

```bash
python3 apps/tools/browser --seat scratch do - <<'EOF'
go https://example.com
snap
EOF
```

The other subcommands are `open`, `close`, `status`, `snap`, `console`,
`go <url>` and `doctor`.

## Trust

The profile holds a signed-in session. A sender on `ALLOW` acts as that person
on every site the profile has open. Allowlist few names. `eval` and `run-code`
run any code in the page or the driver. They stay refused until `ALLOW_EVAL`
is on, so leave it off unless the work needs it.
