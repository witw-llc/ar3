"""One lock per browser seat, taken for the length of one script.

A wake, an `ar3 tool` session and the browser CLI all drive the same Chrome.
Each takes this lock around one script, so two scripts never interleave. The
lock is an OS file lock (`ar3.locking.file_lock`): the OS frees it when the
holder dies, so it is never stale. The lock file also holds a record of the
holder, for the busy message. The record is information only.
"""
import json
import os
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ar3 import clock
from ar3.locking import file_lock
from ar3.proc import pid_alive, process_start_token

from . import session

SEAT_WAIT_SECONDS = 60.0
WAIT_ENV = "AR3_BROWSER_SEAT_WAIT"
POLL_SECONDS = 0.2


class SeatBusy(Exception):
    """The seat stayed held for the whole wait."""

    def __init__(self, who):
        super().__init__(f"seat held by {who}")
        self.who = who


def seat_wait():
    try:
        return float(os.environ[WAIT_ENV])
    except (KeyError, ValueError):
        return SEAT_WAIT_SECONDS


def lock_path(seat):
    return Path(session.seat_home(seat)) / "lock"


def holder(seat):
    """The record of the live holder, or None."""
    try:
        record = json.loads(lock_path(seat).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return None
    if record.get("start") != process_start_token(pid):
        return None
    return record


def describe(record):
    if not record:
        return "another process"
    who = f"{record.get('kind', 'a process')}"
    if record.get("sender"):
        who += f" from {record['sender']}"
    return f"{who} (pid {record.get('pid')}) since {record.get('since', 'an unknown time')}"


@contextmanager
def hold(seat, kind, sender="", verb="", wait=None, on_wait=None):
    """Hold the seat until the block ends. Raises SeatBusy after `wait`
    seconds (default 60, or $AR3_BROWSER_SEAT_WAIT). `verb` goes in the record
    only. Never put it in a message: script text can hold secrets."""
    deadline = time.monotonic() + (seat_wait() if wait is None else wait)
    path = lock_path(seat)
    told = False
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(file_lock(path, blocking=False))
                break
            except BlockingIOError:
                if not told and on_wait is not None:
                    on_wait(describe(holder(seat)))
                told = True
                if time.monotonic() >= deadline:
                    raise SeatBusy(describe(holder(seat))) from None
                time.sleep(POLL_SECONDS)
        pid = os.getpid()
        record = {
            "pid": pid,
            "start": process_start_token(pid),
            "kind": kind,
            "sender": sender,
            "verb": verb,
            "since": clock.stamp(seconds=True),
        }
        try:
            path.write_text(json.dumps(record), encoding="utf-8")
        except OSError:
            pass
        try:
            yield
        finally:
            try:
                path.write_text("", encoding="utf-8")
            except OSError:
                pass
