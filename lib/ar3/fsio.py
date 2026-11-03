"""One atomic-write primitive for every ar3 app.

Writes to a same-directory temp file (a ULID-suffixed name, so concurrent
writers targeting the same path never collide) and `os.replace`s it into
place — the strongest guarantees observed across the suite's several
hand-rolled copies of this pattern: `fsync` before replace for a caller that
must survive a crash immediately after this call returns (a8s's secrets
file), `mode` to chmod the temp file before it ever becomes visible at the
final path (a secret is never briefly world-readable), and the temp file is
removed on any failure so a crash never leaves stray `.tmp` litter behind.
"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from ar3.ulid import new as new_ulid

# A rename fails on Windows while any process holds the source open, and a
# freshly written file in a watched folder is opened immediately by whatever
# watches it — a sync client, an antivirus scanner. The holder lets go in
# milliseconds, so the fix is to wait rather than to fail the write.
REPLACE_ATTEMPTS = 10
REPLACE_BACKOFF_SECONDS = 0.01
REPLACE_BACKOFF_CAP_SECONDS = 0.25


def stat_or_none(path: Path) -> os.stat_result | None:
    """Stat `path`, or None on any OSError.

    Python 3.12 re-raises PermissionError from Path.is_dir() and Path.is_file()
    when the target cannot be statted. A WSL process scanning /mnt/c/Users hits
    that on an unreadable Windows profile, and the profile sorts ahead of the
    one that holds the login. A caller that is only asking whether a path is
    there must not abort the rest of the scan.
    """
    try:
        return os.stat(path)
    except OSError:
        return None


def is_dir(path: Path) -> bool:
    """True when `path` is a directory. False on any OSError, including a missing path."""
    info = stat_or_none(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def is_file(path: Path) -> bool:
    """True when `path` is a regular file. False on any OSError, including a missing path."""
    info = stat_or_none(path)
    return info is not None and stat.S_ISREG(info.st_mode)


def exists(path: Path) -> bool:
    """True when `path` can be statted. False on any OSError, including PermissionError."""
    return stat_or_none(path) is not None


def replace_with_retry(src: Path | str, dst: Path | str) -> None:
    """`os.replace`, retried while another process holds `src` open.

    Raises the last `PermissionError` when the holder never lets go — a
    caller that cannot rename has not written, and must hear it.
    """
    delay = REPLACE_BACKOFF_SECONDS
    for _ in range(REPLACE_ATTEMPTS - 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 2, REPLACE_BACKOFF_CAP_SECONDS)
    os.replace(src, dst)


READ_ATTEMPTS = 20
READ_BACKOFF_SECONDS = 0.025


def read_text_retry(path: Path | str, encoding: str = "utf-8") -> str:
    """`Path.read_text`, retried briefly on PermissionError.

    On Windows a read that meets `os.replace` of the same file (every write
    here is one) can fail with a sharing violation for a moment. Bounded:
    `READ_ATTEMPTS` tries `READ_BACKOFF_SECONDS` apart, then the last
    `PermissionError` is raised, so a caller that must not guess can say
    the file could not be read. Any other error is raised at once.
    """
    for _ in range(READ_ATTEMPTS - 1):
        try:
            return Path(path).read_text(encoding=encoding)
        except PermissionError:
            time.sleep(READ_BACKOFF_SECONDS)
    return Path(path).read_text(encoding=encoding)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    fsync: bool = False,
    mode: int | None = None,
) -> None:
    """Write `text` to `path` via a same-directory temp file + `os.replace`,
    so a reader never observes a partial write and a killed writer never
    corrupts the target. Creates `path`'s parent directory if missing.
    """
    _atomic_write(Path(path), text, fsync=fsync, mode=mode)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    fsync: bool = False,
    mode: int | None = None,
) -> None:
    """`atomic_write_text` for bytes: `data` lands exactly as given, with no
    encoding and no line-ending translation, for a copy that must match its
    source byte for byte."""
    _atomic_write(Path(path), data, fsync=fsync, mode=mode)


def _atomic_write(
    path: Path, payload: str | bytes, *, fsync: bool, mode: int | None
) -> None:
    binary = isinstance(payload, bytes)
    how = {"mode": "wb"} if binary else {"mode": "w", "encoding": "utf-8"}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{new_ulid()}.tmp")
    try:
        if mode is not None:
            # Windows opens a bare descriptor in text mode unless told not to.
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if binary:
                flags |= getattr(os, "O_BINARY", 0)
            fd = os.open(tmp, flags, mode)
            f = os.fdopen(fd, **how)
        else:
            f = tmp.open(**how)
        try:
            f.write(payload)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        finally:
            f.close()
        replace_with_retry(tmp, path)
        if mode is not None:
            try:
                path.chmod(mode)
            except OSError:
                pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# The two errors that mean "nothing is there": the last name does not exist, or
# a parent on the way is a file. Every other OSError (PermissionError,
# IsADirectoryError, ELOOP, an I/O error) means something IS there and cannot
# be read.
_ABSENT = (FileNotFoundError, NotADirectoryError)


class DanglingLink(OSError):
    """A symlink is there but leads nowhere (an unmounted volume, an offline
    sync folder). Something IS configured; it just cannot be reached."""


def _check_links(path: Path) -> None:
    """Refuse a symlink, at the file or at any folder above it, that is present
    but cannot be followed. `open` reports that as FileNotFoundError, the same
    as a file that was never there, so the check looks at each link first."""
    here = Path(os.path.abspath(path))
    for step in (*reversed(here.parents), here):
        try:
            if step.is_symlink() and not step.exists():
                raise DanglingLink(
                    f"{step} is a symlink to {os.readlink(step)} and that "
                    f"leads nowhere (an unmounted or offline target)"
                )
        except PermissionError:
            return  # unreadable: the read itself will say so


def read_text_if_present(path: Path, encoding: str = "utf-8") -> str | None:
    """The text of `path`, or None only when it is genuinely absent.

    A policy file must tell "no policy" from "policy I cannot read". Python
    3.14's `Path.is_file()` answers False for an unreadable parent (3.12
    raises PermissionError instead), so `if not path.is_file()` reads
    existing policy as absent on one interpreter and crashes on the other.
    This opens the file and catches only the absent errors: PermissionError, a
    directory in the file's place, a symlink loop or any other OSError
    propagates for the caller to refuse on. Undecodable bytes raise
    UnicodeDecodeError (a ValueError), never an empty text."""
    _check_links(path)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except _ABSENT:
        return None
    return data.decode(encoding)


def is_present(path: Path) -> bool:
    """False only when `path` is genuinely absent (see `read_text_if_present`).
    A path that exists but cannot be statted or read counts as present, so the
    read that follows fails loudly instead of the caller concluding "no
    file"."""
    try:
        _check_links(path)
        os.stat(path)
    except _ABSENT:
        return False
    except OSError:
        return True
    return True
