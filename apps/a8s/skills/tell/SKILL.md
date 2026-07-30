---
name: "tell"
description: "Send an asynchronous message with the `tell` CLI. Delivery is not immediate; do not wait for a reply."
---

# tell

Send messages via the shell (not by printing the command as text). Put the body
on stdin with the heredoc delimiter quoted, so the shell expands nothing and
`$`, backticks, and backslashes arrive byte-exact:

```
tell <recipient> - <<'EOF'
<your message>
EOF
```

Or point stdin at a file you wrote: `tell <recipient> - < message.md`.

Full surface:

```
tell [--attach PATH ...] [--split] <recipient> [<message...>|-]
```

- `<recipient>` is an opaque name — do not guess who/what it is or change tone.
- A trailing `<message...>` argument suits a short plain body. Anything holding
  `$`, backticks, backslashes, quotes, or newlines goes on stdin — inside double
  quotes the shell eats it (`"$1.25"` sends `.25`).
- `--attach` / `--file` may repeat (or list existing paths after one flag); `--attach=PATH` works.
- Oversized attachments fail immediately unless `--split` chunks them under the size limit.
- Returns immediately. Delivery may take seconds or longer; do not expect a reply in-session.
- If `tell` fails with “cannot send from this directory”, tell the user — do not `cd` to work around it.
