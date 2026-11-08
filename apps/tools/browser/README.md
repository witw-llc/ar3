# tool-browser

Run browser commands in a saved Chrome profile. Each message is a script with
one command per line. The tool returns a transcript and attachments. The
profile keeps sign-ins between messages. Attachments can include snapshots,
screenshots, console logs, videos, and downloads.

Use `ar3 tool browser` from a terminal. Use `tell` from another node.
See [Tool nodes](../../../docs/a8s-tools.md).

## Needs

Use Linux or macOS. Install Chrome and Node. Install the Playwright CLI at the version the tool is proven against:

```bash
npm install -g @playwright/cli@0.1.22
ar3 tool browser doctor
```

`doctor` notes a different installed version. The tool still runs.

The tool needs these executables on `PATH`. A routed wake does not load your
shell startup files. If Node is in a version-manager directory, add that
directory to the node's `env.PATH` after registration.

On Linux, Chrome shows a window when `DISPLAY` is set. Without `DISPLAY`, it
runs without a window. Use `shot` or `snap` to inspect the page in that mode.
A person needs a visible window for manual sign-in. Set `env.XAUTHORITY` if
your desktop requires it.

## Register

```bash
a8s add browser-seat ~/browsers/browser-seat tool-browser --ALLOW=assistant
```

On Linux, set the display to your desktop display:

```bash
a8s vars browser-seat set env.DISPLAY :0
```

If Node is absent from the wake's path, set its complete path. Replace
`<version>` with the installed Node version:

```bash
a8s vars browser-seat set env.PATH "$HOME/.nvm/versions/node/<version>/bin:/usr/local/bin:/usr/bin:/bin"
```

Check the settings and open a site:

```bash
ar3 tool browser --node browser-seat doctor
ar3 tool browser --node browser-seat go https://example.com
```

For a site that requires sign-in, open its sign-in page with `go`. Sign in in
that Chrome window. The tool uses its own profile, separate from your normal
Chrome profile.

Start the node to receive messages:

```bash
a8s start browser-seat
```

`ALLOW` is required for routed messages. Other senders get no reply.

## Usage

Run commands in this node's Chrome profile. Put one command on each line.

On the node's machine:

```bash
ar3 tool browser --node browser-seat
ar3 tool browser --node browser-seat -- go https://example.com
ar3 tool browser --node browser-seat - <<'EOF_SCRIPT'
go https://example.com
snap
EOF_SCRIPT
```

The first command starts an interactive session. The second runs one script.
The third reads one script from stdin. `--node` loads the node's settings and
directory. The local operator passes `ALLOW` unless `--var ALLOW=...` overrides
that setting.

Session rules:

- Enter one command per line. Each reply prints when the command ends.
- End a command line with `<<MARKER` to begin a block. End the block with a
  line that contains only `MARKER`.
- A failed command does not end the session.
- Press Ctrl-C to stop the current command. Press Ctrl-D to end the session.
- `close` closes Chrome. A later browser command can open it again.
- Attached files appear as `artifact: <path>`.
- `help` returns this text without a browser operation.
- Use `--var KEY=VALUE` before a command to override one setting for that run.

Store passwords locally with a hidden prompt:

```bash
ar3 tool browser --node browser-seat secret set PASSWORD --origin https://app.example.com
ar3 tool browser --node browser-seat secret list
```

A script uses `fill-secret <target> PASSWORD`. Messages cannot set, list, or
remove secrets. See Secrets for stdin input and origin limits.

From another node, open two terminals in that node's directory. Run this in
the first terminal:

```bash
tells -f
```

Send a script from the second terminal:

```bash
tell browser-seat - <<'EOF_SCRIPT'
go https://example.com
snap
shot
EOF_SCRIPT
```

The sender must be on `ALLOW`. Replies appear in the first terminal.
Attachments go to `.files/<msg-ulid>/` in the sender's directory.
Use `a8s convo <you> --json` to find a message's files.

Script rules:

- The tool parses the complete script before it runs. It stops at the first
  failed step. It attaches a `*-failure.txt` snapshot unless a sensitive step
  prevents capture.
- The tool refuses local `file:` pages. If a step ends with such a page, it
  replaces the active page with `about:blank` and closes other local-file tabs.
  It also removes the files that step created.
- The tool ignores blank lines and lines that start with `#`.
- A block header contains the command word and `<<MARKER`. The complete block
  is that command's argument. Use it for long text or `run-code`.
- Browser commands open Chrome as needed. `close` and `video-stop` do not.
  Recipe listing and help do not open Chrome.
- A target can be a snapshot reference such as `e5`, a CSS selector, or
  visible text. Visible text is not case-sensitive. No match or multiple
  matches cause an error. Use `snap` before a command such as `click e12`.
- Use `fill-secret` to enter a stored secret. The field's origin must be on
  the secret's allowed list. A missing secret or refused origin stops the
  step before it types a value.
- Use `recipe` to list site recipes. Use `recipe SITE` for help and
  `recipe SITE VERB ARGUMENTS` to run a verb. `recipe SITE check` checks the
  recipe. A required sign-in that is absent returns `needs a person`.
- A wake has a 600-second limit. Its reply body has a 4000-character limit.
  Local session replies have no such character limit.
- Only one script can use a seat at a time. A busy request waits up to 60
  seconds, then returns `busy` without running. A session releases the lock
  after each command. Another request can change the page between commands.

Example sign-in:

```bash
tell browser-seat - <<'EOF_SCRIPT'
go https://site.example/login
snap
EOF_SCRIPT
```

Read the snapshot references before the next script. Store `PASSWORD` for
`https://site.example` on the node's machine first.

```bash
tell browser-seat - <<'EOF_SCRIPT'
fill e7 "user@example.com"
fill-secret e9 PASSWORD
click e11
wait-for-url /home 20
shot
EOF_SCRIPT
```

### Commands

Use `tabs` to find a tab index. Indices start at zero: `shot 1` captures the
second tab. `tab 1` and `shot 1` keep that tab selected for later commands.
Bare `shot` brings the selected tab forward and captures it. The `*` marker
shows the tab used for browser commands. Run `tabs` again after tabs open or close.
An invalid index or extra argument fails the command.

| Command | Syntax | Function |
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
| `type` | `type <text>` | The rest of the line, without changes. Refused, and nothing runs, when the text holds a stored secret. |
| `check`, `uncheck`, `hover` | `<verb> <ref\|selector\|text>` | |
| `text` | `text <selector>` | Inner text of the first match. |
| `find` | `find <text>` | The rest of the line, without changes. |
| `scroll` | `scroll <dy>` | Pixels. |
| `resize` | `resize <width> <height>` | Sets the viewport size in pixels (1 to 9999). |
| `wait` | `wait <seconds>` | A number over 300 is read as milliseconds. At most 300 seconds. |
| `wait-for` | `wait-for <selector> [seconds]` | Waits for a visible match. Default 10. |
| `wait-for-url` | `wait-for-url <substring> [seconds]` | Default 10. |
| `assert-text` | `assert-text <text>` | The rest of the line, without changes. Fails if the text is not on the page. |
| `assert-url` | `assert-url <substring>` | |
| `back`, `forward`, `reload` | no arguments | Return the URL. |
| `dialog-accept` | `dialog-accept [text]` | |
| `dialog-dismiss` | no arguments | |
| `requests` | no arguments | The network log. |
| `console` | no arguments | The console log, inline and attached as `console.log`. |
| `snap` | no arguments | Accessibility snapshot with refs. Attached as `snapshot.txt`. |
| `tabs` | no arguments | Lists zero-based tab indices and URLs. `*` marks the selected tab. |
| `tab` | `tab N` | Selects tab N and returns its URL. |
| `shot` | `shot [N]` | Selects tab N if given, then attaches a screenshot as `screen.png`. |
| `upload` | `upload <abs path>` | Answers a file chooser the page has opened. One file. |
| `drop` | `drop <target> <abs path>...` | Drops files onto an element. Prefer it to `upload`. |
| `download` | `download <target> [seconds]` | Clicks the target, waits for a finished file (default 30 seconds), attaches it. |
| `video-start` | `video-start [slug]` | One recording at a time. A recipe's sensitive step does not open while a recording is on. |
| `video-chapter` | `video-chapter <title>` | Needs a recording. |
| `video-stop` | `video-stop` | Attaches the `.webm`. |
| `eval` | `eval <js expression>` | Needs `ALLOW_EVAL=1`. Refused on a seat that holds secrets, even with `ALLOW_EVAL=1`. |
| `run-code` | `run-code <<END` ... `END` | Playwright statements with `page` in scope. Needs `ALLOW_EVAL=1`. Refused on a seat that holds secrets, even with `ALLOW_EVAL=1`. |
| `recipe` | `recipe [<site> [<verb> [<args>]]]` | A site recipe: No site lists recipes. A site name shows help. A verb name runs that verb. `status` prints `in`, `out` or `unknown`. `check` runs the health check. |
| `help` | `help` | Returns this usage text. Also `/help`, `--help`, `/?`. The message must be that one line. Nothing otherwise runs. |

`upload` and `drop` paths are paths on the node's machine and must be
absolute. A path inside the browser state directory, which holds every seat's
home, is refused. A file the tool saved (a download, a screenshot) is in that
directory, so it cannot be uploaded from there.

### Reply

A reply starts with `<node>: ok` or `<node>: failed: <reason>`. Each command
has a transcript line. Successful lines start with two spaces. Failed lines
start with `! `. The output is indented under the command.

```text
browser-seat: ok

  open
      https://example.com/
  snap
      /home/you/.local/share/ar3/browser/browser-seat/artifacts/20261023T091500-snapshot.txt
```

A busy reply identifies the lock holder and its start time. It ends with
`Nothing ran.`

### Options

Set an option with `a8s add ... --KEY=VALUE` or
`a8s vars <node> set KEY VALUE`. Use `--var KEY=VALUE` before a local command
for a change that applies to one run.

| Variable | Default | Function |
|---|---|---|
| `ALLOW` | Required | Lists permitted sender names. Separate names with commas or spaces. Names are not case-sensitive. Other senders get no reply. |
| `ALLOW_EVAL` | Off | Set to `1` to permit `eval` and `run-code`. A seat with stored secrets refuses both commands. |
| `CHROME` | Found on `PATH` | Selects the Chrome executable. |
| `CDP` | Unset | Attaches to a running Chrome over the Chrome DevTools Protocol instead of launching one. Accepts a channel name (`chrome`, `chrome-beta`, `chrome-dev`, `chrome-canary`), a profile directory or `DevToolsActivePort` path, a localhost port, or a `ws://` / `http://` endpoint. Cannot be combined with `PROFILE`. |
| `PROFILE` | Seat profile | Uses an existing Chrome `--user-data-dir` path instead of `<seat_home>/chrome`. Leaves existing `Preferences` password settings unchanged and refuses to start if a running Chrome already holds that profile. Cannot be combined with `CDP`. |
| `TIMEOUT` | Unset | Sets the idle time in seconds before Chrome closes (or detaches in `CDP` mode). The next script opens or attaches again. Without this setting, the session stays open until a stop. |

```bash
a8s vars browser-seat set TIMEOUT 900
ar3 tool browser --node browser-seat --var TIMEOUT=900 idle
```

### Attaching to a running Chrome or existing profile

To attach a seat to a running Chrome instance on the same machine, open
`chrome://inspect/#remote-debugging` in that Chrome, enable remote debugging for
that browser instance, and set `CDP`:

```bash
a8s vars browser-seat set CDP chrome
ar3 tool browser --seat scratch --cdp chrome status
```

To attach over an SSH local port forward from another host, forward the remote
debugging port or WebSocket endpoint to `127.0.0.1` and set `CDP` to that
`ws://127.0.0.1:<port>/devtools/browser/<id>` or `http://127.0.0.1:<port>` URL.
In `CDP` mode, `close`, `a8s stop`, and `idle` detach the seat's driver daemon
without quitting or signaling the attached Chrome process.

To launch Chrome on a separate existing `--user-data-dir` directory when Chrome
is not already running on it, set `PROFILE` (or pass `--profile PATH`). Chrome
136 and later refuse `--remote-debugging-pipe` on Chrome's default user data
directory; use `CDP=chrome` to attach to the default profile instead.

## Local commands

Put `--node NAME` before the local verb to use a registered browser node.
Use `--seat NAME` for an unregistered local seat. Do not combine them.

```bash
ar3 tool browser --help
ar3 tool browser --node browser-seat status
ar3 tool browser --seat scratch go https://example.com
ar3 tool browser --seat scratch do script.txt --json
ar3 tool browser --seat scratch do --help
```

| Local verb | Arguments | Function |
|---|---|---|
| `doctor` | None | Checks Chrome, Node, and Playwright CLI. |
| `open` | None | Opens Chrome with the selected profile. |
| `close` | None | Saves state and closes Chrome. |
| `status` | None | Reports the browser connection and URL. |
| `go` | `URL` | Opens a page. |
| `snap` | None | Prints the accessibility snapshot. |
| `console` | None | Prints the console log. |
| `do` | `FILE` or `-`, `[--json]` | Runs a script from a file or stdin. `--json` prints structured output. |
| `idle` | `[--timeout SECONDS]` | Uses configured `TIMEOUT`, or `--timeout SECONDS` to override it. Closes an idle browser without starting Chrome. |
| `secret` | `set`, `list`, or `rm` | Manages local secrets. See Secrets. |

Use `COMMAND --help` for that verb's options. Browser commands that use Chrome
accept `--chrome PATH`, `--cdp TARGET`, and `--profile DIR` before or after the
verb. Put `--` before a script to use the message path explicitly. For example,
`-- go URL` returns a message-style transcript; `go URL` uses the local browser
command.

To run an unregistered seat with a visible Linux window, set `DISPLAY` in the
terminal's environment first. `--seat` does not load registered settings.
Keep the same seat name to reuse its profile:

```bash
export DISPLAY=:0
ar3 tool browser --seat scratch do - <<'EOF_SCRIPT'
go https://example.com
snap
EOF_SCRIPT
```

## State

The seat directory is
`${XDG_DATA_HOME:-~/.local/share}/ar3/browser/<seat>/`. It contains the Chrome
profile, artifacts, downloads, scratch files, and secret store. The seat and
scratch directories have mode 0700. `state.json` has mode 0600. The tool sets
these permissions on each run.

The daemon and your terminal must use the same `XDG_DATA_HOME`. Otherwise the
same seat name selects different profiles and lock files. Use the same
`A8S_HOME` to load the same registered node and private recipes.

Chrome stays open between messages. `a8s stop <node>` closes it within 30
seconds. The tool first attempts a normal close, then ends the process tree.
A stop also invalidates operations that started before it. Their next command
fails with `seat closed by a8s stop`. An operation that starts after the stop
can open Chrome again.

`TIMEOUT` closes Chrome after a period without scripts. Manual work in a
visible window does not count as script activity. Set the timeout above the
longest pause in your task. After an idle close, Chrome opens on a blank page.
An idle check leaves Chrome open during a script or launch. It checks again
after the configured interval. Unlike a stop, an idle close does not cancel
scripts that wait for the seat.

The `lock` file identifies the script holder. The OS releases its lock if the
holder exits. `last-active` records the last script activity time. `stop-epoch`
records the stop token. `launch-lock` protects startup and stop; a stop waits
at most 3 seconds for that lock.

Second-user isolation is experimental. Native checks with two OS accounts
remain to be run on Linux and macOS. Do not treat it as a verified security
boundary.

Chrome uses `--remote-debugging-pipe`. It opens no DevTools network port.
The driver socket is in the private `browser/ipc/` directory. If that path is
too long, the tool uses `ar3-browser-<uid>/<digest>` under `XDG_RUNTIME_DIR` or
the system temporary directory. It refuses a directory owned by another user.
The tool accepts a Chrome process only when it uses this profile and belongs
to this seat's driver daemon.

## Platforms

The browser tool supports Linux and macOS. Windows lacks the required process
discovery and verified driver-pipe access controls. The tool refuses to open
a seat on Windows before it starts the driver or browser.

## Secrets

Store a value on the node's machine. Give it a name and allowed origins.
A script supplies the name to `fill-secret`; it does not receive the value.

```bash
ar3 tool browser --node browser-seat secret set PASSWORD --origin https://app.example.com
ar3 tool browser --node browser-seat secret set API_TOKEN --origin https://api.example.com --origin 'https://*.example.org'
ar3 tool browser --node browser-seat secret set API_TOKEN -
ar3 tool browser --node browser-seat secret list
ar3 tool browser --node browser-seat secret rm API_TOKEN
```

At a terminal, `set` uses a hidden prompt. With redirected stdin, it reads
stdin. An optional `-` after the name makes this input source explicit. For
example, a password manager can pipe its output to:

```bash
ar3 tool browser --node browser-seat secret set PASSWORD - --origin https://app.example.com
```

Never put a secret value in an argument or node variable. To manage an
unregistered seat, use `--seat scratch` instead of `--node browser-seat`.
Secret commands require an explicit seat selector. They do not start Chrome
or require Playwright CLI. No message or script can call them.

### Names, values, and origins

- A name starts with an uppercase letter. It contains only `A-Z`, `0-9`, and
  `_`, with at most 64 characters.
- A value must be UTF-8, have at least 4 characters, and contain no NUL. Its
  maximum size is 64 KiB. Input removes one trailing newline.
- A new name requires at least one `--origin`. To replace the value and keep
  its origins, omit `--origin`. To replace the allowed list, supply new origins.
- An origin has the form `http://host[:port]` or `https://host[:port]`, without
  a path. A default port is equivalent to an omitted port.
- `https://*.example.org` permits subdomains but not `example.org`. A wildcard
  requires at least two labels after `*.`. `*.com` is refused. `*.localhost`
  is the exception. The tool accepts `*.co.uk`; choose an appropriately narrow
  host rather than a public suffix.
- `fill-secret` checks the field's document origin immediately before typing.
  For an iframe, this is the iframe origin. A `null` origin is refused,
  including `about:blank`, `srcdoc`, and sandboxed frames.

The transcript shows `fill-secret e9 PASSWORD`. Its result shows
`e9 <- [secret:PASSWORD]`. `list` prints names and origins, never values.
An invalid value stores nothing. Secret commands return 0 on success, 1 on
refusal or storage failure, and 2 for invalid command-line syntax. An error
does not repeat a value supplied as an argument.

### Storage and access

`secrets.json` has mode 0600 in the private seat directory. Its format is:

```json
{"version":1,"secrets":{"NAME":{"value":"...","origins":["https://app.example.com"]}}}
```

The tool refuses a store that other users can read. Correct its mode with
`chmod 600 PATH`. A damaged store stops scripts without printing its content.
The tool cannot redact values until it can read the complete store.

The tool refuses `file:` pages. `upload` and `drop` refuse every path inside
the browser state directory, including symlinks into it. A downloaded file
inside that directory cannot be uploaded directly from there. These commands
can read other files accessible to the node's OS user.

`eval` and `run-code` are refused when the seat stores a secret. `ALLOW_EVAL`
does not override this rule. Reviewed recipes use the tool's own API and can
still run. Keep `ALLOW_EVAL` off on other seats of the same OS user: code on a
seat without secrets could read another seat's files before the local-file
page check runs.

### Command-line and output protection

Stored secrets and values hidden by a recipe are checked before the tool
starts Playwright CLI. If a caller-supplied argument contains such a value,
the tool refuses the command without printing that argument. Use `fill-secret`
instead of `fill` or `type` for a secret.

Targets go to the browser through a private file. If the page returns a
selector that contains a secret, the tool uses element tag names and
positions instead. If it cannot make a safe selector, it asks for a snapshot
reference. A target reference such as `e1234` is not secret text when used as
a reference. The same text in a value argument is checked.

The tool writes secret-fill code to a mode-0600 file under private scratch
storage. Playwright CLI receives its path with `--raw run-code --filename`.
It does not receive the value in its command line. The tool removes the file
after the call. Routed replies also use stdin, not command-line arguments.

The tool replaces protected values with `[secret:NAME]` in transcripts,
errors, text output, URLs, snapshots, console and network logs, and recipe
results. It redacts text before truncation. It also checks filenames and
removes protected values from attachment names. A safe extension of up to
eight letters or digits can remain.

The driver creates its own snapshots and console files. The tool redacts
changed files at the end of a step and scans driver files again before the
next script or local command. It removes a file it cannot redact. These files
remain in private directories while the scan is pending.

On a seat with protected values, driver errors show a fixed error type and
an explanation that the driver message is withheld. They do not repeat any
driver error line. A permitted failure snapshot remains available after
redaction. On a seat without protected values, the full driver error is shown.

Chrome's password-save offer and password breach check are disabled before
each launch. Existing saved-password autofill remains enabled. Manual changes
to those two switches are replaced at the next launch.

### Redaction forms and bounds

The redactor recognizes these representations:

| Representation | Coverage |
|---|---|
| Plain text | Complete values, with the longest values checked first. |
| URL | Percent escapes, `+` for spaces, mixed-case hex, and JavaScript `%uXXXX`. |
| String formats | JSON, JavaScript, YAML, and Python escapes. Includes YAML doubled apostrophes and folded lines. |
| CSS | Escaped characters and identifiers, including a leading escaped digit. |
| Markup | Named and numeric entities. |
| Whitespace | Snapshot whitespace folding and Playwright line-feed/tab preview characters. |
| Nested formats | Up to four layers. YAML line folding is recognized in the first three layers. |
| Truncated text | A value prefix of at least four characters followed by an ellipsis, in one encoding layer. |

URL decoding checks UTF-8 and ISO-8859-1 where needed. Numeric markup control
references are checked both as browser mappings and as their original code
points. Overlapping matches are redacted together. The test matrix is in
`apps/tools/tests/browser/test_layers.py`.

The nested-format search uses a 48-million-character work limit. If it
reaches that limit, it redacts complete candidate regions instead of leaving
them visible. Punctuation-only values can cause large regions to disappear.
The truncated-value search has a 1.5-million-step work limit. On exhaustion,
it redacts the remaining candidate windows before ellipses.

Truncated-value windows span 520 characters for values up to 43 characters,
and 12 characters per value character for longer values. A prefix outside
that window can be missed. Values above 4096 characters are checked in pieces
as well as complete forms. Values above 16384 characters use complete forms
only. Hidden recipe values have a 4096-character limit. A value whose search
patterns cannot be built is refused without revealing it.

### Limits

- Screenshots and videos are not redacted. A visible text field can show the
  value. Do not capture a page that displays a secret.
- Downloaded file contents are not redacted. Their filenames are checked.
- A recipe's `artifact` redacts UTF-8 text. It refuses recognized binary data
  that contains a protected value. Checks include ISO-8859-1 and Windows-1252.
  UTF-16 and UTF-32 checks start at the first byte and cover files up to 16 MB.
  Other encodings, such as Windows-1251, Shift-JIS, and GBK, can be missed.
  UTF-16 with no ASCII characters can also be misidentified.
- The origin check and fill are separate browser calls. A frame that changes
  origin between them could receive the value at its new origin.
- Driver files can contain values before the next redaction pass. Only the
  node's OS user can read the private directories.
- Chrome storage and `state.json` can retain values. The tool does not redact
  them because they preserve sign-ins. No command returns or attaches them.
- A short secret can block unrelated text that contains it. A sender can
  also test guesses through these refusals. Four characters is a technical
  minimum, not a recommended secret strength. Use values that resist guessing.
- A recipe's hidden value exists only for that script. A later script cannot
  redact that value from old console or network buffers unless it is stored.
- A modified Playwright config can move driver output outside the scanned
  scratch directory. The tool removes relevant environment overrides but
  cannot remove the user's `~/.playwright/cli.config.json` file.
- More than four encoding layers are not recognized. Encoded letters or
  digits, such as `%53` for `S`, can evade detection. Truncated values under
  multiple encoding layers can also be missed.
- Masked, reformatted, hashed, or base64 copies are not recognized as the
  original value. Such a copy can appear in output or a selector argument.
- After uncertain sensitive-step cleanup, capture stays off for that script.
  The next script enables capture again. If Chrome could not be closed,
  run `ar3 tool browser --node browser-seat close` before another script.
- Any process under the node's OS user can read the store. File modes restrict
  other users, not processes with the same user identity.

## Site recipes

A recipe provides named commands for one site. It runs in the seat's browser
profile. Use the explicit message separator before recipe commands:

```bash
ar3 tool browser --node browser-seat -- recipe
ar3 tool browser --node browser-seat -- recipe shop
ar3 tool browser --node browser-seat -- recipe shop list --limit 3
```

`shop` is an example recipe. The suite includes `givebacks`, alias `gb`.
See [Recipe reference](recipes/README.md) for its settings and commands.

| Script command | Function |
|---|---|
| `recipe` | Lists names, aliases, titles, and load status. Marks private, replacement, and broken recipes. |
| `recipe SITE` or `recipe SITE help` | Shows origins, settings, secret names, verbs, and examples. |
| `recipe SITE help VERB` | Shows one verb's help. |
| `recipe SITE VERB [ARGUMENTS]` | Runs a verb by recipe name or alias. |
| `recipe SITE status` | Returns `in`, `out`, or `unknown`. |
| `recipe SITE check` | Runs the status probe and examples for read verbs. Does not run `login`. Reports each result. |

Listing and help do not open Chrome. Other recipe operations share the seat
lock, stop checks, timeout activity, transcript, and attachments with scripts.
Arguments are checked before browser work. To preserve quotes in a title,
use stdin with a quoted shell heredoc.

A recipe can return text, structured data, and files. Data appears as compact
JSON, limited to 2000 characters, and as a complete `result.json` attachment.
A routed reply has a 4000-character limit. Local output is complete.

An invalid argument returns usage. A missing sign-in or required manual
step returns `needs a person: <reason>. <action>`. Other failures return the
error and a failure snapshot. A sensitive step returns a fixed failure
message without page contents or a snapshot. It must confirm that the browser
no longer holds the sensitive value. If cleanup cannot confirm this, the
tool closes Chrome before it replies.

The sign-in probe returns `in`, `out`, or `unknown` without changing the site.
A verb that requires sign-in runs only for `in`. A recipe can provide a
`login` verb that uses stored secrets. Only an explicit request runs it.

A recipe declares permitted origins. Direct page and HTTP requests check
them before the request. Navigation caused by a click or server redirect is
checked after navigation. In-page `fetch` does not follow redirects. A recipe's
reviewed in-page code is not subject to these origin checks.

Bundled recipes live in `apps/tools/browser/recipes/`. Private recipes live
in `recipes/` under the registered browser node's root. A symlink to another
directory is allowed. The tool resolves that root from the seat name through
the a8s registry. It never loads recipes from an arbitrary current directory.
A node of another type does not supply browser recipes.

Without a matching browser registration, a seat loads bundled recipes only.
The public CLI does not accept an arbitrary recipe-directory override. A private recipe replaces a bundled recipe with the
same name. A broken file is listed without stopping other recipes. Files
whose names start with `_` are ignored.

Only the operator should write recipe files. Messages can run recipes but
cannot install or change them. Every sender on `ALLOW` can run their read and
write verbs. `ALLOW_EVAL` controls caller-supplied code; it does not disable
reviewed recipe code.

Local script commands return 1 on failure or a busy seat, and 0 on success.
Routed wakes return 0 even for script failures. Read the reply for the result.

## Errors

| Condition | Action |
|---|---|
| Missing browser requirement | Run `ar3 tool browser --node browser-seat doctor`. |
| Browser opens without a window | On Linux, check `env.DISPLAY` and any required `env.XAUTHORITY`. |
| Wrong profile or private recipes absent | Check the selected node, `A8S_HOME`, and `XDG_DATA_HOME`. |
| Seat busy | Read the lock holder in the reply. Let that script finish before a new request. |
| Missing or refused secret | Store its value locally and check its allowed origins. |
| Target has several matches | Run `snap`. Use a specific reference from that snapshot. |
| `seat closed by a8s stop` | Start a new script only when another browser operation is intended. |

## Trust

A sender on `ALLOW` acts as the account signed in to the profile. In `CDP` or
`PROFILE` mode, that includes every active login and saved credential in the
attached browser or profile, and any local process that can reach a forwarded
CDP port can connect to that browser without `ALLOW` or `fill-secret` checks.
A sender on `ALLOW` can also enter a stored secret into a field on any origin
allowed for that secret. Keep the sender list and origin lists narrow. Leave
`ALLOW_EVAL` off unless the task requires it. Use a separate OS user to
restrict filesystem access.
