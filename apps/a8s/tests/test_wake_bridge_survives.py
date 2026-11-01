"""A resident bridge a wake started outlives `a8s stop --force` of that wake;
the wake's other processes do not.

The wake has the shape of an r4t resident turn: its enqueuer starts the
bridge with `engines.resident._start_bridge` and exits, so the bridge's
parent is gone. On POSIX the bridge leads its own session and the wake's
group kill misses it. On Windows the bridge broke away from the wake's job
object, so ending the job misses it, and `taskkill /T` cannot reach it.
A plain child the enqueuer left behind is the control: it must die. A real
node under a throwaway a8s home, on every platform."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ar3.fsio import read_text_retry
from ar3.proc import pid_alive
from ar3.ulid import new as new_ulid
from packaged_python import launcher_led

REPO_ROOT = Path(__file__).resolve().parents[3]
A8S = [sys.executable, str(REPO_ROOT / "apps" / "a8s" / "a8s.py")]

ENQUEUER = r"""
import subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
sys.path.insert(0, sys.argv[3])
from engines import resident
sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
bridge = resident._start_bridge(sleeper, close_fds=True, **quiet)
plain = subprocess.Popen(sleeper, **quiet)
out = Path(sys.argv[1])
out.with_suffix(".tmp").write_text(f"{bridge.pid} {plain.pid}")
out.with_suffix(".tmp").replace(out)
"""

WAKE = r"""
import subprocess, sys, time
subprocess.run([sys.executable, "-c", sys.argv[1], *sys.argv[2:]], check=True)
time.sleep(120)
"""

# The bridge's launching process is the wake leader itself, alive when the
# stop lands: `taskkill /T` on the leader would follow that parent link.
LAUNCHER_STAYS = r"""
import sys, time
code = sys.argv.pop(1)
exec(code, {"__name__": "enqueuer"})
time.sleep(120)
"""


def _wait_for(predicate, what, seconds=30):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(0.05)


def _end(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


@launcher_led
def test_a_bridge_survives_a_forced_stop_of_the_wake_that_started_it(tmp_path):
    _forced_stop_leaves_the_bridge(tmp_path, WAKE)


@pytest.mark.skipif(os.name != "nt", reason="the parent link only matters to taskkill /T")
@launcher_led
def test_a_bridge_survives_a_forced_stop_while_its_launching_process_lives(tmp_path):
    _forced_stop_leaves_the_bridge(tmp_path, LAUNCHER_STAYS)


def _forced_stop_leaves_the_bridge(tmp_path, wake):
    home = tmp_path / "home"
    root = tmp_path / "node"
    root.mkdir()
    env = {**os.environ, "A8S_HOME": str(home), "HOME": str(tmp_path)}
    env.pop("TELL_OUTBOX_DIR", None)
    pids = tmp_path / "bridge.pids"

    def a8s(*args):
        return subprocess.run([*A8S, *args], env=env, capture_output=True, text=True, timeout=60)

    definition = tmp_path / "wake.json"
    definition.write_text(json.dumps({"invoke": [
        sys.executable, "-c", wake, ENQUEUER, str(pids),
        str(REPO_ROOT / "apps" / "r4t"), str(REPO_ROOT / "lib"),
    ]}))
    assert a8s("add", "n1", str(root), str(definition)).returncode == 0
    assert a8s("config", "set", "loop_interval", "0.2").returncode == 0
    agent = home / "agents" / "n1"
    bridge = plain = None
    try:
        started = a8s("start", "n1")
        assert started.returncode == 0, started.stderr
        _wait_for(lambda: (agent / "pid").is_file() and (agent / "inbox").is_dir(), "the node")
        msg_id = new_ulid()
        tmp = agent / "inbox" / f".{msg_id}.tmp"
        tmp.write_text(json.dumps({
            "id": msg_id, "date": "2026-04-29T12:00:00Z", "from": "y", "to": "n1",
            "content": "start the bridge", "files": [],
        }))
        tmp.rename(agent / "inbox" / f"{msg_id}.json")
        _wait_for(pids.is_file, "the bridge")
        bridge, plain = (int(p) for p in read_text_retry(pids).split())
        assert pid_alive(bridge) and pid_alive(plain)

        stopped = a8s("stop", "n1", "--force")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        _wait_for(lambda: not pid_alive(plain), "the wake's plain child to end", 10)
        time.sleep(0.5)
        assert pid_alive(bridge), "the forced stop of the wake ended the bridge"
    finally:
        for pid in (bridge, plain):
            if pid:
                _end(pid)
        if (agent / "pid").is_file():
            a8s("stop", "n1", "--force")
