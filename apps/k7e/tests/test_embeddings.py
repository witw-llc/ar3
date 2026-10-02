"""The semantic track: queued on write, batched on demand, query-only on read.

No test here needs a running ollama, and only one opens a socket at all.
`store` turns the track off outright; `fake_embeddings` gives it a working
stand-in; `dead_embeddings` gives it one that never answers. The degradation
path used to be driven by pointing OLLAMA_URL at an unusable port, which
assumed a refused connection is free — true on POSIX, and about four seconds
per call on Windows.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config
import embeddings
import engine

K7E_PY = str(Path(__file__).resolve().parent.parent / "k7e.py")


class TestEmbedTextItself:
    """The one place a real socket is opened, so the rest do not have to.

    Every other caller treats `None` as an absent server; this proves that is
    what an unreachable one actually produces, rather than an exception
    escaping into a search."""

    def test_an_unreachable_ollama_returns_none(self, store, monkeypatch):
        monkeypatch.setenv("K7E_EMBEDDINGS", "ollama")
        assert engine.embed_text("kestrel rollout", timeout=1.0) is None


class TestWritePath:
    def test_the_bare_store_has_the_track_switched_off(self, store):
        """The default the other fixtures opt out of. A store that still
        queued would still search, and searching is what costs four seconds a
        call on Windows against a port chosen for being unusable."""
        engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        assert engine.pending_embedding_count() == 0

    def test_store_queues_and_never_embeds(self, store, fake_embeddings):
        engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        assert engine.pending_embedding_count() == 1
        assert fake_embeddings == []

    def test_append_queues_too(self, store, fake_embeddings):
        node_id = engine.store_entry("Kestrel rollout", "Roll forward", tags=["ops"])
        engine.process_pending_embeddings()
        fake_embeddings.clear()
        engine.append_entry(node_id, "Edge Cases", "Stalls when the fleet is draining")
        assert engine.pending_embedding_count() == 1
        assert fake_embeddings == []


class TestBacklog:
    def test_batch_drains_the_queue(self, store, fake_embeddings):
        for i in range(3):
            engine.store_entry(f"Runbook {i}", f"Stage {i} of the rollout", tags=["ops"])
        assert engine.process_pending_embeddings() == 3
        assert engine.pending_embedding_count() == 0
        assert len(fake_embeddings) == 3

    def test_batch_embeds_on_the_generous_budget(self, store, fake_embeddings):
        engine.store_entry("Runbook", "Stage one", tags=["ops"])
        engine.process_pending_embeddings()
        assert fake_embeddings[0][1] == engine.EMBED_TIMEOUT

    def test_absent_ollama_leaves_the_queue_and_search_still_answers(self, dead_embeddings):
        engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        assert engine.process_pending_embeddings() == 0
        assert engine.pending_embedding_count() == 1
        assert engine.search("kestrel rollout")[0]["title"] == "Kestrel rollout"


class TestCanonicalEmbeddingInput:
    def snapshot(self, node_id):
        conn = engine._connect()
        try:
            indexed = conn.execute(
                "SELECT f.content, n.embedding_input_hash FROM nodes n "
                "JOIN nodes_fts f ON f.rowid = n.rowid WHERE n.id = ?", (node_id,)
            ).fetchone()
            cached = conn.execute(
                "SELECT vector, provider, model, dimensions, text_hash, text_version, updated_at "
                "FROM embeddings WHERE node_id = ?", (node_id,)
            ).fetchone()
            return indexed, cached
        finally:
            conn.close()

    @pytest.mark.parametrize("content", [
        "Roll the fleet forward",
        "Kestrel caf\u00e9 \u6771\u4eac rollout. " * 40 + "Beyond the embedding cutoff.",
    ], ids=["short", "long-unicode"])
    def test_fresh_write_embeds_persisted_body(self, store, fake_embeddings, content):
        title = "Kestrel rollout"
        node_id = engine.store_entry(title, content, tags=["ops"])
        rendered = engine._node_path(node_id).read_bytes()
        body = engine._extract_body(rendered.decode("utf-8"))
        expected_input = engine._embedding_text(rendered.decode("utf-8"))
        expected_hash = hashlib.sha256(expected_input.encode()).hexdigest()

        indexed, cached = self.snapshot(node_id)
        assert indexed == (content, expected_hash)
        assert cached is None
        assert fake_embeddings == []
        assert engine.process_pending_embeddings() == 1
        assert fake_embeddings == [(expected_input, engine.EMBED_TIMEOUT)]
        assert self.snapshot(node_id)[0] == indexed
        assert self.snapshot(node_id)[1][4:6] == (expected_hash, "title-body-500-reserved-headings-v2")
        assert embeddings.TEXT_VERSION == "title-body-500-reserved-headings-v2"
        assert engine._node_path(node_id).read_bytes() == rendered

    @pytest.mark.parametrize("content", [
        "Roll the fleet forward",
        "Kestrel caf\u00e9 \u6771\u4eac rollout. " * 40 + "Beyond the embedding cutoff.",
    ], ids=["short", "long-unicode"])
    @pytest.mark.parametrize("title", ["Kestrel rollout", "  Kestrel rollout  "])
    def test_first_and_second_plain_reindex_keep_fresh_cache(self, store, fake_embeddings, content, title):
        node_id = engine.store_entry(title, content, tags=["ops"])
        rendered = engine._node_path(node_id).read_bytes()
        assert engine.process_pending_embeddings() == 1
        original_indexed, original_cache = self.snapshot(node_id)
        body = engine._extract_body(rendered.decode("utf-8"))
        calls = list(fake_embeddings)
        assert engine.embedding_coverage() == {"total": 1, "current": 1, "pending": 0}

        for _ in range(2):
            engine.reindex()
            assert engine.embedding_coverage() == {"total": 1, "current": 1, "pending": 0}
            assert engine.pending_embedding_count() == 0
            assert self.snapshot(node_id) == ((body, original_indexed[1]), original_cache)
            assert fake_embeddings == calls
            assert engine.process_pending_embeddings() == 0
            assert fake_embeddings == calls
            assert self.snapshot(node_id) == ((body, original_indexed[1]), original_cache)
            assert engine._node_path(node_id).read_bytes() == rendered

    def test_source_markdown_preserves_authored_sections_and_code(self, store, fake_embeddings):
        title = "History 2042-03-04 / ## Verified Protocol / caf\u00e9"
        content = (
            "## History\n\n* 2042-03-04: Preserve this user-authored history.\n\n"
            "## Verified Protocol\n\nKeep the date 2042-03-04 and \u6771\u4eac.\n\n"
            "```markdown\n## History\n* 2042-03-04: Literal fenced content.\n```\n\n"
            "## Edge Cases\n\nKeep standard section names.\n\n"
            "## False Paths\n\nDo not remove authored sections."
        )
        node_id = engine.store_entry(title, content, tags=["ops"])
        rendered = engine._node_path(node_id).read_bytes()
        body = engine._extract_body(rendered.decode("utf-8"))
        assert body == "\n" + content + "\n"
        assert len(body) < 500
        expected_body = (
            "* 2042-03-04: Preserve this user-authored history.\n"
            "Keep the date 2042-03-04 and \u6771\u4eac.\n"
            "```markdown\n## History\n* 2042-03-04: Literal fenced content.\n```\n"
            "Keep standard section names.\nDo not remove authored sections."
        )
        expected_input = f"{title} {expected_body}"
        expected_hash = hashlib.sha256(expected_input.encode()).hexdigest()
        before, _ = self.snapshot(node_id)
        assert before == (content, expected_hash)

        assert engine.process_pending_embeddings() == 1
        assert fake_embeddings == [(expected_input, engine.EMBED_TIMEOUT)]
        assert self.snapshot(node_id)[0] == before
        cache = self.snapshot(node_id)[1]
        for _ in range(2):
            engine.reindex()
            assert self.snapshot(node_id) == ((body, expected_hash), cache)
            assert engine.process_pending_embeddings() == 0
            assert fake_embeddings == [(expected_input, engine.EMBED_TIMEOUT)]
            assert engine._node_path(node_id).read_bytes() == rendered

    def test_existing_pending_raw_hash_refreshes_without_rewriting_fts(self, store, fake_embeddings):
        title, content = "Kestrel rollout", "Roll the fleet forward"
        node_id = engine.store_entry(title, content, tags=["ops"])
        rendered = engine._node_path(node_id).read_bytes()
        body = engine._extract_body(rendered.decode("utf-8"))
        expected_input = engine._embedding_text(rendered.decode("utf-8"))
        expected_hash = hashlib.sha256(expected_input.encode()).hexdigest()
        raw_hash = hashlib.sha256(f"{title} {content[:500]}".encode()).hexdigest()
        conn = engine._connect()
        conn.execute("UPDATE nodes SET embedding_input_hash = ? WHERE id = ?", (raw_hash, node_id))
        conn.commit()
        conn.close()
        assert self.snapshot(node_id) == ((content, raw_hash), None)

        assert engine.process_pending_embeddings() == 1
        assert fake_embeddings == [(expected_input, engine.EMBED_TIMEOUT)]
        indexed, cache = self.snapshot(node_id)
        assert indexed == (content, expected_hash)
        assert cache[4:6] == (expected_hash, "title-body-500-reserved-headings-v2")
        assert engine.embedding_coverage() == {"total": 1, "current": 1, "pending": 0}
        assert engine.process_pending_embeddings() == 0
        engine.reindex()
        assert self.snapshot(node_id) == ((body, expected_hash), cache)
        assert engine.process_pending_embeddings() == 0
        assert fake_embeddings == [(expected_input, engine.EMBED_TIMEOUT)]
        assert engine._node_path(node_id).read_bytes() == rendered

    def test_substantive_append_needs_one_refresh_then_reindex_reuses_it(self, store, fake_embeddings):
        node_id = engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        assert engine.process_pending_embeddings() == 1
        original_indexed, original_cache = self.snapshot(node_id)
        engine.append_entry(node_id, "Edge Cases", "Pause when the fleet is draining")
        rendered = engine._node_path(node_id).read_bytes()
        body = engine._extract_body(rendered.decode("utf-8"))
        expected_input = engine._embedding_text(rendered.decode("utf-8"))
        expected_hash = hashlib.sha256(expected_input.encode()).hexdigest()

        indexed, cached = self.snapshot(node_id)
        assert indexed == (body, expected_hash)
        assert indexed[1] != original_indexed[1]
        assert cached == original_cache
        assert engine.embedding_coverage() == {"total": 1, "current": 0, "pending": 1}
        assert len(fake_embeddings) == 1
        assert engine.process_pending_embeddings() == 1
        assert fake_embeddings[-1] == (expected_input, engine.EMBED_TIMEOUT)
        refreshed = self.snapshot(node_id)
        assert refreshed[1][4:6] == (expected_hash, "title-body-500-reserved-headings-v2")
        assert refreshed[1] != original_cache

        for _ in range(2):
            engine.reindex()
            assert engine.embedding_coverage() == {"total": 1, "current": 1, "pending": 0}
            assert self.snapshot(node_id) == refreshed
            assert engine.process_pending_embeddings() == 0
            assert len(fake_embeddings) == 2
            assert engine._node_path(node_id).read_bytes() == rendered

    def test_get_and_status_do_not_refresh_pending_or_current_vectors(self, store, fake_embeddings, monkeypatch):
        monkeypatch.setenv("K7E_EMBEDDINGS", "openai")
        monkeypatch.setenv("EMBED_MODEL", "text-embedding-3-small")
        monkeypatch.setenv("K7E_EMBED_DIMENSIONS", "256")
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-runtime-key")

        def no_network(*args, **kwargs):
            pytest.fail("Reading nodes and probing status must stay offline")

        monkeypatch.setattr(embeddings.urllib.request, "urlopen", no_network)
        monkeypatch.setattr(embeddings.urllib.request, "build_opener", no_network)
        node_id = engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        rendered = engine._node_path(node_id).read_bytes()

        for current in (0, 1):
            before = self.snapshot(node_id)
            calls = list(fake_embeddings)
            assert engine.get(node_id) == rendered.decode("utf-8")
            assert engine.embedding_coverage() == {"total": 1, "current": current, "pending": 1 - current}
            assert engine.pending_embedding_count() == 1 - current
            report = config.status()
            assert f"{current}/1 current; {1 - current} pending" in report
            assert "API access unverified" in report
            assert self.snapshot(node_id) == before
            assert fake_embeddings == calls
            assert engine._node_path(node_id).read_bytes() == rendered
            if not current:
                assert engine.process_pending_embeddings() == 1


class TestReadPath:
    def test_search_embeds_the_query_and_nothing_else(self, store, fake_embeddings):
        for i in range(3):
            engine.store_entry(f"Runbook {i}", f"Stage {i} of the rollout", tags=["ops"])
        engine.process_pending_embeddings()
        fake_embeddings.clear()
        engine.search("kestrel rollout")
        assert [text for text, _ in fake_embeddings] == ["kestrel rollout"]

    def test_query_rides_the_short_budget(self, store, fake_embeddings):
        engine.search("anything")
        assert fake_embeddings[0][1] == engine.QUERY_EMBED_TIMEOUT

    def test_query_budget_is_configurable(self, store, fake_embeddings, monkeypatch):
        monkeypatch.setenv("K7E_EMBED_QUERY_TIMEOUT", "0.25")
        engine.search("anything")
        assert fake_embeddings[0][1] == 0.25

    def test_absent_ollama_degrades_to_fts(self, dead_embeddings):
        engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        results = engine.search("kestrel rollout")
        assert results[0]["title"] == "Kestrel rollout"
        assert engine.LAST_QUERY_EMBED_OK is False
        assert engine.LAST_QUERY_EMBED_MS is not None

    def test_live_track_reports_its_latency(self, store, fake_embeddings):
        engine.search("kestrel rollout")
        assert engine.LAST_QUERY_EMBED_OK is True
        assert engine.LAST_QUERY_EMBED_MS >= 0


class TestOffSwitch:
    def test_off_queues_nothing_and_embeds_nothing(self, store, fake_embeddings, monkeypatch):
        monkeypatch.setenv("K7E_EMBEDDINGS", "off")
        engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        assert engine.pending_embedding_count() == 0
        assert engine.process_pending_embeddings() == 0
        assert engine.search("kestrel rollout")[0]["title"] == "Kestrel rollout"
        assert fake_embeddings == []
        assert engine.LAST_QUERY_EMBED_MS is None


class TestEmbedPendingCLI:
    def cli(self, home, *args):
        env = os.environ.copy()
        env["K7E_HOME"] = str(home)
        env["OLLAMA_URL"] = "http://localhost:99999"
        return subprocess.run(
            [sys.executable, K7E_PY, *args], env=env, capture_output=True, text=True
        )

    def test_json_report_names_the_backlog(self, store, absent_ollama):
        engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        res = self.cli(store, "embed-pending", "--json")
        assert res.returncode == 0
        report = json.loads(res.stdout)
        assert report["embedded"] == 0
        assert report["pending"] == 1
        assert report["seconds"] >= 0

    def test_search_notes_an_unanswered_track_on_stderr(self, store, absent_ollama):
        engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        res = self.cli(store, "search", "kestrel rollout", "--ids")
        assert "K7E-000-00001" in res.stdout
        assert "semantic track unavailable" in res.stderr
