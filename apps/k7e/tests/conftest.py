"""Shared fixtures for K7E tests."""

import sys
from pathlib import Path

import pytest

K7E_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(K7E_DIR))

import config


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
