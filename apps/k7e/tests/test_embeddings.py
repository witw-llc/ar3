"""The semantic track: queued on write, batched on demand, query-only on read.

The tests never need a running ollama — `fake_embeddings` stands in for it, and
the bare `store` fixture points OLLAMA_URL at a dead port, which is exactly the
absent-service case.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import engine

K7E_PY = str(Path(__file__).resolve().parent.parent / "k7e.py")


class TestWritePath:
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

    def test_absent_ollama_leaves_the_queue_and_search_still_answers(self, store):
        engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        assert engine.process_pending_embeddings() == 0
        assert engine.pending_embedding_count() == 1
        assert engine.search("kestrel rollout")[0]["title"] == "Kestrel rollout"


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

    def test_absent_ollama_degrades_to_fts(self, store):
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

    def test_json_report_names_the_backlog(self, store):
        engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        res = self.cli(store, "embed-pending", "--json")
        assert res.returncode == 0
        report = json.loads(res.stdout)
        assert report["embedded"] == 0
        assert report["pending"] == 1
        assert report["seconds"] >= 0

    def test_search_notes_an_unanswered_track_on_stderr(self, store):
        engine.store_entry("Kestrel rollout", "Roll the fleet forward", tags=["ops"])
        res = self.cli(store, "search", "kestrel rollout", "--ids")
        assert "K7E-000-00001" in res.stdout
        assert "semantic track unavailable" in res.stderr
