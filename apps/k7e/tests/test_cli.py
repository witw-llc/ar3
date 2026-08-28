"""CLI round-trip tests — subprocess, not imports."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import write_path_executable

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


class TestDistillExitCode:
    """A dead LLM bridge and a quiet one both extract nothing. Only the exit
    code can tell a caller which happened, and r4t's dream sweep reads exactly
    that before advancing a watermark past the captures it just consumed."""

    @pytest.fixture
    def journal(self, tmp_path):
        p = tmp_path / "journal.md"
        p.write_text(
            "Deploys run from the hotfix branch, never from main. The release "
            "tag is cut by the owner after the suite is green on both hosts, "
            "and the mirror push is the same event as the merge.\n"
        )
        return str(p)

    @pytest.fixture
    def long_journal(self, tmp_path):
        """Past the 3000-char chunk size, so distillation makes more than one
        LLM call and a single failed call is a PARTIAL loss."""
        p = tmp_path / "long.md"
        para = (
            "Deploys run from the hotfix branch, never from main. The release "
            "tag is cut by the owner after the suite is green on both hosts. "
            "The mirror push is the same event as the merge, and the version "
            "bump rides the same pull request as the change it describes.\n\n"
        )
        p.write_text(para * 20)
        return str(p)

    def _bridge(self, tmp_path, name, body):
        """A distill or rerank bridge as a Python program, plus a marker file
        it writes before doing anything else.

        The marker is the point. Every bridge here used to be
        `sh -c '<one-liner>'`, and a test that asserts a *failure* could not
        tell a bridge that answered wrongly from one that never launched —
        pointing all six at a shell that does not exist left this class at
        3 failed / 8 passed on both platforms, so five of those greens could
        not fail. `sh` is also not a thing a Windows box is owed.
        """
        marker = tmp_path / f"{name}-ran"
        script = write_path_executable(tmp_path, name, (
            "import sys\n"
            "sys.stdin.read()\n"
            f"open({str(marker)!r}, 'w', encoding='utf-8').close()\n"
            + body
        ))
        return str(script), marker

    def _env(self, tmp_path, command):
        env = os.environ.copy()
        env["K7E_HOME"] = str(tmp_path / "store")
        env["OLLAMA_URL"] = "http://localhost:99999"
        env["K7E_DISTILL_COMMAND"] = command
        subprocess.run([sys.executable, K7E_PY, "stats"], env=env, capture_output=True)
        return env

    def test_a_bridge_that_cannot_launch_fails_the_run(self, tmp_path, journal):
        env = self._env(tmp_path, "k7e-no-such-harness")
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", journal],
            env=env, capture_output=True, text=True,
        )
        assert r.returncode == 1
        assert "LLM call(s) failed" in r.stderr
        assert "No new knowledge extracted." not in r.stdout

    def test_a_command_that_cannot_be_parsed_fails_the_run(self, tmp_path, journal):
        """A configuration that cannot be parsed fails identically for every
        file, so it is not the per-file skip `distill` catches ValueError for.
        Before this it skipped every capture and exited 0, and a sweep reads 0
        as a successful dream and advances past captures nothing read."""
        env = self._env(tmp_path, 'k7e-llm --note "unclosed')
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", journal],
            env=env, capture_output=True, text=True,
        )
        assert r.returncode == 1, r.stdout + r.stderr
        assert "LLM call(s) failed" in r.stderr
        assert "No new knowledge extracted." not in r.stdout

    def test_the_same_journal_stores_through_a_working_bridge(self, tmp_path, journal):
        """The positive control for the test above. A negative control proves
        the instrument can report failure; only a positive one proves it can
        report success, and a silent no-op sits exactly between them — this
        journal returning `nothing extracted` for a *working* bridge would
        make the assertion above prove nothing."""
        payload = json.dumps([{
            "title": "Parse control", "content": "The bridge ran and answered.",
            "tags": ["control"],
        }])
        bridge, ran = self._bridge(tmp_path, "control", f"print({payload!r})\n")
        env = self._env(tmp_path, bridge)
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", journal],
            env=env, capture_output=True, text=True,
        )
        assert ran.exists()
        assert r.returncode == 0, r.stderr
        assert "No new knowledge extracted." not in r.stdout

    def test_a_partial_failure_fails_the_run(self, tmp_path, long_journal):
        """One chunk lost out of many still loses input. The caller's
        watermark is per capture, so exit 0 says the whole capture was read
        and the failed chunk is never offered again."""
        payload = json.dumps([{
            "title": "Deploy branch claim",
            "content": "The note said deploys come from the hotfix branch and "
                       "not from main, cut by the owner after the suite is green.",
            "tags": ["deploy"],
        }])
        # Python rather than `sh -c`, which no Windows box has. What this test
        # is about is the second chunk failing, not the shell.
        script = write_path_executable(tmp_path, "flaky", (
            "import os, sys\n"
            "sys.stdin.read()\n"
            f"fired = {str(tmp_path / 'fired')!r}\n"
            "if os.path.exists(fired): sys.exit(7)\n"
            "open(fired, 'w', encoding='utf-8').close()\n"
            f"sys.stdout.write({payload!r})\n"
        ))
        env = self._env(tmp_path, str(script))
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", long_journal],
            env=env, capture_output=True, text=True,
        )
        assert r.returncode == 1
        assert "LLM call(s) failed" in r.stderr
        assert "the input they covered was not read" in r.stderr
        assert "[stored]" in r.stdout  # the chunk that DID work was kept

    def test_exit_zero_prose_is_a_failure_not_an_empty_answer(
        self, tmp_path, journal
    ):
        """A bridge that prints its own auth error and exits 0 looks like a
        good answer to any layer that only checks the status and a non-empty
        stdout. The required shape is a JSON array; its absence is the tell."""
        bridge, ran = self._bridge(
            tmp_path, "auth-error", "print('Error: not authenticated')\n"
        )
        env = self._env(tmp_path, bridge)
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", journal],
            env=env, capture_output=True, text=True,
        )
        assert ran.exists(), "the bridge never launched, so the failure proves nothing"
        assert r.returncode == 1
        assert "no JSON array in output" in r.stderr
        assert "No new knowledge extracted." not in r.stdout

    def test_a_dead_reranker_does_not_fail_a_healthy_distill(
        self, tmp_path, journal
    ):
        """The failure ledger is global and `diff_against_store` searches,
        which may rerank. Reading a dead reranker as a dead distill bridge
        retries a capture that was distilled perfectly well — forever.

        The bridge returns a real candidate and the store already holds
        matching entries, so the candidate reaches `diff_against_store`, the
        search runs, and the dead reranker is actually invoked — an empty
        response would never reach that path."""
        payload = json.dumps([{
            "title": "Deploy branch claim",
            "content": (
                "Deploys come from the hotfix branch and never from main, and "
                "the release tag is cut by the owner once the suite is green."
            ),
            "tags": ["deploy"],
        }])
        bridge = write_path_executable(tmp_path, "bridge", (
            "import sys\n"
            "sys.stdin.read()\n"
            f"print({payload!r})\n"
        ))
        env = self._env(tmp_path, str(bridge))
        subprocess.run(
            [sys.executable, K7E_PY, "config", "rerank", "true"],
            env=env, capture_output=True,
        )
        # Two entries the candidate's own title retrieves, because `_rerank`
        # returns early on a single hit — one seed and the path never runs.
        for title, content in [
            ("Deploy branch policy", "Deploys come from the hotfix branch and "
             "never from main; the release tag is cut by the owner once the "
             "suite is green."),
            ("Deploy branch exceptions", "Deploys from main are refused; the "
             "hotfix branch is the only deploy source and the owner cuts the "
             "release tag."),
        ]:
            subprocess.run(
                [sys.executable, K7E_PY, "store", title, "--content", content],
                env=env, capture_output=True,
            )
        rerank, rerank_ran = self._bridge(tmp_path, "dead-rerank", "sys.exit(9)\n")
        env["K7E_RERANK_COMMAND"] = rerank
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", journal],
            env=env, capture_output=True, text=True,
        )
        assert rerank_ran.exists(), "the reranker never launched"
        assert "[llm:rerank]" in r.stderr, "the reranker path never ran"
        assert r.returncode == 0, r.stderr
        assert "the input they covered was not read" not in r.stderr

    @pytest.mark.parametrize("prose", [
        "Error: token [expired]",
        "Error code [401]",
        "Error: rate limited, retry [later]",
    ])
    def test_bracket_shaped_prose_is_not_an_answer(self, tmp_path, journal, prose):
        """A shape check passes anything with brackets in it. Only a payload
        the parser can actually turn into candidates counts as read."""
        bridge, ran = self._bridge(tmp_path, "prose", f"print({prose!r})\n")
        env = self._env(tmp_path, bridge)
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", journal],
            env=env, capture_output=True, text=True,
        )
        assert ran.exists(), "the bridge never launched, so the failure proves nothing"
        assert r.returncode == 1
        assert "LLM call(s) failed" in r.stderr
        assert "No new knowledge extracted." not in r.stdout

    def test_an_empty_array_stays_a_real_answer(self, tmp_path, journal):
        bridge, _ = self._bridge(tmp_path, "empty-array", "print('  []  ')\n")
        env = self._env(tmp_path, bridge)
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", journal],
            env=env, capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "No new knowledge extracted." in r.stdout

    def test_a_working_bridge_with_nothing_to_say_succeeds(self, tmp_path, journal):
        bridge, _ = self._bridge(tmp_path, "silent", "print('[]')\n")
        env = self._env(tmp_path, bridge)
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", journal],
            env=env, capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "No new knowledge extracted." in r.stdout
        assert "the LLM bridge failed" not in r.stderr

    def test_all_malformed_but_candidate_shaped_is_not_a_failure(
        self, tmp_path, journal
    ):
        """Every item in the array carries title and content keys, so the
        model plainly attempted the schema even though every value is the
        wrong type. Recording a failure here would retry a bridge that
        shapes that same item identically every time — a deterministic
        retry loop, not a transport failure that might clear."""
        payload = json.dumps([
            {"title": "Malformed list content", "content": ["a", "b"]},
        ])
        bridge = write_path_executable(tmp_path, "bridge", (
            "import sys\n"
            "sys.stdin.read()\n"
            f"print({payload!r})\n"
        ))
        env = self._env(tmp_path, str(bridge))
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", journal],
            env=env, capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        assert "skipping candidate" in r.stderr
        assert "[stored]" not in r.stdout

    def test_mixed_payload_stores_the_valid_item_and_drops_the_rest(
        self, tmp_path, journal
    ):
        """A payload with one real candidate alongside a malformed dict and a
        bare int: the valid one is stored, and each of the other two prints
        its own drop line — nothing is silently lost."""
        payload = json.dumps([
            {
                "title": "Deploy branch claim",
                "content": (
                    "Deploys come from the hotfix branch and never from "
                    "main, and the release tag is cut by the owner once "
                    "the suite is green."
                ),
                "tags": ["deploy"],
            },
            {"title": "missing content"},
            401,
        ])
        bridge = write_path_executable(tmp_path, "bridge", (
            "import sys\n"
            "sys.stdin.read()\n"
            f"print({payload!r})\n"
        ))
        env = self._env(tmp_path, str(bridge))
        r = subprocess.run(
            [sys.executable, K7E_PY, "distill", journal],
            env=env, capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        assert "[stored]" in r.stdout
        assert r.stderr.count("skipping candidate") == 2
