# h4l — Hall chat rooms (v0)

Branch: `plan/a8s-chatrooms`.

Standalone app: `apps/h4l/`, shim `~/bin/h4l`. a8s wiring via
`apps/a8s/connectors/h4l/example-definition.json` only.

## Deferred

- tell auto-sync ([#149](https://github.com/neilobremski/bin/issues/149))
- @mentions, shared-repo sender spike

## Done

- Registry prefix routing ([#148](https://github.com/neilobremski/bin/issues/148)) —
  `a8s namespace <prefix> <agent>` binds a prefix so `tell <prefix>:<sub> ...`
  routes to the single bound node with the full address preserved in `to`
  (the node self-routes internally via `$RECIPIENT`).

## Usage

```bash
# Standalone
h4l dispatch --root ~/chat-node --from ALICE --node HALL --message '/post war hi'

# a8s
a8s add chatroom ~/chat-node apps/a8s/connectors/h4l/example-definition.json
tell chatroom '#war hi'
```

## IRC-style

Post: `#<room> <message>` (primary) or `/post <room> <message>`. IRC aliases:
`/part` → `/leave`, `/names` → `/members`. Optional `#` on room names in commands.

## Commands

`/post`, `/join`, `/leave`, `/part`, `/invite`, `/list`, `/view`, `/members`,
`/names`, `/help` — or `#<room> <message>` to post without a slash command.

`/view` shows the latest N messages (default 10) as convo-style markdown (`##` for your
posts, `###` for others). Footer reports viewed range and total count, with `tell`
hints for older/newer/latest (`--start` or bare `/view`), and arbitrary
windows (`/view <room> <start> <limit>`).

State: `<root>/.chatrooms/rooms/<slug>/`.

## v0 decisions

| Topic | Decision |
|-------|----------|
| `/list` | All rooms + all members |
| `/post` | Auto-create room; auto-join poster |
| Poster ACK | stdout + `tell` to sender |
| Errors | `tell` to sender |
| Permissions | Open / trusted |
| Slugs | `[a-z0-9_-]+`, lowercase display |
| `/view` | Convo-style markdown; default last 10; footer with range/total; `--start`, `--limit` |
| Notify | Truncated body (~1k) + footer with live node name and room slug |
| Maintenance | `h4l clear --older-than` / `--all` via idle.invoke when configured |

## Status

- [x] Phase 1 CLI + tests
- [x] example-definition.json + README + docs/h4l.md
- [ ] Phase 2 end-to-end a8s attached_loop test (optional)
