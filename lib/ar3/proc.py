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

`pid_alive` and `process_start_token` are the two halves of reading a pid
file: the first says a process holds the number, the second says which one,
so a pid the OS handed to another process after a reboot does not read as
the one that wrote the file.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


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
        start_new_session=(os.name == "posix"),
        env=env,
    )


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


def terminate_group(proc: subprocess.Popen | int, *, grace_seconds: float = 0.5) -> None:
    """SIGTERM `proc`'s process group, wait `grace_seconds`, then SIGKILL it.
    `proc` is a `Popen` (its `.pid` is used) or a bare pid. No-op on Windows
    beyond a plain `SIGTERM`/`kill` — there is no process group to target.

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
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    _signal_pgid(pgid, pid, signal.SIGTERM)
    time.sleep(grace_seconds)
    _signal_pgid(pgid, pid, signal.SIGKILL)


def _linux_start_token(stat: str, boot_id: str) -> str | None:
    """`<boot id> <starttime>` from the text of `/proc/<pid>/stat`. starttime
    is field 22, in clock ticks since boot, so the boot id carries it across
    a reboot. `comm` (field 2) may hold spaces and parentheses, so the count
    starts after its last `)`."""
    fields = stat[stat.rfind(")") + 1:].split()
    if len(fields) < 20:
        return None
    return f"{boot_id} {fields[19]}"


def process_start_token(pid: int) -> str | None:
    """A string that names one process start: two reads agree only for the
    same process, so a pid the OS recycled after a reboot reads differently.

    Linux reads `/proc/<pid>/stat` and the kernel boot id. Other POSIX
    systems ask `ps -o lstart=` in UTC, which has one-second resolution; a
    pid recycled within the same second as the first start goes unnoticed.
    Windows returns None: there is no reader here yet, and the pid-only
    liveness check stands. None also means the process is gone or the read
    failed, and the caller then falls back to liveness alone."""
    if os.name == "nt":
        return None
    stat = Path(f"/proc/{pid}/stat")
    if stat.parent.parent.is_dir() and Path("/proc/self/stat").exists():
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
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = " ".join(result.stdout.split())
    return started if result.returncode == 0 and started else None
