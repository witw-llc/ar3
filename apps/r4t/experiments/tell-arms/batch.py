"""The 20-message probe batch, identical across all three arms.

Shapes per TELL-ARMS-PROTOCOL.md: 5 dollar, 5 backtick, 5 backslash-path,
5 mixed. The four protocol-supplied examples lead their shape (marked
`protocol`); the rest are generated in the same shape.

`payload` is the literal the reply body must contain, character for
character. `instruction` is what the human sends — identical wrapper for
every message so the only variable across arms is how sending is taught.
"""

WRAPPER = (
    "Send me back the following line as your entire message body, character "
    "for character. Do not add commentary, do not change or drop a single "
    "character:\n\n{payload}"
)

BATCH = [
    # ---- dollar (5) ----
    ("d1", "dollar", "confirm the refund is $1.25 and the budget is $500", True),
    ("d2", "dollar", "the invoice total is $2.50 and the retainer is $1000", False),
    ("d3", "dollar", "line item: $9.99 plus $100 shipping", False),
    ("d4", "dollar", "we owe $3.75 and they owe $250", False),
    ("d5", "dollar", "cap is $50 per seat, floor is $7.25", False),
    # ---- backtick (5) ----
    ("b1", "backtick", "quote the literal command `whoami` back to me", True),
    ("b2", "backtick", "run `pwd` first, then `date`", False),
    ("b3", "backtick", "the check is `git status` verbatim", False),
    ("b4", "backtick", "escape nothing: `echo hi` stays literal", False),
    ("b5", "backtick", "the flag is `--dry-run` in backticks", False),
    # ---- backslash path (5) ----
    ("p1", "backslash", "repeat the path C:\\temp\\notes.txt exactly", True),
    ("p2", "backslash", "the log lives at C:\\logs\\app\\run.log", False),
    ("p3", "backslash", "config is at C:\\Users\\dev\\tool.ini", False),
    ("p4", "backslash", "copy from D:\\build\\out\\bin.exe", False),
    ("p5", "backslash", "the share is \\\\server\\share\\data.csv", False),
    # ---- mixed (5) ----
    ("m1", "mixed", "invoice line: $19.99 for `setup.exe` at C:\\bin", True),
    ("m2", "mixed", "charge $5.00 to run `installer.msi` from C:\\tmp", False),
    ("m3", "mixed", "budget $300 covers `make build` in C:\\src\\app", False),
    ("m4", "mixed", "refund $12.50 after `chkdsk` on D:\\data", False),
    ("m5", "mixed", "fee $0.99 for `curl -s` writing C:\\out\\res.json", False),
]


def messages():
    for mid, shape, payload, from_protocol in BATCH:
        yield {
            "id": mid,
            "shape": shape,
            "payload": payload,
            "from_protocol": from_protocol,
            "instruction": WRAPPER.format(payload=payload),
        }


if __name__ == "__main__":
    for m in messages():
        print(f"{m['id']}\t{m['shape']}\t{m['payload']!r}")
