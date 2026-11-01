"""`a8s stop`, `stop --force`, `kill` and `restart` on Windows, against real
node processes on POSIX.

On Windows `os.kill` is `TerminateProcess`: it ends the target outright, and
no handler runs. A launcher forces the Windows branches of the CLI and the
handler (`IS_WINDOWS`) and makes `os.kill(pid, SIGTERM)` a SIGKILL, which is
what that call does there. The process launches stay POSIX, so the wake's
tree is ended by the foundation's group kill here; the `taskkill /T` arm it
uses on Windows is covered in apps/ar3/tests/test_launch_options.py. The
POSIX freeze (SIGSTOP) and the real-process kills of a frozen handler's
children are here too. The in-process tests that need no POSIX signal are in
test_windows_stop.py.

Modes: `windows` is this release on Windows; `deaf` is a handler that reads
no stop request (one from before it); `old-cli` is a stop from before it,
which signalled the handler and so ended it outright."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_stop_force import A8S, REPO_ROOT, Rig, _alive
from test_windows_stop import _unit_in_trash

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="emulates TerminateProcess with SIGKILL, freezes with SIGSTOP and ends wakes by process group",
)

LAUNCHER = """
import os, signal, sys
sys.path.insert(0, sys.argv.pop(1))
mode = sys.argv.pop(1)
import core
import commands, daemon
from ar3 import proc

posix_options = proc.launch_options
def launch_options(**kw):
    kw.pop("windows", None)
    return posix_options(**kw)
commands.launch_options = daemon.launch_options = launch_options

posix_kill = os.kill
def terminate_process(pid, sig):
    posix_kill(pid, signal.SIGKILL if sig == signal.SIGTERM else sig)
os.kill = terminate_process

commands.IS_WINDOWS = mode != "old-cli"
daemon.IS_WINDOWS = mode != "deaf"
commands.STOP_FORCE_GRACE_S = float(os.environ.get("TEST_STOP_FORCE_GRACE_S", "8"))
from cli import main
sys.exit(main(sys.argv[1:]))
"""

WAKE = """
import os, subprocess, sys, time
from pathlib import Path
seconds = float(sys.argv[2])
child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
out = Path(sys.argv[1])
out.with_suffix(".tmp").write_text(f"{os.getpid()} {child.pid}")
out.with_suffix(".tmp").rename(out)
time.sleep(seconds)
child.wait()
"""


PIPE_WRITING_WAKE = """
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
out = Path(sys.argv[1])
out.with_suffix(".tmp").write_text(f"{os.getpid()} {child.pid}")
out.with_suffix(".tmp").rename(out)
while True:
    try:
        os.write(1, b"wake still working\\n")
    except BrokenPipeError:
        os._exit(0)
    time.sleep(0.001)
"""

DELAYED_CLI = """
import sys, time
sys.path.insert(0, sys.argv.pop(1))
import core, commands
original = commands._wait_handlers_stopped
def wait(*args):
    result = original(*args)
    time.sleep(0.2)
    return result
commands._wait_handlers_stopped = wait
from cli import main
sys.exit(main(sys.argv[1:]))
"""


class WindowsRig(Rig):
    def __init__(self, tmp_path: Path, wake_seconds: float, wake_script: str | None = None):
        self.wake_seconds = wake_seconds
        self.wake_script = wake_script
        super().__init__(tmp_path)
        self.launcher = tmp_path / "windows_launcher.py"
        self.launcher.write_text(LAUNCHER)
        self.handler: subprocess.Popen | None = None

    def add_node(self, name: str, root: Path, pids_file: Path) -> None:
        root.mkdir(exist_ok=True)
        definition = self.tmp / f"{name}-wake.json"
        definition.write_text(json.dumps({
            "invoke": [sys.executable, "-c", self.wake_script or WAKE, str(pids_file), str(self.wake_seconds)],
        }))
        assert self.a8s("add", name, str(root), str(definition)).returncode == 0

    def emulated(self, mode: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.launcher), str(REPO_ROOT / "apps" / "a8s"), mode, *args],
            env={**self.env, "TEST_STOP_FORCE_GRACE_S": "1"},
            capture_output=True, text=True, timeout=timeout,
        )

    def run_handler(self, mode: str) -> str:
        """Attach n1 under an emulated handler and put a wake in flight.
        Returns the id of the message the wake took."""
        log = (self.tmp / "handler.log").open("w")
        self.handler = subprocess.Popen(
            [sys.executable, str(self.launcher), str(REPO_ROOT / "apps" / "a8s"), mode, "run", "n1"],
            env=self.env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        # Reaped the moment it exits, as an `a8s start` handler is by init:
        # an unreaped zombie would read as a live handler to the stop.
        threading.Thread(target=self.handler.wait, daemon=True).start()
        self.wait_attached("n1")
        msg_id = self.send("n1")
        self.wait_for(self.pids_file.is_file, "the wake to start")
        self.wait_for((self.agent / "wake-pid").is_file, "the wake record")
        return msg_id

    def handler_log(self) -> str:
        return (self.tmp / "handler.log").read_text()

    def assert_tree_gone(self, *pids: int, seconds: float = 5) -> None:
        self.wait_for(lambda: not any(_alive(p) for p in pids), "the wake's tree to end", seconds)

    def assert_dead_letter(self, msg_id: str, out: str | None = None) -> None:
        assert (self.agent / "trash" / f"{msg_id}.json").is_file()
        assert not (self.agent / "inbox" / f"{msg_id}.json").exists()
        assert (self.agent / "dead-letters" / f"{msg_id}.json").is_file()
        assert not (self.agent / "wake-pid").exists()
        if out is not None:
            assert f"n1: the killed wake's 1 message(s) are dead letters: {msg_id}" in out

    def assert_left_clean(self) -> None:
        assert not (self.agent / "pid").exists()
        assert not (self.agent / "stop-request").exists()
        assert "no nodes running" in self.a8s("ps").stdout


def _cleanup(r: WindowsRig) -> None:
    if r.handler is not None and r.handler.poll() is None:
        r.handler.kill()
        r.handler.wait()
    for pids_file in r.tmp.glob("*.pids"):
        for pid in [int(p) for p in pids_file.read_text().split()]:
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
    for agent in (r.home / "agents").glob("*"):
        if (agent / "pid").is_file():
            try:
                os.kill(int((agent / "pid").read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass


@pytest.fixture
def long_wake(tmp_path):
    r = WindowsRig(tmp_path, wake_seconds=300)
    yield r
    _cleanup(r)


@pytest.fixture
def piping_wake(tmp_path):
    r = WindowsRig(tmp_path, wake_seconds=300, wake_script=PIPE_WRITING_WAKE)
    yield r
    _cleanup(r)


@pytest.fixture
def short_wake(tmp_path):
    r = WindowsRig(tmp_path, wake_seconds=3)
    yield r
    _cleanup(r)


def test_control_a_terminated_handler_orphans_its_wake(long_wake):
    """The defect, reproduced: a stop that signals the handler on Windows ends
    it outright, reports it stopped, and leaves its wake running with its mail
    never settled."""
    rig = long_wake
    msg_id = rig.run_handler("windows")
    wake, child = rig.wake_pids()
    stopped = rig.emulated("old-cli", "stop", "n1")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert "n1: stopped" in stopped.stdout
    rig.wait_for(lambda: rig.handler.poll() is not None, "the handler to die")
    time.sleep(0.5)
    assert _alive(wake) and _alive(child)
    assert (rig.agent / "wake-pid").is_file()
    assert not (rig.agent / "dead-letters" / f"{msg_id}.json").exists()


def test_plain_stop_waits_for_the_wake_then_detaches(short_wake):
    rig = short_wake
    msg_id = rig.run_handler("windows")
    wake, child = rig.wake_pids()
    stopped = rig.emulated("windows", "stop", "n1")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert not _alive(wake) and not _alive(child)
    assert rig.handler.wait(timeout=5) == 0
    assert f"n1: sent stop request to PID {rig.handler.pid}" in stopped.stdout
    assert "SIGTERM" not in stopped.stdout
    assert "n1: stopped" in stopped.stdout
    assert "received stop request from PID" in rig.handler_log()
    assert (rig.agent / "trash" / f"{msg_id}.json").is_file()
    assert not (rig.agent / "dead-letters" / f"{msg_id}.json").exists()
    assert not (rig.agent / "wake-pid").exists()
    rig.assert_left_clean()


def test_force_stop_ends_the_wake_tree_and_dead_letters_its_mail(long_wake):
    rig = long_wake
    msg_id = rig.run_handler("windows")
    wake, child = rig.wake_pids()
    begun = time.monotonic()
    stopped = rig.emulated("windows", "stop", "n1", "--force")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert time.monotonic() - begun < 10
    rig.assert_tree_gone(wake, child)
    assert rig.handler.wait(timeout=5) == 0
    assert f"n1: sent force stop request to PID {rig.handler.pid}" in stopped.stdout
    assert "killing it" not in stopped.stderr
    assert "force stop request — killing wake subprocess group" in rig.handler_log()
    rig.assert_dead_letter(msg_id, stopped.stdout)
    rig.assert_left_clean()


def test_kill_ends_the_wake_tree_and_dead_letters_its_mail(long_wake):
    rig = long_wake
    msg_id = rig.run_handler("windows")
    wake, child = rig.wake_pids()
    killed = rig.emulated("windows", "kill", "n1")
    assert killed.returncode == 0, killed.stdout + killed.stderr
    rig.assert_tree_gone(wake, child)
    assert rig.handler.wait(timeout=10) == 0
    rig.assert_dead_letter(msg_id)
    rig.assert_left_clean()


def test_restart_force_ends_the_old_wake_before_the_new_handler(long_wake):
    rig = long_wake
    msg_id = rig.run_handler("windows")
    wake, child = rig.wake_pids()
    old = rig.handler.pid
    restarted = rig.emulated("windows", "restart", "n1", "--force")
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    rig.assert_tree_gone(wake, child)
    rig.assert_dead_letter(msg_id, restarted.stdout)
    rig.wait_for((rig.agent / "pid").is_file, "the node to attach again")
    assert rig.handler_pid() != old


def test_a_handler_that_reads_no_request_is_killed_with_its_wake(long_wake):
    """A handler from before the stop request (or one wedged past taking it)
    is ended outright, its wake's tree first, and the wake's mail is settled
    as the forced stop settles it."""
    rig = long_wake
    msg_id = rig.run_handler("deaf")
    wake, child = rig.wake_pids()
    begun = time.monotonic()
    stopped = rig.emulated("windows", "stop", "n1")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert time.monotonic() - begun < 10
    rig.assert_tree_gone(wake, child)
    assert rig.handler.wait(timeout=5) == -signal.SIGKILL
    assert "did not take the stop request within 1s — killing it" in stopped.stderr
    rig.assert_dead_letter(msg_id, stopped.stdout)
    rig.assert_left_clean()


def test_a_wake_the_unheard_handler_starts_during_the_grace_is_killed_too(long_wake):
    """A deaf handler idle when the stop reads its wake can start one before
    the grace ends. The stop kills the handler, then reads the wake from its
    record: the late wake's tree ends and its mail is dead-lettered."""
    rig = long_wake
    rig.handler = subprocess.Popen(
        [sys.executable, str(rig.launcher), str(REPO_ROOT / "apps" / "a8s"), "deaf", "run", "n1"],
        env=rig.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    threading.Thread(target=rig.handler.wait, daemon=True).start()
    rig.wait_attached("n1")
    stop = subprocess.Popen(
        [sys.executable, str(rig.launcher), str(REPO_ROOT / "apps" / "a8s"), "windows", "stop", "n1"],
        env={**rig.env, "TEST_STOP_FORCE_GRACE_S": "3"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        rig.wait_for((rig.agent / "stop-request").is_file, "the stop request")
        msg_id = rig.send("n1")
        rig.wait_for(rig.pids_file.is_file, "the wake to start during the grace")
        rig.wait_for((rig.agent / "wake-pid").is_file, "the wake record")
        wake, child = rig.wake_pids()
        out, err = stop.communicate(timeout=30)
    finally:
        if stop.poll() is None:
            stop.kill()
            stop.wait()
    assert stop.returncode == 0, out + err
    assert "did not take the stop request within 3s — killing it" in err
    rig.assert_tree_gone(wake, child)
    assert rig.handler.wait(timeout=5) == -signal.SIGKILL
    rig.assert_dead_letter(msg_id, out)
    rig.assert_left_clean()


def test_a_wake_that_exits_when_its_pipe_breaks_leaves_no_child_after_a_windows_stop(piping_wake):
    """The wake writes to the pipe the unheard handler holds and exits the
    moment the handler dies. The handler is frozen until the wake is read, so
    the wake's tree is still there to end."""
    rig = piping_wake
    msg_id = rig.run_handler("deaf")
    wake, child = rig.wake_pids()
    stopped = rig.emulated("windows", "stop", "n1")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    rig.assert_tree_gone(wake, child)
    assert rig.handler.wait(timeout=5) == -signal.SIGKILL
    rig.assert_dead_letter(msg_id, stopped.stdout)
    rig.assert_left_clean()


def test_posix_force_stop_of_a_frozen_handler_leaves_no_child_when_the_cli_pauses(piping_wake):
    """A POSIX handler that is stopped (SIGSTOP) cannot act on a signal. The
    force stop kills it outright, and a 200 ms pause in the CLI after the
    handler wait must not let the wake's pipe break before the wake is read."""
    rig = piping_wake
    log = (rig.tmp / "handler.log").open("w")
    rig.handler = subprocess.Popen(
        [*A8S, "run", "n1"], env=rig.env, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    threading.Thread(target=rig.handler.wait, daemon=True).start()
    rig.wait_attached("n1")
    msg_id = rig.send("n1")
    rig.wait_for(rig.pids_file.is_file, "the wake to start")
    rig.wait_for((rig.agent / "wake-pid").is_file, "the wake record")
    wake, child = rig.wake_pids()
    os.kill(rig.handler.pid, signal.SIGSTOP)
    launcher = rig.tmp / "delayed_cli.py"
    launcher.write_text(DELAYED_CLI)
    stopped = subprocess.run(
        [sys.executable, str(launcher), str(REPO_ROOT / "apps" / "a8s"), "stop", "n1", "--force"],
        env=rig.env, capture_output=True, text=True, timeout=60,
    )
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    time.sleep(0.3)
    rig.assert_tree_gone(wake, child)
    assert rig.handler.wait(timeout=5) == -signal.SIGKILL
    rig.assert_dead_letter(msg_id, stopped.stdout)


def test_posix_stop_still_signals_and_waits_for_the_wake(short_wake):
    """The negative control: a POSIX stop sends SIGTERM, writes no stop
    request, and lets the wake finish and settle."""
    rig = short_wake
    started = rig.a8s("start", "n1")
    assert started.returncode == 0, started.stderr
    rig.wait_attached("n1")
    msg_id = rig.send("n1")
    rig.wait_for(rig.pids_file.is_file, "the wake to start")
    wake, child = rig.wake_pids()
    handler = rig.handler_pid()
    stopped = rig.a8s("stop", "n1")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert f"n1: sent SIGTERM to PID {handler}" in stopped.stdout
    assert "stop request" not in stopped.stdout
    assert not _alive(wake) and not _alive(child)
    assert (rig.agent / "trash" / f"{msg_id}.json").is_file()
    assert not (rig.agent / "dead-letters" / f"{msg_id}.json").exists()
    rig.assert_left_clean()


def test_an_unconfirmed_freeze_is_cancelled_and_reads_false(monkeypatch):
    from commands import _suspend_process

    sent = []
    monkeypatch.setattr("commands.os.kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr("commands._process_stopped", lambda pid: False)
    monkeypatch.setattr("commands.STOP_SUSPEND_CONFIRM_S", 0.05)
    assert _suspend_process(10) is False
    assert sent == [(10, signal.SIGSTOP), (10, signal.SIGCONT)]


def test_a_confirmed_freeze_is_not_resumed(monkeypatch):
    from commands import _suspend_process

    sent = []
    states = iter([False, False, True])
    monkeypatch.setattr("commands.os.kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr("commands._process_stopped", lambda pid: next(states))
    assert _suspend_process(10) is True
    assert sent == [(10, signal.SIGSTOP)]


def test_a_refused_freeze_reads_false(monkeypatch):
    from commands import _suspend_process

    def refuse(pid, sig):
        raise PermissionError()

    monkeypatch.setattr("commands.os.kill", refuse)
    assert _suspend_process(10) is False


def test_posix_freeze_checks_the_stopped_state_of_a_real_process():
    from commands import _process_stopped, _resume_process, _suspend_process

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert _process_stopped(sleeper.pid) is False
        assert _suspend_process(sleeper.pid) is True
        assert _process_stopped(sleeper.pid) is True
        assert _resume_process(sleeper.pid) is True
    finally:
        sleeper.kill()
        sleeper.wait()


# ---------- real processes, no CLI wrapper ----------

PIPE_WAKE = r"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], stdout=subprocess.DEVNULL)
open(sys.argv[1], "w").write(f"{os.getpid()} {child.pid}")
while True:
    try:
        os.write(1, b"wake still working\n")
    except BrokenPipeError:
        os._exit(0)
    time.sleep(0.001)
"""

GRANDPARENT_CHILD = r"""
import subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
open(sys.argv[1], "w").write(str(g.pid))
time.sleep(300)
"""

BUSY_HANDLER = r"""
import signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
mode, out, helper = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
sleeper = [sys.executable, "-c", "import time; time.sleep(300)"]
if mode == "pipe":
    subprocess.Popen([sys.executable, helper, str(out) + ".wake"],
                     stdout=subprocess.PIPE, start_new_session=True)
elif mode == "unrecorded":
    kid = subprocess.Popen([sys.executable, helper, str(out) + ".grand"])
    out.write_text(str(kid.pid))
out.with_suffix(".ready").write_text("ready")
while True:
    if mode == "forking":
        kid = subprocess.Popen(sleeper)
        with open(str(out) + ".kids", "a") as f:
            f.write(f"{kid.pid}\n")
    time.sleep(0.01)
"""


class RealHandler:
    """A fake handler process the stop path acts on directly. It ignores
    SIGTERM, holds the node's pid file, and starts the processes `mode` names."""

    def __init__(self, tmp_path: Path, mode: str):
        from core import pid_path
        from registry import save_registry

        self.out = tmp_path / f"{mode}.out"
        script, helper = tmp_path / "busy_handler.py", tmp_path / "helper.py"
        script.write_text(BUSY_HANDLER)
        helper.write_text(PIPE_WAKE if mode == "pipe" else GRANDPARENT_CHILD)
        self.proc = subprocess.Popen(
            [sys.executable, str(script), mode, str(self.out), str(helper)],
            start_new_session=True,
        )
        threading.Thread(target=self.proc.wait, daemon=True).start()
        root = tmp_path / "root"
        root.mkdir()
        save_registry({"alpha": {"root": str(root)}})
        pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
        pid_path("alpha").write_text(str(self.proc.pid))
        self.wait_for(self.out.with_suffix(".ready").is_file, "the handler")

    def wait_for(self, predicate, what, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        pytest.fail(f"timed out waiting for {what}")

    def pids(self, suffix: str) -> list[int]:
        path = Path(str(self.out) + suffix)
        text = path.read_text() if path.is_file() else ""
        return [int(p) for p in text.split()]

    def stop(self, reason="test"):
        from commands import _kill_outright

        return _kill_outright({self.proc.pid: "alpha"}, {}, ["alpha"], ["alpha"], reason)

    def everything(self) -> list[int]:
        found = [self.proc.pid, *self.pids(".wake"), *self.pids(".grand"), *self.pids(".kids")]
        if self.out.is_file() and self.out.read_text().isdigit():
            found.append(int(self.out.read_text()))
        return found


@pytest.fixture
def real_handler(fake_home, tmp_path, monkeypatch):
    made = []

    def make(mode):
        handler = RealHandler(tmp_path, mode)
        made.append(handler)
        return handler

    monkeypatch.setattr("commands.IS_WINDOWS", False)
    monkeypatch.setattr("commands.STOP_POLL_S", 0.01)
    yield make
    for handler in made:
        for pid in handler.everything():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def test_a_wake_that_exits_on_a_broken_pipe_leaves_no_child(real_handler):
    """The wake writes to a pipe the handler holds and exits the moment the
    handler dies. The stop freezes the handler and ends the wake's group while
    its leader is alive, so the child it started dies with it."""
    from core import dead_letters_dir, wake_pid_path, write_wake_pid

    handler = real_handler("pipe")
    handler.wait_for(lambda: len(handler.pids(".wake")) == 2, "the wake and its child")
    wake, child = handler.pids(".wake")
    _unit_in_trash("alpha", "01PIPE.json")
    write_wake_pid("alpha", wake, ["01PIPE.json"])
    assert handler.stop() is True
    handler.wait_for(lambda: not any(_alive(p) for p in (wake, child, handler.proc.pid)), "all dead")
    assert (dead_letters_dir("alpha") / "01PIPE.json").is_file()
    assert not wake_pid_path("alpha").exists()


def test_an_unrecorded_child_dies_with_its_descendants(real_handler):
    """The handler's child shares the handler's group and has a child of its
    own. No record names either; both die."""
    handler = real_handler("unrecorded")
    handler.wait_for(lambda: len(handler.pids(".grand")) == 1, "the grandchild")
    kid = int(handler.out.read_text())
    (grand,) = handler.pids(".grand")
    assert handler.stop() is True
    handler.wait_for(
        lambda: not any(_alive(p) for p in (kid, grand, handler.proc.pid)), "all dead"
    )


def test_a_handler_that_keeps_starting_children_leaves_none(real_handler, monkeypatch):
    """The listing of the handler's children is held up 300 ms, far longer
    than the handler needs to start another. A frozen handler starts none in
    that time: every child it ever started is dead."""
    import commands

    handler = real_handler("forking")
    handler.wait_for(lambda: len(handler.pids(".kids")) >= 3, "children")
    listing = commands._child_pids

    def slow(pid):
        found = listing(pid)
        time.sleep(0.3)
        return found

    monkeypatch.setattr("commands._child_pids", slow)
    assert handler.stop() is True
    time.sleep(0.2)
    kids = handler.pids(".kids")
    assert kids and not any(_alive(p) for p in kids + [handler.proc.pid])
