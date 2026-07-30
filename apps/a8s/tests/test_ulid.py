"""Tests for the pure-stdlib ULID module."""
from __future__ import annotations

import time

import pytest

from ulid import ALPHABET, LENGTH, is_ulid, new, parse


class TestNew:
    def test_length(self):
        assert len(new()) == LENGTH

    def test_alphabet_only(self):
        u = new()
        for ch in u:
            assert ch in ALPHABET

    def test_uniqueness_in_tight_loop(self):
        # 1000 ULIDs in a tight loop should all be unique even with the same
        # ms timestamp — randomness gives us collision probability ~2^-80.
        seen = {new() for _ in range(1000)}
        assert len(seen) == 1000

    def test_timestamp_is_recent(self):
        before_ms = int(time.time() * 1000)
        u = new()
        after_ms = int(time.time() * 1000)
        ts, _ = parse(u)
        assert before_ms <= ts <= after_ms


class TestParse:
    def test_round_trip(self):
        u = new()
        ts, rnd = parse(u)
        assert isinstance(ts, int)
        assert isinstance(rnd, bytes)
        assert len(rnd) == 10

    def test_invalid_length(self):
        with pytest.raises(ValueError, match="must be 26 chars"):
            parse("TOOSHORT")

    def test_invalid_character(self):
        # 'I' and 'L' and 'O' and 'U' are excluded from Crockford base32.
        bad = "0" * 25 + "I"
        with pytest.raises(ValueError, match="invalid Crockford char"):
            parse(bad)

    def test_lowercase_accepted(self):
        u = new()
        ts, rnd = parse(u.lower())
        ts2, rnd2 = parse(u)
        assert ts == ts2
        assert rnd == rnd2


class TestSortability:
    def test_lexicographic_order_matches_time(self):
        # Two ULIDs separated by >1ms must sort by their wall-clock order.
        a = new()
        time.sleep(0.005)
        b = new()
        assert a < b
        ts_a, _ = parse(a)
        ts_b, _ = parse(b)
        assert ts_a < ts_b


class TestIsUlid:
    def test_accepts_valid(self):
        assert is_ulid(new()) is True

    def test_rejects_short(self):
        assert is_ulid("ABC") is False

    def test_rejects_invalid_char(self):
        assert is_ulid("0" * 25 + "I") is False

    def test_accepts_lowercase(self):
        assert is_ulid(new().lower()) is True
