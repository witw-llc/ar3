"""`a8s stop --force` against real node processes.

Every node here runs under a throwaway a8s home, so these tests never signal a
process that another test or a real node owns. The wake child ignores SIGTERM,
the case a second SIGTERM alone cannot end."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ar3.ulid import new as new_ulid

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="needs SIGSTOP and process groups; the Windows escalation is covered by "
    "the unit tests in test_node_status.py",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
A8S = [sys.executable, str(REPO_ROOT / "apps" / "a8s" / "a8s.py")]

STUBBORN_WAKE = """
import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
helper = subprocess.Popen([sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"])
out = Path(sys.argv[1])
out.with_suffix(".tmp").write_text(f"{os.getpid()} {helper.pid}")
out.with_suffix(".tmp").rename(out)
print("wake started", flush=True)
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        return True
    return bool(state) and not state.startswith("Z")


class Rig:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.root = tmp_path / "node"
        self.root.mkdir()
        self.env = {**os.environ, "A8S_HOME": str(self.home), "HOME": str(tmp_path)}
        self.env.pop("TELL_OUTBOX_DIR", None)
        self.pids_file = tmp_path / "wake.pids"
        self.add_node("n1", self.root, self.pids_file)
        assert self.a8s("config", "set", "loop_interval", "0.2").returncode == 0
        self.agent = self.home / "agents" / "n1"

    def add_node(self, name: str, root: Path, pids_file: Path) -> None:
        root.mkdir(exist_ok=True)
        definition = self.tmp / f"{name}-stubborn.json"
        definition.write_text(json.dumps({
            "invoke": [sys.executable, "-c", STUBBORN_WAKE, str(pids_file)],
        }))
        assert self.a8s("add", name, str(root), str(definition)).returncode == 0

    def a8s(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*A8S, *args], env=self.env, capture_output=True, text=True, timeout=timeout
        )

    def handler_pid(self) -> int:
        return int((self.agent / "pid").read_text())

    def wait_for(self, predicate, what: str, seconds: float = 20) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        pytest.fail(f"timed out waiting for {what}")

    def wait_attached(self, *names: str) -> None:
        """The pid file alone can lead the handler's inbox: a message dropped
        in before the directory exists fails on a slow runner."""
        for name in names:
            agent = self.home / "agents" / name
            self.wait_for(
                lambda a=agent: (a / "pid").is_file() and (a / "inbox").is_dir(),
                f"{name} to attach with its inbox",
            )
            self.wait_for(
                lambda a=agent: _alive(int((a / "pid").read_text() or 0)),
                f"{name}'s handler to be alive",
            )

    def send(self, name: str, content: str = "hold the wake") -> str:
        msg_id = new_ulid()
        inbox = self.home / "agents" / name / "inbox"
        tmp = inbox / f".{msg_id}.tmp"
        tmp.write_text(json.dumps({
            "id": msg_id, "date": "2026-04-29T12:00:00Z", "from": "y", "to": name,
            "content": content, "files": [],
        }))
        tmp.rename(inbox / f"{msg_id}.json")
        return msg_id

    def start_with_a_wake_in_flight(self) -> str:
        started = self.a8s("start", "n1")
        assert started.returncode == 0, started.stderr
        self.wait_attached("n1")
        msg_id = self.send("n1")
        self.wait_for(self.pids_file.is_file, "the wake to start")
        self.wait_for((self.agent / "wake-pid").is_file, "the wake record")
        return msg_id

    def wake_pids(self) -> list[int]:
        return [int(p) for p in self.pids_file.read_text().split()]


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    for pids_file in tmp_path.glob("*.pids"):
        for pid in [int(p) for p in pids_file.read_text().split()]:
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
    for agent in (r.home / "agents").glob("*"):
        if (agent / "pid").is_file():
            try:
                pid = int((agent / "pid").read_text())
                os.kill(pid, signal.SIGCONT)
                os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass


def test_force_ends_a_node_whose_wake_ignores_sigterm_within_seconds(rig):
    msg_id = rig.start_with_a_wake_in_flight()
    wake, helper = rig.wake_pids()
    begun = time.monotonic()
    stopped = rig.a8s("stop", "n1", "--force")
    elapsed = time.monotonic() - begun
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert elapsed < 10, f"stop --force took {elapsed:.1f}s"
    assert not (rig.agent / "pid").exists()
    rig.wait_for(lambda: not _alive(wake) and not _alive(helper), "the wake group to end", 5)
    assert (rig.agent / "inbox" / f"{msg_id}.json").is_file()
    assert not (rig.agent / "wake-retry").exists()
    assert "backoff cleared" in stopped.stdout


def test_force_kills_a_handler_that_cannot_act_on_a_signal(rig):
    """A stopped process holds every signal but SIGKILL pending, which is what
    a handler blocked in a call that never returns looks like to `a8s stop`."""
    msg_id = rig.start_with_a_wake_in_flight()
    handler = rig.handler_pid()
    wake, helper = rig.wake_pids()
    os.kill(handler, signal.SIGSTOP)

    begun = time.monotonic()
    stopped = rig.a8s("stop", "n1", "--force")
    elapsed = time.monotonic() - begun
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert elapsed < 20, f"stop --force took {elapsed:.1f}s"
    assert "killing it" in stopped.stderr
    rig.wait_for(
        lambda: not any(_alive(p) for p in (handler, wake, helper)),
        "the handler and its wake to die",
        5,
    )
    assert not (rig.agent / "pid").exists()
    assert not (rig.agent / "wake-pid").exists()
    assert (rig.agent / "inbox" / f"{msg_id}.json").is_file()
    assert not (rig.agent / "trash" / f"{msg_id}.json").exists()
    assert "returned 1 message(s) from the killed wake" in stopped.stdout

    ps = rig.a8s("ps")
    assert "no nodes running" in ps.stdout
    restarted = rig.a8s("start", "n1")
    assert restarted.returncode == 0, restarted.stderr
    rig.wait_for((rig.agent / "pid").is_file, "the node to attach again")
    assert rig.a8s("stop", "n1", "--force").returncode == 0


def test_a_plain_stop_waits_for_the_wake_and_does_not_kill_it(rig):
    rig.start_with_a_wake_in_flight()
    handler = rig.handler_pid()
    wake, _helper = rig.wake_pids()
    plain = subprocess.Popen(
        [*A8S, "stop", "n1"], env=rig.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        time.sleep(3)
        assert plain.poll() is None, "a plain stop must keep waiting for the wake"
        assert _alive(handler) and _alive(wake)
    finally:
        plain.kill()
        plain.wait()
    assert rig.a8s("stop", "n1", "--force").returncode == 0


def test_force_stopping_one_member_of_a_shared_handler_ends_the_other_members_wake(rig):
    """One handler serves both members, so killing it ends the wake of the
    member that was not named too. That member's mail goes back to its inbox
    and its backoff clears, as it would had both been named."""
    n2_pids = rig.tmp / "n2-wake.pids"
    rig.add_node("n2", rig.tmp / "node2", n2_pids)
    n2 = rig.home / "agents" / "n2"
    assert rig.a8s("alias", "pair", "n1").returncode == 0
    assert rig.a8s("alias", "pair", "n2").returncode == 0
    started = rig.a8s("start", "pair")
    assert started.returncode == 0, started.stderr
    rig.wait_attached("n1", "n2")
    handler = rig.handler_pid()
    assert int((n2 / "pid").read_text()) == handler

    msg_id = rig.send("n2")
    rig.wait_for(n2_pids.is_file, "n2's wake to start")
    rig.wait_for((n2 / "wake-pid").is_file, "n2's wake record")
    wake, helper = [int(p) for p in n2_pids.read_text().split()]
    os.kill(handler, signal.SIGSTOP)

    stopped = rig.a8s("stop", "n1", "--force")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert "killing it" in stopped.stderr
    assert "n2: stopped too" in stopped.stdout
    rig.wait_for(
        lambda: not any(_alive(p) for p in (handler, wake, helper)),
        "the handler and n2's wake to die",
        5,
    )
    assert not (rig.agent / "pid").exists()
    assert not (n2 / "pid").exists()
    assert not (n2 / "wake-pid").exists()
    assert (n2 / "inbox" / f"{msg_id}.json").is_file()
    assert not (n2 / "trash" / f"{msg_id}.json").exists()
    assert "n2: returned 1 message(s) from the killed wake" in stopped.stdout
    assert "no nodes running" in rig.a8s("ps").stdout
