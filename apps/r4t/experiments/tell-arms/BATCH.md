# TELL-ARMS — the probe batch

20 messages, identical across all three arms, delivered one at a time with
`r4t seat send` and waited on to completion. Shapes per PROTOCOL.md §2:
5 dollar, 5 backtick, 5 backslash-path, 5 mixed. The four protocol-supplied
examples lead their shape and are marked ✱; the other sixteen are generated in
the same shape.

## The wrapper (identical for every message)

Every message the human sends is this template with `{payload}` substituted —
so the only thing that differs across arms is how sending is taught, and the
only thing that differs across messages is the hazard payload:

```
Send me back the following line as your entire message body, character for
character. Do not add commentary, do not change or drop a single character:

{payload}
```

The wrapper is passed as one `argv` element from a Python driver, never through
a shell, so the payload reaches the member's queue byte-exact. That is verified:
the turn-capture prompts in the run record show each payload intact.

## The payloads

The reply body must contain the payload verbatim. A run is scored against these
strings and nothing else.

| # | id | shape | payload |
| --- | --- | --- | --- |
| 1 | d1 | dollar ✱ | `confirm the refund is $1.25 and the budget is $500` |
| 2 | d2 | dollar | `the invoice total is $2.50 and the retainer is $1000` |
| 3 | d3 | dollar | `line item: $9.99 plus $100 shipping` |
| 4 | d4 | dollar | `we owe $3.75 and they owe $250` |
| 5 | d5 | dollar | `cap is $50 per seat, floor is $7.25` |
| 6 | b1 | backtick ✱ | ``quote the literal command `whoami` back to me`` |
| 7 | b2 | backtick | ``run `pwd` first, then `date` `` |
| 8 | b3 | backtick | ``the check is `git status` verbatim`` |
| 9 | b4 | backtick | ``escape nothing: `echo hi` stays literal`` |
| 10 | b5 | backtick | ``the flag is `--dry-run` in backticks`` |
| 11 | p1 | backslash ✱ | `repeat the path C:\temp\notes.txt exactly` |
| 12 | p2 | backslash | `the log lives at C:\logs\app\run.log` |
| 13 | p3 | backslash | `config is at C:\Users\dev\tool.ini` |
| 14 | p4 | backslash | `copy from D:\build\out\bin.exe` |
| 15 | p5 | backslash | `the share is \\server\share\data.csv` |
| 16 | m1 | mixed ✱ | ``invoice line: $19.99 for `setup.exe` at C:\bin`` |
| 17 | m2 | mixed | ``charge $5.00 to run `installer.msi` from C:\tmp`` |
| 18 | m3 | mixed | ``budget $300 covers `make build` in C:\src\app`` |
| 19 | m4 | mixed | ``refund $12.50 after `chkdsk` on D:\data`` |
| 20 | m5 | mixed | ``fee $0.99 for `curl -s` writing C:\out\res.json`` |

## Scoring anchors (derived, not chosen)

Scoring is mechanical. Each payload's *anchors* are computed from the payload
itself: delete every hazard character together with the region a shell would
destroy — `$` plus the following `[A-Za-z0-9_]*`, a backtick pair and
everything between, a `\` plus the one character after it — then keep the
remaining fragments carrying 4+ letters and use the outermost two. Those are
the words no shell bug can touch, so their presence separates "relayed but
mangled" from "answered something else".

| id | anchors |
| --- | --- |
| d1 | `confirm the refund is` · `.25 and the budget is` |
| d2 | `the invoice total is` · `.50 and the retainer is` |
| d3 | `line item:` · `shipping` |
| d4 | `we owe` · `.75 and they owe` |
| d5 | `cap is` · `per seat, floor is` |
| b1 | `quote the literal command` · `back to me` |
| b2 | `first, then` |
| b3 | `the check is` · `verbatim` |
| b4 | `escape nothing:` · `stays literal` |
| b5 | `the flag is` · `in backticks` |
| p1 | `repeat the path C:` · `otes.txt exactly` |
| p2 | `the log lives at C:` · `un.log` |
| p3 | `config is at C:` · `ool.ini` |
| p4 | `copy from D:` · `in.exe` |
| p5 | `the share is` · `ata.csv` |
| m1 | `invoice line:` |
| m2 | `charge` · `from C:` |
| m3 | `budget` · `covers` |
| m4 | `refund` · `.50 after` |
| m5 | `writing C:` · `es.json` |

Verdicts, in order of application:

- **EXACT** — the payload appears verbatim in an envelope body (whitespace runs
  normalized; every hazard character byte-exact).
- **GARBLED** — not verbatim, but every anchor is present: the line was
  relayed and the hazard characters are what changed. A payload with no anchor
  scores GARBLED on any non-exact envelope — the conservative call, since
  GARBLED counts against the arm in the pre-registered rule and OFF-SCRIPT
  does not.
- **NO-SEND** — the turn completed and no envelope reached the human's seat.
- **OFF-SCRIPT** — an envelope exists but the anchors are absent: the member
  answered with something else entirely. Reported as its own column; the
  protocol names only garbled and no-send, so it is not folded into either.
