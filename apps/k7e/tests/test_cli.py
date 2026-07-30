"""CLI round-trip tests — subprocess, not imports."""
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

    def test_reindex_recovers(self, cli_env):
        subprocess.run([sys.executable, K7E_PY, "store", "Recover", "--content", "important", "--tags", "t"], env=cli_env, capture_output=True)
        # Delete index
        import pathlib
        idx = pathlib.Path(cli_env["K7E_HOME"]) / ".index.db"
        idx.unlink(missing_ok=True)
        subprocess.run([sys.executable, K7E_PY, "reindex"], env=cli_env, capture_output=True)
        r = subprocess.run([sys.executable, K7E_PY, "search", "important", "--ids"], env=cli_env, capture_output=True, text=True)
        assert "K7E-000-00001" in r.stdout
