# Filedrop

A **filedrop** is an a8s node that receives mail as files in `<root>/.inbox/`
instead of waking a CLI. Use it from a human terminal or a desktop IDE session
(Cursor, Claude Code, Codex) — not a deployed agent woken by a8s.

Deployed agents never see this doc: a8s sets `TELL_OUTBOX_DIR` on wake, so
`tell` resolves their outbox with no setup. Send-side usage for those agents
reads as [`tell` skill markdown](../skills/tell/SKILL.md).

**Filedrop agents (IDE seats):** point them at
[`https://github.com/witw-llc/ar3-private/wiki/Playbook-a8s-Filedrop-Agent`](https://github.com/witw-llc/ar3-private/wiki/Playbook-a8s-Filedrop-Agent) — session playbook
(signals not contracts; own root only; no infra edits).

To find this guide later: search the repo for **filedrop**.

## Setup (once)

```bash
mkdir -p ~/filedrops/neil-macbook
a8s add neil-macbook ~/filedrops/neil-macbook filedrop

# Keep the handler running so mail lands in .inbox even when nobody is watching
a8s start neil-macbook
```

`filedrop` is the bundled definition (`definitions/filedrop.json`): file-proxy
delivery, no CLI wake. Same pattern for a per-app seat (`cursor-drop`, etc.).

## Day to day

```bash
# Optional for humans: set once in your shell rc
export TELL_OUTBOX_DIR=~/filedrops/neil-macbook/.outbox

# Watch inbound only (no echo of what you sent — prefer this over `a8s convo -f`)
tells -f

# Reply (same TELL_OUTBOX_DIR)
tell alice "sounds good"
```

Desktop IDE agents should set `TELL_OUTBOX_DIR` on **every** shell that runs
`tell` / `tells`, pointing at *their* filedrop outbox — not a shared human
default. Otherwise outbound mail is stamped from the wrong seat (classic
"messages from myself" failure).

```bash
export TELL_OUTBOX_DIR=~/filedrops/cursor-drop/.outbox
tells -f          # background OK; .inbox still fills when this is down
tell neil-macbook - <<'EOF'
done with the refactor
EOF
```

When CWD is inside a unique registered filedrop root, `tell` / `tells` can
infer the outbox without the env var (see below). Outside that root, set
`TELL_OUTBOX_DIR`.

## Outbox resolution

`tell` / `tells` pick an outbox in this order:

1. **`TELL_OUTBOX_DIR`** if set (deployed agents; also the unambiguous desktop choice).
2. Else, if `~/.a8s` is readable, match **configured** agent outboxes against CWD:
   - CWD *is* the outbox, or
   - CWD *contains* the outbox, or
   - CWD is inside that agent's registered root.
3. **Exactly one** match → use it.
4. **Several** matches → refuse; set `TELL_OUTBOX_DIR` (typical when CWD is `$HOME`).
5. **None** / no registry (e.g. `install-client` tell-only) → refuse; set `TELL_OUTBOX_DIR`.

`install-client` copies tell without a8s config access — env-only, by design.

## Why `tells` not `convo -f`

| | `tells -f` | `a8s convo -f` |
|---|---|---|
| Source | this seat's `.inbox` | machine-wide archive |
| Shows | inbound only | to *and* from |
| Needs | outbox/inbox for this seat | registered agent name |

For a filedrop loop you want inbound only.

## Related

- Agent session playbook: [`https://github.com/witw-llc/ar3-private/wiki/Playbook-a8s-Filedrop-Agent`](https://github.com/witw-llc/ar3-private/wiki/Playbook-a8s-Filedrop-Agent)
- File-proxy mechanics: [README — File proxy](../README.md#file-proxy)
- Tell internals: [tell.md](tell.md)
- Operator skill (send-only, for deployed agents): [`skills/tell/SKILL.md`](../skills/tell/SKILL.md)
