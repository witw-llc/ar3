"""One spawn-in-its-own-group + kill-the-group primitive for every Ark app.

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
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


def spawn(argv: list[str], *, cwd: Path | str, stdin_devnull: bool = True) -> subprocess.Popen:
    """Start `argv` in its own process group on POSIX."""
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL if stdin_devnull else None,
        start_new_session=(os.name == "posix"),
    )


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
