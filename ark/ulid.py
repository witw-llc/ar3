"""Pure-stdlib ULID generator and parser.

ULIDs are 26-character Crockford base32 strings: 48-bit ms timestamp followed
by 80 bits of randomness. They sort lexicographically by time, which makes
them a drop-in replacement for the timestamp-prefixed filenames a8s used to
generate for outbox/inbox messages.

This is a minimal implementation — just `new()` and `parse()`. No monotonic
guarantees within a single millisecond (two ULIDs generated in the same ms
sort in random order, since the randomness tiebreaks). For a8s that's fine:
ULIDs are addresses, not sequence numbers.
"""
from __future__ import annotations

import secrets
import time

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32 (no I, L, O, U)
LENGTH = 26
_TS_BITS = 48
_RND_BITS = 80
_RND_BYTES = _RND_BITS // 8
_TS_MASK = (1 << _TS_BITS) - 1
_RND_MASK = (1 << _RND_BITS) - 1


def new() -> str:
    """Return a new 26-char ULID for the current wall clock."""
    ts_ms = int(time.time() * 1000) & _TS_MASK
    rnd = int.from_bytes(secrets.token_bytes(_RND_BYTES), "big")
    n = (ts_ms << _RND_BITS) | rnd
    chars = []
    for _ in range(LENGTH):
        chars.append(ALPHABET[n & 0x1f])
        n >>= 5
    return "".join(reversed(chars))


def parse(s: str) -> tuple[int, bytes]:
    """Split a ULID into `(timestamp_ms, randomness_bytes)`.

    Raises `ValueError` on malformed input."""
    if len(s) != LENGTH:
        raise ValueError(f"ULID must be {LENGTH} chars, got {len(s)}: {s!r}")
    n = 0
    for ch in s.upper():
        i = ALPHABET.find(ch)
        if i < 0:
            raise ValueError(f"invalid Crockford char {ch!r} in {s!r}")
        n = (n << 5) | i
    ts = n >> _RND_BITS
    rnd = (n & _RND_MASK).to_bytes(_RND_BYTES, "big")
    return ts, rnd


def is_ulid(s: str) -> bool:
    """True if `s` is a syntactically valid ULID. Cheap to call in hot paths."""
    if len(s) != LENGTH:
        return False
    for ch in s.upper():
        if ALPHABET.find(ch) < 0:
            return False
    return True
