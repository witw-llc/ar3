"""`connect_read_only`'s file-URI conversion, exercised through the readers.

`lib/ar3/home.py` deliberately returns `A8S_HOME` unexpanded to absolute, so
`A8S_HOME=./state` is a legitimate override. `Path.as_uri()` refuses a
relative path outright, so `sqlite_store.connect_read_only` must absolute()
the path before building the URI it opens `convo`, `tx` and `trace` through
(#276 round 4) — and the escaping that conversion relies on has to survive an
absolute path holding a space, `#`, `?` or non-ASCII character too.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

# Windows reserves `?` in file names, so a store at `a?dir` cannot exist there
# and the case would fail while creating the directory, not in the reader. The
# escaping itself is covered everywhere by the pure-URI test below.
SPECIAL_NAMES = [
    "a dir",
    "a#dir",
    pytest.param(
        "a?dir",
        marks=pytest.mark.skipif(os.name == "nt", reason="Windows reserves ? in file names"),
    ),
    "café",
]

from ar3.ulid import new as new_ulid
from commands import cmd_convo, cmd_trace, cmd_transactions
from convo import record
from core import conversations_path, transactions_path
from registry import save_registry
from txlog import log


def _register(tmp_path):
    root = tmp_path / "bob"
    root.mkdir()
    save_registry({"Bob": {"root": str(root)}})


def _write_one_message(msg_id):
    record(
        {"id": msg_id, "from": "Alice", "to": "Bob", "content": "retained message"},
        recipients=["Bob"],
    )
    log("ROUTED", msg_id=msg_id, sender="Alice", recipient="Bob")


def _zero_byte(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _unrelated_schema(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE somebody_elses (id INTEGER)")


class TestReadersUnderARelativeA8SHome:
    """`A8S_HOME=./state` is a legitimate override; the readers must resolve
    it to an absolute file URI instead of letting `as_uri()` raise."""

    def test_convo_reads_rows_written_under_a_relative_home(
        self, fake_home, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("A8S_HOME", "./state")
        _register(tmp_path)
        _write_one_message(new_ulid())
        assert cmd_convo(["bob"]) == 0
        assert "retained message" in capsys.readouterr().out

    def test_tx_reads_rows_written_under_a_relative_home(
        self, fake_home, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("A8S_HOME", "./state")
        _register(tmp_path)
        _write_one_message(new_ulid())
        assert cmd_transactions([]) == 0
        assert "ROUTED" in capsys.readouterr().out

    def test_trace_reads_rows_written_under_a_relative_home(
        self, fake_home, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("A8S_HOME", "./state")
        _register(tmp_path)
        msg_id = new_ulid()
        _write_one_message(msg_id)
        assert cmd_trace([msg_id]) == 0
        assert "ROUTED" in capsys.readouterr().out

    @pytest.mark.parametrize("shape", ["zero-byte", "unrelated-schema"])
    def test_convo_still_refuses_a_non_archive_under_a_relative_home(
        self, fake_home, tmp_path, monkeypatch, capsys, shape
    ):
        """Round 3's #276 refusal holds when the path naming the store is
        relative, not only when a caller happens to pass an absolute one."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("A8S_HOME", "./state")
        _register(tmp_path)
        path = conversations_path()
        (_unrelated_schema if shape == "unrelated-schema" else _zero_byte)(path)
        before = path.read_bytes()
        assert cmd_convo(["bob"]) == 1
        assert f"a8s: cannot read {path}" in capsys.readouterr().err
        assert path.read_bytes() == before

    @pytest.mark.parametrize("shape", ["zero-byte", "unrelated-schema"])
    @pytest.mark.parametrize("reader", ["tx", "trace"])
    def test_tx_and_trace_still_refuse_a_non_log_under_a_relative_home(
        self, fake_home, tmp_path, monkeypatch, capsys, shape, reader
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("A8S_HOME", "./state")
        _register(tmp_path)
        path = transactions_path()
        (_unrelated_schema if shape == "unrelated-schema" else _zero_byte)(path)
        before = path.read_bytes()
        if reader == "tx":
            assert cmd_transactions([]) == 1
        else:
            assert cmd_trace([new_ulid()]) == 1
        assert f"a8s: cannot read {path}" in capsys.readouterr().err
        assert path.read_bytes() == before


class TestReadersEscapeSpecialCharactersInTheAbsolutePath:
    """Positive controls for the URI conversion itself: `as_uri()` percent-
    encodes what a `file://` URI cannot carry literally, and an absolute
    store path holding one of those characters must still round-trip."""

    @pytest.mark.parametrize("name", SPECIAL_NAMES)
    def test_convo_reads_rows_from_a_store_at_a_specially_named_path(
        self, fake_home, tmp_path, monkeypatch, capsys, name
    ):
        monkeypatch.setenv("A8S_HOME", str(tmp_path / name))
        _register(tmp_path)
        _write_one_message(new_ulid())
        assert cmd_convo(["bob"]) == 0
        assert "retained message" in capsys.readouterr().out

    @pytest.mark.parametrize("name", SPECIAL_NAMES)
    def test_tx_and_trace_read_rows_from_a_log_at_a_specially_named_path(
        self, fake_home, tmp_path, monkeypatch, capsys, name
    ):
        monkeypatch.setenv("A8S_HOME", str(tmp_path / name))
        _register(tmp_path)
        msg_id = new_ulid()
        _write_one_message(msg_id)
        assert cmd_transactions([]) == 0
        assert "ROUTED" in capsys.readouterr().out
        assert cmd_trace([msg_id]) == 0
        assert "ROUTED" in capsys.readouterr().out


def test_a_question_mark_in_the_path_is_escaped_before_the_query_string(tmp_path):
    uri = (tmp_path / "a?dir" / "conversations.sqlite3").absolute().as_uri()
    assert uri.endswith("/a%3Fdir/conversations.sqlite3")
    assert "?" not in uri
    assert f"{uri}?mode=ro".count("?") == 1
