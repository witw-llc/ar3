"""Shared fixtures for K7E tests."""

import hashlib
import os
import re
import stat
import sys
import time
from pathlib import Path

import pytest

K7E_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(K7E_DIR))
# `ar3` sits in `<repo>/lib`, shared by every app. Put it on the path here
# rather than relying on some k7e module having run first.
sys.path.append(str(K7E_DIR.parent.parent / "lib"))

import config

# Shared CI runners vary enough that absolute thresholds flake; loosen only
# when the workflow sets K7E_PERF_FACTOR (default 1 keeps local runs strict).
PERF_FACTOR = float(os.environ.get("K7E_PERF_FACTOR", "1"))


def write_path_executable(directory: Path, name: str, source: str) -> Path:
    """Write a Python stub the OS can execute, and return the path to exec.

    A `#!` file plus the exec bit is a POSIX-only idea. Windows raises
    `OSError [WinError 193]` on one, whether it is found on PATH or named
    outright, so a stub written that way is not a stand-in for a binary there
    at all. On Windows the logic lands as `<name>.py` beside a `<name>.cmd`
    launcher — which is both what PATHEXT resolves and what an explicit path
    can point at — and everywhere else as the stub itself.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (directory / f"{name}.py").write_text(source, encoding="utf-8")
        launcher = directory / f"{name}.cmd"
        launcher.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "%~dp0{name}.py" %*\r\n'
            "exit /b %ERRORLEVEL%\r\n",
            encoding="utf-8",
        )
        return launcher
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


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
    # The semantic track is turned OFF by configuration, not starved of a
    # server. Pointing `OLLAMA_URL` at an unusable port assumed that a refused
    # connection is free — true on POSIX, false on Windows, where the seat
    # measured 4.13s per refusal against an instant one here. A class doing
    # dozens of searches then spends minutes failing to reach a server it was
    # never supposed to consult, and its result depends on the failure path.
    # `fake_embeddings` turns the track back on for the tests that want it.
    # The unusable URL stays as the second belt: nothing here reaches ollama.
    monkeypatch.setenv("K7E_EMBEDDINGS", "off")
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
def dead_embeddings(store, monkeypatch):
    """The track switched on with an ollama that never answers — without a
    socket. `embed_text` returns None on every failure it catches, so
    returning None IS an absent server as far as any caller can tell, and the
    degradation path is exercised at no cost on any platform.

    `embed_text`'s own behaviour against a genuinely unreachable URL is proved
    once, in test_embeddings.py, rather than at every caller."""
    calls = []

    import engine

    def embed(text, timeout=engine.EMBED_TIMEOUT):
        calls.append((text, timeout))
        return None

    monkeypatch.setenv("K7E_EMBEDDINGS", "ollama")
    monkeypatch.setattr(engine, "embed_text", embed)
    return calls


@pytest.fixture
def absent_ollama(store, monkeypatch):
    """The same degradation path for the tests that run k7e as a subprocess,
    where a monkeypatch cannot reach. Those pay one real refused connection —
    instant on POSIX, about four seconds on Windows — which is why in-process
    callers take `dead_embeddings` instead."""
    monkeypatch.setenv("K7E_EMBEDDINGS", "ollama")
    return store


@pytest.fixture
def fake_embeddings(store, monkeypatch):
    """A live semantic track with no ollama behind it. Yields the call log —
    (text, timeout) per embed — so a test can assert what got embedded and on
    whose budget.

    Takes `store` rather than sitting beside it, so the track is switched back
    on after the store has switched it off no matter which order a test lists
    them in."""
    calls = []

    import engine

    monkeypatch.setenv("K7E_EMBEDDINGS", "ollama")

    def embed(text, timeout=engine.EMBED_TIMEOUT):
        calls.append((text, timeout))
        return fake_vector(text)

    monkeypatch.setattr(engine, "embed_text", embed)
    return calls
