"""One spawn-in-its-own-group + kill-the-group primitive for every ar3 app.

`spawn` starts a child in its own POSIX process group (`start_new_session`)
so a later `terminate_group` can reach whatever it forks, not just the
immediate child — a harness CLI commonly spawns tool subprocesses that
killing the child alone would leak. `terminate_group` sends SIGTERM to the
group, waits `grace_seconds` for a clean exit, then SIGKILL — the safest
escalation observed across the suite's process-teardown call sites, applied
uniformly instead of an immediate SIGKILL. The pgid is resolved once, before
SIGTERM, and both signals target that same pgid — a group leader that exits
during the grace period (or even before `terminate_group` is first called;
macOS refuses `getpgid` on an already-zombied pid, unlike Linux) must not
strand SIGKILL with no leader pid left to resolve. When `getpgid` can't
resolve it, `pid` itself stands in as the pgid — true whenever `pid` names a
`spawn`-started leader, since `start_new_session` makes a new session's pgid
equal to its own pid by construction — and only a `killpg` failure on that
guess falls back further, to a plain kill of the pid itself.

Windows has no process group to signal, and no signal a windowless process
can catch, so `terminate_group` ends the process TREE there at once with
`taskkill /T /F`: the process and every descendant still linked to it by
parent pid. A descendant whose parent already exited is not linked and is
not reached. A caller that must reach it starts the process in a job object
(`start_in_job`, suspended until it is assigned): every process it starts
joins the job, linked or not, and `terminate_job` ends them all.

`pid_alive` and `process_start_token` are the two halves of reading a pid
file: the first says a process holds the number, the second says which one,
so a pid the OS handed to another process does not read as the one that
wrote the file. Every platform has a reader: `/proc` on Linux, `ps` on other
POSIX systems, `GetProcessTimes` on Windows.

`missing_interpreter` reads the other half of a launch. A script found on
PATH still needs the interpreter its `#!` line names, and `#!/usr/bin/env
node` searches PATH a second time — so a CLI installed by npm resolves and
still exits 127 on a PATH with no `node`.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path


CREATE_BREAKAWAY_FROM_JOB = 0x01000000
ERROR_ACCESS_DENIED = 5


def launch_options(
    *, new_session: bool = False, interactive: bool = False,
    breakaway: bool = False, windows: bool | None = None,
) -> dict:
    """Subprocess policy: hide background consoles and preserve explicit groups.

    Interactive children inherit the caller's console. On Windows,
    DETACHED_PROCESS would defeat CREATE_NO_WINDOW, so it is never combined
    with the hidden-console policy. ``windows`` permits platform-branch tests.
    A new Windows process group disables Ctrl+C for that group; it does not
    provide POSIX tree termination. GUI and vendor-created child windows are
    outside this policy. I/O, environment and lifetime remain the caller's decisions.

    ``breakaway`` starts a Windows child outside the caller's job object
    (CREATE_BREAKAWAY_FROM_JOB), the way a new session leaves its group on
    POSIX, which has nothing to add. A job that forbids breakaway refuses
    the launch with ``ERROR_ACCESS_DENIED``; the caller decides whether to
    launch again without it.
    """
    if windows is None:
        windows = os.name == "nt"
    if windows:
        flags = 0 if interactive else 0x08000000  # CREATE_NO_WINDOW
        if new_session:
            flags |= 0x00000200  # CREATE_NEW_PROCESS_GROUP
        if breakaway:
            flags |= CREATE_BREAKAWAY_FROM_JOB
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True} if new_session else {}


def spawn(
    argv: list[str],
    *,
    cwd: Path | str,
    stdin_devnull: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start `argv` in its own process group on POSIX. `env` is the child's
    WHOLE environment when given, the way `subprocess` reads it — a caller
    adding one variable passes `{**os.environ, ...}`; None inherits."""
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL if stdin_devnull else None,
        **launch_options(new_session=True, interactive=not stdin_devnull),
        env=env,
    )


def missing_interpreter(program: Path | str, path: str | None = None) -> str | None:
    """The interpreter `program`'s `#!` line names, when a launch on `path`
    cannot run it. None when the interpreter resolves, and None for a binary,
    an unreadable file or a file with no `#!` line — those are not this
    function's failure to report. `path` is the PATH the launch will get;
    None reads this process's."""
    if os.name != "posix":
        return None
    try:
        with open(program, "rb") as f:
            head = f.read(256)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    words = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split()
    if not words:
        return None
    interpreter = words[0]
    if os.path.basename(interpreter) != "env":
        return None if os.access(interpreter, os.X_OK) else interpreter
    named = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
    if not named:
        return None
    return None if shutil.which(named[0], path=path) else named[0]


def pid_alive(pid: int) -> bool:
    """Whether `pid` names a live process, without disturbing it.

    On Windows, signal 0 IS `CTRL_C_EVENT` — not a probe CPython declined to
    add (bpo-14480, rejected). `os.kill(pid, 0)` used to be able to
    terminate the target anyway: a thirteen-year-old bug (bpo-14484 /
    gh-128932, fixed 2025-01-17, backported to 3.12/3.13) let a failed
    `GenerateConsoleCtrlEvent` fall through into `OpenProcess` +
    `TerminateProcess` instead of returning. Ask the kernel directly
    instead: `OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)`,
    then `WaitForSingleObject(handle, 0)` — `GetExitCodeProcess` alone can't
    tell a running process from one that exited with code 259
    (`STILL_ACTIVE`). A NULL handle from `OpenProcess` reads as dead only for
    `ERROR_INVALID_PARAMETER`; any other error (including access denied)
    reads as alive, mirroring the POSIX branch's `PermissionError` → True."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        ERROR_INVALID_PARAMETER = 87
        WAIT_TIMEOUT = 0x102

        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
        )
        if not handle:
            return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_pgid(pgid: int, pid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _end_tree(pid: int) -> None:
    """`taskkill /T /F` on `pid` and its descendants. When taskkill cannot
    run or does not succeed, `pid` alone is ended (`os.kill` is
    `TerminateProcess` on Windows)."""
    try:
        done = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **launch_options(),
        )
        if done.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def terminate_group(proc: subprocess.Popen | int, *, grace_seconds: float = 0.5) -> None:
    """SIGTERM `proc`'s process group, wait `grace_seconds`, then SIGKILL it.
    `proc` is a `Popen` (its `.pid` is used) or a bare pid. On Windows it
    ends the process tree at once (`_end_tree`); there is no group and no
    grace there.

    The pgid is captured once, before SIGTERM, and reused for SIGKILL. A
    group leader that can exit during the grace period (while a grandchild
    lingers, e.g. one that ignores SIGTERM) — or has already exited by the
    time `terminate_group` is even called — would otherwise make a second
    `os.getpgid(pid)` fail right when SIGKILL needs it most: the leader is
    gone, but the pid it leaves behind no longer resolves to the group the
    grandchild is still in. Reusing the captured pgid keeps both signals
    aimed at the whole group regardless of what happened to the leader in
    between. When `getpgid` itself can't resolve a pgid (it refuses even on
    a still-listed zombie on macOS), `pid` stands in as the pgid — true by
    construction for any `spawn`-started leader, since `start_new_session`
    makes a new session's pgid equal to its own pid. `killpg` failing on
    that guess (the group is genuinely gone, or `pid` was never a leader)
    falls back to a plain kill of the pid itself."""
    pid = proc.pid if isinstance(proc, subprocess.Popen) else proc
    if os.name != "posix":
        _end_tree(pid)
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    _signal_pgid(pgid, pid, signal.SIGTERM)
    time.sleep(grace_seconds)
    _signal_pgid(pgid, pid, signal.SIGKILL)


_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _job_kernel32():
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


def _set_job_limits(kernel32, job: int, flags: int) -> bool:
    """Set `JOBOBJECT_EXTENDED_LIMIT_INFORMATION` with only `flags` in
    `LimitFlags`. The layout is the 64-bit one (144 bytes) and the 32-bit one
    alike: SIZE_T and ULONG_PTR follow the pointer size."""
    import ctypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = Extended()
    info.BasicLimitInformation.LimitFlags = flags
    return bool(kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
        ctypes.sizeof(info),
    ))


def windows_job(pid: int) -> int | None:
    """On Windows, a new job object holding `pid`: the handle, which the
    caller holds for the process's lifetime and passes to `terminate_job`
    or `release_job`. None on any other platform, and None when the job
    cannot be made; the caller then has the process tree alone.

    Every process `pid` starts from now on joins the job, whether or not
    its parent is still alive, which is what `taskkill /T` cannot reach.
    Assign right after the spawn: a process `pid` started before this call
    is outside the job. The job ends its processes when its last handle
    closes (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), so a caller killed
    outright takes them with it; the handle is not inheritable, so no child
    keeps the job open. A child started with `CREATE_BREAKAWAY_FROM_JOB`
    may leave (`JOB_OBJECT_LIMIT_BREAKAWAY_OK`), as a POSIX child may leave
    its group with `setsid`."""
    if os.name != "nt":
        return None
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100
    try:
        kernel32 = _job_kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        process = None
        try:
            process = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, pid)
            if (
                process
                and _set_job_limits(
                    kernel32, job,
                    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK,
                )
                and kernel32.AssignProcessToJobObject(job, process)
            ):
                return job
        finally:
            if process:
                kernel32.CloseHandle(process)
        kernel32.CloseHandle(job)
    except (OSError, AttributeError):
        pass
    return None


def terminate_job(job: int) -> bool:
    """End every process in `job` (`TerminateJobObject`, exit code 1, as
    `taskkill /F` leaves). The handle stays open; `release_job` closes it."""
    try:
        return bool(_job_kernel32().TerminateJobObject(job, 1))
    except (OSError, AttributeError):
        return False


def release_job(job: int) -> None:
    """Close `job` and leave whatever still runs in it running: the kill on
    close is cleared first, so a process a finished wake left behind lives
    on, as it does on POSIX when a wake exits."""
    try:
        kernel32 = _job_kernel32()
        _set_job_limits(kernel32, job, _JOB_OBJECT_LIMIT_BREAKAWAY_OK)
        kernel32.CloseHandle(job)
    except (OSError, AttributeError):
        pass


CREATE_SUSPENDED = 0x00000004
SUSPENDED_EXIT_WAIT_S = 5.0


def _resume_process_threads(pid: int) -> bool:
    """Resume every thread of `pid` (a process started with CREATE_SUSPENDED
    has one, its primary thread). Popen closes the thread handle, so the
    threads are found in a Toolhelp thread snapshot and opened with
    `OpenThread`. True when at least one thread was resumed and none
    failed."""
    import ctypes

    TH32CS_SNAPTHREAD = 0x00000004
    THREAD_SUSPEND_RESUME = 0x0002

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ThreadID", ctypes.c_uint32),
            ("th32OwnerProcessID", ctypes.c_uint32),
            ("tpBasePri", ctypes.c_int32),
            ("tpDeltaPri", ctypes.c_int32),
            ("dwFlags", ctypes.c_uint32),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        kernel32.Thread32First.argtypes = kernel32.Thread32Next.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ThreadEntry),
        ]
        kernel32.OpenThread.restype = ctypes.c_void_p
        kernel32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.ResumeThread.restype = ctypes.c_uint32
        kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snapshot in (None, ctypes.c_void_p(-1).value):
            return False
        resumed = False
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(ThreadEntry)
            more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if not thread:
                        return False
                    try:
                        if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            return False
                        resumed = True
                    finally:
                        kernel32.CloseHandle(thread)
                more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return resumed
    except (OSError, AttributeError):
        return False


def _close_job(job: int) -> None:
    try:
        _job_kernel32().CloseHandle(job)
    except (OSError, AttributeError):
        pass


def _close_pipes(proc: subprocess.Popen) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def start_in_job(
    argv: list[str], **popen_kwargs
) -> tuple[subprocess.Popen, int | None, str | None]:
    """`subprocess.Popen(argv, **popen_kwargs)`, held in a Windows job object
    from its first instruction: `(process, job, note)`. On POSIX the job is
    None and so is the note.

    On Windows the process starts suspended (CREATE_SUSPENDED), is assigned
    to a new job (`windows_job`), then resumed (`_resume_process_threads`),
    so nothing it starts — a launcher's real program included, as a venv's
    python.exe or a `.cmd` shim starts one at once — can begin outside the
    job. The note is one line for the caller's log when the start did not
    go to plan:

    - the job could not be made or assigned: the process is resumed with no
      job, and a forced stop reaches its tree by parent links only;
    - it could not be resumed: it never ran, so it is terminated, and only
      once its exit is confirmed (`SUSPENDED_EXIT_WAIT_S`) and reaped is it
      started again, unsuspended and with no job. An exit that cannot be
      confirmed raises OSError: a second copy is never started beside a
      first that may still run."""
    if os.name != "nt":
        return subprocess.Popen(argv, **popen_kwargs), None, None
    flags = popen_kwargs.pop("creationflags", 0)
    proc = subprocess.Popen(argv, creationflags=flags | CREATE_SUSPENDED, **popen_kwargs)
    job = windows_job(proc.pid)
    if _resume_process_threads(proc.pid):
        if job is None:
            return proc, None, (
                "started with no job object (it could not be made or assigned); "
                "a forced stop reaches its processes by parent links only"
            )
        return proc, job, None
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=SUSPENDED_EXIT_WAIT_S)
    except subprocess.TimeoutExpired:
        if job is not None:
            _close_job(job)
        raise OSError(
            f"pid {proc.pid} could not be resumed from its suspended start, and its "
            f"end could not be confirmed; not starting it again"
        ) from None
    finally:
        _close_pipes(proc)
    if job is not None:
        _close_job(job)
    proc = subprocess.Popen(argv, creationflags=flags, **popen_kwargs)
    return proc, None, (
        "could not resume its suspended start, so it was ended and started "
        "again with no job object; a forced stop reaches its processes by "
        "parent links only"
    )


def _under(path: str, directory: str) -> bool:
    import ntpath

    path, directory = ntpath.normcase(path), ntpath.normcase(directory).rstrip("\\")
    return bool(directory) and path.startswith(directory + "\\")


def _venv_base(program: str) -> str | None:
    """The base interpreter a venv launcher at `program` starts (`home` or
    `executable` in its pyvenv.cfg), or None when it is not one."""
    import ntpath

    here = ntpath.dirname(program)
    for folder in (here, ntpath.dirname(here)):
        try:
            text = Path(folder, "pyvenv.cfg").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() in ("home", "executable") and value.strip():
                return value.strip()
        return ""
    return None


def _in_job(pid: int, job: int) -> bool | None:
    """`IsProcessInJob(pid, job)`: True or False when the call succeeds,
    None when the process cannot be opened or the call fails."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return None
        try:
            result = ctypes.c_int(0)
            if not kernel32.IsProcessInJob(process, job, ctypes.byref(result)):
                return None
            return bool(result.value)
        finally:
            kernel32.CloseHandle(process)
    except (OSError, AttributeError):
        return None


def windows_job_escape_note(
    argv0: str, pid: int, job: int, *, windows: bool | None = None,
) -> str | None:
    """One line for the log when a process started in `job` may have
    processes outside it, or None. A diagnostic only: nothing is changed.

    A packaged (MSIX, for example Microsoft Store) app activated through its
    app execution alias under `%LOCALAPPDATA%\\Microsoft\\WindowsApps`, or a
    venv launcher whose base interpreter is such a packaged app, starts the
    real program outside the caller's job, and everything it starts with
    it. The packaged interpreter run directly stays in the job. The leader
    itself is also checked: `IsProcessInJob(leader, job)`, read only when
    the call succeeds."""
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return None
    program = shutil.which(argv0) or argv0
    alias_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps")
    base = _venv_base(program)
    if os.environ.get("LOCALAPPDATA") and _under(program, alias_dir):
        how = "through an app execution alias"
    elif base and "\\windowsapps\\" in base.lower().replace("/", "\\") + "\\":
        how = "through a venv launcher of a packaged (Store) Python"
    elif _in_job(pid, job) is False:
        how = "but its leader is not in the job"
    else:
        return None
    return (
        f"started {how}; its processes may escape the job, so a forced stop "
        f"may leave some running"
    )


def _linux_start_token(stat: str, boot_id: str) -> str | None:
    """`<boot id> <starttime>` from the text of `/proc/<pid>/stat`. starttime
    is field 22, in clock ticks since boot, so the boot id carries it across
    a reboot. `comm` (field 2) may hold spaces and parentheses, so the count
    starts after its last `)`."""
    fields = stat[stat.rfind(")") + 1:].split()
    if len(fields) < 20:
        return None
    return f"{boot_id} {fields[19]}"


def _windows_start_token(pid: int) -> str | None:
    """`pid`'s creation time (`GetProcessTimes`) as 16 hex digits, which names
    one process start. None when the process cannot be opened or has exited:
    a handle another process still holds keeps an exited process's times
    readable, and that process must not read as the one a record names."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x102

    class FileTime(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(FileTime)] * 4
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
    )
    if not handle:
        return None
    try:
        if kernel32.WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:
            return None
        created, exited, kernel, user = (FileTime() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        return f"{created.dwHighDateTime:08x}{created.dwLowDateTime:08x}"
    finally:
        kernel32.CloseHandle(handle)


_START_TOKEN_FORMATS = {
    # `_linux_start_token`: the kernel boot id, a space, starttime in ticks.
    "linux": re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12} [0-9]+"
    ),
    # `ps -o lstart=` in UTC with LC_ALL=C, runs of spaces collapsed.
    "ps": re.compile(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
        r"[0-9]{1,2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}"
    ),
    # `_windows_start_token`: the creation FILETIME as 16 hex digits.
    "windows": re.compile(r"[0-9a-f]{16}"),
}


def _start_token_kind() -> str:
    """Which reader `process_start_token` uses on this machine."""
    if os.name == "nt":
        return "windows"
    if Path("/proc").is_dir() and Path("/proc/self/stat").exists():
        return "linux"
    return "ps"


def valid_start_token(token: object, kind: str | None = None) -> bool:
    """Whether `token` has the exact form `process_start_token` writes on
    this machine (or on `kind`: `linux`, `ps`, `windows`). A stamp that does
    not is not evidence that a pid was recycled: a caller that must not
    guess reads it as unknown."""
    pattern = _START_TOKEN_FORMATS[kind or _start_token_kind()]
    return isinstance(token, str) and pattern.fullmatch(token) is not None


def process_start_token(pid: int) -> str | None:
    """A string that names one process start: two reads agree only for the
    same process, so a pid the OS recycled reads differently.

    Linux reads `/proc/<pid>/stat` and the kernel boot id. Other POSIX
    systems ask `ps -o lstart=` in UTC, which has one-second resolution; a
    pid recycled within the same second as the first start goes unnoticed.
    Windows reads the creation time (`_windows_start_token`), in 100 ns
    units since 1601, so it holds across a reboot. None means the process
    is gone or the read failed, and the caller then falls back to liveness
    alone. A record written with no token (`""` or None) also reads by
    liveness alone."""
    if os.name == "nt":
        try:
            return _windows_start_token(pid)
        except (OSError, AttributeError):
            return None
    stat = Path(f"/proc/{pid}/stat")
    if _start_token_kind() == "linux":
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return _linux_start_token(stat.read_text(), boot_id)
        except OSError:
            return None
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
            **launch_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = " ".join(result.stdout.split())
    return started if result.returncode == 0 and started else None
