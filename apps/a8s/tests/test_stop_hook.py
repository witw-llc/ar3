"""`stop.invoke`: a definition names how to end a long-lived process its wakes
leave behind, and every verb that releases the node runs it.

Real node processes under a throwaway a8s home, as in test_stop_force.py."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from definitions import build_stop_command

REPO_ROOT = Path(__file__).resolve().parents[3]
A8S = [sys.executable, str(REPO_ROOT / "apps" / "a8s" / "a8s.py")]

# Appends "<recipient> <cwd>" so a test reads who ran it and where.
STOP_SCRIPT = (
    "import os, sys; from pathlib import Path; "
    "Path(sys.argv[1]).open('a').write(sys.argv[2] + ' ' + os.getcwd() + '\\n')"
)


class TestBuildStopCommand:
    def test_none_without_a_stop_block(self):
        assert build_stop_command({"invoke": ["x"]}, "n1") is None
        assert build_stop_command({"invoke": ["x"], "stop": {}}, "n1") is None

    def test_expands_recipient_and_optional_vars(self):
        definition = {"stop": {"invoke": ["r4t", "stop", "--agent", "$RECIPIENT", "--x=$X?"]}}
        assert build_stop_command(definition, "n1") == ["r4t", "stop", "--agent", "n1"]
        assert build_stop_command(definition, "n1", vars={"X": "1"}) == [
            "r4t", "stop", "--agent", "n1", "--x=1",
        ]


pytestmark_posix = pytest.mark.skipif(
    os.name != "posix", reason="drives real node processes and SIGSTOP"
)


class Rig:
    def __init__(self, tmp_path: Path, stop_script: str = STOP_SCRIPT):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.root = tmp_path / "node"
        self.root.mkdir()
        self.ran = tmp_path / "stop.ran"
        self.env = {**os.environ, "A8S_HOME": str(self.home), "HOME": str(tmp_path)}
        self.env.pop("TELL_OUTBOX_DIR", None)
        definition = tmp_path / "hooked.json"
        definition.write_text(json.dumps({
            "invoke": [sys.executable, "-c", "pass"],
            "stop": {"invoke": [sys.executable, "-c", stop_script, str(self.ran), "$RECIPIENT"]},
        }))
        assert self.a8s("add", "n1", str(self.root), str(definition)).returncode == 0
        assert self.a8s("config", "set", "loop_interval", "0.2").returncode == 0
        self.agent = self.home / "agents" / "n1"

    def a8s(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*A8S, *args], env=self.env, capture_output=True, text=True, timeout=60
        )

    def start(self) -> int:
        started = self.a8s("start", "n1")
        assert started.returncode == 0, started.stderr
        return self.attached()

    def attached(self) -> int:
        """The handler's pid once the node has attached. `start` and the
        start half of `restart` return as soon as the child is spawned; the
        pid file lands later, so a verb that reads it waits here first."""
        deadline = time.time() + 20
        while time.time() < deadline:
            if (self.agent / "pid").is_file() and (self.agent / "inbox").is_dir():
                return int((self.agent / "pid").read_text())
            time.sleep(0.05)
        pytest.fail("node did not attach")

    def runs(self) -> list[str]:
        return self.ran.read_text().splitlines() if self.ran.is_file() else []


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    if (r.agent / "pid").is_file():
        try:
            pid = int((r.agent / "pid").read_text())
            os.kill(pid, signal.SIGCONT)
            os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass


@pytestmark_posix
def test_stop_runs_the_hook_once_in_the_node_root(rig):
    rig.start()
    stopped = rig.a8s("stop", "n1")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert rig.runs() == [f"n1 {rig.root.resolve()}"]


@pytestmark_posix
def test_kill_runs_the_hook(rig):
    rig.start()
    killed = rig.a8s("kill", "n1")
    assert killed.returncode == 0, killed.stdout + killed.stderr
    assert rig.runs() == [f"n1 {rig.root.resolve()}"]


@pytestmark_posix
def test_restart_runs_the_hook_for_the_stop_half(rig):
    rig.start()
    restarted = rig.a8s("restart", "n1")
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    assert rig.runs() == [f"n1 {rig.root.resolve()}"]
    rig.attached()
    assert rig.a8s("stop", "n1").returncode == 0
    assert len(rig.runs()) == 2


@pytestmark_posix
def test_a_handler_killed_outright_still_gets_its_hook_run_by_the_stop(rig):
    handler = rig.start()
    os.kill(handler, signal.SIGSTOP)
    stopped = rig.a8s("stop", "n1", "--force")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert "killing it" in stopped.stderr
    assert rig.runs() == [f"n1 {rig.root.resolve()}"]


WINDOWS_TERMINATE = """
import os, runpy, signal, sys
real_kill = os.kill
handler = int(sys.argv.pop(1))
def terminate(pid, sig):
    # Windows os.kill ends the process outright: no handler, no cleanup.
    return real_kill(pid, signal.SIGKILL if pid == handler and sig == signal.SIGTERM else sig)
os.kill = terminate
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(sys.argv[0]))
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _with_windows_terminate(rig, handler: int, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", WINDOWS_TERMINATE, str(handler), str(A8S[-1]), *args],
        env=rig.env, capture_output=True, text=True, timeout=60,
    )


@pytestmark_posix
def test_a_handler_terminated_outright_by_a_plain_stop_gets_its_hook_once(rig):
    handler = rig.start()
    stopped = _with_windows_terminate(rig, handler, "stop", "n1")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert rig.runs() == [f"n1 {rig.root.resolve()}"]


@pytestmark_posix
def test_exit_runs_the_hook_once_for_a_handler_terminated_outright(rig):
    handler = rig.start()
    exited = _with_windows_terminate(rig, handler, "exit")
    assert exited.returncode == 0, exited.stdout + exited.stderr
    assert rig.runs() == [f"n1 {rig.root.resolve()}"]


@pytestmark_posix
def test_exit_runs_the_hook_once_for_a_handler_that_ran_it(rig):
    rig.start()
    assert rig.a8s("exit").returncode == 0
    deadline = time.time() + 10
    while time.time() < deadline and not rig.runs():
        time.sleep(0.05)
    time.sleep(0.5)
    assert rig.runs() == [f"n1 {rig.root.resolve()}"]


@pytestmark_posix
def test_a_takeover_is_not_a_stop_so_only_the_final_release_runs_the_hook(rig):
    rig.start()
    taken = rig.a8s("run", "n1", "--drain", "0.5")
    assert taken.returncode == 0, taken.stdout + taken.stderr
    assert rig.runs() == [f"n1 {rig.root.resolve()}"]


@pytestmark_posix
def test_exit_never_runs_a_second_hook_beside_one_the_handler_is_still_running(tmp_path):
    rig = Rig(tmp_path, stop_script="import time; time.sleep(7); " + STOP_SCRIPT)
    handler = rig.start()
    try:
        assert rig.a8s("exit").returncode == 0
        deadline = time.time() + 20
        while time.time() < deadline and not rig.runs():
            time.sleep(0.1)
        time.sleep(1)
        assert rig.runs() == [f"n1 {rig.root.resolve()}"]
    finally:
        try:
            os.kill(handler, signal.SIGKILL)
        except OSError:
            pass


@pytestmark_posix
def test_force_stop_waits_for_a_hook_the_killed_handler_started_and_never_replays_it(tmp_path):
    rig = Rig(tmp_path, stop_script="import time; time.sleep(10); " + STOP_SCRIPT)
    handler = rig.start()
    try:
        stopped = rig.a8s("stop", "n1", "--force")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "killing it" in stopped.stderr
        assert rig.runs() == [f"n1 {rig.root.resolve()}"]
        time.sleep(1)
        assert len(rig.runs()) == 1
    finally:
        try:
            os.kill(handler, signal.SIGKILL)
        except OSError:
            pass
