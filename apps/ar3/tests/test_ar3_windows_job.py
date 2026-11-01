"""`ar3.proc.windows_job`, `terminate_job` and `release_job`.

The mocked tests force the Windows branch and record the kernel32 calls, so
they run on every platform. The native tests run only on Windows: they prove
that a job reaches a grandchild whose parent has exited, which
`taskkill /T` cannot, and that closing the caller's handle ends the tree."""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import ar3.proc as proc

KILL_ON_JOB_CLOSE = 0x2000
BREAKAWAY_OK = 0x0800
EXTENDED_LIMIT_INFORMATION = 9
PROCESS_TERMINATE_AND_SET_QUOTA = 0x0101
JOB, PROCESS = 0x10, 0x20


class _FakeDll:
    def __init__(self, **entry_points):
        for name, fn in entry_points.items():
            setattr(self, name, fn)


def _kernel32(monkeypatch, *, job=JOB, process=PROCESS, limits=1, assign=1, terminate=1):
    calls = []

    def set_information(handle, kind, info, size):
        flags = info._obj.BasicLimitInformation.LimitFlags
        calls.append(("SetInformationJobObject", handle, kind, flags, size))
        return limits

    dll = _FakeDll(
        CreateJobObjectW=lambda attributes, name: calls.append(
            ("CreateJobObjectW", attributes, name)) or job,
        SetInformationJobObject=set_information,
        OpenProcess=lambda access, inherit, pid: calls.append(
            ("OpenProcess", access, inherit, pid)) or process,
        AssignProcessToJobObject=lambda j, p: calls.append(("Assign", j, p)) or assign,
        TerminateJobObject=lambda j, code: calls.append(("Terminate", j, code)) or terminate,
        CloseHandle=lambda h: calls.append(("CloseHandle", h)) or 1,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: dll, raising=False)
    monkeypatch.setattr(proc, "os", SimpleNamespace(name="nt"))
    return calls


def _limit_size():
    return 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112


def test_posix_makes_no_job(monkeypatch):
    monkeypatch.setattr(proc, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: pytest.fail("no DLL on POSIX"), raising=False)
    assert proc.windows_job(42) is None


def test_windows_puts_the_process_in_a_job_that_kills_on_close(monkeypatch):
    calls = _kernel32(monkeypatch)
    assert proc.windows_job(42) == JOB
    assert calls == [
        ("CreateJobObjectW", None, None),
        ("OpenProcess", PROCESS_TERMINATE_AND_SET_QUOTA, False, 42),
        ("SetInformationJobObject", JOB, EXTENDED_LIMIT_INFORMATION,
         KILL_ON_JOB_CLOSE | BREAKAWAY_OK, _limit_size()),
        ("Assign", JOB, PROCESS),
        ("CloseHandle", PROCESS),
    ]


def test_windows_no_job_when_the_job_cannot_be_made(monkeypatch):
    calls = _kernel32(monkeypatch, job=None)
    assert proc.windows_job(42) is None
    assert calls == [("CreateJobObjectW", None, None)]


@pytest.mark.parametrize("failure", [
    {"process": None}, {"limits": 0}, {"assign": 0},
])
def test_windows_a_failed_step_closes_every_handle_and_makes_no_job(monkeypatch, failure):
    calls = _kernel32(monkeypatch, **failure)
    assert proc.windows_job(42) is None
    closed = [c[1] for c in calls if c[0] == "CloseHandle"]
    assert closed == ([JOB] if failure == {"process": None} else [PROCESS, JOB])


@pytest.mark.parametrize("error", [OSError, AttributeError])
def test_windows_with_no_kernel32_makes_no_job(monkeypatch, error):
    def missing(name, **kw):
        raise error("no kernel32")

    monkeypatch.setattr(ctypes, "WinDLL", missing, raising=False)
    monkeypatch.setattr(proc, "os", SimpleNamespace(name="nt"))
    assert proc.windows_job(42) is None
    assert proc.terminate_job(JOB) is False
    proc.release_job(JOB)


@pytest.mark.parametrize("result", [1, 0])
def test_terminate_ends_the_job_and_keeps_the_handle(monkeypatch, result):
    calls = _kernel32(monkeypatch, terminate=result)
    assert proc.terminate_job(JOB) is bool(result)
    assert calls == [("Terminate", JOB, 1)]


def test_release_clears_the_kill_on_close_before_it_closes(monkeypatch):
    calls = _kernel32(monkeypatch)
    proc.release_job(JOB)
    assert calls == [
        ("SetInformationJobObject", JOB, EXTENDED_LIMIT_INFORMATION, BREAKAWAY_OK, _limit_size()),
        ("CloseHandle", JOB),
    ]


class _Started:
    def __init__(self, pid, *, exits=True):
        self.pid = pid
        self.killed = False
        self.exits = exits
        self.stdin = self.stdout = self.stderr = None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if not self.exits:
            raise subprocess.TimeoutExpired("x", timeout)
        return 1


def _suspended_start(monkeypatch, *, job=JOB, resumes=True, exits=True):
    """`start_in_job` with Popen, the job and the resume recorded."""
    calls = []
    started = []

    def popen(argv, **kwargs):
        p = _Started(100 + len(started), exits=exits)
        started.append(p)
        calls.append(("Popen", kwargs.get("creationflags")))
        return p

    monkeypatch.setattr(proc.subprocess, "Popen", popen)
    monkeypatch.setattr(proc, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(proc, "windows_job", lambda pid: calls.append(("job", pid)) or job)
    monkeypatch.setattr(proc, "_resume_process_threads", lambda pid: calls.append(("resume", pid)) or resumes)
    monkeypatch.setattr(proc, "_close_job", lambda j: calls.append(("close job", j)))
    return calls, started


def test_windows_start_is_suspended_until_it_is_in_its_job(monkeypatch):
    calls, started = _suspended_start(monkeypatch)
    p, job, note = proc.start_in_job(["wake"], creationflags=0x08000200)
    assert (p, job, note) == (started[0], JOB, None)
    assert calls == [("Popen", 0x08000204), ("job", 100), ("resume", 100)]


def test_windows_start_with_no_job_is_resumed_and_says_so(monkeypatch):
    calls, started = _suspended_start(monkeypatch, job=None)
    p, job, note = proc.start_in_job(["wake"], creationflags=0x08000200)
    assert p is started[0] and job is None
    assert "no job object" in note
    assert calls == [("Popen", 0x08000204), ("job", 100), ("resume", 100)]


def test_windows_start_that_cannot_resume_is_ended_then_started_again(monkeypatch):
    calls, started = _suspended_start(monkeypatch, resumes=False)
    p, job, note = proc.start_in_job(["wake"], creationflags=0x08000200)
    assert started[0].killed
    assert p is started[1] and job is None
    assert "started again with no job object" in note
    assert calls == [
        ("Popen", 0x08000204), ("job", 100), ("resume", 100),
        ("close job", JOB), ("Popen", 0x08000200),
    ]


def test_windows_start_whose_end_is_not_confirmed_fails_and_starts_no_copy(monkeypatch):
    calls, started = _suspended_start(monkeypatch, resumes=False, exits=False)
    with pytest.raises(OSError, match="not starting it again"):
        proc.start_in_job(["wake"], creationflags=0x08000200)
    assert started[0].killed and len(started) == 1
    assert calls[-1] == ("close job", JOB)


def test_posix_start_has_no_job_and_no_extra_flags(monkeypatch):
    seen = []
    monkeypatch.setattr(proc, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(proc.subprocess, "Popen", lambda argv, **kw: seen.append(kw) or "proc")
    assert proc.start_in_job(["wake"], start_new_session=True) == ("proc", None, None)
    assert seen == [{"start_new_session": True}]


def _threads(monkeypatch, entries, *, open_ok=True, resume=1):
    """A fake Toolhelp thread snapshot of `(owner pid, thread id)` entries."""
    calls = []
    index = {"i": 0}

    def fill(entry_ref):
        i = index["i"]
        if i >= len(entries):
            return 0
        entry_ref._obj.th32OwnerProcessID, entry_ref._obj.th32ThreadID = entries[i]
        index["i"] += 1
        return 1

    dll = _FakeDll(
        CreateToolhelp32Snapshot=lambda flags, pid: calls.append(("snapshot", flags)) or 0x30,
        Thread32First=lambda snap, ref: fill(ref),
        Thread32Next=lambda snap, ref: fill(ref),
        OpenThread=lambda access, inherit, tid: calls.append(("OpenThread", access, tid)) or (0x40 if open_ok else None),
        ResumeThread=lambda h: calls.append(("ResumeThread", h)) or resume,
        CloseHandle=lambda h: calls.append(("CloseHandle", h)) or 1,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: dll, raising=False)
    return calls


def test_resume_resumes_only_the_threads_of_the_process(monkeypatch):
    calls = _threads(monkeypatch, [(7, 70), (42, 420), (8, 80)])
    assert proc._resume_process_threads(42) is True
    assert calls == [
        ("snapshot", 0x4), ("OpenThread", 0x2, 420), ("ResumeThread", 0x40),
        ("CloseHandle", 0x40), ("CloseHandle", 0x30),
    ]


@pytest.mark.parametrize("failure", [{"open_ok": False}, {"resume": 0xFFFFFFFF}])
def test_resume_that_fails_reads_false_and_closes_the_snapshot(monkeypatch, failure):
    calls = _threads(monkeypatch, [(42, 420)], **failure)
    assert proc._resume_process_threads(42) is False
    assert calls[-1] == ("CloseHandle", 0x30)


def test_resume_of_a_process_with_no_thread_reads_false(monkeypatch):
    _threads(monkeypatch, [(7, 70)])
    assert proc._resume_process_threads(42) is False


def _in_job_dll(monkeypatch, *, opened=0x60, ok=1, member=1):
    calls = []

    def is_in_job(process, job, result):
        calls.append(("IsProcessInJob", process, job))
        result._obj.value = member
        return ok

    dll = _FakeDll(
        OpenProcess=lambda access, inherit, pid: opened,
        IsProcessInJob=is_in_job,
        CloseHandle=lambda h: calls.append(("CloseHandle", h)) or 1,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: dll, raising=False)
    return calls


def _venv(tmp_path, home):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (tmp_path / "venv" / "pyvenv.cfg").write_text(f"home = {home}\nversion = 3.13.1\n")
    exe = scripts / "python.exe"
    exe.write_text("")
    return str(exe)


def _escape(monkeypatch, argv0, **kw):
    monkeypatch.setattr(proc.shutil, "which", lambda name: None)
    return proc.windows_job_escape_note(argv0, 42, JOB, windows=True, **kw)


def test_escape_note_names_an_app_execution_alias(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\u\\AppData\\Local")
    _in_job_dll(monkeypatch)
    note = _escape(monkeypatch, "C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe")
    assert "through an app execution alias" in note
    assert "may escape the job" in note


def test_escape_note_names_a_venv_launcher_of_a_packaged_base(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\u\\AppData\\Local")
    _in_job_dll(monkeypatch)
    exe = _venv(tmp_path, "C:\\Program Files\\WindowsApps\\PythonSoftwareFoundation.Python.3.13_x64")
    assert "venv launcher of a packaged" in _escape(monkeypatch, exe)


def test_escape_note_is_silent_for_an_ordinary_venv_in_the_job(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\u\\AppData\\Local")
    calls = _in_job_dll(monkeypatch, member=1)
    exe = _venv(tmp_path, "C:\\Program Files\\Python313")
    assert _escape(monkeypatch, exe) is None
    assert ("IsProcessInJob", 0x60, JOB) in calls and calls[-1] == ("CloseHandle", 0x60)


def test_escape_note_names_a_leader_outside_our_job(monkeypatch):
    _in_job_dll(monkeypatch, member=0)
    assert "leader is not in the job" in _escape(monkeypatch, "C:\\tools\\wake.exe")


@pytest.mark.parametrize("dll", [{"ok": 0}, {"opened": None}])
def test_escape_note_says_nothing_when_membership_cannot_be_read(monkeypatch, dll):
    _in_job_dll(monkeypatch, member=0, **dll)
    assert _escape(monkeypatch, "C:\\tools\\wake.exe") is None


def test_escape_note_is_none_on_posix(monkeypatch):
    assert proc.windows_job_escape_note("python3", 42, JOB, windows=False) is None


# ---------- native Windows ----------

native = pytest.mark.skipif(os.name != "nt", reason="needs Windows job objects")


def _activated_through_a_packaged_launcher() -> bool:
    """Whether this interpreter runs through a WindowsApps app execution
    alias, or is a venv launcher whose base is a packaged (Store) Python.
    Either one starts the real python outside the caller's job object, so
    a test led by `sys.executable` cannot pass. The packaged base run
    directly stays in the job and runs these tests."""
    local = os.environ.get("LOCALAPPDATA", "")
    alias = os.path.join(local, "Microsoft", "WindowsApps").lower() + os.sep
    if local and os.path.abspath(sys.executable).lower().startswith(alias):
        return True
    base = getattr(sys, "_base_executable", "") or ""
    return sys.prefix != sys.base_prefix and "\\windowsapps\\" in base.lower()


launcher_led = pytest.mark.skipif(
    _activated_through_a_packaged_launcher(),
    reason="the leader is sys.executable, started through a WindowsApps alias or a venv "
    "of a packaged (Store) Python, which starts the real python outside the job; "
    "test_native_a_cmd_leader_s_orphan_ends_with_its_job covers the job here",
)

# The leader starts its grandchild at once. Under a venv, sys.executable is
# a launcher that starts the real python at once too: both must begin inside
# the job, which only a suspended start guarantees.
LEADER = r"""
import pathlib, subprocess, sys
out = pathlib.Path(sys.argv[1])
grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
out.with_suffix(".tmp").write_text(str(grand.pid))
out.with_suffix(".tmp").replace(out)
"""


def _wait_for(predicate, what, seconds=10):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(0.02)


def _read_pid(path):
    """A pid file written by replace; on Windows a read during the replace
    can meet a sharing violation, so it is retried briefly."""
    from ar3.fsio import read_text_retry

    return int(read_text_retry(path))


def _orphan_in_a_job(tmp_path):
    """A leader started in a job that starts a grandchild and exits: the
    grandchild's parent is gone, so no tree walk reaches it."""
    out = tmp_path / "grand.pid"
    leader, job, note = proc.start_in_job([sys.executable, "-c", LEADER, str(out)])
    assert job is not None, note
    _wait_for(out.is_file, "the grandchild")
    leader.wait(timeout=10)
    return job, _read_pid(out)


def _end(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


@native
@launcher_led
def test_native_terminate_reaches_a_grandchild_whose_parent_exited(tmp_path):
    job, grand = _orphan_in_a_job(tmp_path)
    try:
        assert proc.pid_alive(grand)
        assert proc.terminate_job(job) is True
        _wait_for(lambda: not proc.pid_alive(grand), "the grandchild to end", 5)
    finally:
        proc.release_job(job)
        _end(grand)


@native
@launcher_led
def test_native_release_leaves_the_grandchild_running(tmp_path):
    job, grand = _orphan_in_a_job(tmp_path)
    try:
        proc.release_job(job)
        time.sleep(0.5)
        assert proc.pid_alive(grand)
    finally:
        _end(grand)


HOLDER = r"""
import pathlib, sys, time
sys.path.insert(0, sys.argv[2])
from ar3.proc import start_in_job
out = pathlib.Path(sys.argv[1])
leader, job, note = start_in_job([sys.executable, "-c", sys.argv[3], str(out)])
if job is None:
    sys.exit(3)
time.sleep(60)
"""


@native
@launcher_led
def test_native_a_holder_killed_outright_takes_the_job_with_it(tmp_path):
    """The handler's case: it holds the job and is ended by
    `TerminateProcess`, so its handle closes and the job's kill on close
    ends the orphaned grandchild."""
    out = tmp_path / "grand.pid"
    lib = str(Path(proc.__file__).resolve().parents[1])
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(out), lib, LEADER])
    try:
        _wait_for(lambda: out.is_file() or holder.poll() is not None, "the grandchild")
        assert holder.poll() is None, f"holder exited {holder.returncode}"
        grand = _read_pid(out)
        assert proc.pid_alive(grand)
        holder.kill()
        holder.wait(timeout=10)
        _wait_for(lambda: not proc.pid_alive(grand), "the grandchild to end", 5)
    finally:
        if holder.poll() is None:
            holder.kill()
        if out.is_file():
            _end(_read_pid(out))


LEADER_CMD = """@echo off
start "" /b ping -n 60 127.0.0.1 > nul
exit /b 0
"""


def _children_of(pid: int, exe: str) -> list[int]:
    """Pids of `exe` whose recorded parent is `pid` (a Toolhelp process
    snapshot; the record outlives the parent)."""
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ProcessEntry),
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    found = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32ParentProcessID == pid and entry.szExeFile.lower() == exe:
                found.append(entry.th32ProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


@native
def test_native_a_cmd_leader_s_orphan_ends_with_its_job(tmp_path):
    """A .cmd wake starts a grandchild and exits, so the grandchild's parent
    is gone before the kill and `taskkill /T` could not reach it. Ending the
    job does. No Python launcher is involved, so this runs on any Windows."""
    script = tmp_path / "leader.cmd"
    script.write_text(LEADER_CMD)
    leader, job, note = proc.start_in_job(["cmd.exe", "/c", str(script)])
    assert job is not None, note
    grand = []
    try:
        _wait_for(lambda: grand.extend(_children_of(leader.pid, "ping.exe")) or grand, "the grandchild")
        leader.wait(timeout=10)
        assert proc.pid_alive(grand[0])
        assert proc.terminate_job(job) is True
        _wait_for(lambda: not proc.pid_alive(grand[0]), "the grandchild to end", 5)
    finally:
        proc.release_job(job)
        for pid in grand:
            _end(pid)
