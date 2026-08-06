"""CLI round-trip tests — subprocess, not imports."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

K7E_PY = str(Path(__file__).resolve().parent.parent / "k7e.py")


class TestCLIRoundTrip:
    @pytest.fixture
    def cli_env(self, tmp_path):
        env = os.environ.copy()
        env["K7E_HOME"] = str(tmp_path)
        env["OLLAMA_URL"] = "http://localhost:99999"
        # Initialize
        subprocess.run([sys.executable, K7E_PY, "stats"], env=env, capture_output=True)
        return env

    def test_store_search_get(self, cli_env):
        r = subprocess.run(
            [sys.executable, K7E_PY, "store", "Port Forwarding", "--tags", "ssh", "--content", "Use -L for local, -R for remote"],
            env=cli_env, capture_output=True, text=True
        )
        assert r.returncode == 0
        assert "Stored K7E-" in r.stdout

        r = subprocess.run([sys.executable, K7E_PY, "search", "port forwarding", "--ids"], env=cli_env, capture_output=True, text=True)
        assert "K7E-000-00001" in r.stdout

        r = subprocess.run([sys.executable, K7E_PY, "get", "K7E-000-00001"], env=cli_env, capture_output=True, text=True)
        assert "Use -L for local" in r.stdout

    def test_store_via_stdin(self, cli_env):
        r = subprocess.run(
            [sys.executable, K7E_PY, "store", "Stdin Test", "--tags", "test"],
            input="Piped content here", env=cli_env, capture_output=True, text=True
        )
        assert r.returncode == 0
        assert "Stored" in r.stdout

    def test_append_via_cli(self, cli_env):
        subprocess.run([sys.executable, K7E_PY, "store", "Base", "--content", "original", "--tags", "t"], env=cli_env, capture_output=True)
        r = subprocess.run(
            [sys.executable, K7E_PY, "append", "K7E-000-00001", "--section", "Edge Cases", "--content", "new info"],
            env=cli_env, capture_output=True, text=True
        )
        assert r.returncode == 0
        assert "Appended" in r.stdout

    def test_ids_mode_clean_output(self, cli_env):
        subprocess.run([sys.executable, K7E_PY, "store", "A", "--content", "a", "--tags", "x"], env=cli_env, capture_output=True)
        subprocess.run([sys.executable, K7E_PY, "store", "B", "--content", "b", "--tags", "x"], env=cli_env, capture_output=True)
        r = subprocess.run([sys.executable, K7E_PY, "list", "--ids"], env=cli_env, capture_output=True, text=True)
        lines = r.stdout.strip().splitlines()
        for line in lines:
            assert line.startswith("K7E-"), f"Non-ID line in --ids output: {line!r}"

    def test_stats_json(self, cli_env):
        subprocess.run([sys.executable, K7E_PY, "store", "X", "--content", "x", "--tags", "y"], env=cli_env, capture_output=True)
        r = subprocess.run([sys.executable, K7E_PY, "stats", "--json"], env=cli_env, capture_output=True, text=True)
        import json
        data = json.loads(r.stdout)
        assert data["total_nodes"] == 1

    def _seed(self, cli_env, n):
        for i in range(1, n + 1):
            subprocess.run(
                [sys.executable, K7E_PY, "store", f"Entry {i}", "--tags", "t",
                 "--content", f"Body of entry number {i}, long enough to be real."],
                env=cli_env, capture_output=True,
            )
        return [f"K7E-000-{i:05d}" for i in range(1, n + 1)]

    def _get(self, cli_env, *args):
        return subprocess.run(
            [sys.executable, K7E_PY, "get", *args],
            env=cli_env, capture_output=True, text=True
        )

    def test_get_batches_many_ids_in_one_call(self, cli_env):
        """The inject path fetches a whole ranking pool before it can weigh
        anything, and the per-call cost is interpreter startup, not the read.
        One call has to return the lot."""
        ids = self._seed(cli_env, 3)
        r = self._get(cli_env, *ids, "--no-track", "--json")
        assert r.returncode == 0, r.stderr
        got = json.loads(r.stdout)
        assert [e["id"] for e in got] == ids
        assert "Body of entry number 2" in got[1]["text"]

    def test_get_of_one_id_is_unchanged(self, cli_env):
        """The single-id form is the documented interactive one — no
        separator, no wrapper, just the entry."""
        (nid,) = self._seed(cli_env, 1)
        r = self._get(cli_env, nid, "--no-track")
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("---\nid: K7E-000-00001\n")
        assert "k7e:" not in r.stdout

    def test_a_missing_id_does_not_cost_the_batch(self, cli_env):
        """A caller sizing a pool would rather pack the entries that exist
        than pack none."""
        ids = self._seed(cli_env, 2)
        r = self._get(cli_env, ids[0], "K7E-000-99999", ids[1], "--no-track", "--json")
        assert r.returncode == 0, r.stderr
        assert [e["id"] for e in json.loads(r.stdout)] == ids
        assert "K7E-000-99999 not found" in r.stderr

    def test_a_batch_of_nothing_found_still_fails(self, cli_env):
        r = self._get(cli_env, "K7E-000-99998", "K7E-000-99999", "--no-track")
        assert r.returncode == 1
        assert r.stdout.strip() == ""

    def test_tracking_covers_the_whole_batch(self, cli_env):
        import sqlite3
        ids = self._seed(cli_env, 3)
        assert self._get(cli_env, *ids).returncode == 0
        conn = sqlite3.connect(Path(cli_env["K7E_HOME"]) / ".index.db")
        counts = {
            i: conn.execute(
                "SELECT use_count FROM nodes WHERE id = ?", (i,)
            ).fetchone()[0]
            for i in ids
        }
        assert counts == {i: 1 for i in ids}

    def test_no_track_covers_the_whole_batch(self, cli_env):
        import sqlite3
        ids = self._seed(cli_env, 3)
        assert self._get(cli_env, *ids, "--no-track").returncode == 0
        conn = sqlite3.connect(Path(cli_env["K7E_HOME"]) / ".index.db")
        for i in ids:
            row = conn.execute("SELECT use_count FROM nodes WHERE id = ?", (i,)).fetchone()
            assert row[0] == 0, i

    def test_distill_prints_a_skipped_file(self, cli_env, tmp_path):
        """A file distill could not read has no title to print. The operator
        still has to be told which file went unread, and the CLI must not die
        reaching for a field the skip result does not carry."""
        bad = tmp_path / "captures" / "bad.md"
        bad.parent.mkdir()
        bad.write_bytes(b"\xff\xfe not decodable as utf-8 \x00")
        env = dict(cli_env, K7E_DISTILL_COMMAND="cat")
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", str(bad.parent)],
            env=env, capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr
        assert "[skipped] " in r.stdout
        assert "bad.md" in r.stdout

    def test_reindex_recovers(self, cli_env):
        subprocess.run([sys.executable, K7E_PY, "store", "Recover", "--content", "important", "--tags", "t"], env=cli_env, capture_output=True)
        # Delete index
        import pathlib
        idx = pathlib.Path(cli_env["K7E_HOME"]) / ".index.db"
        idx.unlink(missing_ok=True)
        subprocess.run([sys.executable, K7E_PY, "reindex"], env=cli_env, capture_output=True)
        r = subprocess.run([sys.executable, K7E_PY, "search", "important", "--ids"], env=cli_env, capture_output=True, text=True)
        assert "K7E-000-00001" in r.stdout
