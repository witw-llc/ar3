from __future__ import annotations

import secrets
import time

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LENGTH = 26
_RND_BYTES = 10
_TS_BITS = 48
_RND_BITS = 80
_TS_MASK = (1 << _TS_BITS) - 1
_RND_MASK = (1 << _RND_BITS) - 1


def new() -> str:
    ts_ms = int(time.time() * 1000) & _TS_MASK
    rnd = int.from_bytes(secrets.token_bytes(_RND_BYTES), "big")
    n = (ts_ms << _RND_BITS) | rnd
    chars = []
    for _ in range(LENGTH):
        chars.append(ALPHABET[n & 0x1f])
        n >>= 5
    return "".join(reversed(chars))
