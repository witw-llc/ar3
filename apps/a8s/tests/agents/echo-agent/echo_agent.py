"""Echo agent wake handler.

Receives `<age> <sender> <recipient> <message>` from the a8s wake
expansion, prints the canonical `Date:` / `From:` / `To:` / blank /
body lines to stdout (so `a8s logs echoman` keeps working), and
persists each tell to `.files/<ulid>.txt` for easy monitoring.

Trailing `ATTACHED FILE: <path>` lines in the message body are stripped from
the persisted body and the referenced files are copied to
`.files/<ulid>-<basename>` via shutil.copy2. Missing referenced files
are logged to stderr and skipped (the wake still succeeds).
"""
from __future__ import annotations

import os
import secrets
import shutil
import sys
import time
from pathlib import Path

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH = 26
_TS_BITS = 48
_RND_BITS = 80
_RND_BYTES = _RND_BITS // 8
_TS_MASK = (1 << _TS_BITS) - 1


def new_ulid() -> str:
    ts_ms = int(time.time() * 1000) & _TS_MASK
    rnd = int.from_bytes(secrets.token_bytes(_RND_BYTES), "big")
    n = (ts_ms << _RND_BITS) | rnd
    chars = []
    for _ in range(ULID_LENGTH):
        chars.append(ALPHABET[n & 0x1f])
        n >>= 5
    return "".join(reversed(chars))


def split_body_and_files(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    files: list[str] = []
    while lines and lines[-1].startswith("ATTACHED FILE: "):
        files.insert(0, lines.pop()[len("ATTACHED FILE: "):].strip())
    return "\n".join(lines), files


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: echo-agent-cli <age> <sender> <recipient> <message>",
            file=sys.stderr,
        )
        return 2
    age, sender, recipient, raw_message = argv
    body, files = split_body_and_files(raw_message)

    print(f"Date: {age}")
    print(f"From: {sender}")
    print(f"To: {recipient}")
    print("")
    print(raw_message)

    ulid = new_ulid()
    files_dir = Path.cwd() / ".files"
    files_dir.mkdir(exist_ok=True)

    out_path = files_dir / f"{ulid}.txt"
    header = (
        f"Date: {age}\n"
        f"From: {sender}\n"
        f"To: {recipient}\n"
        f"ULID: {ulid}\n"
        f"\n"
    )
    tmp = files_dir / f".{ulid}.txt.tmp"
    tmp.write_text(header + body, encoding="utf-8")
    os.replace(tmp, out_path)

    for ref in files:
        src = Path(ref)
        if not src.is_file():
            print(f"echo-agent: skipping missing file {ref!r}", file=sys.stderr)
            continue
        dest = files_dir / f"{ulid}-{src.name}"
        try:
            shutil.copy2(src, dest)
        except OSError as e:
            print(
                f"echo-agent: failed to copy {ref!r}: {e}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
