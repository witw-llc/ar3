"""Outbound-side tests for gmail_connector — mock urlopen, assert POST shape."""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_CONNECTOR_DIR = Path(__file__).resolve().parent.parent.parent.parent / "connectors" / "gmail"
sys.path.insert(0, str(_CONNECTOR_DIR))

import gmail_connector  # noqa: E402


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(captured: list, response_body: dict):
    def _impl(req, timeout=None):
        captured.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": dict(req.headers),
            "body": json.loads(req.data.decode("utf-8")) if req.data else None,
            "timeout": timeout,
        })
        return _FakeResponse(json.dumps(response_body).encode("utf-8"))
    return _impl


def test_send_posts_correct_shape(monkeypatch, capsys):
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")
    captured: list = []
    fake = _fake_urlopen(captured, {"status": "sent"})
    with patch.object(gmail_connector.urllib.request, "urlopen", fake):
        rc = gmail_connector.send("rcpt@example.com", "NEIL", "hello there")
    assert rc == 0
    assert len(captured) == 1
    call = captured[0]
    assert call["url"] == "https://example/exec"
    assert call["method"] == "POST"
    assert call["headers"]["Content-type"] == "application/json"
    assert call["body"] == {
        "action": "gmail.send",
        "key": "TESTKEY",
        "to": "rcpt@example.com",
        "subject": "NEIL",
        "body": "hello there",
    }
    out = capsys.readouterr().out
    assert "sent to rcpt@example.com: NEIL" in out


def test_send_missing_url_env(monkeypatch, capsys):
    monkeypatch.delenv("GAS_BRIDGE_URL", raising=False)
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")
    rc = gmail_connector.send("a@b.com", "subj", "body")
    assert rc == 2
    err = capsys.readouterr().err
    assert "GAS_BRIDGE_URL" in err
    assert "must be set" in err


def test_send_missing_key_env(monkeypatch, capsys):
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.delenv("GAS_BRIDGE_KEY", raising=False)
    rc = gmail_connector.send("a@b.com", "subj", "body")
    assert rc == 2
    err = capsys.readouterr().err
    assert "GAS_BRIDGE_KEY" in err or "GAS_BRIDGE_URL/KEY" in err


def test_send_bridge_error_response(monkeypatch, capsys):
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")
    captured: list = []
    fake = _fake_urlopen(captured, {"error": "missing 'to'"})
    with patch.object(gmail_connector.urllib.request, "urlopen", fake):
        rc = gmail_connector.send("rcpt@example.com", "subj", "body")
    assert rc == 1
    err = capsys.readouterr().err
    assert "missing 'to'" in err


def test_send_extracts_file_lines_as_bridge_attachments(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "primes.py"
    src.write_bytes(b"print(2)\n")
    captured: list = []
    fake = _fake_urlopen(captured, {"status": "sent"})
    body = "Here you go.\nATTACHED FILE: ./primes.py"
    with patch.object(gmail_connector.urllib.request, "urlopen", fake):
        rc = gmail_connector.send("rcpt@example.com", "GERRY", body)
    assert rc == 0
    payload = captured[0]["body"]
    assert payload["body"] == "Here you go."  # FILE: line stripped from body
    # Bridge schema: a.name, a.data, a.mimeType — NOT filename/content_base64.
    # Mismatch produces a "newBlob on object Utilities" error in GAS.
    assert payload["attachments"] == [{
        "name": "primes.py",
        "data": base64.b64encode(b"print(2)\n").decode("ascii"),
        "mimeType": "text/x-python",
    }]


def test_send_unreadable_file_logs_and_continues(monkeypatch, capsys):
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")
    captured: list = []
    fake = _fake_urlopen(captured, {"status": "sent"})
    body = "Cover note.\nATTACHED FILE: ./does-not-exist.bin"
    with patch.object(gmail_connector.urllib.request, "urlopen", fake):
        rc = gmail_connector.send("rcpt@example.com", "S", body)
    assert rc == 0
    err = capsys.readouterr().err
    assert "failed to read attachment" in err
    payload = captured[0]["body"]
    assert "attachments" not in payload  # nothing successfully read
    assert payload["body"] == "Cover note."


def test_main_argparse(monkeypatch):
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")
    captured: list = []
    fake = _fake_urlopen(captured, {"status": "sent"})
    with patch.object(gmail_connector.urllib.request, "urlopen", fake):
        rc = gmail_connector.main([
            "--to", "x@y.com", "--subject", "S", "--body", "B",
        ])
    assert rc == 0
    assert captured[0]["body"]["to"] == "x@y.com"
    assert captured[0]["body"]["subject"] == "S"
    assert captured[0]["body"]["body"] == "B"
