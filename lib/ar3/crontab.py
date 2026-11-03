"""The account's crontab, rewritten under one lock every writer shares.

A crontab is one file per OS account, whatever config home or install wrote
the lines in it. A read-modify-write that two writers run at once loses one
writer's lines, so every writer in the suite (a8s autostart, the ar3
auto-update trigger) takes the same lock, kept in a private directory of the
account itself, and edits through `rewrite`, which refuses to leave a line it
does not own changed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from ar3.fsio import atomic_write_text
from ar3.locking import file_lock
from ar3.proc import launch_options


class CrontabError(Exception):
    """The crontab could not be read, written or locked."""


def account_lock_root() -> Path:
    """A private directory of the OS account itself, found from the account
    database and never from HOME, TMPDIR, LOGNAME or a config home, so every
    config home of the account reaches the same lock. Tests replace this
    function; nothing else may."""
    import pwd

    uid = os.getuid()
    root = Path(pwd.getpwuid(uid).pw_dir) / ".a8s-locks"
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        st = root.lstat()
    except OSError as e:
        raise CrontabError(f"cannot prepare the account lock directory {root}: {e}") from e
    if root.is_symlink() or st.st_uid != uid or st.st_mode & 0o077:
        raise CrontabError(f"{root} is not a private directory owned by this account")
    return root


def _run(argv: list[str], *, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, input=input, capture_output=True, text=True, timeout=60,
            **launch_options(),
        )
    except FileNotFoundError as e:
        raise CrontabError(f"{argv[0]} is not installed") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CrontabError(f"{argv[0]} failed: {e}") from e


def read() -> str:
    """The crontab text; an account with no crontab reads as empty."""
    proc = _run(["crontab", "-l"])
    if proc.returncode == 0:
        return proc.stdout
    if "no crontab" in (proc.stderr or "").lower():
        return ""
    raise CrontabError(f"crontab -l failed: {(proc.stderr or '').strip() or proc.returncode}")


def rewrite(
    owned: Callable[[str], bool],
    add: str | None,
    backup: Path,
    *,
    lock_root: Path | None = None,
) -> None:
    """Drop every line `owned` claims, append `add` when given, and verify that
    no other line changed. The prior text is kept at `backup`."""
    root = account_lock_root() if lock_root is None else lock_root
    with file_lock(root / "crontab.lock"):
        _rewrite_locked(owned, add, backup)


def _rewrite_locked(owned: Callable[[str], bool], add: str | None, backup: Path) -> None:
    for _ in range(2):
        before = read()
        lines = before.splitlines()
        kept = [ln for ln in lines if not owned(ln)]
        if add is not None:
            kept.append(add)
        if read() == before:
            break
    else:
        raise CrontabError("crontab changed while it was being updated; try again")
    if kept == lines and (add is None or add in lines):
        return
    if before:
        atomic_write_text(backup, before, mode=0o600)
    if kept:
        proc = _run(["crontab", "-"], input="\n".join(kept) + "\n")
    elif before:
        proc = _run(["crontab", "-r"])
    else:
        return
    if proc.returncode != 0:
        raise CrontabError(f"crontab rejected the update: {(proc.stderr or '').strip() or proc.returncode}")
    after = read().splitlines()
    unrelated_before = [ln for ln in lines if not owned(ln)]
    unrelated_after = [ln for ln in after if not owned(ln) or ln == add]
    if [ln for ln in unrelated_after if ln != add] != unrelated_before:
        raise CrontabError(f"crontab changed during the update; the prior copy is {backup}")
    if (add is not None) != any(ln == add for ln in after):
        raise CrontabError("crontab does not hold the expected entry after the update")
