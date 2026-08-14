"""One atomic-write primitive for every Ark app.

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
from pathlib import Path

from ark.ulid import new as new_ulid


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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{new_ulid()}.tmp")
    try:
        if mode is not None:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            f = os.fdopen(fd, "w", encoding="utf-8")
        else:
            f = tmp.open("w", encoding="utf-8")
        try:
            f.write(text)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        finally:
            f.close()
        os.replace(tmp, path)
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
