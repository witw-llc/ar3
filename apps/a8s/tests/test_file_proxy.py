"""Tests for file-proxy agent type — filesystem delivery instead of CLI invocation."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import (
    Participant,
    agent_log_path,
    inbox_dir,
    touch_last_active,
)
from daemon import attached_loop, maybe_run_idle, wake_once
from definitions import files_ttl_seconds, is_file_proxy
from mailbox import _write_outbox, ensure_mailboxes
from registry import participants_from_registry, save_registry


def _read_log(name: str) -> str:
    return agent_log_path(name).read_text() if agent_log_path(name).is_file() else ""


def _write_file_proxy_def(
    path: Path,
    *,
    timeout: int = 30,
    ttl_hours: int | None = None,
    inbox_dir: str | None = None,
) -> None:
    defn: dict = {"proxy": "file", "idle": {"timeout": timeout}}
    if ttl_hours is not None:
        defn["files_ttl_hours"] = ttl_hours
    if inbox_dir is not None:
        defn["inbox_dir"] = inbox_dir
    path.write_text(json.dumps(defn))


def _proxy_participant(name: str, root: Path, defp: Path) -> Participant:
    save_registry({name: {"root": str(root.resolve()), "definition": str(defp.resolve())}})
    return next(p for p in participants_from_registry() if p.name == name)


class TestIsFileProxy:
    def test_recognizes_file_proxy(self):
        assert is_file_proxy({"proxy": "file"}) is True

    def test_rejects_normal_definition(self):
        assert is_file_proxy({"invoke": ["echo", "hello"]}) is False

    def test_rejects_other_proxy_value(self):
        assert is_file_proxy({"proxy": "mqtt"}) is False

    def test_rejects_empty_definition(self):
        assert is_file_proxy({}) is False


class TestFilesTtlSeconds:
    def test_default_48_hours(self):
        assert files_ttl_seconds({}) == 48 * 3600

    def test_custom_value(self):
        assert files_ttl_seconds({"files_ttl_hours": 24}) == 24 * 3600

    def test_string_value_parses(self):
        assert files_ttl_seconds({"files_ttl_hours": "12"}) == 12 * 3600

    def test_invalid_falls_back_to_48(self):
        assert files_ttl_seconds({"files_ttl_hours": "bogus"}) == 48 * 3600


class TestWakeOnceFileProxy:
    """wake_once should move inbox files to <root>/.inbox/ instead of invoking CLI."""

    def test_delivers_to_root_inbox(self, fake_home, tmp_path):
        d = tmp_path / "agent"
        d.mkdir()
        defp = tmp_path / "proxy.json"
        _write_file_proxy_def(defp)
        save_registry({"PROXY": {"root": str(d), "definition": str(defp)}})
        p = Participant("PROXY", d)
        ensure_mailboxes(p)

        msg = {"id": "test1", "from": "ALICE", "to": "PROXY", "content": "hello", "files": []}
        msg_file = inbox_dir("PROXY") / "test1.json"
        msg_file.write_text(json.dumps(msg))

        wake_once(p, msg_file)

        delivered = d / ".inbox" / "test1.json"
        assert delivered.is_file()
        assert json.loads(delivered.read_text())["content"] == "hello"
        assert "proxy: delivered test1.json" in _read_log("PROXY")

    def test_delivers_to_custom_inbox_dir(self, fake_home, tmp_path):
        d = tmp_path / "agent"
        d.mkdir()
        external = tmp_path / "sync-inbox"
        defp = tmp_path / "proxy.json"
        _write_file_proxy_def(defp, inbox_dir=str(external))
        p = _proxy_participant("PROXY", d, defp)
        ensure_mailboxes(p)

        msg = {"id": "test1", "from": "ALICE", "to": "PROXY", "content": "hello", "files": []}
        msg_file = inbox_dir("PROXY") / "test1.json"
        msg_file.write_text(json.dumps(msg))

        wake_once(p, msg_file)

        delivered = external / "test1.json"
        assert delivered.is_file()
        assert not (d / ".inbox" / "test1.json").exists()

    def test_batch_delivers_all_inbox_files(self, fake_home, tmp_path):
        d = tmp_path / "agent"
        d.mkdir()
        defp = tmp_path / "proxy.json"
        _write_file_proxy_def(defp)
        save_registry({"PROXY": {"root": str(d), "definition": str(defp)}})
        p = Participant("PROXY", d)
        ensure_mailboxes(p)

        for i in range(3):
            msg = {"id": f"msg{i}", "from": "ALICE", "to": "PROXY", "content": f"msg-{i}", "files": []}
            (inbox_dir("PROXY") / f"msg{i}.json").write_text(json.dumps(msg))

        # Trigger with the first file — all three should be delivered
        wake_once(p, inbox_dir("PROXY") / "msg0.json")

        inbox_dest = d / ".inbox"
        delivered = sorted(f.name for f in inbox_dest.iterdir())
        assert delivered == ["msg0.json", "msg1.json", "msg2.json"]

    def test_does_not_invoke_subprocess(self, fake_home, tmp_path, monkeypatch):
        d = tmp_path / "agent"
        d.mkdir()
        defp = tmp_path / "proxy.json"
        _write_file_proxy_def(defp)
        save_registry({"PROXY": {"root": str(d), "definition": str(defp)}})
        p = Participant("PROXY", d)
        ensure_mailboxes(p)

        msg = {"id": "test1", "from": "ALICE", "to": "PROXY", "content": "hi", "files": []}
        msg_file = inbox_dir("PROXY") / "test1.json"
        msg_file.write_text(json.dumps(msg))

        called = []
        monkeypatch.setattr("daemon.run_with_prefix", lambda *a, **kw: called.append(1) or 0)
        wake_once(p, msg_file)
        assert called == []


class TestNormalAgentUnaffected:
    """Normal (non-proxy) agents still invoke CLI as before."""

    def test_normal_agent_invokes_cli(self, fake_home, tmp_path, fixtures_dir):
        d = tmp_path / "normal"
        d.mkdir()
        save_registry({"NORM": {"root": str(d), "definition": str(fixtures_dir / "mock.json")}})
        p = Participant("NORM", d)
        ensure_mailboxes(p)

        msg = {"id": "n1", "from": "BOB", "to": "NORM", "content": "check", "date": "2026-05-01T00:00:00Z", "files": []}
        msg_file = inbox_dir("NORM") / "n1.json"
        msg_file.write_text(json.dumps(msg))

        wake_once(p, msg_file)

        log = _read_log("NORM")
        assert "exec:" in log
        assert "FROM:BOB" in log
        assert not (d / ".inbox").exists()


class TestIdleFileProxy:
    """Idle for file-proxy agents: move remaining inbox, TTL cleanup on .files/."""

    def test_idle_moves_inbox_and_cleans_ttl(self, fake_home, tmp_path):
        d = tmp_path / "agent"
        d.mkdir()
        defp = tmp_path / "proxy.json"
        _write_file_proxy_def(defp, timeout=1, ttl_hours=1)
        save_registry({"PROXY": {"root": str(d), "definition": str(defp)}})
        p = Participant("PROXY", d)
        ensure_mailboxes(p)

        # Seed an inbox message
        msg = {"id": "idle1", "from": "X", "to": "PROXY", "content": "stale", "files": []}
        (inbox_dir("PROXY") / "idle1.json").write_text(json.dumps(msg))

        # Create an old file in .files/
        files_path = d / ".files"
        files_path.mkdir(parents=True, exist_ok=True)
        old_file = files_path / "old.txt"
        old_file.write_text("expired")
        old_mtime = time.time() - 7200  # 2 hours ago
        os.utime(old_file, (old_mtime, old_mtime))

        # Create a fresh file that should survive
        fresh_file = files_path / "fresh.txt"
        fresh_file.write_text("keep")

        # Mark last-active as stale
        touch_last_active("PROXY", datetime.now(timezone.utc) - timedelta(seconds=60))

        fired = maybe_run_idle(p)
        assert fired is True

        # Inbox message moved to .inbox/
        assert (d / ".inbox" / "idle1.json").is_file()
        assert not (inbox_dir("PROXY") / "idle1.json").is_file()

        # Old file removed, fresh file kept
        assert not old_file.is_file()
        assert fresh_file.is_file()

        log = _read_log("PROXY")
        assert "TTL cleanup removed 1 file(s)" in log

    def test_idle_uses_custom_files_dir_for_ttl(self, fake_home, tmp_path):
        d = tmp_path / "agent"
        d.mkdir()
        external_files = tmp_path / "attachment-store"
        defp = tmp_path / "proxy.json"
        defp.write_text(
            json.dumps(
                {
                    "proxy": "file",
                    "idle": {"timeout": 1},
                    "files_ttl_hours": 1,
                    "files_dir": str(external_files),
                }
            )
        )
        p = _proxy_participant("PROXY", d, defp)
        ensure_mailboxes(p)

        external_files.mkdir(parents=True, exist_ok=True)
        old_file = external_files / "old.txt"
        old_file.write_text("expired")
        os.utime(old_file, (time.time() - 7200, time.time() - 7200))

        touch_last_active("PROXY", datetime.now(timezone.utc) - timedelta(seconds=60))
        assert maybe_run_idle(p) is True
        assert not old_file.is_file()

    def test_idle_skips_when_not_yet_expired(self, fake_home, tmp_path):
        d = tmp_path / "agent"
        d.mkdir()
        defp = tmp_path / "proxy.json"
        _write_file_proxy_def(defp, timeout=300)
        save_registry({"PROXY": {"root": str(d), "definition": str(defp)}})
        p = Participant("PROXY", d)
        ensure_mailboxes(p)

        touch_last_active("PROXY", datetime.now(timezone.utc) - timedelta(seconds=10))
        assert maybe_run_idle(p) is False


class TestAttachedLoopFileProxy:
    """Integration: attached_loop handles file-proxy agents end-to-end."""

    def test_routed_message_delivered_via_proxy(self, fake_home, tmp_path, fixtures_dir):
        sender_root = tmp_path / "sender"
        proxy_root = tmp_path / "proxy"
        sender_root.mkdir()
        proxy_root.mkdir()

        defp = tmp_path / "proxy.json"
        _write_file_proxy_def(defp)

        save_registry({
            "SENDER": {"root": str(sender_root), "definition": str(fixtures_dir / "mock.json")},
            "PROXY": {"root": str(proxy_root), "definition": str(defp)},
        })
        ensure_mailboxes(Participant("SENDER", sender_root))
        ensure_mailboxes(Participant("PROXY", proxy_root))

        _write_outbox("SENDER", sender_root, "PROXY", "file-proxy test", [])

        rc = attached_loop(["SENDER", "PROXY"], 0.1, single_pass=True)
        assert rc == 0

        inbox_dest = proxy_root / ".inbox"
        assert inbox_dest.is_dir()
        delivered = list(inbox_dest.iterdir())
        assert len(delivered) == 1
        content = json.loads(delivered[0].read_text())
        assert content["content"] == "file-proxy test"
        assert "proxy: delivered" in _read_log("PROXY")

    def test_custom_inbox_dir_via_attached_loop(self, fake_home, tmp_path, fixtures_dir):
        sender_root = tmp_path / "sender"
        proxy_root = tmp_path / "proxy"
        inbox_external = tmp_path / "drive-inbox"
        sender_root.mkdir()
        proxy_root.mkdir()

        defp = tmp_path / "proxy.json"
        _write_file_proxy_def(defp, inbox_dir=str(inbox_external))

        save_registry({
            "SENDER": {"root": str(sender_root), "definition": str(fixtures_dir / "mock.json")},
            "PROXY": {"root": str(proxy_root), "definition": str(defp)},
        })
        ensure_mailboxes(Participant("SENDER", sender_root))
        ensure_mailboxes(Participant("PROXY", proxy_root))

        _write_outbox("SENDER", sender_root, "PROXY", "custom inbox", [])

        rc = attached_loop(["SENDER", "PROXY"], 0.1, single_pass=True)
        assert rc == 0

        delivered = list(inbox_external.glob("*.json"))
        assert len(delivered) == 1
        assert json.loads(delivered[0].read_text())["content"] == "custom inbox"
        assert not (proxy_root / ".inbox").exists()
