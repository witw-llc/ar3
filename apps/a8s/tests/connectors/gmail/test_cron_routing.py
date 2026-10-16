"""Inbound-side tests for gmail_cron — mock bridge HTTP and `tell`
shell-out, assert the unread → get → strip → tell → mark-read flow.

The cron is now pure transport: it takes `--from <address>`, polls the
bridge for unread mail from that address, strips Re:/Fwd: from each
subject, and shells `tell <stripped> <body>` from inherited cwd. There
is no registry or definition reading inside the cron — `tell` decides
whether the recipient is a valid participant. Each message id is recorded
in `.gmail-told` before its tell and is never told again."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_CONNECTOR_DIR = Path(__file__).resolve().parent.parent.parent.parent / "connectors" / "gmail"
sys.path.insert(0, str(_CONNECTOR_DIR))

import gmail_cron  # noqa: E402


# ---------- helpers ----------

class _BridgeStub:
    """Records gmail.* POSTs and serves canned responses by action."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[dict] = []

    def __call__(self, url: str, payload: dict) -> dict:
        self.calls.append({"url": url, "payload": payload})
        action = payload.get("action")
        resp = self.responses.get(action)
        if resp is None:
            raise AssertionError(f"unexpected bridge action: {action}")
        if callable(resp):
            return resp(payload)
        return resp


def _calls_for(stub: _BridgeStub, action: str) -> list[dict]:
    return [c for c in stub.calls if c["payload"].get("action") == action]


@pytest.fixture(autouse=True)
def _connector_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")
    return tmp_path


def _told() -> list[str]:
    path = Path(gmail_cron.TOLD_FILE)
    return path.read_text().splitlines() if path.exists() else []


class _Result:
    def __init__(self, returncode: int):
        self.returncode = returncode


def _thread(thread_id: str, message_id: str, subject: str) -> dict:
    return {
        "thread_id": thread_id,
        "messages": [
            {"id": "OLDER", "subject": subject, "plain": "older"},
            {"id": message_id, "subject": subject, "plain": "newest"},
        ],
        "count": 2,
    }


# ---------- tests ----------

def test_happy_path_unread_to_tell(monkeypatch):
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")

    bridge = _BridgeStub({
        "gmail.search": {"messages": [{"id": "T1"}], "count": 1},
        "gmail.get": {
            "thread_id": "T1",
            "messages": [{
                "id": "M1",
                "subject": "Re: NEIL",
                "from": "human@example.com",
                "plain": "thanks for the message",
            }],
            "count": 1,
        },
        "gmail.read": {"status": "marked_read", "thread_id": "T1"},
    })

    tell_calls: list = []

    class _Result:
        returncode = 0

    def fake_run(argv, check=False, **kwargs):
        # Cron must NOT pass cwd explicitly — it inherits from a8s,
        # which sets it to the agent's root for force-stamping.
        assert "cwd" not in kwargs, "cron must not override cwd"
        tell_calls.append({"argv": list(argv), "check": check})
        return _Result()

    with patch.object(gmail_cron, "_bridge_post", bridge), \
         patch.object(gmail_cron.subprocess, "run", fake_run):
        rc = gmail_cron.run("human@example.com")

    assert rc == 0

    # Search query is is:unread from:<from-address>
    search = _calls_for(bridge, "gmail.search")
    assert len(search) == 1
    assert search[0]["payload"]["query"] == "is:unread from:human@example.com"
    assert search[0]["payload"]["count"] == 20
    assert search[0]["payload"]["key"] == "TESTKEY"

    # gmail.get with the thread id
    gets = _calls_for(bridge, "gmail.get")
    assert len(gets) == 1
    assert gets[0]["payload"]["thread_id"] == "T1"

    # tell <stripped-subject> <body>
    assert len(tell_calls) == 1
    assert tell_calls[0]["argv"] == [
        sys.executable,
        str(gmail_cron.A8S_PY),
        "tell",
        "NEIL",
        "thanks for the message",
    ]

    reads = _calls_for(bridge, "gmail.read")
    assert len(reads) == 1
    assert reads[0]["payload"]["thread_id"] == "T1"
    assert _told() == ["M1"]


def test_refused_tell_fails_and_is_not_told_again(capsys):
    """tell exits non-zero for unknown agent names. The message id stays
    recorded, so the next pass only marks the thread read."""
    bridge = _BridgeStub({
        "gmail.search": {"messages": [{"id": "T2"}], "count": 1},
        "gmail.get": _thread("T2", "M2", "Re: NEVER-REGISTERED"),
        "gmail.read": {"status": "marked_read", "thread_id": "T2"},
    })
    tell_calls: list = []

    def fake_run(argv, check=False, **kwargs):
        tell_calls.append(argv)
        return _Result(5)

    with patch.object(gmail_cron, "_bridge_post", bridge), \
         patch.object(gmail_cron.subprocess, "run", fake_run):
        assert gmail_cron.run("human@example.com") == 1
        assert _told() == ["M2"]
        assert "dropped" in capsys.readouterr().err
        assert gmail_cron.run("human@example.com") == 0

    assert len(tell_calls) == 1
    assert _told() == ["M2"]
    assert len(_calls_for(bridge, "gmail.read")) == 2


def test_id_recorded_before_tell():
    bridge = _BridgeStub({
        "gmail.search": {"messages": [{"id": "T4"}], "count": 1},
        "gmail.get": _thread("T4", "M4", "Re: CODER"),
        "gmail.read": {"status": "marked_read", "thread_id": "T4"},
    })
    seen_at_tell: list = []

    def fake_run(argv, check=False, **kwargs):
        seen_at_tell.append(_told())
        return _Result(0)

    with patch.object(gmail_cron, "_bridge_post", bridge), \
         patch.object(gmail_cron.subprocess, "run", fake_run):
        assert gmail_cron.run("human@example.com") == 0

    assert seen_at_tell == [["M4"]]


def test_recorded_message_is_only_marked_read(tmp_path):
    (tmp_path / gmail_cron.TOLD_FILE).write_text("M5\n")
    bridge = _BridgeStub({
        "gmail.search": {"messages": [{"id": "T5"}], "count": 1},
        "gmail.get": _thread("T5", "M5", "Re: CODER"),
        "gmail.read": {"status": "marked_read", "thread_id": "T5"},
    })

    def fake_run(argv, check=False, **kwargs):
        raise AssertionError("a recorded message must not be told again")

    with patch.object(gmail_cron, "_bridge_post", bridge), \
         patch.object(gmail_cron.subprocess, "run", fake_run):
        assert gmail_cron.run("human@example.com") == 0

    assert len(_calls_for(bridge, "gmail.read")) == 1
    assert _told() == ["M5"]


def test_new_reply_in_told_thread_is_told(tmp_path):
    """Gmail marks a thread unread again on a new reply. The record is
    keyed on the message, so the new reply still reaches the agent."""
    (tmp_path / gmail_cron.TOLD_FILE).write_text("M6\n")
    bridge = _BridgeStub({
        "gmail.search": {"messages": [{"id": "T6"}], "count": 1},
        "gmail.get": _thread("T6", "M7", "Re: CODER"),
        "gmail.read": {"status": "marked_read", "thread_id": "T6"},
    })
    tell_calls: list = []

    def fake_run(argv, check=False, **kwargs):
        tell_calls.append(argv[-1])
        return _Result(0)

    with patch.object(gmail_cron, "_bridge_post", bridge), \
         patch.object(gmail_cron.subprocess, "run", fake_run):
        assert gmail_cron.run("human@example.com") == 0

    assert tell_calls == ["newest"]
    assert _told() == ["M6", "M7"]


def test_failed_mark_read_does_not_retell():
    def read_fails(payload):
        return {"error": "quota"}

    bridge = _BridgeStub({
        "gmail.search": {"messages": [{"id": "T8"}], "count": 1},
        "gmail.get": _thread("T8", "M8", "Re: CODER"),
        "gmail.read": read_fails,
    })
    tell_calls: list = []

    def fake_run(argv, check=False, **kwargs):
        tell_calls.append(argv)
        return _Result(0)

    with patch.object(gmail_cron, "_bridge_post", bridge), \
         patch.object(gmail_cron.subprocess, "run", fake_run):
        gmail_cron.run("human@example.com")
        gmail_cron.run("human@example.com")

    assert len(tell_calls) == 1


def test_message_without_id_is_not_told():
    bridge = _BridgeStub({
        "gmail.search": {"messages": [{"id": "T9"}], "count": 1},
        "gmail.get": {"messages": [{"subject": "Re: CODER", "plain": "x"}]},
    })

    def fake_run(argv, check=False, **kwargs):
        raise AssertionError("tell needs a recorded id first")

    with patch.object(gmail_cron, "_bridge_post", bridge), \
         patch.object(gmail_cron.subprocess, "run", fake_run):
        assert gmail_cron.run("human@example.com") == 0

    assert _told() == []


def test_empty_subject_skipped(monkeypatch):
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")

    bridge = _BridgeStub({
        "gmail.search": {"messages": [{"id": "T3"}], "count": 1},
        "gmail.get": {
            "thread_id": "T3",
            "messages": [{
                "id": "M3",
                "subject": "",
                "plain": "no subject",
            }],
            "count": 1,
        },
    })

    tell_calls: list = []

    def fake_run(argv, check=False, **kwargs):
        tell_calls.append(argv)
        raise AssertionError("tell must NOT be called when subject is empty")

    with patch.object(gmail_cron, "_bridge_post", bridge), \
         patch.object(gmail_cron.subprocess, "run", fake_run):
        rc = gmail_cron.run("human@example.com")

    # No attempts → exit 0; left unread.
    assert rc == 0
    assert tell_calls == []
    assert _calls_for(bridge, "gmail.read") == []
    assert _told() == []


def test_no_unread_returns_zero(monkeypatch):
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")

    bridge = _BridgeStub({
        "gmail.search": {"messages": [], "count": 0},
    })

    with patch.object(gmail_cron, "_bridge_post", bridge):
        rc = gmail_cron.run("human@example.com")
    assert rc == 0


def test_missing_env_vars_exit_2(monkeypatch, capsys):
    monkeypatch.delenv("GAS_BRIDGE_URL", raising=False)
    monkeypatch.delenv("GAS_BRIDGE_KEY", raising=False)
    rc = gmail_cron.run("human@example.com")
    assert rc == 2
    err = capsys.readouterr().err
    assert "GAS_BRIDGE" in err


def test_empty_from_address_exit_2(monkeypatch, capsys):
    monkeypatch.setenv("GAS_BRIDGE_URL", "https://example/exec")
    monkeypatch.setenv("GAS_BRIDGE_KEY", "TESTKEY")
    rc = gmail_cron.run("")
    assert rc == 2
    err = capsys.readouterr().err
    assert "--from" in err


def test_main_requires_from_flag(capsys):
    """argparse exits 2 with a usage message when --from is missing."""
    with pytest.raises(SystemExit) as ei:
        gmail_cron.main([])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "--from" in err


def test_main_passes_from_to_run():
    captured: list = []

    def fake_run(addr):
        captured.append(addr)
        return 0

    with patch.object(gmail_cron, "run", fake_run):
        rc = gmail_cron.main(["--from", "human@example.com"])
    assert rc == 0
    assert captured == ["human@example.com"]
