"""Opt-in live check of the hosted embeddings provider.

It makes one paid request, so the `llm` marker alone does not run it: the
operator also sets K7E_LIVE_OPENAI=1 and supplies OPENAI_API_KEY.
"""
import os

import pytest

import embeddings

pytestmark = pytest.mark.llm


def test_one_real_request_returns_a_vector_of_the_asked_size(store, monkeypatch):
    if os.environ.get("K7E_LIVE_OPENAI") != "1" or not os.environ.get("OPENAI_API_KEY", "").strip():
        pytest.skip("set K7E_LIVE_OPENAI=1 and OPENAI_API_KEY to make one paid request")
    monkeypatch.setenv("K7E_EMBEDDINGS", "openai")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("K7E_EMBED_DIMENSIONS", "8")
    vector = embeddings.embed("synthetic sentence about a garden", timeout=15)
    assert vector is not None
    assert len(vector) == 8
