"""A running node keeps its stores openable to a read-only reader.

Both stores are WAL, and a `mode=ro` open needs `<store>-wal` and
`<store>-shm` to be there already — the reader cannot make them unless the
directory is writable. The writers open per write and close again, so a seat
with read+execute on the a8s home was told "unable to open database file"
about a store that was fine, for whatever fraction of the time nothing else
was connected. A node holds one connection to each store while it runs; when
none runs, the reader says which of the two things is missing.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import convo
import txlog
from commands import cmd_convo
from core import conversations_path, transactions_path
from convo import ConversationArchiveError, record
from txlog import TransactionLogError

from mqtt_cluster import start_attached_loop, stop_attached_loop, using_a8s_home

MISSING_HOLD = (
    "no WAL side files sit beside the store, and a reader that cannot create "
    "them cannot open it until a node holds the store open; is a node running "
    "on this machine?"
)


def _side_files(store: Path) -> list[Path]:
    return [
        store.with_name(store.name + suffix)
        for suffix in ("-wal", "-shm")
        if store.with_name(store.name + suffix).exists()
    ]


def _archive_one_row(tmp_path: Path) -> None:
    from registry import save_registry

    root = tmp_path / "bob"
    root.mkdir(exist_ok=True)
    save_registry({"Bob": {"root": str(root)}})
    record(
        {"id": "01JHOLD00000000000000001", "from": "Alice", "to": "Bob", "content": "x"},
        recipients=["Bob"],
    )


class TestNoHoldNoRead:
    """The field failure: nothing holds the store, and the reader cannot say
    why from what SQLite tells it."""

    def test_the_archive_says_what_is_missing(
        self, fake_home, tmp_path, unwritable_dir
    ):
        _archive_one_row(tmp_path)
        assert _side_files(conversations_path()) == []
        unwritable_dir(conversations_path().parent)
        with pytest.raises(ConversationArchiveError) as raised:
            convo.open_for_read()
        assert str(raised.value).startswith(f"cannot read {conversations_path()}: ")
        assert str(raised.value).endswith(MISSING_HOLD)

    def test_the_transaction_log_says_the_same(
        self, fake_home, tmp_path, unwritable_dir
    ):
        txlog.log("ROUTED", msg_id="01JHOLD00000000000000002", sender="A", recipient="B")
        assert _side_files(transactions_path()) == []
        unwritable_dir(transactions_path().parent)
        with pytest.raises(TransactionLogError) as raised:
            txlog.read_recent()
        assert str(raised.value).startswith(f"cannot read {transactions_path()}: ")
        assert str(raised.value).endswith(MISSING_HOLD)

    def test_the_command_names_the_path_and_exits_one(
        self, fake_home, tmp_path, capsys, unwritable_dir
    ):
        _archive_one_row(tmp_path)
        unwritable_dir(conversations_path().parent)
        assert cmd_convo(["bob", "--limit", "3"]) == 1
        err = capsys.readouterr().err
        assert f"a8s: cannot read {conversations_path()}: " in err
        assert MISSING_HOLD in err

    def test_a_writable_directory_reads_the_same_store(self, fake_home, tmp_path):
        """The positive control: nothing is wrong with the store itself, and
        a reader that can create the side files never sees any of this."""
        _archive_one_row(tmp_path)
        assert _side_files(conversations_path()) == []
        assert len(convo.load_entries()) == 1


class TestHeldConnection:
    def test_the_hold_makes_the_side_files_and_closing_takes_them_away(
        self, fake_home, tmp_path
    ):
        _archive_one_row(tmp_path)
        held = convo.hold_open()
        try:
            assert len(_side_files(conversations_path())) == 2
        finally:
            held.close()
        assert _side_files(conversations_path()) == []

    def test_a_held_connection_lets_a_read_only_reader_in(
        self, fake_home, tmp_path, unwritable_dir
    ):
        _archive_one_row(tmp_path)
        held = convo.hold_open()
        try:
            unwritable_dir(conversations_path().parent)
            entries = convo.load_entries()
        finally:
            held.close()
        assert [e["from"] for e in entries] == ["Alice"]

    def test_the_hold_does_not_block_a_truncate_checkpoint(self, fake_home, tmp_path):
        """The hold must cost the writers nothing. Idle and outside any
        transaction it takes no read lock, so the checkpoint that `a8s update`
        runs still empties the WAL."""
        held = txlog.hold_open()
        try:
            for i in range(200):
                txlog.log(
                    "ROUTED",
                    msg_id=f"01JHOLD{i:017d}",
                    sender="A",
                    recipient="B",
                    detail="x" * 200,
                )
            wal = transactions_path().with_name(transactions_path().name + "-wal")
            assert wal.stat().st_size > 0
            writer = sqlite3.connect(transactions_path())
            try:
                busy, _log, _checkpointed = writer.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
            finally:
                writer.close()
            assert busy == 0
            assert wal.stat().st_size == 0
            assert len(txlog.read_recent(limit=500)) == 200
        finally:
            held.close()


class TestRunningNode:
    """The whole point, end to end: a node process, and a reader beside it."""

    @staticmethod
    def _register(home: Path, root: Path) -> None:
        from registry import save_registry

        root.mkdir(parents=True, exist_ok=True)
        definition = root / "a8s-proxy.json"
        definition.write_text(json.dumps({"proxy": "file", "idle": {"timeout": 30}}))
        home.mkdir(parents=True, exist_ok=True)
        with using_a8s_home(home):
            save_registry(
                {"AG": {"root": str(root.resolve()), "definition": str(definition)}}
            )

    @staticmethod
    def _wait_for_side_files(home: Path, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with using_a8s_home(home):
                stores = [conversations_path(), transactions_path()]
                if all(len(_side_files(s)) == 2 for s in stores):
                    return
            time.sleep(0.05)
        raise AssertionError(f"no WAL side files under {home} within {timeout}s")

    def test_the_node_holds_both_stores_while_it_runs(
        self, tmp_path, unwritable_dir
    ):
        home = tmp_path / "home"
        self._register(home, tmp_path / "AG")
        proc = start_attached_loop(home, "AG")
        try:
            self._wait_for_side_files(home)
            with using_a8s_home(home):
                unwritable_dir(home)
                assert txlog.read_recent(limit=5)
                # The archive holds no rows here — nothing was delivered — but
                # opening it at all is what the sandboxed seat could not do.
                assert convo.load_entries() == []
        finally:
            # SQLite deletes the side files when the last connection closes,
            # and cannot when the directory is read-only — so the shutdown
            # below is given back the write bit it would have had in the
            # field, where the node's own user owns the a8s home.
            home.chmod(0o755)
            stop_attached_loop(proc)
        with using_a8s_home(home):
            assert _side_files(transactions_path()) == []
            assert _side_files(conversations_path()) == []
