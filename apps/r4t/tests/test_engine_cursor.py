"""Cursor quota token sources: the IDE database, then the agent CLI's Keychain."""
from __future__ import annotations

import io
import json
import subprocess

import pytest

from engines import cursor
from engines.base import QuotaError

REAL_KEYCHAIN_TOKEN = cursor._keychain_token

PAYLOAD = {
    "billingCycleEnd": "1788222978000",
    "membershipType": "pro_plus",
    "planUsage": {"totalPercentUsed": 20.0},
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def wire(monkeypatch):
    """Real Keychain reader, fake `security` and fake network."""
    monkeypatch.setattr(cursor, "_keychain_token", REAL_KEYCHAIN_TOKEN)
    monkeypatch.setattr(cursor.sys, "platform", "darwin")
    state = {"security": [], "bearer": None, "keychain": ("kc-token\n", 0)}

    def run(argv, **kwargs):
        state["security"].append(argv)
        out, code = state["keychain"]
        return subprocess.CompletedProcess(argv, code, out, "")

    def urlopen(request, timeout=None):
        state["bearer"] = request.get_header("Authorization")
        return _Response(json.dumps(PAYLOAD).encode())

    monkeypatch.setattr(cursor.subprocess, "run", run)
    monkeypatch.setattr(cursor.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(cursor, "state_db_candidates", lambda: [])
    monkeypatch.setattr(cursor, "state_db", lambda: None)
    return state


def test_database_token_never_asks_the_keychain(wire, monkeypatch):
    monkeypatch.setattr(cursor, "_state_value", lambda key: {
        cursor.ACCESS_TOKEN_KEY: "db-token",
        "cursorAuth/stripeMembershipType": "pro",
    }.get(key))
    result = cursor.quota()
    assert wire["security"] == []
    assert wire["bearer"] == "Bearer db-token"
    assert "Keychain" not in result["note"]


def test_keychain_token_answers_when_the_database_is_absent(wire):
    result = cursor.quota()
    assert wire["security"] == [[
        "security", "find-generic-password",
        "-s", "cursor-access-token", "-a", "cursor-user", "-w",
    ]]
    assert wire["bearer"] == "Bearer kc-token"
    assert result["note"].endswith("; token from the agent CLI's Keychain item")
    assert result["plan"] == "pro_plus"


@pytest.mark.parametrize("answer", [("", 0), ("tok\n", 44)])
def test_absent_keychain_item_names_both_sources(wire, answer):
    wire["keychain"] = answer
    with pytest.raises(QuotaError) as caught:
        cursor.quota()
    message = str(caught.value)
    assert "no Cursor state database" in message
    assert "cursor-access-token" in message
    assert "agent login" in message


def test_missing_security_binary_counts_as_absent(wire, monkeypatch):
    def run(argv, **kwargs):
        raise FileNotFoundError("security")

    monkeypatch.setattr(cursor.subprocess, "run", run)
    with pytest.raises(QuotaError):
        cursor.quota()


def test_other_platforms_do_not_try_the_keychain(wire, monkeypatch):
    monkeypatch.setattr(cursor.sys, "platform", "linux")
    with pytest.raises(QuotaError) as caught:
        cursor.quota()
    assert wire["security"] == []
    assert "not verified" in str(caught.value)
