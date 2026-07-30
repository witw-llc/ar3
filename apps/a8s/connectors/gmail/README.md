# a8s Gmail connector

The first non-agent participant in a8s. A connector is just an a8s agent
whose definition's `invoke` runs a script instead of an LLM CLI. This one
bridges a8s and a human's email inbox via the [GAS Bridge](https://github.com/neilobremski/gas).

## Strict opacity

Other agents `tell <name> "..."` with no awareness that the recipient is
a Gmail-backed human (vs. another LLM agent or another script). The
connector is the only thing that knows about Gmail — there is no
email-shaped wire format on the a8s side. Outbound, the connector
decides "subject is `$SENDER`, body is `$MESSAGE`". Inbound, the cron
strips Gmail's `Re:` / `Fwd:` prefixes and shells `tell <stripped> <body>`.

## What it does

- **Outbound**: when an a8s wake fires, `gmail_connector.py` POSTs
  `gmail.send` to the bridge with `subject=$SENDER` and `body=$MESSAGE`.
- **Inbound**: every `idle.timeout` seconds of quiet, a8s fires
  `gmail_cron.py --from <address>` from the agent's registered root.
  The script polls the bridge for `is:unread from:<address>`, strips
  `Re:`/`Fwd:` from each subject, and shells `tell <stripped-subject>
  <body>`. cwd is set by a8s, so the shelled `tell` force-stamps `from`
  to the connector's participant name.

There is no separate cron install — the schedule is the agent
definition's `idle.timeout`. No registry or definition reading happens
inside the cron itself; the from-address is dependency-injected via the
`idle.invoke` argv.

The connector talks HTTP directly to the bridge — no Python deps beyond
stdlib, and no dependency on the `gas` CLI.

## Setup

1. Configure the bridge env vars (the connector and cron both read them):

   ```
   export GAS_BRIDGE_URL=https://script.google.com/.../exec
   export GAS_BRIDGE_KEY=...
   ```

2. Copy the example definition and edit BOTH addresses (one in `--to`,
   one in `--from` — they're the same value, the recipient address
   replies will come back from):

   ```
   cp apps/a8s/connectors/gmail/example-definition.json ~/.<name>-gmail.json
   $EDITOR ~/.<name>-gmail.json
   ```

3. Register the connector as an a8s participant (name it after the
   human — if it routes to your inbox, name it after yourself):

   ```
   a8s add neil ~/bin/apps/a8s/connectors/gmail ~/.neil-gmail.json
   a8s start neil
   ```

4. Smoke-test outbound:

   ```
   tell neil "smoke test"
   ```

   An email arrives with subject = the agent enclosing your CWD and body
   = `"smoke test"`. Reply to the email; within `idle.timeout` seconds
   of quiet, the cron picks it up and routes back into a8s.

## How replies route

A reply email's subject is normally `Re: <original-subject>`. Since the
original subject was the sending agent's a8s name, stripping `Re:`
(repeated, case-insensitive, with optional whitespace) yields the agent
to route the reply to. Examples:

- `Re: NEIL` → `NEIL`
- `Re: Re: NEIL` → `NEIL`
- `RE:NEIL` → `NEIL`
- `Fwd: NEIL` → `NEIL`
- `re: fwd: NEIL` → `NEIL`
- `NEIL urgent` → left as `NEIL urgent`; `tell` rejects it (registry +
  canonical-name validation) and the email stays unread with a warning
  to stderr.

Unknown / malformed subjects are NOT marked read — fix the subject in
Gmail and the next idle tick picks them up.

## Limitations

- No attachments yet. `FILE:` payloads in a8s messages are stringified
  into the email body but no MIME attachment is created. (Cross-cluster
  files / attachment passthrough is tracked in #62.)
- No HTML email — body is plain text only.
- Polling, not push. Reply latency is bounded by `idle.timeout`.
- One Gmail account per connector instance. Multiple bridges = multiple
  registered participants pointing at separate definition files.
