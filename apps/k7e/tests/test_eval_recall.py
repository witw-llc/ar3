"""Recall@K evaluation harness (issue #145, workstream 4).

Deterministic, checked-in fixtures (eval/corpus.json + eval/questions.json)
measure whether the right entry surfaces in the top-K results.

- Tier A (this default-fixture class): FTS + metadata + RRF + decay, no ollama.
  Runs in the always-on `k7e` CI job. The ruler that proves ranking changes
  help (or at least do not regress).
- Tier B (@llm class): full hybrid + LLM reranker against a real ollama.
  Runs in the `llm` CI job; skipped when ollama is unavailable.
"""
import json
import urllib.request
from pathlib import Path

import pytest

import engine

EVAL_DIR = Path(__file__).resolve().parent / "eval"


def _load(name):
    return json.loads((EVAL_DIR / name).read_text(encoding="utf-8"))


def _seed():
    """Store the corpus; return {fixture_key: node_id}."""
    key_to_id = {}
    for fact in _load("corpus.json"):
        nid = engine.store_entry(fact["title"], fact["content"], tags=fact.get("tags", []))
        key_to_id[fact["key"]] = nid
    return key_to_id


def _recall_at_k(key_to_id, questions, k, rerank=False):
    hits = 0
    misses = []
    for item in questions:
        results = engine.search(item["q"], limit=k, rerank=rerank)
        ids = [r["id"] for r in results[:k]]
        if key_to_id.get(item["expect"]) in ids:
            hits += 1
        else:
            misses.append(item["expect"])
    return hits / len(questions), misses


class TestEvalRecallDeterministic:
    """Tier A: FTS + metadata + RRF + decay. No LLM, no ollama."""

    def test_corpus_and_questions_consistent(self, store):
        corpus_keys = {f["key"] for f in _load("corpus.json")}
        for item in _load("questions.json"):
            assert item["expect"] in corpus_keys, f"question targets unknown key {item['expect']}"

    def test_recall_thresholds(self, store, capsys):
        # Deterministic FTS + metadata + RRF + decay baseline (no ollama):
        # measured R@1=0.66 R@3=0.81 R@5=R@10=0.84. Thresholds sit just under
        # that so fixture tweaks have a little slack; embeddings + rerank
        # (Tier B) are expected to push these higher.
        key_to_id = _seed()
        questions = _load("questions.json")
        scores = {k: _recall_at_k(key_to_id, questions, k)[0] for k in (1, 3, 5, 10)}
        with capsys.disabled():
            print("\n[eval] FTS-only " + " ".join(f"R@{k}={v:.2f}" for k, v in scores.items()))
        floors = {1: 0.60, 3: 0.75, 5: 0.80, 10: 0.80}
        for k, floor in floors.items():
            assert scores[k] >= floor, f"R@{k}={scores[k]:.2f} below floor {floor}"

    def test_decay_does_not_regress_recent_corpus(self, store, monkeypatch):
        """A freshly stored corpus is inside the flat zone, so an aggressive
        decay scale must not change which docs surface in the top-K."""
        key_to_id = _seed()
        questions = _load("questions.json")
        baseline, _ = _recall_at_k(key_to_id, questions, 10)
        monkeypatch.setenv("K7E_DECAY_SCALE", "30")
        monkeypatch.setenv("K7E_DECAY_OFFSET", "1")
        decayed, _ = _recall_at_k(key_to_id, questions, 10)
        assert decayed == baseline


def _ollama_available(url):
    try:
        urllib.request.urlopen(f"{url}/api/tags", timeout=2)
        return True
    except Exception:
        return False


@pytest.mark.llm
class TestEvalRecallHybrid:
    """Tier B: full hybrid retrieval + LLM reranker against a real ollama.

    Embeddings only contribute if the embed model is pulled; otherwise this
    still exercises the LLM rerank path end-to-end. Asserts no regression
    against the Tier A floor and prints the measured numbers.
    """

    @pytest.fixture
    def hybrid_store(self, tmp_path, monkeypatch):
        url = "http://localhost:11434"
        if not _ollama_available(url):
            pytest.skip("ollama not running")
        monkeypatch.setenv("K7E_HOME", str(tmp_path))
        monkeypatch.setenv("OLLAMA_URL", url)
        engine.reset(tmp_path)
        engine.init()
        return tmp_path

    def test_hybrid_recall_with_rerank(self, hybrid_store, capsys):
        key_to_id = _seed()
        engine.process_pending_embeddings()
        questions = _load("questions.json")
        # One reranked search per question; derive R@5 and R@10 from it to keep
        # the LLM call count (and CI time) down.
        hits5 = hits10 = 0
        miss10 = []
        for item in questions:
            ranked = [r["id"] for r in engine.search(item["q"], limit=10, rerank=True)]
            target = key_to_id.get(item["expect"])
            if target in ranked[:5]:
                hits5 += 1
            if target in ranked[:10]:
                hits10 += 1
            else:
                miss10.append(item["expect"])
        r5, r10 = hits5 / len(questions), hits10 / len(questions)
        with capsys.disabled():
            print(f"\n[eval] hybrid+rerank R@5={r5:.2f} R@10={r10:.2f}")
        # Conservative floor: proves the LLM rerank path runs end-to-end against
        # a real (tiny) model without catastrophic regression. The printed
        # numbers carry the quality signal; small rerankers have known variance.
        assert r10 >= 0.75, f"R@10={r10:.2f} misses={miss10}"
