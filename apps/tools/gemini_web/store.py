"""One Gemini conversation per correspondent, remembered across restarts.

A correspondent who gets a new conversation every message gets an agent with no
memory of the last thing it said, which is the whole value of a chat. The store
is the map from a sender to the conversation URL that belongs to them.

It is small, it is JSON, and it is written whole: a turn either records the new
conversation or it does not. A store that cannot be read is treated as empty
and said out loud in the reply, because a wake that dies on its own bookkeeping
loses the message it was woken for.

**Every mutation reloads under an exclusive lock.** An atomic replace makes each
write whole, which is not the same as making two writers safe: without the lock
two runs both read the same map, each add their own correspondent, and the
second replace erases the first one's conversation. That sender then gets a new
chat with no memory, which is the exact failure this file exists to prevent.
"""
import errno
import json
import os
import tempfile
import time

try:
    import fcntl
except ImportError:  # Windows has no fcntl; the handler answers before any lock.
    fcntl = None

APP_DIR = os.path.join("ar3", "gemini-web")

# How long a mutation waits for another run's lock before giving up. Short: a
# store write takes microseconds, so a wait this long means a run died holding
# the file, and the caller has a sentence to say about it either way.
LOCK_WAIT_SECONDS = 10.0


class StoreError(Exception):
    """The store could not be read or written — a disk or permission failure."""


class Busy(Exception):
    """Another run of this seat holds the lock."""


def state_root(seat):
    """Where one seat's sessions live, off the install directory an update replaces."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_DIR, seat)


class FileLock:
    """An exclusive lock on one path, with a bounded wait.

    Shared with the pending outbox, which needs the same thing for the same
    reason: a mutation two runs can both start is a mutation that loses work.

    flock is released when the file closes and when the process dies, so a run
    killed mid-turn does not leave the seat locked out.
    """

    def __init__(self, path, timeout=0.0, poll=0.05):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._handle = None

    def __enter__(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._handle = open(self.path, "a+")
        except OSError as exc:
            # The same failure the store itself has, one step earlier: a
            # directory nobody can write. It gets the same sentence.
            raise StoreError(f"the lock at {self.path} could not be opened: {exc}") from exc
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    self._close()
                    raise StoreError(
                        f"the lock at {self.path} could not be taken: {exc}"
                    ) from exc
                if time.monotonic() >= deadline:
                    self._close()
                    raise Busy(self.path) from None
                time.sleep(self.poll)

    def __exit__(self, *_):
        self._close()
        return False

    def _close(self):
        if self._handle is not None:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
            except OSError:
                pass
            self._handle.close()
            self._handle = None


def turn_lock(seat, root=None, timeout=0.0):
    """One turn at a time per seat.

    The store is not the only state a turn shares: the browser seat is one
    window, and two turns interleaved in it navigate away from each other's
    conversation and collect each other's replies. This lock covers a whole
    turn, from the first navigation to the reply, so a manual `ask` and a wake
    cannot overlap.
    """
    root = root or state_root(seat)
    return FileLock(os.path.join(root, f".{seat}.turn.lock"), timeout=timeout)


class SessionStore:
    """The conversations one a8s seat holds, keyed by correspondent."""

    def __init__(self, seat, root=None):
        self.seat = seat
        self.root = root or state_root(seat)
        self.warning = ""
        self._sessions = None

    @property
    def path(self):
        return os.path.join(self.root, f"{self.seat}.json")

    @property
    def _lock_path(self):
        return os.path.join(self.root, f".{self.seat}.store.lock")

    def _read_file(self):
        """The stored map, or {} with `warning` set to why it is empty."""
        try:
            with open(self.path) as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self.warning = (
                f"the session store at {self.path} could not be read ({exc}); "
                "this turn starts a fresh conversation"
            )
            return {}
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, dict):
            self.warning = (
                f"the session store at {self.path} is not in the expected shape; "
                "this turn starts a fresh conversation"
            )
            return {}
        # An entry counts only when its url is a string. Anything else is a
        # hand-edit or a foreign writer, and carrying it on would fail deep in
        # the browser with a TypeError instead of here with a sentence.
        return {
            name: entry
            for name, entry in sessions.items()
            if isinstance(entry, dict) and isinstance(entry.get("url"), str)
        }

    def _load(self):
        if self._sessions is None:
            self._sessions = self._read_file()
        return self._sessions

    def all(self):
        return dict(self._load())

    def get(self, sender):
        return self._load().get(sender.lower())

    def remember(self, sender, url):
        def change(sessions):
            sessions[sender.lower()] = {
                "url": url,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            return True

        self._mutate(change)

    def forget(self, sender):
        def change(sessions):
            return sessions.pop(sender.lower(), None) is not None

        return self._mutate(change)

    def _mutate(self, change):
        """Reload, change, write — all inside the lock.

        The reload is the point. Whatever this process cached is stale the
        moment another run writes, and a change applied to a stale map deletes
        that run's work.
        """
        try:
            with FileLock(self._lock_path, timeout=LOCK_WAIT_SECONDS):
                sessions = self._read_file()
                changed = change(sessions)
                if changed:
                    self._write(sessions)
                self._sessions = sessions
                return changed
        except Busy as exc:
            raise StoreError(
                f"another run of seat {self.seat!r} is holding the session store "
                f"({exc}); nothing was changed"
            ) from None

    def _write(self, sessions):
        """Whole-file, atomic: a half-written store is one nobody can read."""
        try:
            os.makedirs(self.root, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", dir=self.root, prefix=f".{self.seat}-", suffix=".json", delete=False
            )
        except OSError as exc:
            raise StoreError(self._unwritable(exc)) from exc
        try:
            with handle:
                json.dump({"sessions": sessions}, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(handle.name, self.path)
        except OSError as exc:
            self._discard(handle.name)
            raise StoreError(self._unwritable(exc)) from exc
        except BaseException:
            self._discard(handle.name)
            raise

    def _unwritable(self, exc):
        return f"the session store at {self.path} could not be written: {exc}"

    @staticmethod
    def _discard(path):
        try:
            os.unlink(path)
        except OSError:
            pass
