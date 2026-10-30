"""`a8s stop`, `stop --force`, `kill` and `restart` on Windows, against real
node processes on POSIX.

On Windows `os.kill` is `TerminateProcess`: it ends the target outright, and
no handler runs. A launcher forces the Windows branches of the CLI and the
handler (`IS_WINDOWS`) and makes `os.kill(pid, SIGTERM)` a SIGKILL, which is
what that call does there. The process launches stay POSIX, so the wake's
tree is ended by the foundation's group kill here; the `taskkill /T` arm it
uses on Windows is covered in apps/ar3/tests/test_launch_options.py.

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

import txlog

from test_stop_force import A8S, REPO_ROOT, Rig, _alive

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="emulates TerminateProcess with SIGKILL and ends wakes by process group",
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


# ---------- the handler side, in process ----------


def _listen(names, handle, until):
    import daemon

    done = threading.Event()
    thread = threading.Thread(target=daemon._stop_request_listener, args=(names, handle, done))
    thread.start()
    deadline = time.monotonic() + 5
    while not until() and time.monotonic() < deadline:
        time.sleep(0.02)
    done.set()
    thread.join(timeout=2)


@pytest.mark.parametrize("force,already,calls", [
    (False, 0, 1), (True, 0, 2), (True, 1, 1), (False, 1, 0),
])
def test_a_request_is_taken_as_that_many_signals(fake_home, monkeypatch, force, already, calls):
    import daemon
    from core import pid_path, stop_request_path

    monkeypatch.setattr(daemon, "STOP_REQUEST_POLL_S", 0.01)
    monkeypatch.setattr(daemon, "_SIGNAL_COUNT", already)
    pid_path("n1").parent.mkdir(parents=True, exist_ok=True)
    pid_path("n1").write_text(str(os.getpid()))
    seen = []

    def handle(signum, frame, cause=None):
        daemon._SIGNAL_COUNT += 1
        seen.append((signum, cause))

    daemon._write_stop_request("n1", os.getppid(), force)
    _listen(["n1"], handle, lambda: not stop_request_path("n1").exists())
    assert not stop_request_path("n1").exists()
    assert seen == [(signal.SIGTERM, f"stop request from PID {os.getppid()}")] * calls


def test_a_request_for_a_node_another_handler_holds_is_left_alone(fake_home, monkeypatch):
    import daemon
    from core import pid_path, stop_request_path

    monkeypatch.setattr(daemon, "STOP_REQUEST_POLL_S", 0.01)
    pid_path("n1").parent.mkdir(parents=True, exist_ok=True)
    pid_path("n1").write_text(str(os.getppid()))
    monkeypatch.setattr(daemon, "_read_handler_pid", lambda name: os.getppid())
    daemon._write_stop_request("n1", os.getppid(), False)
    rounds = iter(range(20))
    _listen(["n1"], lambda *a, **k: pytest.fail("not this handler's request"),
            lambda: next(rounds, None) is None)
    assert stop_request_path("n1").is_file()


@pytest.mark.parametrize("windows", [True, False])
def test_only_a_windows_handler_listens_and_it_stops_listening_on_detach(
    fake_home, tmp_path, fixtures_dir, monkeypatch, windows
):
    import daemon
    from core import Participant
    from mailbox import ensure_mailboxes
    from registry import save_registry

    root = tmp_path / "mock-agent"
    root.mkdir()
    save_registry({"MOCK": {"root": str(root), "definition": str(fixtures_dir / "mock.json")}})
    ensure_mailboxes(Participant("MOCK", root))
    threads = []
    listener = daemon._stop_request_listener

    def recorded(names, handle, done):
        threads.append(threading.current_thread())
        listener(names, handle, done)

    monkeypatch.setattr(daemon, "IS_WINDOWS", windows)
    monkeypatch.setattr(daemon, "_stop_request_listener", recorded)
    assert daemon.attached_loop(["MOCK"], 0.1, single_pass=True) == 0
    assert len(threads) == (1 if windows else 0)
    assert not any(t.is_alive() for t in threads)


@pytest.mark.parametrize("body", ["", "12", "12 3", "x 1", "0 1"])
def test_a_malformed_request_is_reaped(fake_home, body):
    import daemon
    from core import stop_request_path

    stop_request_path("n1").parent.mkdir(parents=True, exist_ok=True)
    stop_request_path("n1").write_text(body)
    assert daemon._read_stop_request("n1") is None
    assert not stop_request_path("n1").exists()


def test_a_request_from_a_dead_requester_is_reaped(fake_home):
    import daemon
    from core import stop_request_path

    daemon._write_stop_request("n1", 2**31 - 1, True)
    assert daemon._read_stop_request("n1") is None
    assert not stop_request_path("n1").exists()


# ---------- the CLI side, in process ----------


def _held_node(tmp_path):
    from core import pid_path
    from registry import save_registry

    root = tmp_path / "root"
    root.mkdir()
    save_registry({"alpha": {"root": str(root)}})
    pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
    pid_path("alpha").write_text(str(os.getppid()))


@pytest.mark.parametrize("force", [False, True])
def test_posix_cli_signals_and_writes_no_request(fake_home, tmp_path, monkeypatch, force):
    from commands import cmd_stop
    from core import pid_path

    _held_node(tmp_path)
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        pid_path("alpha").unlink(missing_ok=True)

    monkeypatch.setattr("commands.IS_WINDOWS", False)
    monkeypatch.setattr("commands.os.kill", kill)
    monkeypatch.setattr("commands._pid_alive", lambda pid: pid_path("alpha").is_file())
    monkeypatch.setattr("daemon._pid_alive", lambda pid: pid_path("alpha").is_file())
    monkeypatch.setattr("commands._write_stop_request", lambda *a: pytest.fail("POSIX writes no request"))
    monkeypatch.setattr("commands.STOP_POLL_S", 0.01)
    assert cmd_stop(["alpha", "--force"] if force else ["alpha"]) == 0
    assert sent == [(os.getppid(), signal.SIGTERM)] * (2 if force else 1)


@pytest.mark.parametrize("force", [False, True])
def test_windows_cli_writes_a_request_and_never_signals(fake_home, tmp_path, monkeypatch, force):
    from commands import cmd_stop
    from core import pid_path

    _held_node(tmp_path)
    written = []

    def take(name, requester, forced):
        written.append((name, requester, forced))
        pid_path("alpha").unlink(missing_ok=True)

    monkeypatch.setattr("commands.IS_WINDOWS", True)
    monkeypatch.setattr("commands.os.kill", lambda *a: pytest.fail("os.kill is TerminateProcess on Windows"))
    monkeypatch.setattr("commands._write_stop_request", take)
    monkeypatch.setattr("commands._pid_alive", lambda pid: pid_path("alpha").is_file())
    monkeypatch.setattr("daemon._pid_alive", lambda pid: pid_path("alpha").is_file())
    monkeypatch.setattr("commands.STOP_POLL_S", 0.01)
    assert cmd_stop(["alpha", "--force"] if force else ["alpha"]) == 0
    assert written == [("alpha", os.getpid(), force)]


def _mock_unheard_handler(
    monkeypatch, order, on_suspend=None, on_kill=None, children=(), freezes=True
):
    """A node held by this test's parent that never takes its stop request.
    Every freeze, resume and kill lands in `order` instead of reaching a
    process; `children` are the direct children the handler is said to have,
    each with a start token that holds until the test changes it."""
    from core import pid_path

    def suspend(pid):
        order.append(("suspend", pid))
        if on_suspend:
            on_suspend()
        return freezes

    def hard_kill(targets):
        for target in targets:
            order.append(target)
            if on_kill:
                on_kill(target)
            if target == ("pid", os.getppid()):
                pid_path("alpha").unlink(missing_ok=True)

    monkeypatch.setattr("commands.IS_WINDOWS", True)
    monkeypatch.setattr("commands._suspend_process", suspend)
    monkeypatch.setattr("commands._resume_process", lambda pid: order.append(("resume", pid)))
    monkeypatch.setattr("commands._child_pids", lambda pid: list(children))
    monkeypatch.setattr("commands._start_token", lambda pid: f"token-{pid}")
    monkeypatch.setattr("commands._hard_kill", hard_kill)
    monkeypatch.setattr("commands._pid_alive", lambda pid: pid_path("alpha").is_file())
    monkeypatch.setattr("daemon._pid_alive", lambda pid: pid_path("alpha").is_file())
    monkeypatch.setattr("commands.STOP_FORCE_GRACE_S", 0.1)
    monkeypatch.setattr("commands.STOP_POLL_S", 0.01)


def _unit_in_trash(name, *filenames):
    from core import trash_dir

    trash_dir(name).mkdir(parents=True, exist_ok=True)
    for f in filenames:
        (trash_dir(name) / f).write_text("{}")


def test_a_wake_spawned_as_the_handler_is_frozen_is_killed_before_it(
    fake_home, tmp_path, monkeypatch, capsys
):
    """The spawn boundary: the unheard handler starts a wake at the last moment
    before it is frozen. The stop reads the wake only after the freeze, so it
    kills that wake's tree before it kills the handler, and dead-letters the
    wake's mail."""
    from commands import cmd_stop
    from core import dead_letters_dir, wake_pid_path, write_wake_pid

    _held_node(tmp_path)
    _unit_in_trash("alpha", "01LATE.json")
    order = []
    _mock_unheard_handler(
        monkeypatch, order,
        on_suspend=lambda: write_wake_pid("alpha", os.getpid(), ["01LATE.json"]),
    )
    assert cmd_stop(["alpha"]) == 0
    assert order == [
        ("suspend", os.getppid()), ("tree", os.getpid()), ("pid", os.getppid()),
    ]
    assert (dead_letters_dir("alpha") / "01LATE.json").is_file()
    assert not wake_pid_path("alpha").exists()
    assert "alpha: the killed wake's 1 message(s) are dead letters: 01LATE" in capsys.readouterr().out


def test_a_child_the_frozen_handler_has_not_recorded_is_killed_before_it(
    fake_home, tmp_path, monkeypatch
):
    """A child the handler started and has not yet recorded as a wake is a
    direct child of the frozen handler: it ends after the recorded wake and
    before the handler."""
    from commands import cmd_stop

    _held_node(tmp_path)
    order = []
    child = os.getpid() + 1
    _mock_unheard_handler(monkeypatch, order, children=[child])
    assert cmd_stop(["alpha"]) == 0
    assert order == [("suspend", os.getppid()), ("tree", child), ("pid", os.getppid())]


def test_a_child_whose_pid_changed_hands_is_not_killed(fake_home, tmp_path, monkeypatch):
    """The child listed under the frozen handler exits and its pid goes to
    another process before the kill: the start token no longer matches."""
    from commands import cmd_stop

    _held_node(tmp_path)
    order = []
    child = os.getpid() + 1
    _mock_unheard_handler(monkeypatch, order, children=[child])
    tokens = iter(["old", "new", "new", "new"])
    monkeypatch.setattr("commands._start_token", lambda pid: next(tokens))
    assert cmd_stop(["alpha"]) == 0
    assert order == [("suspend", os.getppid()), ("pid", os.getppid())]


def test_a_child_already_killed_as_the_wake_is_killed_once(fake_home, tmp_path, monkeypatch):
    from commands import cmd_stop
    from core import write_wake_pid

    _held_node(tmp_path)
    _unit_in_trash("alpha", "01WAKE.json")
    order = []
    _mock_unheard_handler(
        monkeypatch, order,
        on_suspend=lambda: write_wake_pid("alpha", os.getpid(), ["01WAKE.json"]),
        children=[os.getpid()],
    )
    assert cmd_stop(["alpha"]) == 0
    assert order == [
        ("suspend", os.getppid()), ("tree", os.getpid()), ("pid", os.getppid()),
    ]


def test_the_stop_hook_the_handler_started_is_not_killed_as_a_child(
    fake_home, tmp_path, monkeypatch
):
    """The hook is a child of the handler that the stop waits for, never one
    of the children it kills."""
    from commands import cmd_stop
    from daemon import _write_stop_hook_mark

    _held_node(tmp_path)
    hook, other = os.getpid() + 1, os.getpid() + 2
    order = []
    _mock_unheard_handler(
        monkeypatch, order, children=[hook, other],
        on_suspend=lambda: _write_stop_hook_mark(
            "alpha", {"state": "running", "pid": hook, "start": "x"}
        ),
    )
    assert cmd_stop(["alpha"]) == 0
    assert order == [("suspend", os.getppid()), ("tree", other), ("pid", os.getppid())]


def test_a_claim_with_no_pid_dead_letters_the_unit_it_names(
    fake_home, tmp_path, monkeypatch, capsys
):
    """A child the handler started before it recorded the wake has no pid in
    the record, only the claim written before the spawn. The claim names the
    envelopes, and they become dead letters as a recorded wake's do."""
    from commands import cmd_stop
    from core import dead_letters_dir, trash_dir, wake_pid_path, write_wake_claim

    _held_node(tmp_path)
    _unit_in_trash("alpha", "01CLAIM.json")
    write_wake_claim("alpha", ["01CLAIM.json"])
    order = []
    child = os.getpid() + 1
    _mock_unheard_handler(monkeypatch, order, children=[child])
    assert cmd_stop(["alpha"]) == 0
    assert order == [("suspend", os.getppid()), ("tree", child), ("pid", os.getppid())]
    assert (trash_dir("alpha") / "01CLAIM.json").is_file()
    assert (dead_letters_dir("alpha") / "01CLAIM.json").is_file()
    assert not wake_pid_path("alpha").exists()
    assert [e["event"] for e in txlog.read_events("01CLAIM")] == ["DROPPED"]
    assert "alpha: the killed wake's 1 message(s) are dead letters: 01CLAIM" in capsys.readouterr().out


def test_a_wake_whose_leader_is_gone_still_has_its_mail_settled(
    fake_home, tmp_path, monkeypatch
):
    """Killing and settling are separate. The record names a leader that has
    exited, so the stop finds no process to kill, and the wake's mail still
    becomes dead letters because the record proves the wake never settled."""
    from commands import cmd_stop
    from core import dead_letters_dir, write_wake_pid

    _held_node(tmp_path)
    _unit_in_trash("alpha", "01GONE.json")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    write_wake_pid("alpha", gone.pid, ["01GONE.json"])
    order = []
    _mock_unheard_handler(monkeypatch, order)
    assert cmd_stop(["alpha"]) == 0
    assert order == [("suspend", os.getppid()), ("pid", os.getppid())]
    assert (dead_letters_dir("alpha") / "01GONE.json").is_file()


def test_a_handler_that_cannot_be_frozen_is_ended_wake_first_and_reported(
    fake_home, tmp_path, monkeypatch, capsys
):
    """The fallback order: the wake the record names, the handler, then any
    wake the record names after it. The stop says it could not freeze."""
    from commands import cmd_stop
    from core import dead_letters_dir, write_wake_pid

    _held_node(tmp_path)
    _unit_in_trash("alpha", "01WAKE.json", "01NEWER.json")
    write_wake_pid("alpha", os.getpid(), ["01WAKE.json"])
    order = []
    later = os.getpid() + 1

    def after_handler(target):
        if target == ("pid", os.getppid()):
            write_wake_pid("alpha", later, ["01NEWER.json"])

    _mock_unheard_handler(monkeypatch, order, on_kill=after_handler, freezes=False)
    monkeypatch.setattr("core._pid_alive", lambda pid: pid in (os.getpid(), later))
    monkeypatch.setattr("core.process_start_token", lambda pid: None)
    assert cmd_stop(["alpha"]) == 0
    assert order == [
        ("suspend", os.getppid()), ("tree", os.getpid()), ("pid", os.getppid()),
        ("tree", later),
    ]
    assert not any(step[0] == "resume" for step in order)
    assert "could not freeze PID" in capsys.readouterr().err
    assert (dead_letters_dir("alpha") / "01NEWER.json").is_file()


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


def test_an_error_after_the_freeze_resumes_the_handler(fake_home, tmp_path, monkeypatch):
    """No exit leaves a handler stopped: a failure between the freeze and the
    handler's kill resumes it."""
    from commands import cmd_stop

    _held_node(tmp_path)
    order = []
    _mock_unheard_handler(monkeypatch, order)

    def explode(pid):
        raise RuntimeError("listing children failed")

    monkeypatch.setattr("commands._child_pids", explode)
    with pytest.raises(RuntimeError):
        cmd_stop(["alpha"])
    assert order == [("suspend", os.getppid()), ("resume", os.getppid())]


def test_a_killed_handler_is_not_resumed(fake_home, tmp_path, monkeypatch):
    from commands import cmd_stop

    _held_node(tmp_path)
    order = []
    _mock_unheard_handler(monkeypatch, order)
    assert cmd_stop(["alpha"]) == 0
    assert not any(step[0] == "resume" for step in order)


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


class _FakeDll:
    """A ctypes DLL whose entry points are plain functions that log their calls."""

    def __init__(self, **entry_points):
        for name, fn in entry_points.items():
            setattr(self, name, fn)


def _fake_windll(monkeypatch, calls, *, handle=0x1234, status=0):
    import ctypes

    def open_process(access, inherit, pid):
        calls.append(("OpenProcess", access, inherit, pid))
        return handle

    def close_handle(h):
        calls.append(("CloseHandle", h))
        return 1

    def nt(entry):
        def call(h):
            calls.append((entry, h))
            return status
        return call

    dlls = {
        "kernel32": _FakeDll(OpenProcess=open_process, CloseHandle=close_handle),
        "ntdll": _FakeDll(
            NtSuspendProcess=nt("NtSuspendProcess"), NtResumeProcess=nt("NtResumeProcess")
        ),
    }
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: dlls[name], raising=False)


def test_windows_freeze_opens_suspends_and_closes(monkeypatch):
    """Mocked ctypes: native Windows behavior is not proven here."""
    from commands import _nt_process_call

    calls = []
    _fake_windll(monkeypatch, calls)
    assert _nt_process_call(42, "NtSuspendProcess") is True
    assert calls == [
        ("OpenProcess", 0x0800, False, 42),
        ("NtSuspendProcess", 0x1234),
        ("CloseHandle", 0x1234),
    ]


def test_windows_freeze_reads_false_when_the_process_cannot_be_opened(monkeypatch):
    from commands import _nt_process_call

    calls = []
    _fake_windll(monkeypatch, calls, handle=0)
    assert _nt_process_call(42, "NtSuspendProcess") is False
    assert calls == [("OpenProcess", 0x0800, False, 42)]


def test_windows_freeze_reads_false_unless_the_status_is_zero(monkeypatch):
    from commands import _nt_process_call

    calls = []
    _fake_windll(monkeypatch, calls, status=0xC0000022)
    assert _nt_process_call(42, "NtSuspendProcess") is False
    assert calls[-1] == ("CloseHandle", 0x1234)


def test_windows_without_sigstop_suspends_and_resumes_through_the_dll(monkeypatch):
    from commands import _resume_process, _suspend_process

    calls = []
    _fake_windll(monkeypatch, calls)
    monkeypatch.setattr("commands.signal", type("NoStop", (), {})())
    assert _suspend_process(42) is True
    assert _resume_process(42) is True
    assert [c[0] for c in calls if c[0].startswith("Nt")] == ["NtSuspendProcess", "NtResumeProcess"]


def test_windows_start_token_is_the_creation_time(monkeypatch):
    import ctypes

    from commands import _windows_start_token

    calls = []

    def get_times(handle, created, exited, kernel, user):
        calls.append(handle)
        created._obj.dwLowDateTime = 7
        created._obj.dwHighDateTime = 9
        return 1

    dll = _FakeDll(
        OpenProcess=lambda access, inherit, pid: 0x55,
        CloseHandle=lambda h: 1,
        GetProcessTimes=get_times,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: dll, raising=False)
    assert _windows_start_token(42) == "0000000900000007"
    assert calls == [0x55]


def test_children_of_a_process_are_found_from_proc_or_ps():
    from commands import _child_pids

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert child.pid in _child_pids(os.getpid())
        assert os.getpid() not in _child_pids(os.getpid())
    finally:
        child.kill()
        child.wait()


def test_a_start_token_names_one_process():
    from commands import _start_token

    a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        token = _start_token(a.pid)
        assert token and _start_token(a.pid) == token
        assert _start_token(os.getpid()) != token
    finally:
        a.kill()
        a.wait()
    assert _start_token(a.pid) is None


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


def test_windows_kill_fallback_is_a_stop_request_not_terminate(fake_home, tmp_path, monkeypatch):
    from commands import cmd_kill

    _held_node(tmp_path)
    written = []
    monkeypatch.setattr("commands.IS_WINDOWS", True)
    monkeypatch.setattr("commands.signal", type("NoUsr1", (), {"SIGTERM": signal.SIGTERM})())
    monkeypatch.setattr("commands.os.kill", lambda *a: pytest.fail("os.kill is TerminateProcess on Windows"))
    monkeypatch.setattr("commands._write_stop_request", lambda *a: written.append(a))
    monkeypatch.setattr("commands._pid_alive", lambda pid: True)
    monkeypatch.setattr("daemon._pid_alive", lambda pid: True)
    monkeypatch.setattr("commands.KILL_TIMEOUT_S", 0.05)
    monkeypatch.setattr("commands.STOP_KILL_WAIT_S", 0.05)
    monkeypatch.setattr("commands.STOP_POLL_S", 0.01)
    monkeypatch.setattr("commands.STOP_HOOK_SECONDS", 0)
    assert cmd_kill(["alpha"]) == 1
    assert written == [("alpha", os.getpid(), False)]
