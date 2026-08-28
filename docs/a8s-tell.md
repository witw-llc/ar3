# tell — internals

Operator documentation for how `tell` works under the hood. Agent-facing usage
lives in [`skills/tell/SKILL.md`](../apps/a8s/skills/tell/SKILL.md) (send-only). Desktop /
filedrop setup: [a8s-filedrop.md](a8s-filedrop.md).

Bodies ride in on stdin — `tell <name> - <<'EOF' … EOF` with the delimiter
quoted, or `tell <name> - < body.md`. That is what every teaching surface shows,
because the shell never touches the text: `$`, backticks, and backslashes land
byte-exact. The trailing-argv form covers short plain bodies; inside double
quotes the shell expands `$…` and runs backticks before `tell` is reached.

## Surface

| Entry | Path |
|-------|------|
| Operator shim | `tell` at the repo root → `a8s tell` (or `source install.sh` / `get.sh`) |
| System install | `AR3_SYSTEM=1` `get.sh` → `/usr/local/bin/tell` → `/usr/local/lib/ar3/tell` |
| Implementation | `apps/a8s/tell.py` (`tell_main`) |
| Router | `apps/a8s/mailbox.py` (`route_outboxes`) |
| Receive-side | `apps/a8s/tells.py` (`tells_main`) — see below |

## Send path (async)

0. **`tell --check`** — optional self-test: verifies the resolved outbox is writable (creates the path when missing). A recipient name validates registry routing when the resolved outbox is a registered one; on a staging outbox it reports `not checked` rather than guessing. No envelope written.
1. **`TELL_OUTBOX_DIR` or CWD filedrop** — tell writes to the env path when set;
   otherwise may resolve a unique configured outbox from CWD when the registry
   is reachable (see [a8s-filedrop.md](a8s-filedrop.md)). System installs for
   agent users without a readable registry always need the env var.
2. Build message body (argv, stdin, or `-`); parse trailing `FILE:` lines via `mailbox._split_content_and_files`. `--attach` / `--file` append to the same `files` array (`--attach=PATH` and multiple paths after one flag are supported). Oversized sources fail immediately unless `--split` chunks them under `TELL_FILE_MAX` / `max_file_bytes`. Allocate `msg_id`, copy each file into `<outbox>/<msg_id>/<basename>`, then write `<outbox>/<msg_id>.json` with **filename-only** `files` entries (no `path` field).
3. Optionally read the a8s state root (default `~/.config/a8s`) to stamp `from` when CWD sits inside a registered agent root, and to validate the recipient — see [Who validates the recipient](#who-validates-the-recipient). Validation runs before any file is staged, and an abort between staging and the envelope write removes the partial `<outbox>/<msg_id>/` bundle — the outbox never keeps a bundle without its `.json`.

Envelope shape:

```json
{
  "id": "01J…",
  "date": "<iso8601 Z>",
  "to": "<recipient>",
  "content": "...",
  "files": [{"filename": "avatar.jpg"}],
  "from": "<sender>"
}
```

A node that speaks a protocol with its peers may add a `meta` object
(`"meta": {"class": "auto"}`). a8s carries it across every hop — local routing,
alias fan-out, remote publish and receive — and expands it into the wake as
`$META` without reading a key; the vocabulary belongs to the nodes at the edges.
`tell` writes no `meta` itself.

On disk alongside the JSON:

```
<outbox>/
  01J….json
  01J…/
    avatar.jpg
```

`from` is omitted when registry is unreachable; the router **force-overwrites** `from` based on which agent owns the outbox directory. When a namespace is bound to that agent, mail leaving the namespace presents as the bare prefix (`acme`) and mail inside it keeps `acme:<sub-sender>`.

4. **Ingest** — move `<msg_id>.json` and `<outbox>/<msg_id>/` together into `agents/<SENDER>/pending/` under the a8s state root.
5. **Route** — copy pending bundle bytes into each recipient's `<files_dir>/<msg_id>/` (default `.files`). Inbox JSON keeps filename-only `files`. Wake `$MESSAGE` appends absolute `ATTACHED FILE:` lines.

## `TELL_OUTBOX_DIR`

The outbox path tell writes to.

**Priority:**

1. `TELL_OUTBOX_DIR` when set (required for deployed agents — a8s injects it on wake).
2. Else a unique configured outbox matched from CWD when the a8s state root is readable
   (desktop / filedrop seats — see [a8s-filedrop.md](a8s-filedrop.md)).
3. Else fail.

```bash
export TELL_OUTBOX_DIR=/var/filedrops/agent-one/.outbox
tell GEMINI "hello"
```

Created when missing.

When a8s wakes an agent, it sets `TELL_OUTBOX_DIR` in the invoke subprocess environment to the agent definition's resolved `outbox_dir` (default `<agent-root>/.outbox`). Use a separate absolute `outbox_dir` to keep outgoing tell traffic outside the agent workspace.

a8s sets it last, over anything the node declares in `definition.env`, so a definition cannot redirect its own outbox. A definition that opts into `wake_shell: "login"` is the one way to lose that guarantee: the rc files run inside the wrapped shell, after a8s has handed the variable over, so an unguarded `export TELL_OUTBOX_DIR=...` in an rc file wins. See [Wake environment](a8s.md#wake-environment-optional).

Does not affect `sender_from_cwd()`; the router still force-stamps `from` from outbox ownership.

**Inherited-variable warning.** When `TELL_OUTBOX_DIR` names a registered agent's own outbox and CWD is neither that agent's root nor the outbox itself, `tell` prints a warning to stderr and sends anyway; `tell --check` reports the same line. That pair is the shape a stale variable makes when it leaks from a live seat into another shell — every check passes, and the mail leaves under the wrong name. A staging outbox is not registered and an agent in its own root matches, so neither warns.

## Who validates the recipient

Recipient validation belongs to whoever routes the outbox, so `tell` asks one
question: **is the resolved outbox a registered agent's own outbox?**

- **Yes** — `tell` feeds the a8s router, and the registry is the authority on
  who may be addressed. An unknown name fails at the terminal before anything
  is staged (`no agent or alias named 'ghost'`), with the usual remote fallback
  when remotes are configured.
- **No** — `tell` is a staging writer for another router. r4t points a caged
  roster member's `TELL_OUTBOX_DIR` at a per-turn staging dir it drains itself,
  and roster members are not a8s agents, so `tell moss` must stage and let
  `dispatch.release_staging` resolve the name against the roster. That consumer
  canonicalizes bare roster names to intra-roster routes, dead-letters an
  explicit `node:<nobody>` sub-address, logs `UNKNOWN-MEMBER` for a bare name
  matching no member, and hands anything genuinely external to a8s — which
  rejects an unknown recipient at ingest (`unknown recipient …; trashing`).

`from` stamping does not follow this split: it still applies whenever CWD sits
inside a registered agent root, and the router force-overwrites `from` from
outbox ownership — the filesystem is the unforgeable identity. One exception:
when several registered agents share one physical outbox directory, a claimed
`from` naming a co-registered peer on that path is honored (see
[a8s-filedrop.md](a8s-filedrop.md#shared-mount-bridge-principals)).

## `tells` (receive side)

`tells [-f] [--timeout SEC] [--body-max N] [--glow [THEME]] [--heading-out|in …]` (`apps/a8s/tells.py`)
is the receive-side complement of `tell`. It resolves the node the same way
`tell` does — the file-proxy inbox is `.inbox` beside the outbox
(`<outbox-parent>/.inbox`).

1. Snapshot the `.json` envelopes already in `.inbox`.
2. Poll (0.1s) up to `--timeout` seconds (default 5) for new envelopes; `-f` /
   `--timeout 0` follows until Ctrl+C.
3. Print each new envelope as `sender: body` by default. Bodies longer than
   `--body-max` / `TELLS_BODY_MAX` (default **16000** chars; `0` = unlimited)
   are clipped and followed by `tells --recover <token>`, which prints the full
   `content` from that inbox JSON. The token is the envelope's path in
   base64url, so the printed line survives a paste into any shell — no quoting
   makes an arbitrary path inert, since bash and PowerShell expand `$name` and
   `$(...)` inside double quotes and cmd expands `%NAME%`. `tells --show PATH`
   takes a plain path for a reader who has one. Neither needs an outbox or a
   registry: a clipped message is recoverable from wherever the reader is. With `--glow` and/or `--heading-out` /
   `--heading-in`, print the same markdown as `a8s convo` (shared
   `format_entry` / GlowStream). Timeout prints one stderr line and exits 1.

Non-destructive: it observes new arrivals without consuming them, so it never competes to remove `.inbox` files and each run waits from its own baseline. Partial writes (mid-delivery on a cross-mount move) are tolerated — an unreadable file is skipped and retried on the next poll. It only reports messages that land after it starts; anything already waiting is ignored.

## System install

Machine-wide / agent-user installs go through the public install story — not an
a8s verb. `AR3_SYSTEM=1` on `get.sh` clones into `AR3_DIR` (default
`/usr/local/lib/ar3`) and symlinks the suite shims into `AR3_BIN` (default
`/usr/local/bin`) instead of editing a shell rc:

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh \
  | sudo AR3_SYSTEM=1 sh
```

That puts `tell` on the shared PATH so `run_as` agent users resolve it without
reading an operator home clone. Those seats still have no registry access —
always set `TELL_OUTBOX_DIR` (r4t injects it across the boundary).

## Tests

- `apps/a8s/tests/test_tell.py` — send path, attachments, outbox resolution
- `apps/a8s/tests/test_tells.py` — receive-side wait behavior
