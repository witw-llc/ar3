"""Performance regression tests."""
import time
import pytest
import engine


@pytest.fixture
def store_500(store):
    for i in range(500):
        engine.store_entry(f"Node {i}", f"Content about subject-{i} with detail-{i}", tags=[f"area-{i%20}"])
    return store


@pytest.fixture
def store_1000(store):
    for i in range(1000):
        engine.store_entry(f"Entry {i}", f"Text about topic-{i} keyword-{i}", tags=[f"cat-{i%30}"])
    return store


class TestSearchPerformance:
    def test_search_at_500_under_100ms(self, store_500):
        start = time.perf_counter()
        for _ in range(5):
            engine.search("subject-250 detail-250")
        avg = (time.perf_counter() - start) / 5
        assert avg < 0.1, f"Avg search: {avg:.3f}s"

    @pytest.mark.slow
    def test_search_at_1000_under_200ms(self, store_1000):
        start = time.perf_counter()
        for _ in range(5):
            engine.search("topic-500 keyword-500")
        avg = (time.perf_counter() - start) / 5
        assert avg < 0.2, f"Avg search: {avg:.3f}s"


class TestStorePerformance:
    def test_store_at_500_under_50ms(self, store_500):
        start = time.perf_counter()
        engine.store_entry("Benchmark", "timed", tags=["bench"])
        elapsed = time.perf_counter() - start
        assert elapsed < 0.05, f"Store: {elapsed:.3f}s"


class TestReindexPerformance:
    @pytest.mark.slow
    def test_reindex_500_under_5s(self, store_500):
        start = time.perf_counter()
        engine.reindex()
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"Reindex 500: {elapsed:.2f}s"
