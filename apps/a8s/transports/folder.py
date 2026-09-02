"""Folder Transport — envelopes across a directory somebody else syncs.

A laptop and a desktop already share a folder: Dropbox, iCloud Drive,
OneDrive, Drive. Point both machines at the same one and messages cross with
no broker, no port, no account and no host that either machine has to reach:

  a8s remote box ~/Dropbox/A8S

The wire is one file per message, named for the message's own ULID and
holding the same envelope bytes MQTT would carry. Nothing new is invented at
the boundary, so a network can mix a folder remote and a broker remote. What
this wire promises is at-least-once delivery — the ledger and the inbox
claim collapse the repeats — and nothing about order: a sync client
materializes files on its own schedule, so two machines publishing into one
folder arrive interleaved.

The path is the base, exactly as `a8s storage` reads it, so one folder can
carry both the envelopes and the attachment bundles they refer to:

  <base>/<ULID>.json     one envelope
  <base>/<ULID>/         that message's attachment bundle (sync_folder)

Every machine sharing the folder wants to read each envelope, and the machine
that read it first has no way to know how many others are still offline. So
each machine keeps its own ledger of consumed ULIDs under its config home,
and the folder itself is what gets swept: `--retain-days` (default 3, `0`
keeps forever) drops an envelope only when both the ULID mint time and the
file's mtime clear the window — a rewritten mtime only postpones the reap,
and a delayed send's fresh write is never swept by the publish that made it.
The sweep rides both a publish and the poll, at most once an hour, so a
machine that only receives still reaps. The folder is a wire, not an
archive; delivered mail is already archived per-machine in
`conversations.sqlite3`.

A machine that joins a folder which has carried mail for a month is not owed
that month, and the folder cannot be trusted to hold still while it registers
— a sync client downloads the backlog on its own schedule, long after the
command returned. So the cutoff is a ULID stamped into the spec at
registration (`joined`), not a snapshot of the listing: an envelope named
below it is somebody else's history whenever it lands.

A ULID orders by the clock that minted it, so that cutoff separates two
machines only as far as their clocks agree, and a peer running slow would
otherwise mint genuinely new mail into the discard pile. `JOIN_SKEW_GRACE_MS`
is the allowance, and it states the contract: cross-machine join ordering
assumes clocks within the grace, and the worst case for a machine joining a
busy folder is one grace period of recent backlog delivered as current mail.

The hazard is the same one `services/sync_folder.py` handles: a sync client
publishes a name before the bytes behind it land. A publish therefore writes
`.<ULID>.json.part` and renames, and a poll that reads a file whose parsed
`id` does not match its own name treats it as not here yet and tries again.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core import MAX_SEEN_IDS, folder_ledger_path, out
from transports import OnMessage, Transport, TransportError
from ar3 import clock
from ar3.fsio import (
    REPLACE_ATTEMPTS,
    REPLACE_BACKOFF_CAP_SECONDS,
    REPLACE_BACKOFF_SECONDS,
    replace_with_retry,
)
from ar3.ulid import is_ulid, new as new_ulid, parse as parse_ulid


# Recognized option keys. `node_tag`, `client_id` and `clean_session` are the
# loader's broker-session vocabulary and a folder has no session to name;
# they are accepted and ignored so `load_remotes` can hand every transport the
# same option bag.
_KNOWN_OPTS: set[str] = {
    "poll_seconds",
    "prefix",
    "retain_days",
    "probe",
    "joined",
    "node_tag",
    "client_id",
    "clean_session",
}

DEFAULT_POLL_SECONDS = 15.0
# A sync client's own latency is measured in seconds, so a tighter poll buys
# nothing and costs a directory listing per interval on somebody's laptop.
MIN_POLL_SECONDS = 1.0

# The folder must outlast the broker's ~24h retention and cover a 3-day
# weekend outage. Delivered mail is already archived per-machine in
# conversations.sqlite3, so the folder itself is a wire, not an archive.
DEFAULT_RETAIN_DAYS = 3

# The sweep is a directory pass; riding every publish and every poll would
# make it as frequent as either, so it self-throttles to once an hour instead.
SWEEP_INTERVAL_SECONDS = 3600

# How far below `joined` an envelope may be stamped and still count as new
# mail. A ULID is ordered by the clock of the machine that minted it, so
# without an allowance a peer whose clock runs behind this one publishes
# straight into the backlog and that mail is never delivered. Redelivery is the
# cheap side of the trade — the ledger already dedups anything the grace lets
# through, while a discarded envelope is gone — so the window is generously
# wider than any clock two consumer machines are likely to disagree by.
JOIN_SKEW_GRACE_MS = 3_600_000

# Cross-process ledger mutex. `a8s start` runs a handler process per agent and
# every one of them appends to this remote's single ledger, so a compaction in
# one process can otherwise `os.replace` away an append from another. Mirrors
# `network.claim_message`: one atomic exclusive create names one winner, and a
# holder that died mid-write is broken by the lock's own age.
LEDGER_LOCK_WAIT_SECONDS = 2.0
LEDGER_LOCK_POLL_SECONDS = 0.02
LEDGER_LOCK_STALE_SECONDS = 30.0


def _unlink_with_retry(path: Path) -> None:
    """Delete, waiting out a Windows holder. Never raises: every caller is
    cleaning up after itself and has nothing better to do about a failure."""
    delay = REPLACE_BACKOFF_SECONDS
    for _ in range(REPLACE_ATTEMPTS):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 2, REPLACE_BACKOFF_CAP_SECONDS)
        except OSError:
            return


def _envelope_id(envelope: bytes) -> str | None:
    """The ULID this envelope belongs under, or None if it is not one."""
    try:
        msg = json.loads(envelope)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict):
        return None
    msg_id = msg.get("id")
    if not isinstance(msg_id, str) or not is_ulid(msg_id):
        return None
    return msg_id


class FolderTransport(Transport):
    """One configured folder remote.

    Args:
        remote_id: stable name from `network.json`. Also names this remote's
            consumed-ULID ledger, so it may not contain a path separator.
        path: the folder both machines share. Absolute after `~` expansion.
        **opts: per-remote options forwarded from `network.json`. Recognized:
            poll_seconds (default 15, floored at 1), prefix (default none —
            the typed path is the base, matching `a8s storage`), retain_days
            (default 3, `0` keeps forever — a sweep of envelopes older than N
            days by both ULID mint time and mtime, riding publish and the
            poll, at most once an hour), probe (a reachability check instead
            of a poll thread; see `start`), joined (the ULID `a8s remote`
            stamped at registration — envelopes older than it are somebody
            else's backlog; absent means consume whatever is there).
    """

    def __init__(self, remote_id: str, *, path: str, **opts: Any) -> None:
        unknown = set(opts) - _KNOWN_OPTS
        if unknown:
            raise ValueError(
                f"remote {remote_id!r}: unknown option(s) {sorted(unknown)} "
                f"(known: {sorted(_KNOWN_OPTS)})"
            )
        seps = {os.sep, os.altsep or os.sep, "/"}
        if any(sep in remote_id for sep in seps):
            raise ValueError(
                f"remote {remote_id!r}: name cannot contain a path separator"
            )
        raw = (path or "").strip()
        if not raw:
            raise ValueError(f"remote {remote_id!r}: folder path is required")
        root = Path(raw).expanduser()
        if not root.is_absolute():
            raise ValueError(f"remote {remote_id!r}: folder path must be absolute")
        prefix_raw = opts.get("prefix")
        prefix = ("" if prefix_raw is None else str(prefix_raw)).strip("/")

        self._remote_id = remote_id
        self._root = root
        self._base = root / prefix if prefix else root
        self._poll_seconds = self._resolve_poll_seconds(remote_id, opts)
        self._retain_days = self._resolve_retain_days(remote_id, opts)
        self._joined = self._resolve_joined(remote_id, opts)
        self._cutoff_ms = (
            parse_ulid(self._joined)[0] - JOIN_SKEW_GRACE_MS if self._joined else 0
        )
        self._probe = bool(opts.get("probe", False))
        self._ledger_path = folder_ledger_path(remote_id)
        self._lock_path = self._ledger_path.with_suffix(
            self._ledger_path.suffix + ".lock"
        )
        self._ledger_lock = threading.Lock()
        self._consumed = self._read_ledger()
        self._warned: set[str] = set()
        self._on_message: Optional[OnMessage] = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._reachable = False
        self._last_sweep = 0.0

    @staticmethod
    def _resolve_poll_seconds(remote_id: str, opts: dict) -> float:
        raw = opts.get("poll_seconds")
        if raw is None or str(raw).strip() == "":
            return DEFAULT_POLL_SECONDS
        try:
            value = float(str(raw).strip())
        except ValueError:
            raise ValueError(f"remote {remote_id!r}: poll_seconds must be a number")
        return max(value, MIN_POLL_SECONDS)

    @staticmethod
    def _resolve_retain_days(remote_id: str, opts: dict) -> int:
        raw = opts.get("retain_days")
        if raw is None or str(raw).strip() == "":
            return DEFAULT_RETAIN_DAYS
        try:
            days = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"remote {remote_id!r}: retain_days must be a whole number")
        if days < 0:
            raise ValueError(f"remote {remote_id!r}: retain_days cannot be negative")
        return days

    @staticmethod
    def _resolve_joined(remote_id: str, opts: dict) -> str:
        raw = opts.get("joined")
        if raw is None or str(raw).strip() == "":
            return ""
        value = str(raw).strip().upper()
        if not is_ulid(value):
            raise ValueError(f"remote {remote_id!r}: joined must be a ULID")
        return value

    @property
    def id(self) -> str:
        return self._remote_id

    # ---------- ledger ----------

    def _read_ledger(self) -> set[str]:
        try:
            text = self._ledger_path.read_text(encoding="utf-8")
        except OSError:
            return set()
        return {line.strip() for line in text.splitlines() if line.strip()}

    def _ledger_lines(self) -> list[str]:
        try:
            text = self._ledger_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    def _acquire_ledger_lock(self) -> bool:
        """Take the sidecar mutex, or answer False after a bounded wait.

        Never raises and never blocks for long: a caller that loses the race
        degrades rather than stalling delivery, so failing to acquire has to be
        as cheap as acquiring.
        """
        path = self._lock_path
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

    def _release_ledger_lock(self) -> None:
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _record_consumed(self, *ulids: str) -> None:
        fresh = [u for u in ulids if u not in self._consumed]
        if not fresh:
            return
        self._consumed.update(fresh)
        with self._ledger_lock:
            held = self._acquire_ledger_lock()
            try:
                p = self._ledger_path
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    with p.open("a", encoding="utf-8") as f:
                        f.write("".join(u + "\n" for u in fresh))
                except OSError as e:
                    # An unwritable ledger redelivers every envelope on every
                    # restart, forever, and the folder looks fine while it does.
                    self._warn_once(
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
                if len(self._ledger_lines()) > MAX_SEEN_IDS:
                    self._compact()
            finally:
                if held:
                    self._release_ledger_lock()

    def _compact(self) -> None:
        """Rewrite the ledger as the IDs whose envelopes are still in the folder.

        Called with the sidecar lock held. The cap is a trigger, not a bound.
        Nothing deletes an envelope on receive, so forgetting a ULID whose file
        is still there hands that envelope back to the receive path at the next
        restart — a duplicate inbox write and a duplicate wake. Only the entries
        whose file is gone may go, and if that leaves the ledger above the cap
        it stays above the cap.
        """
        if not self._base.is_dir():
            # An unmounted folder lists as empty, which would read as "every
            # envelope is gone" and erase the whole record.
            return
        present = {p.stem for p in self._all_envelopes()}
        # Read last, and inside the lock: listing a synced folder can take a
        # while, and whatever a sibling process appended in that time is in the
        # file rather than in a list this call read on the way in.
        lines = self._ledger_lines()
        kept: list[str] = []
        keep_set: set[str] = set()
        for u in lines:
            if u in present and u not in keep_set:
                keep_set.add(u)
                kept.append(u)
        p = self._ledger_path
        tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
        try:
            tmp.write_text("".join(u + "\n" for u in kept), encoding="utf-8")
            os.replace(str(tmp), str(p))
        except OSError:
            return
        # Drop only what the rewrite dropped: a publish on another thread may
        # have appended an ID after `lines` was read, and it is still recorded.
        self._consumed.difference_update(set(lines) - keep_set)

    def touch_ledger(self) -> None:
        """Create the ledger file, empty, when `a8s remote` registers this one.

        What keeps a joining machine out of somebody else's backlog is the
        `joined` cutoff in the spec, not this file; the file marks that this
        node has been registered and has read nothing yet.
        """
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            self._ledger_path.touch(exist_ok=True)
        except OSError:
            pass

    # ---------- folder ----------

    def _all_envelopes(self) -> list[Path]:
        """Every envelope file in the base, oldest ULID first, cutoff ignored."""
        try:
            return sorted(
                p for p in self._base.glob("*.json") if is_ulid(p.stem) and p.is_file()
            )
        except OSError:
            return []

    def _listing(self) -> list[Path]:
        """The envelopes this machine is entitled to, oldest ULID first.

        The comparison is on the millisecond a ULID carries rather than on the
        string, because the boundary is `joined` minus `JOIN_SKEW_GRACE_MS` and
        that number is not a ULID. Everything from the grace boundary up is
        current mail; below it is a backlog that predates this membership. A
        spec with no cutoff — hand-written `network.json` — consumes what is
        there. Every name here passed `is_ulid`, so `parse_ulid` cannot raise.

        A cutoff is also how this transport fails silently: a clock that ran
        fast at registration stamps a `joined` no peer can reach, and every
        envelope is discarded as history. So the count says so once, and the
        operator has something to read in `a8s logs`.
        """
        files = self._all_envelopes()
        if not self._joined:
            return files
        kept = [p for p in files if parse_ulid(p.stem)[0] >= self._cutoff_ms]
        dropped = len(files) - len(kept)
        if dropped:
            # Keyed on the remote, not the count: a still-publishing peer under
            # a poisoned cutoff grows the count every poll, and a warning that
            # fires per poll is the log spam that trains an operator to stop
            # reading warnings.
            joined_local = clock.stamp(
                datetime.fromtimestamp(
                    parse_ulid(self._joined)[0] / 1000, tz=timezone.utc
                ),
                seconds=True,
            )
            self._warn_once(
                "backlog",
                f"WARN: remote {self._remote_id}: ignoring {dropped} "
                f"envelope{'' if dropped == 1 else 's'} predating this "
                f"machine's join ({self._joined} = {joined_local}) as backlog — "
                f"if that time is in the future, the clock was ahead at "
                f"registration; a8s unremote + re-add re-joins at now",
            )
        return kept

    def _sweep(self) -> None:
        """Drop envelopes past `retain_days`, once both clocks agree they
        are old.

        Self-throttled to once an hour: this rides both `publish` and every
        poll, and a directory pass on every one of those would cost a listing
        per publish and per poll interval for a check that only ever matters
        once an hour. A candidate is reaped only when both its ULID mint time
        and its file's mtime clear the window — either clock alone fails in a
        different direction. mtime alone can be rewritten forward by a
        resync, which only postpones the reap; mint time alone would
        classify a delayed send as already expired, so a sender resuming
        after an outage longer than the window would delete its own
        just-written file and report success. Requiring both means a fresh
        write always survives — its mtime is now — while an untouched old
        file still ages out.
        """
        now = time.time()
        if now - self._last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        if not self._retain_days:
            return
        self._last_sweep = now
        cutoff_ms = (now - self._retain_days * 86400) * 1000
        cutoff = now - self._retain_days * 86400
        for path in self._all_envelopes():
            try:
                if parse_ulid(path.stem)[0] >= cutoff_ms:
                    continue
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink(missing_ok=True)
            except OSError:
                continue

    def _check_reachable(self) -> None:
        """Answer whether this machine can reach the folder right now.

        A daemon tolerates a missing folder because a sync mount can arrive
        minutes after login. `a8s health` is the operator asking the question
        directly, so it does not — and it creates nothing: a check that
        manufactures the directory it is checking can never report the one
        thing the operator is asking about.
        """
        if not self._root.is_dir():
            raise TransportError(f"{self._remote_id}: folder not found: {self._root}")
        if not self._base.is_dir():
            raise TransportError(
                f"{self._remote_id}: prefix folder not found: {self._base}"
            )
        probe = self._base / f".a8s-health-{new_ulid()}.part"
        try:
            probe.write_bytes(b"")
        except OSError as e:
            raise TransportError(f"{self._remote_id}: folder not writable: {e}") from e
        finally:
            _unlink_with_retry(probe)

    # ---------- Transport ----------

    def start(self, on_message: OnMessage) -> None:
        if self._started:
            raise TransportError(f"{self._remote_id}: already started")
        self._on_message = on_message
        if self._probe:
            self._check_reachable()
            self._reachable = True
            self._started = True
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f"a8s-folder-{self._remote_id}",
            daemon=True,
        )
        self._thread.start()
        self._started = True
        # Say what is true right now rather than what the operator hoped for:
        # a folder that never mounts otherwise reports itself connected until
        # somebody goes looking for the mail that never came. The poll loop
        # keeps this current from here on.
        self._reachable = self._root.is_dir()

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None
        self._started = False

    def is_connected(self) -> bool:
        return self._reachable

    def publish(self, envelope: bytes) -> None:
        msg_id = _envelope_id(envelope)
        if msg_id is None:
            raise TransportError(
                f"{self._remote_id}: envelope is not a JSON object with a ULID id"
            )
        staging = self._base / f".{msg_id}.json.part"
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            staging.write_bytes(envelope)
            # Retried: a sync client or a scanner opens a new file in a watched
            # folder the moment it appears, and on Windows a rename of a file
            # somebody else holds fails outright. Losing the race here would
            # cost the message a full backoff cycle.
            replace_with_retry(staging, self._base / f"{msg_id}.json")
        except OSError as e:
            # Single attempt: the replace retry already spent its budget, and a
            # .part left behind is inert — the envelope glob never matches it,
            # and a retry of the same envelope overwrites the same name.
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
            raise TransportError(
                f"{self._remote_id}: folder write failed: {e}"
            ) from e
        # We publish into the folder we read from, so our own envelope is
        # consumed the moment it is written.
        self._record_consumed(msg_id)
        self._sweep()

    # ---------- poll ----------

    def _poll_loop(self) -> None:
        while True:
            self._poll_once()
            if self._stop.wait(self._poll_seconds):
                return

    def _poll_once(self) -> None:
        self._reachable = self._root.is_dir()
        self._sweep()
        for path in self._listing():
            stem = path.stem
            if stem in self._consumed:
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            inner = _envelope_id(raw)
            # Case-folded, because `is_ulid` is: a name that disagrees with its
            # own envelope only in case is the same message, and comparing the
            # strings would re-read that file on every poll forever.
            if inner is None or inner.upper() != stem.upper():
                # Half-arrived is the sync client's normal mid-flight state,
                # so this is retried rather than consumed. The warning fires
                # once per file so one that never completes cannot fill a log.
                self._warn_once(
                    f"partial:{path.name}",
                    f"WARN: remote {self._remote_id}: {path.name} is not a "
                    f"whole envelope yet",
                )
                continue
            cb = self._on_message
            if cb is None:
                continue
            try:
                cb(raw)
            except Exception as e:
                # Retried on the next pass, and the ledger is not stamped — so
                # a handler that fails every time is an invisible 15-second
                # loop unless it says so.
                self._warn_once(
                    f"deliver:{type(e).__name__}",
                    f"WARN: remote {self._remote_id}: delivery failed "
                    f"({type(e).__name__}: {e}); retrying",
                )
                continue
            self._record_consumed(stem)

    def _warn_once(self, key: str, message: str) -> None:
        """Say it the first time and never again for the same `key`.

        This transport's failures are all shaped like nothing arriving, so
        every one of them owes the operator a line in `a8s logs` — and a poll
        loop owes it exactly once, not every fifteen seconds.
        """
        if key in self._warned:
            return
        self._warned.add(key)
        out(message)
