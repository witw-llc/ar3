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
  and attaches a `*-failure.txt` snapshot of the page it stopped on. While a
  recipe's sensitive step is open, or after one whose page could not be
  confirmed clear, nothing of the page is captured.
- The tool opens no local file as a page. `go` refuses a `file:` URL, and a step that ends
  with a `file:` page in any tab of Chrome fails: the page the driver drives is sent to
  `about:blank`, any other such tab is closed, and the files that step wrote are removed.
- Blank lines and lines that start with `#` are ignored.
- A line that ends in `<<MARKER` opens a block that runs to a line holding
  `MARKER`. The line carries only the command word. The block is the whole
  argument (used for `run-code` and for long text).
- Every command except `close` and `video-stop` opens Chrome if it is not open.
- A target is a `snap` ref such as `e5`, else a CSS selector, else visible text
  (case-insensitive). Zero matches, or several, is an error that lists the
  candidates. So send `snap` first, then `click e12`.
- A secret never rides in a script. Name it: `fill-secret e9 PASSWORD`. The
  operator stores the value on the node's machine, with the origins it may be
  typed on, and the reply shows `[secret:PASSWORD]` where the value would be. A
  secret that is not stored fails the step. So does a field on an origin the
  secret does not allow, and nothing is typed. The failure says what the
  operator runs, or which origins are allowed.
- A site recipe is a website's named commands. `recipe` lists them, `recipe <site>`
  shows one with its verbs, `recipe <site> <verb> <arguments>` runs a verb, and
  `recipe <site> check` runs its health check. A verb that needs a sign-in stops
  with `needs a person: ...` when the seat is signed out. Listing and help open no
  Chrome. The README, Site recipes, has the rest.
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
# The operator stored PASSWORD once, with `browser secret set`.
fill e7 "me@example.com"
fill-secret e9 PASSWORD
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
| `save` | `save` | Writes the profile's storage state (cookies and the local storage of each site) to `state.json` in the seat directory, mode 0600. The reply is `saved`. No verb returns or attaches the file. |
| `url` | `url` | The current URL. |
| `go` | `go <url>` | `http`, `https` and `about` only. A `file:` URL is refused, in any spelling: the tool opens no local file as a page. |
| `click` | `click <ref\|selector\|text>` | |
| `fill` | `fill <label\|selector> <text>` | Quote text that has spaces. Refused, and nothing runs, when the text holds a stored secret. |
| `fill-secret` | `fill-secret <label\|selector> <NAME>` | Types the stored secret `NAME` into the field, if the field's page is on an origin the secret allows. The reply names the secret and never shows its value. |
| `select` | `select <label\|selector> <value>` | |
| `press` | `press <key>` | `Enter`, `Tab`, `Escape`, and so on. |
| `type` | `type <text>` | The rest of the line, verbatim. Refused, and nothing runs, when the text holds a stored secret. |
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
| `video-start` | `video-start [slug]` | One recording at a time. A recipe's sensitive step does not open while a recording is on. |
| `video-chapter` | `video-chapter <title>` | Needs a recording. |
| `video-stop` | `video-stop` | Attaches the `.webm`. |
| `eval` | `eval <js expression>` | Needs `ALLOW_EVAL=1`. Refused on a seat that holds secrets, whatever `ALLOW_EVAL` says. |
| `run-code` | `run-code <<END` ... `END` | Playwright statements with `page` in scope. Needs `ALLOW_EVAL=1`. Refused on a seat that holds secrets, whatever `ALLOW_EVAL` says. |
| `recipe` | `recipe [<site> [<verb> [<args>]]]` | A site recipe: no word lists the recipes, a site shows it, a verb runs it. `status` prints `in`, `out` or `unknown`. `check` runs the health check. |
| `help` | `help` | The usage text. Also `/help`, `--help`, `/?`. The message must be that one line. Nothing else runs. |

`upload` and `drop` paths are paths on the node's machine and must be
absolute. A path inside the browser state directory, which holds every seat's
home, is refused. A file the tool saved (a download, a screenshot) is in that
directory, so it cannot be uploaded from there.

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
| `ALLOW_EVAL` | off | `1` permits `eval` and `run-code`, except on a seat that holds secrets, which refuses both. |
| `CHROME` | found on `PATH` | The Chrome binary. |
| `PORT` | Chrome picks a free one | A fixed Chrome debugging port. Unset, Chrome picks a free port and records it in the profile, so seats never collide. Set, the seat proves ownership through the process that holds the listening socket (`lsof`, or `ss` on Linux). It fails closed when another process holds the port, and when its own running Chrome recorded a different port. |
| `TIMEOUT` | unset | Seconds with no script before Chrome closes. The profile keeps its logins, and the next script opens Chrome again. Unset, Chrome stays up between wakes until `a8s stop`. |

A wake attaches only to a Chrome that holds this seat's profile, on the port
that profile recorded. A listener on the recorded port that is not that Chrome
is never driven.

## State

The Chrome profile, artifacts, downloads, scratch files and secret store live in
`${XDG_DATA_HOME:-~/.local/share}/ar3/browser/<node>/`. That directory and its
`scratch` directory are mode 0700: every run makes them so, and tightens ones
made earlier. `state.json`, which `save` and `close` write, is mode 0600. The browser stays
running between wakes. `a8s stop <node>` closes it. The stop has 30 seconds:
the graceful close keeps to a shorter budget and then ends the process tree.

With `TIMEOUT` set, Chrome also closes when no script has used the seat for that
long. a8s runs `browser idle` for the node once the node has been quiet for
`TIMEOUT` seconds, and the verb checks the seat itself:

- It does nothing when no Chrome runs on the seat. It never starts one.
- It leaves Chrome up when a script holds the seat, when a launch is in
  progress, or when a script used the seat less than `TIMEOUT` seconds ago. The
  next check comes `TIMEOUT` seconds later.
- Otherwise it closes Chrome as `close` does, and prints how long the seat sat
  idle.

Only a script counts as use. A person who works in a headed window by hand,
with no script running, does not keep Chrome open. Set `TIMEOUT` well above the
longest pause between two scripts of one task: a Chrome that closes between them
comes back on a blank page. An idle close is not a stop: it leaves the stop
token alone, so a script that waited for the seat opens Chrome again and runs.
The `last-active` file in the seat directory holds the time a script last held
the seat, in seconds since the epoch.

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

The tool also reads the a8s registry, in the home a8s resolves (`A8S_HOME`, else the
XDG config home), to find the node its seat names and the root that node was
registered with. A run whose `A8S_HOME` differs from the daemon's finds no node and
loads the bundled recipes only.

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
`go <url>`, `idle --timeout <seconds>`, `secret set|list|rm` and `doctor`.

## Secrets

A script can sign in to a site while its sender never holds the password. The
operator stores the value on the node's machine, with the origins it may be typed
on. The script names it, and the tool types it.

```bash
printf '%s' "$PASSWORD" | python3 apps/tools/browser secret set PASSWORD --origin https://app.example.com --seat garmin
python3 apps/tools/browser secret set API_TOKEN --origin https://api.example.com --origin 'https://*.example.org' --seat garmin
python3 apps/tools/browser secret list --seat garmin    # each name with its origins, never a value
python3 apps/tools/browser secret rm API_TOKEN --seat garmin
```

The second command asks for the value at a hidden prompt. Then a script says
`fill-secret e9 PASSWORD`. The target resolves as it does for `fill`. The
transcript line shows `fill-secret e9 PASSWORD`, and the outcome shows
`e9 <- [secret:PASSWORD]`.

- A name starts with a capital letter and holds `A-Z`, `0-9` and `_`, 64
  characters at most. A value has at least 4 characters, because a shorter one
  cannot be scrubbed safely, and no NUL character.
- A secret needs at least one `--origin`. An origin is `scheme://host[:port]` with
  `http` or `https` and no path. A leading `*.` in the host
  (`https://*.example.org`) matches any subdomain and not the bare domain. A
  wildcard needs at least two labels after the `*.`: `https://*.example.com` and
  `https://*.co.uk` pass, and `https://*.com` is refused because it would match
  every site under `.com`. `https://*.localhost` is the one exception. The
  port is part of the origin, and the scheme's own port is the same as no port.
  `secret set` refuses anything else and says why. A later `secret set` of the
  same name replaces the value and the origins.
- `fill-secret` asks the target field for the origin of its own document, right
  before it types, so a field in a cross-origin iframe counts as the iframe's
  origin and not as the page around it. Outside the list the step fails, types
  nothing, and names the origin it saw and the origins allowed:
  `fill-secret: secret PASSWORD may not be typed on origin https://evil.example; allowed origins: https://app.example.com`.
  A frame with no origin of its own (`about:blank`, `srcdoc`, a sandboxed frame)
  reads as `null` and is refused, even on an allowed page.
- `secret set` reads the value from stdin when stdin is not a terminal, and drops
  one trailing newline. At a terminal it asks with a prompt that does not echo.
  No verb prints a value.
- The store is `secrets.json` in the seat directory, mode 0600:
  `{"version": 1, "secrets": {"NAME": {"value": "...", "origins": ["https://app.example.com"]}}}`.
  The tool refuses a store that group or other users can read, and the message
  gives the fix (`chmod 600 <path>`). A damaged store is an error that names the
  file and does not print its content. While the store cannot be read, no script
  runs on the seat, because the tool cannot scrub with a store it cannot read.
- The store is out of reach of a script. Only the verbs above, run on the node's
  machine, set, list or remove a secret, and a line such as `secret set ...` in a
  script is an unknown command.
- The tool opens no local file as a page. The page of another seat's store is a file, and the
  scrub of this seat does not know that seat's secrets. A screenshot is never scrubbed.
  Three layers hold the rule:
  - The tool takes `PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS` out of the
    environment of every call to `playwright-cli`. That switch makes the driver open
    local files, and the driver takes it from the call that starts it. So the driver
    the tool starts has none, whatever the node's environment says.
  - `go` refuses every `file:` URL, and says that local files are not opened.
  - A step that ends with a `file:` page in any tab fails, whatever brought the page
    there. So does a recipe's call that navigates to one. Chrome's DevTools endpoint
    gives the address of every tab, and the tool asks it after each step (a few
    milliseconds, no call to the driver; when it does not answer, as for a port the owner
    fixed, the tool asks the driver for the page it drives). The page the driver drives is
    sent to `about:blank`, any other such tab is closed, the files the step wrote (its page
    files and its attachments) are removed, and no text of the page leaves. A page that
    cannot be sent away or closed takes Chrome with it.
- `upload` and `drop` refuse a path that resolves inside the browser state directory
  (`${XDG_DATA_HOME:-~/.local/share}/ar3/browser`, which holds every seat's home). A
  symlink that leads in counts. They need no setting to read any file on the machine,
  which is why they refuse the directory.
- `eval` and `run-code` are off on a seat that holds a secret, whatever
  `ALLOW_EVAL` says: they could read a field a secret was typed into, change what
  they read before the scrub sees it, or open a URL of their own. The refusal says
  so and names the fix: remove the secrets (`browser secret rm`) or use a seat
  without them. The tool's own code is not the caller's and is not affected:
  `fill-secret`, the session, `wait-for`, `download`, and any reviewed recipe that
  uses the tool's Python API and not the verbs.
- Keep a secret out of node vars. They sit in plain text in the a8s registry and
  in shell history.
- Use `fill-secret` for a secret, never `fill` or `type`. Those put their text in
  the command line of `playwright-cli`, where `ps` shows it to every local user, so
  the tool refuses them when their text holds a stored value (see the rule below).

The rule: a stored secret, and a hidden value (a one-time passcode that a recipe read
from a page and hid with `page.hide`), never

1. appears in the command line of any process, because `ps` shows every local user the
   arguments of every process;
2. reaches a caller, the sender of the script or the person at `ar3 tool`: not in a
   step's output, a reply, an error, an attachment, a snapshot, console or network text,
   a recipe's result or `result.json`. A value shows there as `[secret:NAME]`.
   Screenshots, video and downloaded files are the exceptions, because the tool does not
   scrub them;
3. is readable by another OS user: the store is mode 0600, and it, the Chrome profile and
   the scratch directory are in the seat's directory, which is mode 0700.

Keeping a value out of a command line. The tool looks at every argument before it starts
`playwright-cli`. When one holds a stored or a hidden value, in any form the scrub knows,
`playwright-cli` is not started and the step fails with `playwright-cli was not run: an
argument of its command line held a value that must not be in one`. The message names no
value, no secret and no argument. A value can come into an argument with the text of a
script (`fill #user <value>`, `go https://site/<value>`), with the text of a recipe, or
from the page. A page can make an id, a class or an address from a value it was given: a
user name typed into a field becomes the id of the next button, and naming that button by
its id would put the name in the command line. So the tool does not use a selector that
holds a value. It names the element by its path of tag names and positions (`body >
div:nth-of-type(2) > button`), which holds nothing the page wrote, or it fails with `no
selector for that element is free of a value that must not be in a command line` and asks
for a `snap` ref. What a script typed to name a target (`click Continue`, `text #message`,
`assert-text some words`, `wait-for #go`) goes to the page in a private file, so it is in
no command line. The selector that comes back is, and it holds no value. A short stored
value, such as a four-digit PIN, refuses any text that holds it (`fill #amount 12345` for
the PIN `1234`). What the tool wrote itself (its paths, ports, window sizes and its own
code) is not looked at, so a value that is also a year or a word of a path stops none of the
tool's own calls. A snapshot ref is not looked at where a verb takes the element it acts on
(`click e1234`, `fill e1234 text`, `select e1234 one`, `hover`, `check`, `uncheck`, `drop`), because the
number of a ref says nothing: a page of a thousand elements has an `e1234`, and a stored PIN
of 1234 must not stop the click on it. The same text anywhere else is the caller's text, and
it is refused when it holds a value: `type e482916` and `fill #user e482916` put the PIN
482916 in a command line, so they are refused for it. The reply of a wake goes to `a8s tell`
on stdin, so it is in no command line either.

What the scrub knows. It finds a value one character at a time, so it finds it in any mix
of the ways a page, a browser or Playwright writes it: as typed; with the percent escapes
of a URL and `+` for a space, in either case of hex (a URL keeps `@` and `!` and escapes a
space, and the part before `?` is not written as the part after); with the escapes of a
JSON, JavaScript, YAML or Python string; with the escapes of CSS (`CSS.escape` escapes a
digit at the start of an identifier and not in the middle, so the id `pw-2Fast4U\!` holds
the value `2Fast4U!`); with the entities of markup; with a line feed as U+21B5 and a tab as
U+21C6, which is how the preview of an element in the call log of Playwright shows them; and with
its whitespace folded as a snapshot shows it. It also finds the start of a value, 4 characters or more, that a tool
cut and ended with an ellipsis (`…` or three dots, with dots of the text after them, and a
value of its own that ends or begins in dots), also when the cut is in the middle of the escape of
a character: Playwright cuts the text of an element at 50 characters and the attributes of an
element at 500, in the call log of an error.

It finds a value under more than one layer of these, too. A page that prints
`JSON.stringify({user: value})` in an element, and a snapshot that quotes the text of the
element in the double quotes of YAML, show the value `Synthetic"User\A7492` as
`Synthetic\\\"User\\\\A7492`, and a caller can decode that back. So the scrub undoes
escapes as well. No escape touches a letter or a digit, so it looks for the longest runs of
them in the value, and where they show in the right order, with an escape near them, it takes
the text around them and undoes the escapes of a string (JSON, JavaScript, YAML, Python, with
the folded lines of YAML that end with a backslash), the doubled apostrophe of a YAML string in
single quotes, the escapes of CSS, of a URL (percent escapes, `%uXXXX` of JavaScript's `escape`,
and `+` for a space) and of markup, one family after another, in any order, up to three times.
For a value with a character from U+0080 to U+00FF, the bytes of a URL are read as ISO-8859-1 as well as
UTF-8 (`%DF%A0` is one character to a reader of UTF-8 and the two characters that JavaScript's `escape`
wrote), and for one with a control character from U+0080 to U+009F, a numeric reference to it is read
as a browser reads it (`&#133;` is an ellipsis) and as the code that wrote it meant it (U+0085).
A value that has a space, or a line break, or a character that CSS escapes with a hex number (which
ends with a space), gets the lines of YAML too: a writer (PyYAML) breaks a long line of a plain
string, or of one in single quotes, at a space and indents the next line, and a reader folds the
break and the indent back into one space (and each empty line after the first break into a line
break). A line break of a value is read with the spaces after it, because a block (`|`) indents
each line. A string in single quotes that is folded and has apostrophes doubled is one layer, so it
is one step. The pattern of the value
then reads each result, so a value is found under four layers. What it replaces is the stretch
of the text that the value was made into: the quotes and braces around it stay. A script can
hide a value (`page.hide`) and have it found the same way. A value of letters and digits alone
is the same text in every layer but one (`CSS.escape` writes a first digit as `\32 `), so one that
begins with a letter is found as it is, and one that begins with a digit is looked for by its CSS form.
A value that has a character over U+007F is looked for where a text has, for the first six of
them that have the longest numbers, what any escape of the character holds: the hex digits of
its code (`\u0418`, `&#x418;`, `\418 `, `%u0418`), the decimal ones (`&#1048;`), the first and the last byte of its UTF-8
(`%D0%98`), or its name in markup (`&Icy;`), whatever the layers around them, in the order of the
characters and not farther apart than they can be. A page of numbers and escapes with no such
value in it makes no place, and costs next to nothing.
The proof is a table made by code, in `apps/tools/tests/browser/test_layers.py`: 18 encoders (JSON
and YAML strings with and without non-ASCII text escaped, in the styles of Playwright and of
PyYAML, the lines that PyYAML folds in double quotes, in single quotes and plain, YAML in single
quotes, URLs, JavaScript's `escape`, markup and CSS), every ordered composition of up to three of
them over values with quotes, backslashes, spaces, controls, non-ASCII text and an emoji, and the
scrub of each is exactly the text with the value made its marker. The writers of the lines are
ports of the emitter of PyYAML 6.0.1, which a test compares with PyYAML where it is installed. A page that prints a long run of
backslashes before the value does not hide it either: a window begins just after the last line
end that is not escaped (within 1,024 characters), and where there is none it reads the first run
of backslashes both ways. Two values that overlap in a text, or a value that overlaps itself, are one
stretch. A download that a page names after a value is saved under a name that holds none, because
the reply is sent with the path in a command line; an extension (a dot and up to eight letters or
digits) is kept when the scrub of the name leaves it as it was.

Files the browser and the driver write on their own. They are Chrome's profile and its
storage; `state.json`, the export that `save` and `close` make of the cookies and the
local storage of the sites; and the page files and the console log that `playwright-cli`
keeps under `scratch/.playwright-cli/` (the sweep reads every file there). They are inside
the same boundary as the store and the profile. They are in the 0700 directory, the driver
runs with a umask that makes what it writes mode 0600, `state.json` is mode 0600, and no
verb attaches, returns or uploads one: `save` replies `saved`, and `upload`, `drop` and a
recipe's `artifact` refuse the directory. The driver's files are also scrubbed before any
path can return one to a caller. Every script and every command of
the CLI begins by scrubbing the files the driver has written since the last time it did,
and a step scrubs the files it changed as it ends. A file that cannot be scrubbed is
removed.

How the value reaches the page: `fill-secret` writes a small code file that holds
the value and the secret's origins, mode 0600, in a directory of its own under the
seat's scratch directory, and runs `playwright-cli --raw run-code --filename
<file>`. The command line holds the path and not the value, so `ps` shows nothing
to other users. `playwright-cli` sends its daemon the path and keeps no copy of the
code, and `--raw` keeps the code out of its reply. The tool removes the file when
the call returns.

Scrubbing: the tool reads the store once per script and replaces every stored
value with `[secret:NAME]`, longest first. It does so in each step's output, in
every error (see below), in the reply body, in the snapshot files that `snap` and a failed script attach, and in
the `text`, `find`, `url`, `console` (inline and attached) and `requests` results. It
also does so in the files `playwright-cli` writes for itself under the scratch
directory: the page snapshot of a click holds the value of every field, and the daemon
appends to its console log after a call has returned, so the next script
begins by scrubbing them. The `snap`,
`console`, `go` and `status` verbs of the CLI print through the same scrub. It replaces the
value in each way listed under What the scrub knows, and a text is scrubbed before it is cut
for a message, because a value that is cut in two is not found.

An error of the driver is not scrubbed and shown whole on a seat that holds a secret, or after a
script hid a value. Playwright prints a preview of every element it names in the call log of an
error, in the list of a strict-mode violation and in the line that says what stops a click, with the
text of the element cut at 50 characters and its attributes at 500, and the address of the page cut
at 200 in its lines about a navigation. A value that a page wrote under two layers of escapes (`JSON.stringify` of
its own `JSON.stringify`) and that Playwright cut in two shows its start in such a preview, and the
scrub finds a cut value in one layer and a whole value in several, and neither is this. Some errors
name an element in their first line too (a selector that resolved to the wrong element). So the error is
cut by structure, at the one place where the text of the driver comes in: no line of the driver stays,
not even a short one. The caller sees the kind of the error, read from a fixed list and never copied, and
a note: `TimeoutError: the driver's message is withheld on a seat that holds secrets; the failure
snapshot is attached` (or `Error: ...`). The caller's own line is in the transcript, and the failure
snapshot, which the scrub reads whole, is attached as before. The tool's own messages about output it did not expect (a result
that is not JSON, a screenshot that wrote nothing) quote 200 characters of the text on a seat with no
secret, and on a seat that holds one they say that the text is withheld. On a seat that holds no
secret an error is whole, as the driver wrote it.

Chrome does not offer to save a typed password, and does not run its breach check
on it. Before each launch the tool sets two profile preferences to false:
`credentials_enable_service`, the "Offer to save passwords" switch, and
`profile.password_manager_leak_detection`, the "Warn you if passwords are exposed
in a data breach" switch. Chrome keeps filling passwords it already holds. The
settings take effect when Chrome next starts. If a person turns a switch on, the
next launch turns it off again.

Limits:

- A screenshot and a video are pixels, and the tool does not scrub them. A text
  field shows the secret in them. A password field shows dots.
- A downloaded file is not scrubbed. Its name is: see What the scrub knows. A recipe's
  `artifact` scrubs text; bytes that are not UTF-8 text (a NUL byte is none) and hold a value are
  refused, also when the value is in them as ISO-8859-1, Windows-1252, UTF-16 or UTF-32 (UTF-16 and
  UTF-32 for data of at most 16 MB; they are read from the first byte). A file in another charset (Windows-1251, Shift-JIS, GBK) with a
  value of that script in it is not recognized, and neither is UTF-16 text that has no ASCII
  character in it (it has no NUL byte, and is UTF-8 to the check).
- The origin check and the fill are two calls to the browser, a few milliseconds
  apart. A page that moved the field's frame to another origin in exactly that
  window could have the value typed into the new document.
- `playwright-cli` writes the page snapshot of a click or a `go` before the tool
  scrubs it, and its daemon appends to its console log after a call has
  returned. Until the step ends, or until the next script or CLI command begins, those
  files hold the value typed into any field on the page and whatever the page logged
  about it. The seat's directories are mode 0700, so only the node's OS user can open
  them.
- A site that keeps a value in its own storage keeps it in the seat's profile and in
  `state.json`, readable by the OS user only. `save` and `close` write that copy. The tool
  does not scrub it, because it holds the cookies that keep the seat signed in, and nothing
  returns it.
- A short stored value refuses any text of a script that holds it, so a four-digit PIN
  refuses `fill #amount 12345`. A sender who may run scripts can also find a short value by
  trying guesses: a line that holds it is refused, and the line is echoed with the value
  scrubbed. Store values that cannot be guessed. The floor of 4 characters is the shortest
  value the scrub can handle, not a safe length.
- A value that a script hid is known to that script only. The driver's console and network
  buffers outlive a script, so `console` and `requests` in a later script show what the
  page logged, scrubbed with the stored secrets and not with the earlier script's hidden
  value.
- Chrome's DevTools port listens on the loopback and has no login. Any process on the
  machine, of any user, that finds the port can drive the seat's Chrome as the node's OS
  user: read a field or the cookies, or open the store as a page. The modes 0600 and 0700
  keep other users out of the files, not out of the browser. Run a seat on a machine whose
  local users you trust.
- The eval guard looks at the seat's own store. On another seat of the same
  machine that holds no secret, `ALLOW_EVAL` still lets `run-code` call
  `page.goto("file:///...")` and return what it reads, in the same step, from any
  file the OS user can read, including this seat's `secrets.json`. The step that ends
  on a `file:` page fails, but code that goes back to another page before it returns
  is not caught. Keep `ALLOW_EVAL` off on a machine that holds secrets.
- A `playwright-cli` config file can also let the driver open local files, and so
  can a driver started by hand with the switch. The check at the end of each step
  covers both: a step that ends on a `file:` page fails. Such a file (`~/.playwright/cli.config.json`
  of the node's OS user) can also move the driver's output out of `scratch/.playwright-cli`,
  where the sweep looks. The tool removes the environment variables that do so; it cannot
  remove a file.
- A value under more than four layers of escapes is not found. A letter or a digit that is
  written as an escape (`&#83;` or `%53` or `\u0053` for `S`) is not found either: no
  encoder writes one on its own, and the scrub looks for the letters and digits of a value
  to know where to look. A value that a tool cut and ended with an ellipsis is found in
  one layer, not in more.
- PyYAML's folded lines are read in three layers, not in four: the fourth is read by the pattern of
  the value, which does not know them.
- The undoing of layers costs time that grows with the text around the places where a value
  can be. A place needs an escape near it (or a line end, for a value that a fold can be in), so a
  text with none costs nothing; a value whose letters and digits come in runs of one or two
  characters (`a!b@c#d$`) has a place nearly everywhere in a text that is full of escapes. How far
  from its first run a value can reach is counted in the characters of the text it can take: one for
  a letter or a digit, which no encoder escapes, and 512 for any other character (a key of a
  thousand characters, most of them letters, reaches a few thousand characters of a text, not half
  a million). A text that costs more than `secretstore.WORK_LIMIT` (48 million characters of
  decoding, two or three seconds, counted for a pass over each text, a call for each match of a family
  whether it stands for anything or not, and a search of each text; the small windows of a text are
  decoded first) has the places it did not get to scrubbed whole: from the first run of letters and
  digits of the value that is looked for to the farthest place in reach where its last run is, and with
  what the other characters of the value can take around them. The first run that is looked for is the
  one at the start of the value, or the first of three letters or more, and the last is the one at its
  end, or the last of three letters or more; a third is the longest of the others. A value that begins
  with a letter and ends with a letter or a digit has no more than that stretch, and the farthest copy of
  its last run in reach ends it; one that begins with a character that is not a letter can take 512
  characters for each such character before it, and one that ends with one, after it. Copies that are
  close together in such a text are scrubbed as one stretch, and the text between them goes. A value with a character over
  U+007F and no letter or digit of ASCII (text in another script) has its places where a text has
  what an escape of its characters holds, as above; on a page in the same script that is
  everywhere, and the cost is that of a text full of escapes. A value of punctuation only has no run
  and no number to look for by: it is looked for in a window of its reach (512 characters for each
  of its characters) on either side of each escape, joined up to 64 KB, and on a snapshot of 370 KB
  that is all escapes, most of it is scrubbed whole.
- A value that a tool cut is looked for in the window before each ellipsis: 520 characters for a
  value of 43 characters or fewer (the tool cuts at 500, and the rest is for escapes), and 12
  characters of text for each character of a longer value (a 600-character value is looked for in
  7,200). A cut that shows more of a value than its window is not found. A value over 4,096
  characters has a cut for each of its pieces, so a cut is found in any of them. A tool's ellipsis
  costs a scan of the window for each value, and a run of dots is scanned once for all the places
  the ellipsis can begin at, which are as many as the most dots in a row in a stored value. With
  more than 64 ellipses in a text, the places where a value can begin are found in one pass for
  each value, and a value whose start is nowhere in the text costs nothing for an ellipsis. Every
  place in the window where a value can begin is tried, the farthest first, and none before the
  ellipses that the value cannot hold: the text that a tool shows of a value has in it the ellipses
  the value has and no others, so a cut value does not begin before the run of dots or the
  ellipsis that comes that many runs before its own (and a value that begins with dots can begin in
  the end of the run before). The search is bounded: `secretstore.CUT_WORK_LIMIT` is 1.5 million
  steps, about a second and a half (a step is a character of the text walked, or a hundred
  characters of a window looked at). A text that repeats the start of a value many times, or a value
  that repeats itself, can spend it, and then the window before each ellipsis that is left is
  scrubbed whole. A cut value is
  looked for in the text as it is, so that a password cut after a user name that is a stored value
  of its own is scrubbed as the password. A cut in the middle of a character over U+FFFF (a tool that
  cuts by the length of a JavaScript string cuts an emoji in two) is found.
- A value over 4,096 characters is found by its whole forms (JSON, YAML, CSS, URL, plus, markup)
  and by patterns of pieces of it a character at a time, side by side, so that a text which has it
  in the mix shows one marker for each piece. A value (or a tidied copy of it) over 16,384
  characters has its whole forms and no more: a pattern of a piece costs a tenth of a second for
  each thousand characters to make, and every `hide` makes them again.
- A value that cannot be searched for in a text (the compile of its patterns raises) is refused by
  `hide` with an error that names no part of it, and the scrubber is as it was. A value that a script
  hides is at most 4,096 characters: it is a passcode or a token, and every hide makes the patterns
  of all the values again.
- A page that shows a changed copy of a value (masked, reformatted, hashed, in base64) shows
  the copy. The scrub and the check of a command line know the value in the forms
  above, not in a copy the page made, so a selector made of the copy can be in a
  command line.
- After a sensitive step whose cleanup could not confirm that the page is clear, nothing
  of the page is captured for the rest of that script. The next script starts with
  capture on. When Chrome could not be closed either, the failure names the fix, `browser
  close --seat <seat>`: run it before the seat takes another script.
- Any process that runs as the node's OS user can read `secrets.json`. The mode
  keeps out other users.

## Site recipes

A site recipe gives a website named commands, with this tool as the engine behind
them. A person or an agent then uses the site as an API that runs in a signed-in
browser:

```bash
ar3 tool browser --node garmin -- recipe shop list --limit 3
ar3 tool browser --node garmin -- recipe shop read weekly newsletter
```

`recipe` is a line of the command language, so it works in a message to the node, by
hand through `ar3 tool`, and in a longer script beside plain steps. A recipe run is
part of the script that holds the seat. It takes the seat lock, ends when `a8s stop`
runs, counts as use for `TIMEOUT`, and returns its transcript and files as any script
does. The transcript shows the `recipe` line and, indented under it, each page call
the recipe made, with every stored value replaced by `[secret:NAME]`.

| Line | What it does |
|---|---|
| `recipe` | One line per recipe: name, aliases and title, then the markers `[node]`, `[replaces bundled]` and `[broken: <error>]`. |
| `recipe <site>`, `recipe <site> help` | Title, origins, node settings, secret names, and each verb with its kind, whether it needs sign-in, its usage and an example. `recipe <site> help <verb>` shows one verb. |
| `recipe <site> <verb> [arguments]` | Runs the verb. A site goes by its name or an alias. |
| `recipe <site> status` | Prints `in`, `out` or `unknown`. |
| `recipe <site> check` | Runs the status probe, then each `read` verb that declares an example (never `login`), and prints `healthy` or `failing` for each. The step fails when one fails. |

Listing and help open no Chrome. Running a verb, `status` and `check` open it, as a plain step does.

**Arguments** are words, split as for the other commands, and each verb's arguments are
checked against its declaration: a bad one fails with the verb's usage. A verb may take
the rest of the line as one argument, so `recipe shop read weekly newsletter` needs no
quotes. `ar3 tool` joins its words with spaces, so the quotes you type are gone before
the tool reads the line. Send a title with an apostrophe in it through `ar3 tool
browser --node <node> -` and a heredoc, where the quotes stay.

**Results.** A verb returns text, data, files or a mix. The text is the step's output.
Data prints as compact JSON, cut at 2000 characters, and is attached whole as
`result.json`. Files come back as attachments, as a `shot` does. A wake's reply is cut
at 4000 characters, and `ar3 tool` prints it whole.

**Failures** are of four kinds, and each says what to do next:

- Usage: an unknown recipe, verb or argument. The reply is the verb's usage.
- Needs a person: the site is signed out, wants a code, or shows a consent page. The
  reply reads `needs a person: <reason>. <how>` and names the seat to sign in on.
- A step failed: the reply names the step and the error, and attaches the failure
  snapshot, as for any script.
- A sensitive step failed: a recipe that handles a value it must not show, such as a
  one-time passcode read from a mail, keeps that part in a sensitive step. The reply
  says `<recipe> <verb>: the <name> step failed` and nothing the page or the driver
  said, and attaches no failure snapshot. The step ends with a check that the browser
  holds nothing of the value. If the recipe cannot confirm that, the tool closes Chrome
  before it replies, and the reply says so. The next script opens Chrome again.

`ar3 tool` exits 1 when the script failed or the seat was busy and nothing ran, and 0
otherwise, so an alias works in a shell script. A wake exits 0 either way, because a8s
dead-letters a wake that exits with another code.

**Sign-in.** The seat's signed-in profile is the credential. A recipe has a status probe
that answers `in`, `out` or `unknown` and changes nothing on the site. The probe runs
before each verb that needs sign-in. `out` or `unknown` stops the verb with `needs a
person`, and the recipe never signs in on a guess. A recipe may have a `login` verb that
types stored secrets with `fill-secret` (see Secrets). It runs without the probe, and
only a message or an operator calls it.

**Origins.** A recipe lists the origins it may use, in the syntax of `browser secret
set --origin`. Its page calls refuse a URL outside them before any request is made, and
a page that a link or a redirect took outside them fails the verb. An in-page `fetch` follows no
redirect: a redirect fails the call, because the browser would request the new address before the
origins could be checked.

**Where recipes live.** The bundled recipes are in `apps/tools/browser/recipes/`. They
are general drivers with no personal values, reviewed like any code. A node also loads
the files in `recipes/` in its registered root, the directory `a8s add` was given. A
symlink to another directory works. The tool finds the node in the a8s registry by the
seat's name, and only when that node runs the `tool-browser` definition. The working
directory and the environment decide nothing, so a recipe in the directory a run
happens to stand in is never loaded, and neither is one in the root of a node of
another kind. A seat that names no such node loads the bundled recipes only. `ar3 tool`
with no `--node` drives a seat named `browser`, and `browser do` drives the seat it is
given. `browser do` takes `--recipes DIR` to add a directory by hand. A node's recipe
with a bundled recipe's name replaces it, and the listing says so. A file that fails to
load, or whose `recipe` is missing or named differently from the file, is listed as
`[broken: <error>]` and stops nothing else. A file whose name starts with `_` is not
loaded as a recipe.

**Who may run them.** A recipe is code that acts with the seat's signed-in identity. So a
message can only name a recipe. It can not install, change or send one. Give a browser
node a directory of its own, and let only the operator write to its `recipes/`. Every
sender on `ALLOW` may run the node's recipes, as it may run any script, and the help
marks each `write` verb. `ALLOW_EVAL` gates code that a caller sends. A recipe's own
in-page code is reviewed code: it needs no opt-in, and it is not checked against the
origins.

The bundled `givebacks` recipe, alias `gb`, reads an organization's GiveBacks newsletters: `login`,
`list`, `open`, `pull` and `screenshot`. Its settings, its secrets and an alias for a shell are in
[recipes/README.md](recipes/README.md#givebacks).

[recipes/README.md](recipes/README.md) is the guide to writing a recipe.

## Trust

The profile holds a signed-in session, and Chrome's DevTools port has no login (see the limits
under Secrets): a seat belongs on a machine whose local users you trust.

The profile holds a signed-in session. A sender on `ALLOW` acts as that person
on every site the profile has open, and can have a stored secret typed into any
field on an origin that secret allows. Allowlist few names. `eval` and `run-code`
run any code in the page or the driver. They stay refused until `ALLOW_EVAL`
is on, and on a seat that holds secrets they stay refused whatever it says. Leave
`ALLOW_EVAL` off unless the work needs it.
