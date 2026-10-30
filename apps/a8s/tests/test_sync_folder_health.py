"""A local storage probe must not claim that another machine is syncing."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from commands import cmd_health, cmd_storage
from services.sync_folder import SyncFolderService
from ar3.ulid import new as new_ulid


def test_health_warns_without_peer_writes(fake_home, tmp_path, capsys):
    folder = tmp_path / "shared"
    assert cmd_storage(["shared", str(folder)]) == 0
    capsys.readouterr()
    assert cmd_health() == 1
    text = capsys.readouterr().out
    assert "storage shared: WARN" in text
    assert "local round-trip verified" in text
    assert "no peer write observed" in text
    assert "storage shared: OK" not in text


def _peer_copy(tmp_path, monkeypatch):
    """Exercise the producer on one home, then copy its real output to another."""
    first = tmp_path / "first-copy"
    second = tmp_path / "second-copy"
    monkeypatch.setenv("A8S_HOME", str(tmp_path / "first-home"))
    payload = tmp_path / "note.txt"
    payload.write_text("peer content")
    SyncFolderService("source", url=str(first)).store(payload)
    shutil.copytree(first, second)
    monkeypatch.setenv("A8S_HOME", str(tmp_path / "second-home"))
    return second


def test_health_observes_a_real_peer_write_in_a_replicated_copy(fake_home, tmp_path, monkeypatch, capsys):
    folder = _peer_copy(tmp_path, monkeypatch)
    assert cmd_storage(["shared", str(folder)]) == 0
    capsys.readouterr()
    assert cmd_health() == 0
    text = capsys.readouterr().out
    assert "storage shared: OK (local round-trip verified; newest peer write" in text
    assert "not an end-to-end delivery check" in text
    assert len(list((folder / ".a8s-peers").glob("*.json"))) == 2


def test_new_file_mtime_does_not_refresh_old_peer_evidence(fake_home, tmp_path, monkeypatch, capsys):
    folder = _peer_copy(tmp_path, monkeypatch)
    peer = next((folder / ".a8s-peers").glob("*.json"))
    record = json.loads(peer.read_text())
    record["written_at"] = time.time() - 1000
    peer.write_text(json.dumps(record))
    os.utime(peer, None)
    assert cmd_storage(["shared", str(folder), "--peer-stale-seconds", "60"]) == 0
    capsys.readouterr()
    assert cmd_health() == 1
    text = capsys.readouterr().out
    assert "storage shared: WARN" in text
    assert "stale; limit 60s" in text


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "1.5", "", "True"])
def test_invalid_peer_age_fails_when_configured(fake_home, tmp_path, bad):
    assert cmd_storage(["shared", str(tmp_path / "shared"), "--peer-stale-seconds", bad]) != 0


@pytest.mark.parametrize("bad", ["partial", "nested", "oversized", "future", "nonfinite", "huge-integer", "wrong-writer", "symlink", "fifo"])
def test_invalid_peer_records_never_make_local_probe_green(fake_home, tmp_path, bad):
    folder = tmp_path / "shared"
    svc = SyncFolderService("shared", url=str(folder))
    assert svc.health_observation()[0] is False
    peer = new_ulid()
    path = folder / ".a8s-peers" / f"{peer}.json"
    record = {"version": 1, "writer": peer, "written_at": time.time()}
    if bad == "partial":
        path.write_text('{"version":')
    elif bad == "nested":
        path.write_text("[" * 1500 + "]" * 1500)
    elif bad == "oversized":
        path.write_text(json.dumps(record) + " " * 5000)
    elif bad == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFOs unavailable")
        os.mkfifo(path)
    elif bad == "symlink":
        other = tmp_path / "outside.json"
        other.write_text(json.dumps(record))
        try:
            path.symlink_to(other)
        except OSError:
            pytest.skip("symlinks unavailable")
    else:
        if bad == "future":
            record["written_at"] += 86400
        elif bad == "nonfinite":
            record["written_at"] = float("nan")
        elif bad == "huge-integer":
            record["written_at"] = 10 ** 400
        else:
            record["writer"] = new_ulid()
        path.write_text(json.dumps(record))
    fresh, detail = svc.health_observation()
    assert not fresh and "no peer write observed" in detail


def test_damaged_local_identity_does_not_become_a_new_peer(fake_home, tmp_path):
    import core

    folder = tmp_path / "shared"
    svc = SyncFolderService("shared", url=str(folder))
    svc.health_observation()
    identity = core.resolve_a8s_home() / "sync-folder-writer-id"
    original = identity.read_text()
    identity.write_text("")
    fresh, detail = svc.health_observation()
    assert not fresh and "peer activity unavailable" in detail
    assert identity.read_text() == ""
    identity.write_text(original)
    assert svc.health_observation()[0] is False


def test_observation_failure_does_not_break_attachment_copy(fake_home, tmp_path, monkeypatch):
    folder = tmp_path / "shared"
    svc = SyncFolderService("shared", url=str(folder))
    source = tmp_path / "source.txt"
    source.write_text("attachment")

    def unavailable():
        raise PermissionError("peer metadata unavailable")

    monkeypatch.setattr(svc, "_record_activity", unavailable)
    url = svc.store(source)
    dest = tmp_path / "received.txt"
    assert svc.retrieve(url, dest)
    assert dest.read_text() == "attachment"
    fresh, detail = svc.health_observation()
    assert not fresh and "peer metadata unavailable" in detail


def test_skipped_invalid_service_is_unhealthy(fake_home, tmp_path, capsys):
    from network import save_network_config

    save_network_config({"services": {"broken": {
        "service": "sync_folder", "url": str(tmp_path / "shared"), "peer_stale_seconds": 0,
    }}})
    assert cmd_health() == 1
    text = capsys.readouterr().out
    assert "storage broken: FAIL (not started" in text
    assert "storage: (none configured)" not in text


def test_peer_scan_has_a_work_limit(fake_home, tmp_path, monkeypatch):
    import services.sync_folder as module

    svc = SyncFolderService("shared", url=str(tmp_path / "shared"))
    svc.health_observation()
    peers = tmp_path / "shared" / ".a8s-peers"
    for number in range(3):
        (peers / f"junk{number}").write_text("ignored")
    monkeypatch.setattr(module, "MAX_PEER_RECORDS", 3)
    fresh, detail = svc.health_observation()
    assert not fresh and "limit exceeded" in detail


def test_concurrent_processes_share_one_complete_identity(fake_home, tmp_path, monkeypatch):
    home = tmp_path / "concurrent-home"
    monkeypatch.setenv("A8S_HOME", str(home))
    repo = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(repo / "apps/a8s"), str(repo / "lib")])
    gate = tmp_path / "go"
    code = """
import sys, time
from pathlib import Path
from services.sync_folder import SyncFolderService
gate = Path(sys.argv[1])
deadline = time.monotonic() + 10
while not gate.exists():
    if time.monotonic() > deadline: raise SystemExit('gate timeout')
    time.sleep(.01)
service = SyncFolderService('shared', url=sys.argv[2])
print(service._record_activity())
"""
    workers = [subprocess.Popen(
        [sys.executable, "-c", code, str(gate), str(tmp_path / "shared")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for _ in range(4)]
    try:
        gate.touch()
        results = [worker.communicate(timeout=15) for worker in workers]
        assert all(worker.returncode == 0 for worker in workers), results
        identities = {out.strip() for out, _ in results}
        assert len(identities) == 1
        writer = (home / "sync-folder-writer-id").read_text().strip()
        assert identities == {writer}
        assert len(list((tmp_path / "shared" / ".a8s-peers").glob("*.json"))) == 1
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)
