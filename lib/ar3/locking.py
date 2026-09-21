"""Advisory file locks released by the OS when their owning process exits."""
from contextlib import contextmanager
import errno
import os
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def file_lock(path, *, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking:
                        raise BlockingIOError("lock held") from exc
                    time.sleep(0.02)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        locked = True
        yield
    finally:
        try:
            if locked and os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
