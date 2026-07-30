"""Search quality tests — precision, recall, contradictions, scale."""
import time
import engine

class TestRecall:
    def test_recall_at_5_exact_titles(self, store):
        """Exact title queries should achieve recall@5 >= 1.0."""
        facts = [
            ("Nginx reverse proxy", "Use nginx with proxy_pass directive", ["nginx"]),
            ("Git rebase workflow", "Always rebase feature branches before merge", ["git"]),
            ("Tmux prefix key", "Tmux prefix is Ctrl-b, remap to Ctrl-a", ["tmux"]),
            ("PostgreSQL vacuum", "VACUUM ANALYZE updates table statistics", ["postgres"]),
            ("Docker layer caching", "Put rarely-changing layers first", ["docker"]),
        ]
        ids = [engine.store_entry(t, c, tags=tg) for t, c, tg in facts]
        hits = 0
        for (title, _, _), nid in zip(facts, ids):
            results = engine.search(title, limit=5)
            if any(r["id"] == nid for r in results):
                hits += 1
        recall = hits / len(facts)
        assert recall >= 0.9, f"recall@5 = {recall:.2f}"

    def test_recall_by_content_keywords(self, store):
        """Content keyword queries should find the right entry."""
        engine.store_entry("SSH Config", "Use ProxyJump for bastion host access", tags=["ssh"])
        engine.store_entry("Docker Networks", "Bridge mode isolates containers by default", tags=["docker"])
        engine.store_entry("Vim Macros", "Record with qa, replay with @a", tags=["vim"])

        assert engine.search("ProxyJump bastion")[0]["title"] == "SSH Config"
        assert engine.search("bridge isolates containers")[0]["title"] == "Docker Networks"
        assert engine.search("record replay macro")[0]["title"] == "Vim Macros"

    def test_false_negative_rate(self, store):
        """Store 20 facts with unique content, query each. Zero misses."""
        facts = [(f"Fact-{i}", f"UniqueContent-{i}-Marker-{i*13}", [f"t{i%5}"]) for i in range(20)]
        ids = [engine.store_entry(t, c, tags=tg) for t, c, tg in facts]
        misses = []
        for i, (_, content, _) in enumerate(facts):
            results = engine.search(f"UniqueContent-{i}-Marker-{i*13}")
            if not any(r["id"] == ids[i] for r in results):
                misses.append(i)
        assert len(misses) == 0, f"False negatives: indices {misses}"


class TestContradictions:
    def test_conflicting_entries_both_surface(self, store):
        """Contradictory facts should both be findable."""
        engine.store_entry("Python GIL", "Python GIL prevents true thread parallelism", tags=["python"])
        engine.store_entry("Python no-GIL", "Python 3.13 removes GIL with free-threading", tags=["python"])
        results = engine.search("Python GIL parallelism", limit=5)
        ids = {r["id"] for r in results}
        assert len(ids) >= 2, f"Only {len(ids)} result for contradictory facts"

    def test_no_false_positives_unrelated(self, store):
        """Queries about topics not in store should return empty."""
        engine.store_entry("Redis Caching", "Use Redis for session caching", tags=["redis"])
        engine.store_entry("Docker Compose", "Define services in docker-compose.yml", tags=["docker"])
        assert len(engine.search("quantum computing qubits")) == 0
        assert len(engine.search("chocolate cake recipe")) == 0


class TestScale:
    def test_needle_in_500_nodes(self, store):
        """A unique entry is findable among 500 others."""
        for i in range(500):
            engine.store_entry(f"Filler {i}", f"Generic content about topic {i}", tags=[f"t{i%20}"])
        engine.store_entry("Needle Entry", "xylophone-zebra-quantum-flux-unique", tags=["needle"])
        results = engine.search("xylophone zebra quantum flux")
        assert len(results) >= 1
        assert results[0]["title"] == "Needle Entry"

    def test_search_under_100ms_at_500(self, store):
        for i in range(500):
            engine.store_entry(f"Node {i}", f"Content about subject-{i} details-{i}", tags=[f"a{i%10}"])
        start = time.perf_counter()
        engine.search("subject-250 details-250")
        elapsed = time.perf_counter() - start
        assert elapsed < 0.1, f"Search took {elapsed:.3f}s at 500 nodes"

    def test_store_under_20ms_at_500(self, store):
        for i in range(500):
            engine.store_entry(f"Pre {i}", f"content {i}", tags=["bulk"])
        start = time.perf_counter()
        engine.store_entry("Benchmark", "timed entry", tags=["bench"])
        elapsed = time.perf_counter() - start
        assert elapsed < 0.02, f"Store took {elapsed:.3f}s at 500 nodes"
