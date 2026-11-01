"""A wake's Windows job object: made at the spawn, held until the wake
settles, ended by a forced kill, released (never ended) by a normal settle.

The job primitives are replaced with recorders, so these run on every
platform; the wakes are real processes. Native proof that a job ends a tree
whose leader has exited is in apps/ar3/tests/test_ar3_windows_job.py and
test_wake_wedge.py."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

import daemon

JOB = 0x77


def _start_in_job(monkeypatch, made, job, note=None):
    """`start_in_job` that really starts the wake and reports `job`."""
    def start(argv, **kwargs):
        kwargs.pop("creationflags", None)
        proc = subprocess.Popen(argv, **kwargs)
        made.append(("job", proc.pid))
        return proc, job, note

    monkeypatch.setattr(daemon, "start_in_job", start)


@pytest.fixture
def calls(monkeypatch):
    """Every job and group call, in order. Each test leaves no wake behind."""
    made = []
    _start_in_job(monkeypatch, made, JOB)
    monkeypatch.setattr(daemon, "terminate_job", lambda job: made.append(("end job", job)) or True)
    monkeypatch.setattr(daemon, "release_job", lambda job: made.append(("release", job)))
    monkeypatch.setattr(daemon, "_STUCK_WAKE_READERS", [])
    yield made
    proc = daemon._CURRENT_WAKE_PROC
    if proc is not None:
        if proc.poll() is None:
            proc.kill()
        daemon._clear_wake_state(proc.wait())


def _record_group_kills(monkeypatch, calls):
    def group(proc):
        calls.append(("group", proc.pid))
        if proc.poll() is None:
            proc.kill()

    def leader(proc):
        calls.append(("leader", proc.pid))
        if proc.poll() is None:
            proc.kill()

    monkeypatch.setattr(daemon, "terminate_group", group)
    monkeypatch.setattr(daemon, "_kill_leader", leader)


def _start(tmp_path, script):
    assert daemon._start_wake_subprocess("A", [sys.executable, "-c", script], tmp_path)
    return daemon._CURRENT_WAKE_PROC


def test_a_wake_is_held_in_its_job_until_it_settles(fake_home, tmp_path, calls):
    held = []
    rc = daemon.run_with_prefix(
        "A", [sys.executable, "-c", "pass"], tmp_path,
        on_start=lambda pid: held.append((pid, daemon._WAKE_JOB)),
    )
    assert rc == 0
    ((pid, job),) = held
    assert job == JOB
    assert calls == [("job", pid), ("release", JOB)]
    assert daemon._WAKE_JOB is None


def test_a_forced_kill_ends_the_job_then_the_leader_and_walks_no_tree(
    fake_home, tmp_path, calls, monkeypatch
):
    """`taskkill /T` would follow the parent link to a bridge that broke away
    from the job while its launching process lives; with a job it never runs."""
    _record_group_kills(monkeypatch, calls)
    proc = _start(tmp_path, "import time; time.sleep(30)")
    daemon._kill_wake_subprocess_group()
    assert calls == [("job", proc.pid), ("end job", JOB), ("leader", proc.pid)]


def test_a_forced_kill_ends_the_job_of_a_wake_whose_leader_has_exited(
    fake_home, tmp_path, calls, monkeypatch
):
    """The leader is gone; its descendants are in the job."""
    _record_group_kills(monkeypatch, calls)
    proc = _start(tmp_path, "pass")
    proc.wait()
    daemon._kill_wake_subprocess_group()
    assert calls == [("job", proc.pid), ("end job", JOB), ("leader", proc.pid)]


def test_a_job_that_cannot_be_ended_falls_back_to_the_tree(fake_home, tmp_path, calls, monkeypatch):
    _record_group_kills(monkeypatch, calls)
    monkeypatch.setattr(daemon, "terminate_job", lambda job: calls.append(("end job", job)) and False)
    proc = _start(tmp_path, "import time; time.sleep(30)")
    daemon._kill_wake_subprocess_group()
    assert calls == [("job", proc.pid), ("end job", JOB), ("group", proc.pid)]


def test_with_no_job_a_forced_kill_walks_the_tree(fake_home, tmp_path, calls, monkeypatch):
    """No job (the assignment failed): the tree walk is the only reach."""
    _record_group_kills(monkeypatch, calls)
    _start_in_job(monkeypatch, calls, None)
    proc = _start(tmp_path, "import time; time.sleep(30)")
    daemon._kill_wake_subprocess_group()
    assert calls == [("job", proc.pid), ("group", proc.pid)]


def test_the_drain_timeout_ends_the_job_then_the_leader(fake_home, tmp_path, calls, monkeypatch):
    _record_group_kills(monkeypatch, calls)
    monkeypatch.setattr(daemon, "_drain_wake_stdout", lambda grace: False)
    proc = _start(tmp_path, "pass")
    proc.wait()
    daemon._drain_wake_stdout_after_exit("A")
    assert calls == [("job", proc.pid), ("end job", JOB), ("leader", proc.pid)]


def test_a_wake_that_settles_on_its_own_is_never_ended_by_its_job(
    fake_home, tmp_path, calls
):
    proc = _start(tmp_path, "pass")
    deadline = time.monotonic() + 5
    while daemon._wake_in_flight() or daemon._CURRENT_WAKE_PROC is not None:
        daemon._service_in_flight_wake()
        assert time.monotonic() < deadline, "the wake never settled"
        time.sleep(0.02)
    assert calls == [("job", proc.pid), ("release", JOB)]


def test_a_start_note_reaches_the_agent_log(fake_home, tmp_path, calls, monkeypatch):
    from core import agent_log_path

    _start_in_job(monkeypatch, calls, None, note="started with no job object")
    _start(tmp_path, "pass")
    assert "wake started with no job object" in agent_log_path("A").read_text()


def test_a_wake_that_may_escape_its_job_says_so(fake_home, tmp_path, calls, monkeypatch):
    from core import agent_log_path

    seen = []
    monkeypatch.setattr(
        daemon, "windows_job_escape_note",
        lambda argv0, pid, job: seen.append((argv0, job)) or "started through an app execution alias",
    )
    _start(tmp_path, "pass")
    assert seen == [(sys.executable, JOB)]
    assert "wake started through an app execution alias" in agent_log_path("A").read_text()


@pytest.mark.skipif(os.name == "nt", reason="the real job primitive makes a job on Windows")
def test_posix_has_no_job_and_a_forced_kill_of_an_exited_leader_does_nothing(
    fake_home, tmp_path, monkeypatch
):
    kills = []
    monkeypatch.setattr(daemon, "terminate_group", lambda proc: kills.append(proc.pid))
    monkeypatch.setattr(daemon, "_STUCK_WAKE_READERS", [])
    proc = _start(tmp_path, "pass")
    try:
        assert daemon._WAKE_JOB is None
        proc.wait()
        daemon._kill_wake_subprocess_group()
        assert kills == []
    finally:
        daemon._clear_wake_state(proc.wait())
