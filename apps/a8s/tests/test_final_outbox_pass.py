"""A node that stops routes its outbox once before it detaches.

The real-process test runs a node with a long loop interval, so the only
routing pass that can see mail written just before the stop is the one the
detach makes. The in-process tests pin that a failure in that pass never
holds the node up, and that a loop that does not route does not start."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
A8S = [sys.executable, str(REPO_ROOT / "apps" / "a8s" / "a8s.py")]
TELL = [str(REPO_ROOT / "tell")]


def _run(args, env, **kw):
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60, **kw)


def test_tell_then_immediate_stop_publishes_without_a_restart(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "node"
    root.mkdir()
    folder = tmp_path / "shared"
    folder.mkdir()
    env = {**os.environ, "A8S_HOME": str(home), "HOME": str(tmp_path)}
    env.pop("TELL_OUTBOX_DIR", None)
    assert _run([*A8S, "add", "snd", str(root), "filedrop"], env).returncode == 0
    assert _run([*A8S, "remote", "box", str(folder)], env).returncode == 0

    assert _run([*A8S, "config", "set", "loop_interval", "60"], env).returncode == 0
    started = _run([*A8S, "start", "snd"], env)
    assert started.returncode == 0, started.stderr
    try:
        log = home / "agents" / "snd" / "log.txt"
        deadline = time.time() + 20
        while time.time() < deadline and "attached" not in (
            log.read_text() if log.is_file() else ""
        ):
            time.sleep(0.05)
        time.sleep(1.0)  # the node's first pass has run; the next is 60s away

        told = _run([*TELL, "peer", "written just before the stop"],
                    {**env, "TELL_OUTBOX_DIR": str(root / ".outbox")})
        assert told.returncode == 0, told.stderr
        stopped = _run([*A8S, "stop", "snd"], env)
        assert stopped.returncode == 0, stopped.stderr
    finally:
        _run([*A8S, "stop", "snd", "--force"], env)

    envelopes = list(folder.glob("*.json"))
    assert len(envelopes) == 1, "stop left the outbox mail unrouted"
    assert json.loads(envelopes[0].read_text())["content"] == "written just before the stop"


@pytest.fixture
def one_agent(fake_home, tmp_path):
    from registry import save_registry

    root = tmp_path / "agent"
    root.mkdir()
    save_registry({"A": {"root": str(root)}})
    return root


def _stop_after_two_passes(daemon_mod):
    def watcher():
        deadline = time.time() + 10
        while time.time() < deadline:
            if daemon_mod._STOP_EVENT is not None:
                time.sleep(0.3)
                daemon_mod._STOP_EVENT.set()
                return
            time.sleep(0.02)
    t = threading.Thread(target=watcher)
    t.start()
    return t


def test_a_failing_final_pass_is_logged_and_the_node_still_stops(one_agent, monkeypatch):
    import daemon
    from core import agent_log_path

    calls = []

    def route(handled, **kwargs):
        calls.append(time.monotonic())
        if daemon._STOP_EVENT is not None and daemon._STOP_EVENT.is_set():
            raise RuntimeError("remote went away")

    monkeypatch.setattr(daemon, "route_outboxes", route)
    t = _stop_after_two_passes(daemon)
    assert daemon.attached_loop(["A"], 0.05) == 0
    t.join()
    assert "final outbox pass failed: remote went away" in agent_log_path("A").read_text()
    assert "detached" in agent_log_path("A").read_text()


def test_step_and_drain_do_not_add_a_final_pass(one_agent, monkeypatch):
    import daemon

    calls = []
    monkeypatch.setattr(daemon, "route_outboxes", lambda handled, **kw: calls.append(1))
    assert daemon.attached_loop(["A"], 0.05, single_pass=True) == 0
    assert len(calls) == 1
    calls.clear()
    assert daemon.attached_loop(["A"], 0.05, drain_seconds=0.3) == 0
    assert calls == []


def test_a_final_pass_over_a_routed_file_publishes_nothing_twice(one_agent, tmp_path):
    from mailbox import _write_outbox
    import daemon
    import txlog
    from network import load_remotes
    from registry import participants_from_registry

    folder = tmp_path / "shared"
    folder.mkdir()
    from network import network_config_path

    network_config_path().write_text(json.dumps({
        "remotes": {"box": {"transport": "folder", "path": str(folder), "poll_seconds": 1}},
        "services": {"box": {"service": "sync_folder", "url": str(folder)}},
    }))
    out = _write_outbox("A", one_agent, "peer", "once", [])
    assert daemon.attached_loop(["A"], 0.05, single_pass=True) == 0
    assert [p.name for p in folder.glob("*.json")] == [out.name]
    daemon._route_outboxes_once_more(["A"], os.getpid(), None, [], [])
    assert [p.name for p in folder.glob("*.json")] == [out.name]
    published = [e for e in txlog.read_events(out.stem) if e["event"] == "PUBLISHED"]
    assert len(published) == 1
