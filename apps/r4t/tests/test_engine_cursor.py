"""Cursor quota token sources: the IDE database, the CLI auth file, the Keychain."""
from __future__ import annotations

import http.client
import io
import json
import subprocess
import tempfile
import urllib.error
from pathlib import Path

import pytest

from engines import cursor
from engines.base import QuotaError

REAL_KEYCHAIN_TOKEN = cursor._keychain_token
REAL_CLI_AUTH_TOKEN = cursor._cli_auth_token

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
    assert result["note"].endswith("; token from the IDE state database")
    assert "Keychain" not in result["note"]


def test_keychain_token_answers_when_the_database_and_file_are_absent(wire):
    result = cursor.quota()
    assert wire["security"] == [[
        "security", "find-generic-password",
        "-s", "cursor-access-token", "-a", "cursor-user", "-w",
    ]]
    assert wire["bearer"] == "Bearer kc-token"
    assert result["note"].endswith("; token from the agent CLI's Keychain item")
    assert result["plan"] == "pro_plus"


def _auth_file(raw: str) -> tuple[tempfile.TemporaryDirectory, Path]:
    # Not tmp_path: the wire fixture sets sys.platform to darwin, and on
    # Windows pytest then asks os.getuid() while building the temp dir.
    hold = tempfile.TemporaryDirectory()
    path = Path(hold.name) / "auth.json"
    path.write_text(raw, encoding="utf-8")
    return hold, path


def test_auth_file_answers_before_the_keychain(wire, monkeypatch):
    hold, auth = _auth_file(json.dumps({"accessToken": "file-token", "refreshToken": "r"}))
    try:
        monkeypatch.setattr(cursor, "cli_auth_candidates", lambda: [auth])
        monkeypatch.setattr(cursor, "_cli_auth_token", REAL_CLI_AUTH_TOKEN)
        result = cursor.quota()
    finally:
        hold.cleanup()
    assert wire["security"] == []
    assert wire["bearer"] == "Bearer file-token"
    assert result["note"].endswith("; token from the agent CLI's auth.json")


def test_database_token_beats_the_auth_file(wire, monkeypatch):
    hold, auth = _auth_file(json.dumps({"accessToken": "file-token"}))
    try:
        monkeypatch.setattr(cursor, "cli_auth_candidates", lambda: [auth])
        monkeypatch.setattr(cursor, "_cli_auth_token", REAL_CLI_AUTH_TOKEN)
        monkeypatch.setattr(cursor, "_state_value", lambda key: {
            cursor.ACCESS_TOKEN_KEY: "db-token",
        }.get(key))
        result = cursor.quota()
    finally:
        hold.cleanup()
    assert wire["bearer"] == "Bearer db-token"
    assert result["note"].endswith("; token from the IDE state database")
    assert wire["security"] == []


@pytest.mark.parametrize(
    "raw", ["{not json", "[]", '{"accessToken": "  "}', '{"refreshToken": "only"}']
)
def test_unusable_auth_file_falls_through_to_the_keychain(wire, monkeypatch, raw):
    hold, auth = _auth_file(raw)
    try:
        monkeypatch.setattr(cursor, "cli_auth_candidates", lambda: [auth])
        monkeypatch.setattr(cursor, "_cli_auth_token", REAL_CLI_AUTH_TOKEN)
        result = cursor.quota()
    finally:
        hold.cleanup()
    assert wire["bearer"] == "Bearer kc-token"
    assert result["note"].endswith("; token from the agent CLI's Keychain item")


@pytest.mark.parametrize("answer", [("", 0), ("tok\n", 44)])
def test_absent_keychain_item_names_every_source(wire, answer):
    wire["keychain"] = answer
    with pytest.raises(QuotaError) as caught:
        cursor.quota()
    message = str(caught.value)
    assert "no Cursor state database" in message
    assert "auth.json" in message
    assert "cursor-access-token" in message
    assert "agent login" in message


def test_missing_security_binary_counts_as_absent(wire, monkeypatch):
    def run(argv, **kwargs):
        raise FileNotFoundError("security")

    monkeypatch.setattr(cursor.subprocess, "run", run)
    with pytest.raises(QuotaError):
        cursor.quota()


def test_a_rejected_database_token_falls_through_and_names_the_store(wire, monkeypatch):
    hold, auth = _auth_file(json.dumps({"accessToken": "file-token"}))

    def urlopen(request, timeout=None):
        authz = request.get_header("Authorization")
        if authz == "Bearer db-token":
            raise urllib.error.HTTPError(
                cursor.USAGE_URL, 401, "Unauthorized", {}, io.BytesIO(b""),
            )
        wire["bearer"] = authz
        return _Response(json.dumps(PAYLOAD).encode())

    try:
        monkeypatch.setattr(cursor.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(cursor, "cli_auth_candidates", lambda: [auth])
        monkeypatch.setattr(cursor, "_cli_auth_token", REAL_CLI_AUTH_TOKEN)
        monkeypatch.setattr(cursor, "_state_value", lambda key: {
            cursor.ACCESS_TOKEN_KEY: "db-token",
        }.get(key))
        result = cursor.quota()
    finally:
        hold.cleanup()
    assert wire["bearer"] == "Bearer file-token"
    assert "auth.json" in result["note"]
    assert result["note"].endswith("skipped rejected database (HTTP 401)")


def test_every_rejected_login_names_its_store_and_not_the_token(wire, monkeypatch):
    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            cursor.USAGE_URL, 403, "Forbidden", {}, io.BytesIO(b""),
        )

    monkeypatch.setattr(cursor.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        cursor, "_state_value",
        lambda key: "db-token" if key == cursor.ACCESS_TOKEN_KEY else None,
    )
    with pytest.raises(QuotaError) as caught:
        cursor.quota()
    message = str(caught.value)
    assert "database (HTTP 403)" in message
    assert "keychain (HTTP 403)" in message
    assert "db-token" not in message
    assert "kc-token" not in message


@pytest.mark.parametrize("secret", ["line1\nline2-secret", "line1\tline2", "line1\u00e9line2"])
def test_a_malformed_login_falls_through(wire, monkeypatch, secret):
    seen = []

    def urlopen(request, timeout=None):
        seen.append(request.get_header("Authorization"))
        return _Response(json.dumps(PAYLOAD).encode("utf-8"))

    monkeypatch.setattr(cursor.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        cursor, "_state_value",
        lambda key: secret if key == cursor.ACCESS_TOKEN_KEY else None,
    )
    result = cursor.quota()
    assert seen == ["Bearer kc-token"]
    assert secret not in result["note"]
    assert "cursor login in database is malformed" in result["note"]


def test_every_malformed_login_names_its_store(wire, monkeypatch):
    secret = "line1\nline2-secret"
    monkeypatch.setattr(cursor, "_keychain_token", lambda: secret)
    monkeypatch.setattr(
        cursor, "_state_value",
        lambda key: secret if key == cursor.ACCESS_TOKEN_KEY else None,
    )
    with pytest.raises(QuotaError) as caught:
        cursor.quota()
    message = str(caught.value)
    assert message == (
        "cursor login in database is malformed; "
        "cursor login in keychain is malformed"
    )
    assert secret not in message
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_space_token_does_not_reach_http_client(monkeypatch):
    secret = "bad token"
    called = []

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        called.append(headers)

    monkeypatch.setattr(http.client.HTTPConnection, "request", request)
    monkeypatch.setattr(cursor, "_keychain_token", lambda: None)
    monkeypatch.setattr(cursor, "_cli_auth_token", lambda: None)
    monkeypatch.setattr(
        cursor, "_state_value",
        lambda key: secret if key == cursor.ACCESS_TOKEN_KEY else None,
    )
    with pytest.raises(QuotaError) as caught:
        cursor.quota()
    assert called == []
    assert str(caught.value) == "cursor login in database is malformed"
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_request_error_keeps_a_fixed_message(wire, monkeypatch):
    secret = "line1\nline2-secret"

    def urlopen(request, timeout=None):
        conn = http.client.HTTPConnection("invalid")
        conn.putrequest("POST", "/usage")
        conn.putheader("Authorization", request.get_header("Authorization"))

    monkeypatch.setattr(cursor.urllib.request, "urlopen", urlopen)
    with pytest.raises(QuotaError) as caught:
        cursor._fetch_quota(secret, "database")
    message = str(caught.value)
    assert message == "cursor dashboard endpoint unreachable"
    assert secret not in message
    assert secret.replace("\n", "\\n") not in message
    assert "line2-secret" not in message
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_invalid_utf8_auth_file_falls_through_to_the_keychain(wire, monkeypatch):
    hold = tempfile.TemporaryDirectory()
    path = Path(hold.name) / "auth.json"
    path.write_bytes(b"\xff\xfe not utf-8")
    try:
        monkeypatch.setattr(cursor, "cli_auth_candidates", lambda: [path])
        monkeypatch.setattr(cursor, "_cli_auth_token", REAL_CLI_AUTH_TOKEN)
        result = cursor.quota()
    finally:
        hold.cleanup()
    assert wire["bearer"] == "Bearer kc-token"
    assert result["note"].endswith("; token from the agent CLI's Keychain item")


def test_linux_names_the_auth_file_and_skips_the_keychain(wire, monkeypatch):
    monkeypatch.setattr(cursor.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    with pytest.raises(QuotaError) as caught:
        cursor.quota()
    assert wire["security"] == []
    message = str(caught.value)
    assert "auth.json" in message
    assert "not verified" not in message
    assert ".config" in message
