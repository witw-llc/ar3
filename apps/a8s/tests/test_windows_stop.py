"""`a8s stop`, `stop --force`, `kill` and `restart` on Windows: the handler's
stop-request listener and the CLI's stop path, in process.

Each test forces a platform branch (`IS_WINDOWS`) and replaces the kill
primitives with recorders, or mocks the DLLs, so no test needs POSIX signals
or process groups and the file runs natively on Windows too. The tests that
emulate `TerminateProcess` with SIGKILL against real processes are in
test_windows_stop_posix.py."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

import txlog


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
    monkeypatch.setattr("commands.process_start_token", lambda pid: f"token-{pid}")
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
    monkeypatch.setattr("commands.process_start_token", lambda pid: next(tokens))
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


@pytest.mark.parametrize("stamp,killed", [("not-its-start", False), ("", True)])
def test_a_wake_record_names_its_process_by_start_token(
    fake_home, tmp_path, monkeypatch, stamp, killed
):
    """A stale record whose pid the OS gave to another process names that
    process with the wrong start token, and the stop does not kill it. A
    record with no token (written before every platform had a reader) still
    reads by liveness alone. Real processes and the real token reader."""
    from commands import cmd_stop
    from core import _write_wake_record, dead_letters_dir

    _held_node(tmp_path)
    _unit_in_trash("alpha", "01REUSED.json")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_wake_record("alpha", other.pid, stamp, ["01REUSED.json"])
        order = []
        _mock_unheard_handler(monkeypatch, order)
        assert cmd_stop(["alpha"]) == 0
        assert (("tree", other.pid) in order) is killed
        assert other.poll() is None
        assert (dead_letters_dir("alpha") / "01REUSED.json").is_file()
    finally:
        other.kill()
        other.wait()


def test_a_wake_record_carries_the_start_token_of_its_process(fake_home):
    from ar3.proc import process_start_token
    from core import read_wake_pid, wake_pid_path, write_wake_pid

    write_wake_pid("alpha", os.getpid(), ["01SELF.json"])
    record = json.loads(wake_pid_path("alpha").read_text())
    assert record["start"] and record["start"] == process_start_token(os.getpid())
    assert read_wake_pid("alpha") == (os.getpid(), ["01SELF.json"])


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

    from ar3.proc import _windows_start_token

    calls = []

    def get_times(handle, created, exited, kernel, user):
        calls.append(handle)
        created._obj.dwLowDateTime = 7
        created._obj.dwHighDateTime = 9
        return 1

    dll = _FakeDll(
        OpenProcess=lambda access, inherit, pid: 0x55,
        WaitForSingleObject=lambda h, ms: 0x102,
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
    from commands import process_start_token

    a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        token = process_start_token(a.pid)
        assert token and process_start_token(a.pid) == token
        assert process_start_token(os.getpid()) != token
    finally:
        a.kill()
        a.wait()
    assert process_start_token(a.pid) is None


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
