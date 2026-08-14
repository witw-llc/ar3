"""Shared fixtures for K7E tests."""

import hashlib
import os
import re
import sys
import time
from pathlib import Path

import pytest

K7E_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(K7E_DIR))
# `ark` sits at the repo root, shared by every app. Put it on the path here
# rather than relying on some k7e module having run first.
sys.path.append(str(K7E_DIR.parent.parent))

import config

# Shared CI runners vary enough that absolute thresholds flake; loosen only
# when the workflow sets K7E_PERF_FACTOR (default 1 keeps local runs strict).
PERF_FACTOR = float(os.environ.get("K7E_PERF_FACTOR", "1"))


def best_of_3(operation):
    """Fastest of three timed runs of operation(run_index).

    A one-off runner stall inflates one run and is discarded; a genuine
    regression is slow in all three, so the minimum still fails."""
    elapsed = []
    for run in range(3):
        start = time.perf_counter()
        operation(run)
        elapsed.append(time.perf_counter() - start)
    return min(elapsed)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated K7E store in tmp_path."""
    monkeypatch.setenv("K7E_HOME", str(tmp_path))
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:99999")
    monkeypatch.delenv("K7E_LLM_COMMAND", raising=False)
    for _, env_key in config.LLM_PURPOSES.values():
        monkeypatch.delenv(env_key, raising=False)

    import engine
    engine.reset(tmp_path)
    engine.init()
    return tmp_path


EMBED_DIM = 256


def fake_vector(text):
    """Hashed bag of words. Two texts that share vocabulary point the same way,
    which is all the semantic track needs to be exercised deterministically."""
    vec = [0.0] * EMBED_DIM
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vec[int(hashlib.sha1(word.encode()).hexdigest(), 16) % EMBED_DIM] += 1.0
    return vec


@pytest.fixture
def fake_embeddings(monkeypatch):
    """A live semantic track with no ollama behind it. Yields the call log —
    (text, timeout) per embed — so a test can assert what got embedded and on
    whose budget."""
    calls = []

    import engine

    def embed(text, timeout=engine.EMBED_TIMEOUT):
        calls.append((text, timeout))
        return fake_vector(text)

    monkeypatch.setattr(engine, "embed_text", embed)
    return calls
