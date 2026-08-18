"""Tests for the shared spawn/terminate-group primitive — ar3's
foundation layer."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time

import pytest

from ark.proc import spawn, terminate_group


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="process-group semantics are POSIX-only"
)


def _sleeper(tmp_path, seconds=30):
    script = tmp_path / "sleeper.py"
    script.write_text(f"import time\ntime.sleep({seconds})\n", encoding="utf-8")
    return [sys.executable, str(script)]


class TestSpawn:
    def test_returns_a_popen(self, tmp_path):
        proc = spawn(["true"], cwd=tmp_path)
        try:
            assert isinstance(proc, subprocess.Popen)
            proc.wait(timeout=5)
        finally:
            terminate_group(proc)

    def test_runs_in_its_own_process_group(self, tmp_path):
        proc = spawn(_sleeper(tmp_path), cwd=tmp_path)
        try:
            assert os.getpgid(proc.pid) != os.getpgid(os.getpid())
        finally:
            terminate_group(proc)
            proc.wait(timeout=5)

    def test_stdin_devnull_by_default(self, tmp_path):
        proc = spawn(["cat"], cwd=tmp_path)
        try:
            assert proc.stdin is None
        finally:
            terminate_group(proc)


class TestTerminateGroup:
    def test_kills_a_sleeping_child(self, tmp_path):
        proc = spawn(_sleeper(tmp_path), cwd=tmp_path)
        terminate_group(proc, grace_seconds=0.2)
        proc.wait(timeout=5)
        assert proc.returncode is not None

    def test_kills_a_child_that_ignores_sigterm(self, tmp_path):
        script = tmp_path / "stubborn.py"
        script.write_text(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        proc = spawn([sys.executable, str(script)], cwd=tmp_path)
        start = time.monotonic()
        terminate_group(proc, grace_seconds=0.3)
        proc.wait(timeout=5)
        elapsed = time.monotonic() - start
        assert proc.returncode is not None
        # SIGKILL follows the grace period — this must not hang forever.
        assert elapsed < 5

    def test_accepts_a_bare_pid(self, tmp_path):
        proc = spawn(_sleeper(tmp_path), cwd=tmp_path)
        terminate_group(proc.pid, grace_seconds=0.2)
        proc.wait(timeout=5)
        assert proc.returncode is not None

    def test_already_dead_process_does_not_raise(self, tmp_path):
        proc = spawn(["true"], cwd=tmp_path)
        proc.wait(timeout=5)
        terminate_group(proc, grace_seconds=0.1)

    def test_kills_a_grandchild_that_survives_the_leaders_own_exit(self, tmp_path):
        """The leader here exits (almost) immediately on its own — `exit 0`
        right after backgrounding a SIGTERM-ignoring child — so by the time
        `terminate_group` is even called, the leader is already a zombie:
        `os.getpgid(leader_pid)` fails outright on macOS for a zombie pid,
        even though `ps` still lists it and the process group (and the
        surviving grandchild in it) are very much alive. The old code
        re-derived the pgid for every signal and, on any such failure, fell
        back to a plain kill of the leader's own (already-dead) pid — a
        no-op that leaves the grandchild running forever. The fix must
        still reach the grandchild's group even when `getpgid` never
        resolves at all."""
        pidfile = tmp_path / "grandchild.pid"
        grandchild_script = tmp_path / "ignorer.py"
        grandchild_script.write_text(
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        leader_cmd = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(grandchild_script))} & "
            "echo started; exit 0"
        )
        proc = spawn(["sh", "-c", leader_cmd], cwd=tmp_path)
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert pidfile.exists(), "grandchild never started"
            grandchild_pid = int(pidfile.read_text())
            assert _pid_is_alive(grandchild_pid)

            terminate_group(proc, grace_seconds=0.3)

            deadline = time.monotonic() + 5
            while _pid_is_alive(grandchild_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not _pid_is_alive(grandchild_pid)
        finally:
            proc.wait(timeout=5)
