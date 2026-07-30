"""Usage-weighted ranking — what counts as a use, and how it moves scores."""
import os
import subprocess
import sys
from pathlib import Path

import engine

K7E_PY = str(Path(__file__).resolve().parent.parent / "k7e.py")


def use_count(node_id):
    conn = engine._connect()
    row = conn.execute("SELECT use_count FROM nodes WHERE id = ?", (node_id,)).fetchone()
    conn.close()
    return row[0]


def last_used_at(node_id):
    conn = engine._connect()
    row = conn.execute("SELECT last_used_at FROM nodes WHERE id = ?", (node_id,)).fetchone()
    conn.close()
    return row[0]


class TestWhatCountsAsUse:
    """Consumption counts; a search listing does not. Appearing in a result list
    is a weak signal (the caller may never look at the entry), so only reading
    full content counts: `get(track_usage=True)` and recall synthesis."""

    def test_fresh_entry_starts_at_zero(self, store):
        nid = engine.store_entry("Redis Port", "Redis listens on 6379", tags=["redis"])
        assert use_count(nid) == 0
        assert last_used_at(nid) is None

    def test_search_listing_does_not_count(self, store):
        nid = engine.store_entry("Redis Port", "Redis listens on 6379", tags=["redis"])
        for _ in range(3):
            results = engine.search("redis port")
            assert any(r["id"] == nid for r in results)
        assert use_count(nid) == 0

    def test_untracked_get_does_not_count(self, store):
        nid = engine.store_entry("Redis Port", "Redis listens on 6379", tags=["redis"])
        engine.get(nid)
        assert use_count(nid) == 0

    def test_tracked_get_counts_every_read(self, store):
        nid = engine.store_entry("Redis Port", "Redis listens on 6379", tags=["redis"])
        for expected in (1, 2, 3):
            engine.get(nid, track_usage=True)
            assert use_count(nid) == expected

    def test_tracked_get_stamps_last_used_at(self, store):
        nid = engine.store_entry("Redis Port", "Redis listens on 6379", tags=["redis"])
        engine.get(nid, track_usage=True)
        assert last_used_at(nid) is not None

    def test_cli_get_counts(self, store, tmp_path):
        nid = engine.store_entry("Redis Port", "Redis listens on 6379", tags=["redis"])
        env = os.environ.copy()
        env["K7E_HOME"] = str(tmp_path)
        r = subprocess.run(
            [sys.executable, K7E_PY, "get", nid],
            env=env, capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert use_count(nid) == 1

    def test_recall_counts_returned_sources(self, store):
        nid = engine.store_entry("Redis Eviction", "Redis evicts with allkeys-lru", tags=["redis"])
        _, sources = engine.recall("redis eviction")
        assert [s["id"] for s in sources] == [nid]
        assert use_count(nid) == 1

    def test_recall_leaves_unmatched_entries_alone(self, store):
        hit = engine.store_entry("Redis Eviction", "Redis evicts with allkeys-lru", tags=["redis"])
        miss = engine.store_entry("Vim Macros", "Record with qa, replay with @a", tags=["vim"])
        _, sources = engine.recall("redis eviction")
        assert [s["id"] for s in sources] == [hit]
        assert use_count(hit) == 1
        assert use_count(miss) == 0


class TestUseBoost:
    def test_zero_count_is_identity(self):
        assert engine._use_boost(0, 0.2) == 1.0
        assert engine._use_boost(None, 0.2) == 1.0

    def test_boost_is_log10_scaled(self):
        assert engine._use_boost(9, 0.2) == 1.2
        assert engine._use_boost(99, 0.2) == 1.4

    def test_boost_is_monotonic(self):
        boosts = [engine._use_boost(n, 0.2) for n in range(0, 50)]
        assert boosts == sorted(boosts)
        assert boosts[0] < boosts[-1]

    def test_zero_weight_disables(self):
        assert engine._use_boost(1000, 0.0) == 1.0


class TestRankingWithUsage:
    def test_zero_counts_rank_exactly_as_boost_disabled(self, store, monkeypatch):
        """A fresh index ranks identically to an index with the boost turned
        off — the feature degrades to the pre-usage behavior."""
        for title, content, tags in (
            ("Nginx Reverse Proxy", "Use nginx proxy_pass for upstreams", ["nginx"]),
            ("Nginx TLS", "Terminate TLS at nginx with ssl_certificate", ["nginx", "tls"]),
            ("Nginx Rate Limit", "limit_req_zone throttles nginx requests", ["nginx"]),
        ):
            engine.store_entry(title, content, tags=tags)

        with_boost = [(r["id"], r["score"]) for r in engine.search("nginx", limit=5)]
        monkeypatch.setenv("K7E_USE_WEIGHT", "0")
        without_boost = [(r["id"], r["score"]) for r in engine.search("nginx", limit=5)]
        assert with_boost == without_boost

    def test_score_rises_with_use(self, store):
        nid = engine.store_entry("Redis Eviction", "Redis evicts with allkeys-lru", tags=["redis"])
        before = engine.search("redis eviction")[0]["score"]
        for _ in range(10):
            engine.get(nid, track_usage=True)
        after = engine.search("redis eviction")[0]["score"]
        assert after > before

    def test_used_entry_overtakes_tied_peer(self, store):
        a = engine.store_entry("Redis Cache Eviction", "Redis evicts keys with allkeys-lru policy", tags=["redis"])
        b = engine.store_entry("Redis Eviction Tuning", "Tune redis eviction with maxmemory-policy", tags=["redis"])
        baseline = engine.search("redis eviction policy", limit=5)
        assert {r["id"] for r in baseline} == {a, b}
        assert baseline[0]["score"] == baseline[1]["score"]

        runner_up = baseline[1]["id"]
        engine.get(runner_up, track_usage=True)
        assert engine.search("redis eviction policy", limit=5)[0]["id"] == runner_up

    def test_usage_does_not_overturn_a_large_relevance_gap(self, store):
        """The boost is log-scaled and weighted, so relevance stays dominant."""
        strong = engine.store_entry(
            "Redis Eviction Policy", "Redis eviction policy is allkeys-lru by default",
            tags=["redis", "eviction"],
        )
        weak = engine.store_entry("Redis Eviction Trivia", "Redis eviction was added long ago", tags=["redis"])
        baseline = engine.search("redis eviction policy allkeys", limit=5)
        assert [r["id"] for r in baseline] == [strong, weak]
        assert baseline[0]["score"] > 2 * baseline[1]["score"]

        for _ in range(200):
            engine.get(weak, track_usage=True)
        after = engine.search("redis eviction policy allkeys", limit=5)
        assert [r["id"] for r in after] == [strong, weak]
        assert after[1]["score"] > baseline[1]["score"]

    def test_usage_does_not_surface_a_non_matching_entry(self, store):
        engine.store_entry("Redis Eviction Policy", "Redis eviction policy is allkeys-lru", tags=["redis"])
        unrelated = engine.store_entry("Vim Macros", "Record with qa, replay with @a", tags=["vim"])
        for _ in range(100):
            engine.get(unrelated, track_usage=True)
        results = engine.search("redis eviction policy", limit=5)
        assert unrelated not in [r["id"] for r in results]


class TestReindexResetsUsage:
    def test_counts_reset_on_reindex(self, store):
        nid = engine.store_entry("Redis Port", "Redis listens on 6379", tags=["redis"])
        for _ in range(5):
            engine.get(nid, track_usage=True)
        assert use_count(nid) == 5

        engine.reindex()
        assert use_count(nid) == 0
        assert last_used_at(nid) is None
