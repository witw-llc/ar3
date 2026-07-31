"""Performance regression tests."""
import pytest
import engine
from conftest import PERF_FACTOR, best_of_3


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


def _five_searches(query):
    for _ in range(5):
        engine.search(query)


class TestSearchPerformance:
    def test_search_at_500_under_100ms(self, store_500):
        avg = best_of_3(lambda _: _five_searches("subject-250 detail-250")) / 5
        limit = 0.1 * PERF_FACTOR
        assert avg < limit, f"Avg search: {avg:.3f}s (limit {limit:.3f}s)"

    @pytest.mark.slow
    def test_search_at_1000_under_200ms(self, store_1000):
        avg = best_of_3(lambda _: _five_searches("topic-500 keyword-500")) / 5
        limit = 0.2 * PERF_FACTOR
        assert avg < limit, f"Avg search: {avg:.3f}s (limit {limit:.3f}s)"


class TestStorePerformance:
    def test_store_at_500_under_50ms(self, store_500):
        # Content-hash dedup short-circuits an identical body, so each run
        # needs its own content to exercise the same write path.
        elapsed = best_of_3(lambda run: engine.store_entry("Benchmark", f"timed {run}", tags=["bench"]))
        limit = 0.05 * PERF_FACTOR
        assert elapsed < limit, f"Store: {elapsed:.3f}s (limit {limit:.3f}s)"


class TestReindexPerformance:
    @pytest.mark.slow
    def test_reindex_500_under_5s(self, store_500):
        elapsed = best_of_3(lambda _: engine.reindex())
        limit = 5.0 * PERF_FACTOR
        assert elapsed < limit, f"Reindex 500: {elapsed:.2f}s (limit {limit:.2f}s)"
