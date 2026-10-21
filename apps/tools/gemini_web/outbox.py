"""Replies that were produced but not delivered.

A turn spends a real conversation with Gemini. If `tell` then fails — the
router down, the broker unreachable — the answer exists and nobody has it, and
re-running the turn to get it again would ask Gemini the same question twice.
So a reply that cannot be handed over is written here, and the next run of this
seat delivers it before it does anything else.

A reply's attachments are *copied* here beside it, not referenced. The files a
turn produces are browser-seat artifacts, and that directory is swept; a queue
holding a path into it would deliver an answer that names a file nobody has,
which is worse than not delivering at all.

One file per reply, named so they sort oldest first, each written atomically.
Two things keep a reply from going out twice:

- **One flusher at a time.** `flush` holds an exclusive lock for its whole run.
  A second run finds it taken and leaves the queue alone rather than racing it.
- **A claim carries its own age.** Inside the lock, a reply is renamed before
  it is sent, and the claim's *name* records when it was taken. A rename keeps
  the file's mtime, so an mtime-based expiry would call the brand-new claim on
  an hour-old reply already stale — and a second run would deliver it again
  while the first send was still in flight. The name is written by the rename
  itself, so there is no window in which a live claim looks abandoned.

Recovery is what the age is for: a run that dies mid-send drops its lock with
it, and the next run puts its claim back once the claim is old enough that
nothing can still be sending it.
"""
import json
import os
import shutil
import tempfile
import time
import uuid

from .store import Busy, FileLock, StoreError

SUFFIX = ".pending"
CLAIM = ".claim-"
# Bytes of a held reply's attachments, beside the reply that names them.
FILES = ".files"

# How long a claim is respected before it is taken to belong to a run that
# died. `tell` takes milliseconds and only one flusher runs at a time, so
# nothing healthy is ever reclaimed; without it a crash would strand a reply.
RECLAIM_SECONDS = 300.0


def _files_dir(path):
    """Where one held reply's attachment copies live.

    Derived from the reply's own name with any claim suffix stripped, because a
    claim renames the reply and the files must still be found under the name
    they were written with.
    """
    base = path.split(CLAIM, 1)[0]
    if base.endswith(".json"):
        base = base[: -len(".json")]
    return base + FILES


def _held_files(path, names):
    """Paths of the attachment copies a held reply names, that are really there.

    A name whose bytes have gone is dropped rather than raised on: the reply is
    still worth delivering, and `tell` would refuse the whole thing over one
    missing file.
    """
    directory = _files_dir(path)
    found = []
    for name in names or ():
        full = os.path.join(directory, os.path.basename(str(name)))
        if os.path.isfile(full):
            found.append(full)
    return found


class Outbox:
    """Undelivered replies for one seat, oldest first."""

    def __init__(self, seat, root):
        self.seat = seat
        self.root = root

    @property
    def path(self):
        return os.path.join(self.root, f"{self.seat}{SUFFIX}")

    @property
    def lock_path(self):
        return os.path.join(self.root, f".{self.seat}.flush.lock")

    def keep(self, recipient, body, files=()):
        """Hold a reply that could not be delivered. Returns its file.

        Attachments are copied in before the reply is named, so a queue entry
        that exists is one whose files exist too. A file that cannot be copied
        is dropped from the reply and named in the body rather than failing the
        hold: the answer is worth more than the attachment, and the turn that
        produced it has already been spent.
        """
        os.makedirs(self.path, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=self.path, prefix=f"{time.time():014.3f}-", suffix=".tmp", delete=False
        )
        final = handle.name[: -len(".tmp")] + ".json"
        kept, lost = self._copy_files(_files_dir(final), files)
        if lost:
            body = body.rstrip() + "\n\n--\n" + "\n".join(lost)
        payload = {
            "to": recipient,
            "body": body,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": kept,
        }
        with handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(handle.name, final)
        return final

    @staticmethod
    def _copy_files(directory, files):
        """Copy each attachment in beside the reply. Returns (names, notes)."""
        kept = []
        lost = []
        for source in files or ():
            name = os.path.basename(source)
            try:
                os.makedirs(directory, exist_ok=True)
                shutil.copyfile(source, os.path.join(directory, name))
            except OSError as exc:
                lost.append(f"{name} could not be held for redelivery ({exc}).")
                continue
            kept.append(name)
        return kept, lost

    def waiting(self):
        """Each unclaimed reply as (path, recipient, body, files), oldest first.

        A file that is not readable JSON is skipped rather than raised on: it
        is one lost reply, and dying here would lose the turn that is running.
        A claimed file belongs to a flush and is not listed; only `flush` puts
        claims back, and only under the lock.
        """
        try:
            names = sorted(name for name in os.listdir(self.path) if name.endswith(".json"))
        except OSError:
            return []
        held = []
        for name in names:
            full = os.path.join(self.path, name)
            try:
                with open(full) as handle:
                    payload = json.load(handle)
                held.append((
                    full,
                    str(payload["to"]),
                    str(payload["body"]),
                    _held_files(full, payload.get("files")),
                ))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return held

    def flush(self, send):
        """Try every held reply. Returns (delivered, still waiting).

        `send` takes `(recipient, body, files)` and answers with a status, 0
        for delivered. It is the network sender rather than whatever the current
        call does with its own reply.
        A reply that fails again is put back where it was, in order, and the
        next run tries it.

        A run that cannot take the lock does nothing at all: another run is
        already working the same queue, and both of them delivering is the
        failure this exists to prevent.
        """
        if not os.path.isdir(self.path):
            return 0, 0
        try:
            with FileLock(self.lock_path):
                self._recover()
                return self._send_each(send)
        except Busy:
            return 0, len(self.waiting())
        except (OSError, StoreError):
            # A disk that cannot be read or locked leaves every held reply where
            # it is. The next run tries again; this turn must still run.
            return 0, len(self.waiting())

    def _send_each(self, send):
        delivered = 0
        for path, recipient, body, files in self.waiting():
            claimed = self._claim(path)
            if claimed is None:
                continue
            try:
                ok = int(send(recipient, body, files) or 0) == 0
            except OSError:
                self._restore(claimed, path)
                break
            except BaseException:
                self._restore(claimed, path)
                raise
            if not ok:
                self._restore(claimed, path)
                break
            self.drop(claimed)
            delivered += 1
        return delivered, len(self.waiting())

    def _claim(self, path):
        """Take hold of one held reply, stamping the claim with the time.

        The stamp is in the name because the rename that creates it is the same
        act that takes the claim. Reading the age off the file instead would
        read the moment the reply was *enqueued*, which says nothing about when
        somebody started sending it.
        """
        claimed = f"{path}{CLAIM}{time.time():.3f}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            os.rename(path, claimed)
        except OSError:
            return None
        return claimed

    @staticmethod
    def _claimed_at(name):
        """When a claim was taken, read from its name; None if unreadable."""
        try:
            return float(name.split(CLAIM, 1)[1].split("-", 1)[0])
        except (IndexError, ValueError):
            return None

    def _recover(self):
        """Put back claims old enough that nothing can still be sending them.

        Only called inside the lock. A claim with no readable stamp was not
        made by this code, so it is restored rather than left forever.
        """
        try:
            names = os.listdir(self.path)
        except OSError:
            return
        for name in names:
            if CLAIM not in name:
                continue
            taken = self._claimed_at(name)
            if taken is not None and time.time() - taken < RECLAIM_SECONDS:
                continue
            try:
                os.rename(
                    os.path.join(self.path, name),
                    os.path.join(self.path, name.split(CLAIM, 1)[0]),
                )
            except OSError:
                continue

    @staticmethod
    def _restore(claimed, path):
        """Put a claim back so the next run finds it, in its original order."""
        try:
            os.rename(claimed, path)
        except OSError:
            pass

    @staticmethod
    def drop(path):
        """Forget a delivered reply, and the attachment copies held with it."""
        shutil.rmtree(_files_dir(path), ignore_errors=True)
        try:
            os.unlink(path)
        except OSError:
            pass
