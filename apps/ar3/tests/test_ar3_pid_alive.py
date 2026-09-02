"""Tests for ar3.proc.pid_alive — the one cross-platform pid probe.

No nt skip: on a Windows checkout these same tests exercise the
OpenProcess/GetExitCodeProcess branch for real (#2's acceptance run).
"""
from __future__ import annotations

import os
import subprocess
import sys

from ar3.proc import pid_alive


def test_own_pid_is_alive():
    assert pid_alive(os.getpid()) is True


def test_exited_child_is_dead():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert pid_alive(proc.pid) is False


def test_nonpositive_pids_are_dead():
    assert pid_alive(0) is False
    assert pid_alive(-1) is False
