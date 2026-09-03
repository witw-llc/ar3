# Filedrop

A **filedrop** is an a8s node that receives mail as files in `<root>/.inbox/`
instead of waking a CLI. Use it from a human terminal or a desktop IDE session
(Cursor, Claude Code, Codex) — not a deployed agent woken by a8s.

Deployed agents never see this doc: a8s sets `TELL_OUTBOX_DIR` on wake, so
`tell` resolves their outbox with no setup. Send-side usage for those agents
reads as [`tell` skill markdown](../apps/a8s/skills/tell/SKILL.md).

**Filedrop agents (IDE seats):** point them at
[`https://github.com/witw-llc/ar3-private/wiki/Playbook-a8s-Filedrop-Agent`](https://github.com/witw-llc/ar3-private/wiki/Playbook-a8s-Filedrop-Agent) — session playbook
(signals not contracts; own root only; no infra edits).

To find this guide later: search the repo for **filedrop**.

## Setup (once)

```bash
mkdir -p ~/filedrops/my-desktop
a8s add my-desktop ~/filedrops/my-desktop filedrop

# Keep the handler running so mail lands in .inbox even when nobody is watching
a8s start my-desktop
```

`filedrop` is the bundled definition (`definitions/filedrop.json`): file-proxy
delivery, no CLI wake. Same pattern for a per-app seat (`cursor-drop`, etc.).

## Day to day

```bash
# Optional for humans: set once in your shell rc
export TELL_OUTBOX_DIR=~/filedrops/my-desktop/.outbox

# Watch inbound only (no echo of what you sent — prefer this over `a8s convo -f`)
tells -f

# Reply (same TELL_OUTBOX_DIR)
tell alice "sounds good"
```

**Long messages:** an agent host's own notification path can clip a printed
line well short of what `tells` prints — Claude Code's Monitor cuts each
stdout line at 500 bytes. `tells` soft-wraps lines over `--line-max` bytes
(default 400) and puts `tells --recover <token>` in the header, ahead of the
body, so a clipped notification still shows where to read the rest. Match a
different host's clip with `--line-max` / `TELLS_LINE_MAX` — `0` turns the wrap off, and a positive value under 16 bytes is refused.

Desktop IDE agents should set `TELL_OUTBOX_DIR` on **every** shell that runs
`tell` / `tells`, pointing at *their* filedrop outbox — not a shared human
default. Otherwise outbound mail is stamped from the wrong seat (classic
"messages from myself" failure).

```bash
export TELL_OUTBOX_DIR=~/filedrops/cursor-drop/.outbox
tells -f          # background OK; .inbox still fills when this is down
tell my-desktop - <<'EOF'
done with the refactor
EOF
```

When CWD is inside a unique registered filedrop root, `tell` / `tells` can
infer the outbox without the env var (see below). Outside that root, set
`TELL_OUTBOX_DIR`.

## Shared mount (bridge principals)

Bridges that register **several names on one Drive/sync mount** are intentional:

```bash
a8s add my-google /mnt/gdrive/a8s filedrop
a8s add my-email /mnt/gdrive/a8s filedrop
```

Inbound already shares `<root>/.inbox/` and preserves envelope `to`. Outbound
shares `<root>/.outbox/`: when the router ingests that directory, a claimed
`from` that names a **co-registered peer on the same outbox path** attributes
the pending file and wire `from` to that peer — even when only one of the
names is the handled sender (`a8s start my-google`). Unbacked or foreign
claims keep force-stamp on the scanning handler. Unrelated roots cannot
impersonate each other.

## Outbox resolution

`tell` / `tells` pick an outbox in this order:

1. **`TELL_OUTBOX_DIR`** if set (deployed agents; also the unambiguous desktop choice).
2. Else, if the a8s state root (default `~/.config/a8s`) is readable, match **configured** agent outboxes against CWD:
   - CWD *is* the outbox, or
   - CWD *contains* the outbox, or
   - CWD is inside that agent's registered root.
3. **Exactly one** match → use it.
4. **Several** matches → refuse; set `TELL_OUTBOX_DIR` (typical when CWD is `$HOME`).
5. **None** / no registry (e.g. system-installed tell without a reachable registry) → refuse; set `TELL_OUTBOX_DIR`.

Agent-user / system installs put `tell` on a shared PATH without a8s config
access — env-only, by design.

## Why `tells` not `convo -f`

| | `tells -f` | `a8s convo -f` |
|---|---|---|
| Source | this seat's `.inbox` | machine-wide archive |
| Shows | inbound only | to *and* from |
| Needs | outbox/inbox for this seat | registered agent name |
| Narrows to one sender | no | `--from NAME` |

For a filedrop loop you want inbound only.

## Related

- Agent session playbook: [`https://github.com/witw-llc/ar3-private/wiki/Playbook-a8s-Filedrop-Agent`](https://github.com/witw-llc/ar3-private/wiki/Playbook-a8s-Filedrop-Agent)
- File-proxy mechanics: [a8s.md — File proxy](a8s.md#file-proxy)
- Tell internals: [a8s-tell.md](a8s-tell.md)
- Operator skill (send-only, for deployed agents): [`skills/tell/SKILL.md`](../apps/a8s/skills/tell/SKILL.md)
