"""Consumed ledger — which envelopes this machine already took off a shared wire.

A wire that every machine reads cannot lose an envelope because one machine
read it: the others may still be offline. So nobody deletes on receive, and
each machine keeps its own record of the ULIDs it consumed, under its own
config home. A poll skips every ULID the ledger names, which makes a read
one-time per machine while the envelope stays on the wire for everyone else.
Time removes the envelope; the transport that owns the wire does that sweep.

`a8s start` runs a handler process per agent and each one appends to the same
remote's ledger, so the file has a cross-process mutex beside it. An append
never waits for that mutex: a lost append costs one redelivery, which the
seen-ids ring collapses, while a blocked append would cost delivery itself.
Only compaction waits, and it may always give up.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from core import MAX_SEEN_IDS

# Cross-process ledger mutex. One atomic exclusive create names one winner,
# exactly as `network.claim_message` does, and a holder that died mid-write is
# broken by the lock's own age.
LEDGER_LOCK_WAIT_SECONDS = 2.0
LEDGER_LOCK_POLL_SECONDS = 0.02
LEDGER_LOCK_STALE_SECONDS = 30.0

# The ULIDs whose envelopes are still on the wire, or None when the wire cannot
# answer right now. None skips the compaction: an unmounted folder or a failed
# listing would otherwise read as "every envelope is gone" and erase the record.
PresentIds = Callable[[], Optional[set]]
Warn = Callable[[str, str], None]


class ConsumedLedger:
    """One remote's consumed-ULID record on this machine.

    Args:
        path: the ledger file under the config home.
        remote_id: named in the warning an unwritable ledger owes the log.
        warn: the transport's say-it-once logger, `(key, message)`.
        present: answers which ULIDs are still on the wire; see `PresentIds`.
    """

    def __init__(
        self, path: Path, *, remote_id: str, warn: Warn, present: PresentIds
    ) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._remote_id = remote_id
        self._warn = warn
        self._present = present
        self._thread_lock = threading.Lock()
        self.ids: set[str] = set(self._lines())

    def __contains__(self, ulid: str) -> bool:
        return ulid in self.ids

    def _lines(self) -> list[str]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    def touch(self) -> None:
        """Create the ledger file, empty, if it does not exist yet."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
        except OSError:
            pass

    def _acquire(self) -> bool:
        """Take the sidecar mutex, or answer False after a bounded wait.

        Never raises and never blocks for long: a caller that loses the race
        degrades rather than stalling delivery, so failing to acquire has to be
        as cheap as acquiring.
        """
        path = self.lock_path
        deadline = time.monotonic() + LEDGER_LOCK_WAIT_SECONDS
        while True:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                os.close(
                    os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                )
                return True
            except FileExistsError:
                pass
            except OSError:
                return False
            try:
                held_for = time.time() - path.stat().st_mtime
            except OSError:
                held_for = 0.0  # released between the two calls; go round again
            if held_for > LEDGER_LOCK_STALE_SECONDS:
                # The holder died mid-write. Re-stamp before taking over so two
                # processes racing the same expiry do not both think they won.
                try:
                    prior = path.stat().st_mtime
                    os.utime(path, None)
                    if path.stat().st_mtime != prior:
                        return True
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(LEDGER_LOCK_POLL_SECONDS)

    def _release(self) -> None:
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    def record(self, *ulids: str) -> None:
        fresh = [u for u in ulids if u not in self.ids]
        if not fresh:
            return
        self.ids.update(fresh)
        with self._thread_lock:
            held = self._acquire()
            try:
                p = self.path
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    with p.open("a", encoding="utf-8") as f:
                        f.write("".join(u + "\n" for u in fresh))
                except OSError as e:
                    # An unwritable ledger redelivers every envelope on every
                    # restart, forever, and the wire looks fine while it does.
                    self._warn(
                        f"ledger:{type(e).__name__}",
                        f"WARN: remote {self._remote_id}: ledger write failed "
                        f"({e}); envelopes will be redelivered",
                    )
                    return
                # An append that lost the race still writes: a lost append
                # costs one redelivered envelope, and blocking on the ledger
                # would cost delivery itself. A compaction that lost the race
                # waits instead — it is opportunistic, and rewriting the file
                # while a sibling appends to it is what the mutex is for.
                if not held:
                    return
                if len(self._lines()) > MAX_SEEN_IDS:
                    self._compact()
            finally:
                if held:
                    self._release()

    def _compact(self) -> None:
        """Rewrite the ledger as the IDs whose envelopes are still on the wire.

        Called with the sidecar lock held. The cap is a trigger, not a bound.
        Nothing deletes an envelope on receive, so forgetting a ULID whose
        envelope is still there hands it back to the receive path at the next
        restart — a duplicate inbox write and a duplicate wake. Only the entries
        whose envelope is gone may go, and if that leaves the ledger above the
        cap it stays above the cap.
        """
        present = self._present()
        if present is None:
            return
        # Read last, and inside the lock: asking the wire can take a while, and
        # whatever a sibling process appended in that time is in the file
        # rather than in a list this call read on the way in.
        lines = self._lines()
        kept: list[str] = []
        keep_set: set[str] = set()
        for u in lines:
            if u in present and u not in keep_set:
                keep_set.add(u)
                kept.append(u)
        p = self.path
        tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
        try:
            tmp.write_text("".join(u + "\n" for u in kept), encoding="utf-8")
            os.replace(str(tmp), str(p))
        except OSError:
            return
        # Drop only what the rewrite dropped: a publish on another thread may
        # have appended an ID after `lines` was read, and it is still recorded.
        self.ids.difference_update(set(lines) - keep_set)
