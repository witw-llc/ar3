"""A stale index is re-derived from the markdown by the first verb that opens it."""
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli
import config
import embeddings
import engine

K7E_PY = str(Path(__file__).resolve().parent.parent / "k7e.py")
REBUILD = "k7e: rebuilt the search index for this version"

# The schema version 0.1.99 writes, copied from the released engine.
RELEASED_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    aliases TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    confidence REAL DEFAULT 0.5,
    verification_count INTEGER DEFAULT 0,
    last_updated TEXT,
    tags TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    content_hash TEXT DEFAULT '',
    superseded_by TEXT DEFAULT '',
    last_used_at TEXT,
    use_count INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    title, aliases, tags, content,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS embeddings (
    node_id TEXT PRIMARY KEY,
    vector BLOB,
    model TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pending_embeddings (
    node_id TEXT PRIMARY KEY,
    queued_at TEXT
);

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');
"""

ENTRIES = [
    ("Synthetic garden", "Tomatoes need staking before July.", ["garden"]),
    ("Synthetic kitchen", "Cast iron wants a dry towel after washing.", ["kitchen"]),
    ("Synthetic workshop", "Sharpen the chisel before the dovetail joint.", ["workshop", "wood"]),
]
COUNTER_AHEAD = 5
OLD_ROW_USE_COUNT = 7

VERBS = {
    "list": ["list"],
    "search": ["search", "chisel", "--json"],
    "get": ["get", "K7E-000-00002"],
    "stats": ["stats"],
    "status": ["status"],
    "check": ["check"],
    "store": ["store", "Synthetic attic", "--content", "Insulation sits under the boards."],
    "append": ["append", "K7E-000-00001", "--section", "Notes", "--content", "Mulch in autumn."],
}


def build_store(home, monkeypatch, *, old, extra=0, ahead=COUNTER_AHEAD):
    monkeypatch.setenv("K7E_HOME", str(home))
    monkeypatch.setenv("K7E_EMBEDDINGS", "off")
    engine.reset(home)
    engine.init()
    for title, content, tags in ENTRIES:
        engine.store_entry(title, content, tags=tags)
    for number in range(extra):
        engine.store_entry(f"Bulk entry {number}", f"Bulk body {number} " + "lorem ipsum " * 80, tags=["bulk"])
    if old:
        make_old(home, ahead=ahead)
    return home


def make_old(home, *, ahead=0):
    """Replace the index with one in the released shape, filled from the files."""
    db = home / ".index.db"
    conn = sqlite3.connect(db)
    counter = conn.execute("SELECT value FROM meta WHERE key = 'next_id_counter'").fetchone()[0]
    conn.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(RELEASED_SCHEMA)
    for index, path in enumerate(sorted((home / "nodes").glob("*/K7E-*.md")), start=1):
        node_id = path.stem
        conn.execute(
            "INSERT INTO nodes (id, title, last_updated, use_count) VALUES (?, ?, '2026-01-01', ?)",
            (node_id, f"old row {node_id}", OLD_ROW_USE_COUNT),
        )
        conn.execute(
            "INSERT INTO nodes_fts (rowid, title, aliases, tags, content) "
            "VALUES ((SELECT rowid FROM nodes WHERE id = ?), 'old row', '', '', 'stale text')",
            (node_id,),
        )
        conn.execute(
            "INSERT INTO embeddings (node_id, vector, model, updated_at) VALUES (?, x'00', 'old', '2026-01-01')",
            (node_id,),
        )
        conn.execute("INSERT INTO pending_embeddings (node_id, queued_at) VALUES (?, '2026-01-01')", (node_id,))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('next_id_counter', ?)", (str(int(counter) + ahead),))
    conn.commit()
    conn.close()


def columns(home, table):
    conn = sqlite3.connect(home / ".index.db")
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def point(home, monkeypatch):
    monkeypatch.setenv("K7E_HOME", str(home))
    engine.reset(home)


def run_verb(home, argv, monkeypatch, capsys):
    point(home, monkeypatch)
    capsys.readouterr()
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out.replace(str(home), "<HOME>"), captured.err


@pytest.fixture(autouse=True)
def quiet_environment(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:99999")
    monkeypatch.delenv("K7E_LLM_COMMAND", raising=False)
    for _, env_key in config.LLM_PURPOSES.values():
        monkeypatch.delenv(env_key, raising=False)


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_first_verb_on_an_old_index_rebuilds_and_matches_a_current_store(verb, tmp_path, monkeypatch, capsys):
    old = build_store(tmp_path / "old", monkeypatch, old=True, ahead=0)
    current = build_store(tmp_path / "current", monkeypatch, old=False)
    assert "kind" not in columns(old, "nodes")

    code, expected, expected_err = run_verb(current, VERBS[verb], monkeypatch, capsys)
    assert code == 0
    assert REBUILD not in expected_err

    code, out, err = run_verb(old, VERBS[verb], monkeypatch, capsys)
    assert code == 0
    assert out == expected
    assert err.count(REBUILD) == 1
    assert err.endswith(f"({len(ENTRIES)} entries)\n")
    assert {"kind", "embedding_input_hash"} <= columns(old, "nodes")

    code, _, err = run_verb(old, ["list"], monkeypatch, capsys)
    assert code == 0
    assert REBUILD not in err


def test_rebuild_resets_old_rows_and_lists_every_file(tmp_path, monkeypatch, capsys):
    home = build_store(tmp_path, monkeypatch, old=True)
    code, out, _ = run_verb(home, ["list", "--ids"], monkeypatch, capsys)
    assert code == 0
    assert sorted(out.split()) == ["K7E-000-00001", "K7E-000-00002", "K7E-000-00003"]
    conn = sqlite3.connect(home / ".index.db")
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE use_count != 0").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0] == len(ENTRIES)
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pending_embeddings").fetchone()[0] == 0
    conn.close()


def test_id_counter_survives_and_store_takes_the_next_id(tmp_path, monkeypatch, capsys):
    home = build_store(tmp_path, monkeypatch, old=True)
    expected = f"K7E-000-{len(ENTRIES) + COUNTER_AHEAD + 1:05d}"
    code, out, err = run_verb(home, ["store", "Synthetic attic", "--content", "Insulation."], monkeypatch, capsys)
    assert code == 0
    assert out == f"Stored {expected}: Synthetic attic\n"
    assert err.count(REBUILD) == 1
    conn = sqlite3.connect(home / ".index.db")
    assert conn.execute("SELECT value FROM meta WHERE key = 'next_id_counter'").fetchone()[0] == str(
        len(ENTRIES) + COUNTER_AHEAD + 1)
    conn.close()


def test_rebuild_keeps_the_database_files(tmp_path, monkeypatch, capsys):
    home = build_store(tmp_path, monkeypatch, old=True)
    before = os.stat(home / ".index.db").st_ino
    run_verb(home, ["list"], monkeypatch, capsys)
    assert os.stat(home / ".index.db").st_ino == before


def test_fresh_store_and_current_index_print_no_rebuild_line(tmp_path, monkeypatch, capsys):
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("K7E_HOME", str(fresh))
    engine.reset(fresh)
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    assert REBUILD not in capsys.readouterr().err

    current = build_store(tmp_path / "current", monkeypatch, old=False)
    capsys.readouterr()
    for argv in (["list"], ["stats"], ["status"]):
        code, _, err = run_verb(current, argv, monkeypatch, capsys)
        assert code == 0
        assert REBUILD not in err


def test_only_embeddings_old_rebuilds(tmp_path, monkeypatch, capsys):
    home = build_store(tmp_path, monkeypatch, old=True)
    conn = sqlite3.connect(home / ".index.db")
    for column in ("kind TEXT DEFAULT ''", "embedding_input_hash TEXT DEFAULT ''"):
        conn.execute(f"ALTER TABLE nodes ADD COLUMN {column}")
    conn.commit()
    conn.close()
    assert not engine._REQUIRED_COLUMNS["embeddings"] <= columns(home, "embeddings")
    code, _, err = run_verb(home, ["list"], monkeypatch, capsys)
    assert code == 0
    assert err.count(REBUILD) == 1
    assert engine._REQUIRED_COLUMNS["embeddings"] <= columns(home, "embeddings")


def test_only_nodes_old_rebuilds(tmp_path, monkeypatch, capsys):
    home = build_store(tmp_path, monkeypatch, old=True)
    conn = sqlite3.connect(home / ".index.db")
    for column in ("provider TEXT", "dimensions INTEGER", "text_hash TEXT", "text_version TEXT"):
        conn.execute(f"ALTER TABLE embeddings ADD COLUMN {column}")
    conn.commit()
    conn.close()
    assert "kind" not in columns(home, "nodes")
    code, _, err = run_verb(home, ["list"], monkeypatch, capsys)
    assert code == 0
    assert err.count(REBUILD) == 1
    assert "kind" in columns(home, "nodes")


def test_rebuild_requests_no_vectors_and_every_entry_is_pending(tmp_path, monkeypatch):
    requests = []

    def request(req, timeout):
        requests.append(req)
        return io.BytesIO(json.dumps({"model": "text-embedding-3-small",
                                      "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]}).encode())

    monkeypatch.setattr(embeddings.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=request))
    monkeypatch.setattr(embeddings.urllib.request, "urlopen", request)
    home = build_store(tmp_path, monkeypatch, old=True)
    monkeypatch.setenv("K7E_EMBEDDINGS", "openai")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("K7E_EMBED_DIMENSIONS", "3")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-runtime-key")
    engine.reset(home)

    assert engine.embedding_coverage() == {"total": len(ENTRIES), "current": 0, "pending": len(ENTRIES)}
    assert requests == []
    conn = sqlite3.connect(home / ".index.db")
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    conn.close()
    assert "Vectors: 0/3 current; 3 pending" in config.status()
    assert requests == []


def test_current_shape_vectors_survive_a_rebuild_of_the_entry_table(tmp_path, monkeypatch, capsys):
    requests = []

    def request(req, timeout):
        requests.append(req)
        return io.BytesIO(json.dumps({"model": "text-embedding-3-small",
                                      "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]}).encode())

    monkeypatch.setattr(embeddings.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=request))
    monkeypatch.setattr(embeddings.urllib.request, "urlopen", request)
    home = build_store(tmp_path, monkeypatch, old=False)
    monkeypatch.setenv("K7E_EMBEDDINGS", "openai")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("K7E_EMBED_DIMENSIONS", "3")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-runtime-key")
    engine.reset(home)
    assert engine.process_pending_embeddings() == len(ENTRIES)
    paid = len(requests)
    conn = sqlite3.connect(home / ".index.db")
    conn.execute("DROP TABLE nodes_fts")
    conn.execute("DROP TABLE nodes")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT, last_updated TEXT)")
    conn.commit()
    conn.close()

    capsys.readouterr()
    assert engine.embedding_coverage() == {"total": len(ENTRIES), "current": len(ENTRIES), "pending": 0}
    assert capsys.readouterr().err.count(REBUILD) == 1
    assert len(requests) == paid


def test_failure_during_the_fill_leaves_the_old_index_and_the_next_call_succeeds(tmp_path, monkeypatch, capsys):
    home = build_store(tmp_path, monkeypatch, old=True)
    point(home, monkeypatch)

    def snapshot():
        conn = sqlite3.connect(home / ".index.db")
        try:
            return {
                "nodes": conn.execute("SELECT id, title, use_count FROM nodes ORDER BY id").fetchall(),
                "fts": conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0],
                "embeddings": conn.execute("SELECT node_id, model FROM embeddings ORDER BY node_id").fetchall(),
                "pending": conn.execute("SELECT node_id FROM pending_embeddings ORDER BY node_id").fetchall(),
                "meta": conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall(),
                "columns": columns(home, "nodes") | columns(home, "embeddings"),
            }
        finally:
            conn.close()

    before = snapshot()
    real = engine._embedding_text
    calls = []

    def fail_on_second(text):
        calls.append(text)
        if len(calls) == 2:
            raise OSError("synthetic read fault")
        return real(text)

    monkeypatch.setattr(engine, "_embedding_text", fail_on_second)
    capsys.readouterr()
    with pytest.raises(OSError, match="synthetic read fault"):
        engine.init()
    assert REBUILD not in capsys.readouterr().err
    assert snapshot() == before
    conn = sqlite3.connect(home / ".index.db")
    assert engine._stale_tables(conn)
    conn.close()

    monkeypatch.setattr(engine, "_embedding_text", real)
    code, _, err = run_verb(home, ["list"], monkeypatch, capsys)
    assert code == 0
    assert err.count(REBUILD) == 1
    assert snapshot()["fts"] == len(ENTRIES)


def test_a_second_caller_waits_for_the_rebuild_instead_of_failing_on_a_locked_database(tmp_path, monkeypatch, capsys):
    home = build_store(tmp_path, monkeypatch, old=True)
    point(home, monkeypatch)
    filling = threading.Event()
    real_fill = engine._fill_index

    def slow_fill(conn, embeddings=False):
        filling.set()
        time.sleep(0.6)
        return real_fill(conn, embeddings)

    monkeypatch.setattr(engine, "_fill_index", slow_fill)
    errors = []

    def first():
        try:
            engine.init()
        except Exception as error:
            errors.append(error)

    rebuilder = threading.Thread(target=first)
    rebuilder.start()
    assert filling.wait(5)

    def impatient():
        conn = sqlite3.connect(str(engine.INDEX_DB), timeout=0.1)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    monkeypatch.setattr(engine, "_connect", impatient)
    engine.init()
    rebuilder.join()
    assert errors == []
    assert capsys.readouterr().err.count(REBUILD) == 1
    assert "kind" in columns(home, "nodes")


def test_two_processes_on_one_old_index_rebuild_once(tmp_path, monkeypatch):
    home = build_store(tmp_path, monkeypatch, old=True, extra=1200)
    env = dict(os.environ, K7E_HOME=str(home), K7E_EMBEDDINGS="off", OLLAMA_URL="http://localhost:99999")
    procs = [
        subprocess.Popen([sys.executable, K7E_PY, "list", "--ids"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    results = [proc.communicate(timeout=120) for proc in procs]
    assert [proc.returncode for proc in procs] == [0, 0]
    assert sum(err.count(REBUILD) for _, err in results) == 1
    assert all("locked" not in err for _, err in results)
    assert results[0][0] == results[1][0]
    assert len(results[0][0].split()) == len(ENTRIES) + 1200
