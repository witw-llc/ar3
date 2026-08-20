"""Tests for the folder transport.

No broker and no network: the whole wire is a directory. `fake_home` gives
each test its own config home, which is where the consumed-ULID ledger lives.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ark.ulid import new as new_ulid
from core import folder_ledger_path
from transports import TransportError
from transports.folder import FolderTransport


def _envelope(msg_id: str, content: str = "hi") -> bytes:
    return json.dumps(
        {"id": msg_id, "from": "SENDER", "to": "TARGET", "content": content}
    ).encode("utf-8")


def _ulid_at(prefix: str) -> str:
    """A valid ULID whose sort position is fixed by its leading characters.

    Real ULIDs minted this decade start `01K`, so `01A` is history and `01Z`
    is the future no matter when the suite runs.
    """
    return (prefix + "0" * 26)[:26]


def _ulid_at_ms(ms: int) -> str:
    """A ULID carrying an exact millisecond, with a zeroed random half.

    The skew grace is measured in milliseconds, so these tests need to place a
    ULID a known distance from another one rather than merely above or below.
    """
    from ark.ulid import ALPHABET

    chars = []
    n = ms
    for _ in range(10):
        chars.append(ALPHABET[n & 0x1F])
        n >>= 5
    return "".join(reversed(chars)) + "0" * 16


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "Dropbox" / "A8S"
    d.mkdir(parents=True)
    return d


def _transport(folder, **opts) -> FolderTransport:
    return FolderTransport(remote_id="box", path=str(folder), **opts)


class TestPublish:
    def test_writes_envelope_named_for_its_ulid(self, fake_home, folder):
        t = _transport(folder)
        msg_id = new_ulid()
        raw = _envelope(msg_id)
        t.publish(raw)
        written = folder / f"{msg_id}.json"
        assert written.read_bytes() == raw
        assert [p.name for p in folder.iterdir()] == [written.name]

    def test_invalid_envelope_raises_and_writes_nothing(self, fake_home, folder):
        t = _transport(folder)
        for bad in (b"not json", b'["a","list"]', b'{"id":"nope"}', b"{}"):
            with pytest.raises(TransportError):
                t.publish(bad)
        assert list(folder.iterdir()) == []

    def test_own_publish_is_never_handed_to_own_callback(self, fake_home, folder):
        seen: list[bytes] = []
        t = _transport(folder)
        t.start(seen.append)
        try:
            t.publish(_envelope(new_ulid()))
            t._poll_once()
        finally:
            t.stop()
        assert seen == []

    def test_prefix_puts_envelopes_under_a_subfolder(self, fake_home, folder):
        t = _transport(folder, prefix="mail")
        msg_id = new_ulid()
        t.publish(_envelope(msg_id))
        assert (folder / "mail" / f"{msg_id}.json").is_file()

    def test_a_held_staging_file_is_waited_out(self, fake_home, folder, monkeypatch):
        """Windows: a sync client or a scanner opens the new file the moment it
        appears, and a rename of a file somebody holds fails outright. The
        holder lets go in milliseconds, so the publish waits rather than
        throwing the message into a backoff cycle."""
        import os

        real_replace = os.replace
        calls: list[int] = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(32, "being used by another process")
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", flaky)
        t = _transport(folder)
        msg_id = new_ulid()
        t.publish(_envelope(msg_id))
        assert (folder / f"{msg_id}.json").is_file()
        assert len(calls) == 3

    def test_a_rename_that_never_wins_is_reported(self, fake_home, folder, monkeypatch):
        import os

        import ark.fsio as fsio

        monkeypatch.setattr(fsio, "REPLACE_BACKOFF_SECONDS", 0.0)
        monkeypatch.setattr(fsio, "REPLACE_BACKOFF_CAP_SECONDS", 0.0)

        def never(_src, _dst):
            raise PermissionError(32, "being used by another process")

        monkeypatch.setattr(os, "replace", never)
        t = _transport(folder)
        with pytest.raises(TransportError, match="folder write failed"):
            t.publish(_envelope(new_ulid()))
        assert list(folder.iterdir()) == []


class TestPoll:
    def test_delivers_exactly_once(self, fake_home, folder):
        msg_id = new_ulid()
        (folder / f"{msg_id}.json").write_bytes(_envelope(msg_id))
        seen: list[bytes] = []
        t = _transport(folder)
        t._on_message = seen.append
        t._poll_once()
        assert len(seen) == 1
        t._poll_once()
        assert len(seen) == 1
        # The envelope stays put — other machines still have to read it.
        assert (folder / f"{msg_id}.json").is_file()

    def test_ledger_survives_a_restart(self, fake_home, folder):
        msg_id = new_ulid()
        (folder / f"{msg_id}.json").write_bytes(_envelope(msg_id))
        first: list[bytes] = []
        t1 = _transport(folder)
        t1._on_message = first.append
        t1._poll_once()
        assert len(first) == 1

        second: list[bytes] = []
        t2 = _transport(folder)
        t2._on_message = second.append
        t2._poll_once()
        assert second == []

    def test_truncated_json_is_retried_until_whole(self, fake_home, folder, capsys):
        msg_id = new_ulid()
        path = folder / f"{msg_id}.json"
        raw = _envelope(msg_id)
        path.write_bytes(raw[: len(raw) // 2])
        seen: list[bytes] = []
        t = _transport(folder)
        t._on_message = seen.append
        t._poll_once()
        t._poll_once()
        assert seen == []
        # Warned once, not once per poll.
        assert capsys.readouterr().out.count(path.name) == 1

        path.write_bytes(raw)
        t._poll_once()
        assert seen == [raw]

    def test_non_ulid_stem_and_mismatched_inner_id_are_ignored(self, fake_home, folder):
        (folder / "notes.json").write_bytes(_envelope(new_ulid()))
        stem = new_ulid()
        (folder / f"{stem}.json").write_bytes(_envelope(new_ulid()))
        seen: list[bytes] = []
        t = _transport(folder)
        t._on_message = seen.append
        t._poll_once()
        assert seen == []

    def test_a_lowercased_filename_still_delivers(self, fake_home, folder):
        """A ULID is a ULID in either case — `is_ulid` says so. A name that
        disagrees with its envelope only in case would otherwise be re-read on
        every poll and delivered never."""
        msg_id = new_ulid()
        (folder / f"{msg_id.lower()}.json").write_bytes(_envelope(msg_id))
        seen: list[bytes] = []
        t = _transport(folder)
        t._on_message = seen.append
        t._poll_once()
        assert [json.loads(raw)["id"] for raw in seen] == [msg_id]
        t._poll_once()
        assert len(seen) == 1

    def test_a_failing_callback_says_so_once(self, fake_home, folder, capsys):
        """Nothing arriving is this transport's whole failure surface, so a
        handler that raises every fifteen seconds owes the log one line."""
        msg_id = new_ulid()
        (folder / f"{msg_id}.json").write_bytes(_envelope(msg_id))

        def boom(_raw: bytes) -> None:
            raise RuntimeError("inbox is read-only")

        t = _transport(folder)
        t._on_message = boom
        t._poll_once()
        t._poll_once()
        printed = capsys.readouterr().out
        assert printed.count("delivery failed") == 1
        assert "inbox is read-only" in printed
        # Not recorded: the envelope is retried, not lost.
        assert msg_id not in t._consumed

    def test_an_unwritable_ledger_says_so_once(
        self, fake_home, folder, monkeypatch, capsys
    ):
        """An unwritable ledger redelivers every envelope at every restart,
        forever, and the folder looks perfectly healthy while it does."""
        t = _transport(folder)

        def no_open(*_a, **_k):
            raise PermissionError("read-only config home")

        monkeypatch.setattr(Path, "open", no_open)
        t._record_consumed(new_ulid())
        t._record_consumed(new_ulid())
        assert capsys.readouterr().out.count("ledger write failed") == 1

    def test_missing_folder_is_tolerated(self, fake_home, tmp_path):
        t = FolderTransport(remote_id="box", path=str(tmp_path / "not-mounted-yet"))
        t._on_message = lambda _b: None
        t._poll_once()  # no raise: the sync mount may arrive later


class TestJoined:
    """`joined` is the registration cutoff: the ULID this machine joined at.

    It replaces reading the folder at registration time, which cannot work —
    a sync client downloads the backlog on its own schedule, hours after the
    command returned."""

    def test_envelopes_below_the_cutoff_never_deliver(self, fake_home, folder):
        history = _ulid_at("01A")
        (folder / f"{history}.json").write_bytes(_envelope(history, "backlog"))
        seen: list[bytes] = []
        t = _transport(folder, joined=_ulid_at("01M"), retain_days="0")
        t._on_message = seen.append
        t._poll_once()
        assert seen == []
        assert (folder / f"{history}.json").is_file()
        fresh = _ulid_at("01Z")
        (folder / f"{fresh}.json").write_bytes(_envelope(fresh, "new"))
        t._poll_once()
        assert len(seen) == 1
        assert json.loads(seen[0])["content"] == "new"

    def test_backlog_arriving_after_registration_stays_out(self, fake_home, tmp_path):
        """Carlos's repro: the folder is absent when the remote is registered."""
        absent = tmp_path / "not-mounted-yet"
        t = FolderTransport(
            remote_id="box", path=str(absent), joined=_ulid_at("01M"), retain_days="0"
        )
        t.touch_ledger()
        seen: list[bytes] = []
        t._on_message = seen.append
        t._poll_once()

        absent.mkdir(parents=True)
        history = _ulid_at("01A")
        (absent / f"{history}.json").write_bytes(_envelope(history, "backlog"))
        fresh = _ulid_at("01Z")
        (absent / f"{fresh}.json").write_bytes(_envelope(fresh, "new"))
        t._poll_once()
        assert [json.loads(raw)["content"] for raw in seen] == ["new"]

        # And a fresh process reading the same spec agrees.
        restarted: list[bytes] = []
        t2 = FolderTransport(
            remote_id="box", path=str(absent), joined=_ulid_at("01M"), retain_days="0"
        )
        t2._on_message = restarted.append
        t2._poll_once()
        assert restarted == []

    def test_a_slow_peer_inside_the_grace_still_delivers(self, fake_home, folder):
        """Carlos's repro inverted. A ULID orders by the clock that minted it,
        so a peer running ten minutes behind publishes below this machine's
        cutoff — and that mail is new, not backlog."""
        from transports.folder import JOIN_SKEW_GRACE_MS

        joined_ms = 1_800_000_000_000
        slow = _ulid_at_ms(joined_ms - 600_000)
        (folder / f"{slow}.json").write_bytes(_envelope(slow, "slow peer"))
        seen: list[bytes] = []
        t = _transport(folder, joined=_ulid_at_ms(joined_ms))
        t._on_message = seen.append
        t._poll_once()
        assert [json.loads(raw)["content"] for raw in seen] == ["slow peer"]

        # Two hours below is past any clock disagreement worth honouring, so
        # it is the backlog the cutoff exists to keep out.
        old = _ulid_at_ms(joined_ms - 2 * JOIN_SKEW_GRACE_MS)
        (folder / f"{old}.json").write_bytes(_envelope(old, "backlog"))
        t._poll_once()
        assert [json.loads(raw)["content"] for raw in seen] == ["slow peer"]

    def test_a_spec_without_a_cutoff_consumes_what_is_there(self, fake_home, folder):
        history = _ulid_at("01A")
        (folder / f"{history}.json").write_bytes(_envelope(history, "backlog"))
        seen: list[bytes] = []
        t = _transport(folder, retain_days="0")
        t._on_message = seen.append
        t._poll_once()
        assert len(seen) == 1

    def test_a_filtered_backlog_says_so_once_per_count(
        self, fake_home, folder, capsys
    ):
        """A clock an hour fast at registration stamps a cutoff no peer can
        reach and the node goes deaf. Silence is the failure, so the count is
        said — once, not every poll."""
        for prefix in ("01A", "01B"):
            history = _ulid_at(prefix)
            (folder / f"{history}.json").write_bytes(_envelope(history, "backlog"))
        t = _transport(folder, joined=_ulid_at("01M"), retain_days="0")
        t._on_message = lambda _b: None
        t._poll_once()
        t._poll_once()
        printed = capsys.readouterr().out
        assert printed.count("as backlog") == 1
        assert "ignoring 2 envelopes predating" in printed
        assert _ulid_at("01M") in printed

    def test_a_clock_ahead_at_registration_is_visible(self, fake_home, folder, capsys):
        """The poisoned-cutoff case end to end: current mail, a future join,
        and nothing delivered — with a line that says why."""
        from transports.folder import JOIN_SKEW_GRACE_MS

        now_ms = 1_800_000_000_000
        current = _ulid_at_ms(now_ms)
        (folder / f"{current}.json").write_bytes(_envelope(current, "current mail"))
        seen: list[bytes] = []
        t = _transport(folder, joined=_ulid_at_ms(now_ms + 2 * JOIN_SKEW_GRACE_MS))
        t._on_message = seen.append
        t._poll_once()
        assert seen == []
        assert "ignoring 1 envelope predating" in capsys.readouterr().out

    def test_joined_must_be_a_ulid(self, fake_home, folder):
        with pytest.raises(ValueError, match="joined must be a ULID"):
            _transport(folder, joined="yesterday")
        assert _transport(folder, joined="")._joined == ""

    def test_retain_days_still_sweeps_below_the_cutoff(self, fake_home, folder):
        import os
        import time

        history = _ulid_at_ms(int((time.time() - 40 * 86400) * 1000))
        stale = folder / f"{history}.json"
        stale.write_bytes(_envelope(history))
        old = time.time() - 40 * 86400
        os.utime(stale, (old, old))
        t = _transport(folder, joined=_ulid_at("01M"), retain_days="30")
        t.publish(_envelope(new_ulid()))
        assert not stale.exists()


class TestLedgerRotation:
    """The cap compacts the ledger; it never forgets a retained envelope."""

    def test_rotation_keeps_every_envelope_still_in_the_folder(
        self, fake_home, folder, monkeypatch
    ):
        import transports.folder as folder_mod

        monkeypatch.setattr(folder_mod, "MAX_SEEN_IDS", 2)
        ids = sorted(new_ulid() for _ in range(3))
        for msg_id in ids:
            (folder / f"{msg_id}.json").write_bytes(_envelope(msg_id))
        first: list[bytes] = []
        t1 = _transport(folder)
        t1._on_message = first.append
        t1._poll_once()
        assert len(first) == 3
        # Over the cap and staying over it: all three files are still there.
        assert sorted(folder_ledger_path("box").read_text().split()) == ids

        second: list[bytes] = []
        t2 = _transport(folder)
        t2._on_message = second.append
        t2._poll_once()
        assert second == []

    def test_rotation_drops_ids_whose_envelope_is_gone(
        self, fake_home, folder, monkeypatch
    ):
        import transports.folder as folder_mod

        monkeypatch.setattr(folder_mod, "MAX_SEEN_IDS", 2)
        ids = sorted(new_ulid() for _ in range(3))
        for msg_id in ids:
            (folder / f"{msg_id}.json").write_bytes(_envelope(msg_id))
        t = _transport(folder)
        t._on_message = lambda _b: None
        t._poll_once()
        (folder / f"{ids[0]}.json").unlink()
        t.publish(_envelope(new_ulid()))
        remaining = set(folder_ledger_path("box").read_text().split())
        assert ids[0] not in remaining
        assert remaining == {p.stem for p in folder.glob("*.json")}
        assert t._consumed == remaining

    def test_rotation_keeps_the_ledger_when_the_folder_is_unmounted(
        self, fake_home, tmp_path, monkeypatch
    ):
        import transports.folder as folder_mod

        monkeypatch.setattr(folder_mod, "MAX_SEEN_IDS", 2)
        t = FolderTransport(remote_id="box", path=str(tmp_path / "gone"))
        ids = sorted(new_ulid() for _ in range(3))
        t._record_consumed(*ids)
        assert sorted(folder_ledger_path("box").read_text().split()) == ids


class TestLedgerLock:
    """`a8s start` runs a handler process per agent and they share one ledger,
    so the mutex around it has to be the filesystem's, not this process's."""

    def _fill(self, folder, monkeypatch, count: int = 3):
        import transports.folder as folder_mod

        monkeypatch.setattr(folder_mod, "MAX_SEEN_IDS", 2)
        ids = sorted(new_ulid() for _ in range(count))
        for msg_id in ids:
            (folder / f"{msg_id}.json").write_bytes(_envelope(msg_id))
        t = _transport(folder)
        t._on_message = lambda _b: None
        t._poll_once()
        return t, ids

    def test_compaction_preserves_a_sibling_process_append(
        self, fake_home, folder, monkeypatch
    ):
        """Carlos's repro: a second daemon appends between the trigger and the
        rewrite. Compaction re-reads the ledger inside the lock, so the rewrite
        is computed from what is on disk rather than from a stale snapshot."""
        t, _ids = self._fill(folder, monkeypatch)
        sibling = new_ulid()
        (folder / f"{sibling}.json").write_bytes(_envelope(sibling))
        listing = t._all_envelopes

        def list_then_sibling_appends():
            files = listing()
            with folder_ledger_path("box").open("a", encoding="utf-8") as f:
                f.write(sibling + "\n")
            return files

        monkeypatch.setattr(t, "_all_envelopes", list_then_sibling_appends)
        t.publish(_envelope(new_ulid()))
        assert sibling in folder_ledger_path("box").read_text().split()

    def test_a_held_lock_lets_the_append_through_and_skips_compaction(
        self, fake_home, folder, monkeypatch
    ):
        """Degradation is availability-safe by construction: the append is what
        keeps an envelope from being delivered twice, so it never waits on a
        lock; the compaction is opportunistic, so it always may."""
        import transports.folder as folder_mod

        monkeypatch.setattr(folder_mod, "LEDGER_LOCK_WAIT_SECONDS", 0.05)
        t, ids = self._fill(folder, monkeypatch)
        (folder / f"{ids[0]}.json").unlink()
        t._lock_path.parent.mkdir(parents=True, exist_ok=True)
        t._lock_path.touch()

        published = new_ulid()
        t.publish(_envelope(published))
        recorded = folder_ledger_path("box").read_text().split()
        assert published in recorded
        assert ids[0] in recorded  # compaction waited for a free lock
        assert t._lock_path.is_file()  # and did not release somebody else's

    def test_a_lock_left_by_a_dead_process_is_broken(
        self, fake_home, folder, monkeypatch
    ):
        import os
        import time

        import transports.folder as folder_mod

        monkeypatch.setattr(folder_mod, "LEDGER_LOCK_WAIT_SECONDS", 0.05)
        t, ids = self._fill(folder, monkeypatch)
        (folder / f"{ids[0]}.json").unlink()
        t._lock_path.parent.mkdir(parents=True, exist_ok=True)
        t._lock_path.touch()
        dead = time.time() - 10 * folder_mod.LEDGER_LOCK_STALE_SECONDS
        os.utime(t._lock_path, (dead, dead))

        t.publish(_envelope(new_ulid()))
        assert ids[0] not in folder_ledger_path("box").read_text().split()
        assert not t._lock_path.exists()

    def test_the_rewrite_temp_name_is_process_unique(
        self, fake_home, folder, monkeypatch
    ):
        """Two processes staging a rewrite must not stage over each other."""
        import os

        t, ids = self._fill(folder, monkeypatch)
        (folder / f"{ids[0]}.json").unlink()
        staged: list[str] = []
        real_replace = os.replace

        def recording_replace(src, dst):
            staged.append(str(src))
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", recording_replace)
        t.publish(_envelope(new_ulid()))
        assert any(
            s.endswith(".tmp") and f".{os.getpid()}." in s for s in staged
        )


class TestLifecycle:
    def test_start_then_stop(self, fake_home, folder):
        t = _transport(folder, poll_seconds=1)
        t.start(lambda _b: None)
        assert t._thread is not None and t._thread.is_alive()
        t.stop()
        assert t._thread is None
        assert not t._started

    def test_an_unmounted_folder_does_not_report_itself_connected(
        self, fake_home, tmp_path
    ):
        """A daemon tolerates a folder that is not there yet; it must not call
        that connected, or the operator's only clue is mail that never comes."""
        absent = tmp_path / "not-mounted-yet"
        t = FolderTransport(remote_id="box", path=str(absent), poll_seconds=1)
        t.start(lambda _b: None)
        try:
            assert not t.is_connected()
            absent.mkdir(parents=True)
            t._poll_once()
            assert t.is_connected()
        finally:
            t.stop()

    def test_stop_before_start_is_a_no_op(self, fake_home, folder):
        _transport(folder).stop()

    def test_double_start_raises(self, fake_home, folder):
        t = _transport(folder)
        t.start(lambda _b: None)
        try:
            with pytest.raises(TransportError, match="already started"):
                t.start(lambda _b: None)
        finally:
            t.stop()

    def test_poll_thread_delivers(self, fake_home, folder):
        import threading

        got = threading.Event()
        seen: list[bytes] = []

        def on_msg(raw: bytes) -> None:
            seen.append(raw)
            got.set()

        msg_id = new_ulid()
        (folder / f"{msg_id}.json").write_bytes(_envelope(msg_id))
        t = _transport(folder, poll_seconds=1)
        t.start(on_msg)
        try:
            assert got.wait(timeout=5.0)
            assert len(seen) == 1
        finally:
            t.stop()


class TestProbe:
    def test_probe_consumes_nothing(self, fake_home, folder):
        msg_id = new_ulid()
        (folder / f"{msg_id}.json").write_bytes(_envelope(msg_id))
        seen: list[bytes] = []
        t = _transport(folder, probe=True)
        t.start(seen.append)
        try:
            assert t.is_connected()
            assert t._thread is None
            assert seen == []
        finally:
            t.stop()
        assert not folder_ledger_path("box").exists()
        assert (folder / f"{msg_id}.json").is_file()

    def test_probe_fails_on_missing_folder(self, fake_home, tmp_path):
        t = FolderTransport(
            remote_id="box", path=str(tmp_path / "gone"), probe=True
        )
        with pytest.raises(TransportError, match="folder not found"):
            t.start(lambda _b: None)

    def test_probe_reports_a_missing_prefix_and_creates_nothing(
        self, fake_home, folder
    ):
        """A check that manufactures the directory it is checking can never
        report the thing the operator asked about."""
        t = _transport(folder, prefix="mail", probe=True)
        with pytest.raises(TransportError, match="prefix folder not found"):
            t.start(lambda _b: None)
        assert not (folder / "mail").exists()

    def test_probe_leaves_no_litter(self, fake_home, folder):
        t = _transport(folder, probe=True)
        t.start(lambda _b: None)
        t.stop()
        assert list(folder.iterdir()) == []


class TestOptions:
    def test_unknown_option_raises(self, fake_home, folder):
        with pytest.raises(ValueError, match="unknown option"):
            _transport(folder, boguskey="x")

    def test_loader_option_bag_keys_are_accepted(self, fake_home, folder):
        t = _transport(
            folder,
            node_tag="node-a",
            probe=False,
            client_id="a8s-health-0000",
            clean_session=True,
        )
        assert t.id == "box"

    def test_poll_seconds_coerced_and_floored(self, fake_home, folder):
        assert _transport(folder)._poll_seconds == 15.0
        assert _transport(folder, poll_seconds="30")._poll_seconds == 30.0
        assert _transport(folder, poll_seconds="0.01")._poll_seconds == 1.0

    def test_retain_days_validated(self, fake_home, folder):
        assert _transport(folder)._retain_days == 3
        assert _transport(folder, retain_days="30")._retain_days == 30
        assert _transport(folder, retain_days="0")._retain_days == 0
        with pytest.raises(ValueError, match="whole number"):
            _transport(folder, retain_days="soon")
        with pytest.raises(ValueError, match="negative"):
            _transport(folder, retain_days="-1")

    def test_relative_path_rejected(self, fake_home):
        with pytest.raises(ValueError, match="must be absolute"):
            FolderTransport(remote_id="box", path="Dropbox/A8S")

    def test_name_with_a_path_separator_rejected(self, fake_home, folder):
        with pytest.raises(ValueError, match="path separator"):
            FolderTransport(remote_id="../box", path=str(folder))

    def test_retain_days_sweeps_on_publish(self, fake_home, folder):
        import os
        import time

        stale = _ulid_at_ms(int((time.time() - 40 * 86400) * 1000))
        stale_path = folder / f"{stale}.json"
        stale_path.write_bytes(_envelope(stale))
        old = time.time() - 40 * 86400
        os.utime(stale_path, (old, old))
        t = _transport(folder, retain_days="30")
        fresh = new_ulid()
        t.publish(_envelope(fresh))
        assert not stale_path.exists()
        assert (folder / f"{fresh}.json").is_file()

    def test_an_old_ulid_with_a_fresh_mtime_is_kept(self, fake_home, folder):
        import os
        import time

        stale = _ulid_at_ms(int((time.time() - 40 * 86400) * 1000))
        stale_path = folder / f"{stale}.json"
        stale_path.write_bytes(_envelope(stale))
        now = time.time()
        os.utime(stale_path, (now, now))
        t = _transport(folder, retain_days="30")
        t.publish(_envelope(new_ulid()))
        assert stale_path.exists()

    def test_a_fresh_ulid_with_an_old_mtime_is_kept(self, fake_home, folder):
        import os
        import time

        fresh_id = new_ulid()
        fresh_path = folder / f"{fresh_id}.json"
        fresh_path.write_bytes(_envelope(fresh_id))
        old = time.time() - 40 * 86400
        os.utime(fresh_path, (old, old))
        t = _transport(folder, retain_days="30")
        t.publish(_envelope(new_ulid()))
        assert fresh_path.exists()

    def test_retain_days_zero_sweeps_nothing(self, fake_home, folder):
        import os
        import time

        stale = _ulid_at_ms(int((time.time() - 40 * 86400) * 1000))
        stale_path = folder / f"{stale}.json"
        stale_path.write_bytes(_envelope(stale))
        old = time.time() - 40 * 86400
        os.utime(stale_path, (old, old))
        t = _transport(folder, retain_days="0")
        t.publish(_envelope(new_ulid()))
        assert stale_path.exists()

    def test_a_delayed_publish_survives_its_own_sweep(self, fake_home, folder):
        import time

        t = _transport(folder)
        delayed = _ulid_at_ms(int((time.time() - 4 * 86400) * 1000))
        t.publish(_envelope(delayed))
        assert (folder / f"{delayed}.json").exists()

    def test_the_poll_path_sweeps_too(self, fake_home, folder):
        import os
        import time

        stale = _ulid_at("01A")
        stale_path = folder / f"{stale}.json"
        stale_path.write_bytes(_envelope(stale))
        old = time.time() - 40 * 86400
        os.utime(stale_path, (old, old))
        t = _transport(folder)
        t._on_message = lambda _b: None
        t._poll_once()
        assert not stale_path.exists()

    def test_the_sweep_throttle_holds_off_a_second_poll_pass(self, fake_home, folder):
        import os
        import time

        first = _ulid_at("01A")
        first_path = folder / f"{first}.json"
        first_path.write_bytes(_envelope(first))
        old = time.time() - 40 * 86400
        os.utime(first_path, (old, old))
        t = _transport(folder)
        t._on_message = lambda _b: None
        t._poll_once()
        assert not first_path.exists()

        second = _ulid_at("01B")
        second_path = folder / f"{second}.json"
        second_path.write_bytes(_envelope(second))
        t._poll_once()
        assert second_path.exists()
