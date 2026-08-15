"""pytest scaffolding for a8s.

- Adds `apps/a8s/` to `sys.path` so tests can `import core, registry, ...`
  the same way `a8s.py` does at runtime.
- Provides a `fake_home` fixture that redirects `Path.home()` to a per-test
  tmp dir so tests never touch the real `~/.a8s/`.
- Provides an `agents_root` fixture pointing at the existing fixture dirs
  under `apps/a8s/tests/agents/`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# `apps/a8s/tests/conftest.py` -> `apps/a8s/`
_PKG_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG_DIR))
# `ark` sits at the repo root, shared by every app. Put it on the path here
# rather than relying on some a8s module having run first.
sys.path.append(str(_PKG_DIR.parent.parent))

from mqtt_cluster import mqtt_broker  # noqa: E402 — re-export for pytest


@pytest.fixture(autouse=True)
def _no_ambient_xdg(monkeypatch):
    """a8s honors XDG_CONFIG_HOME (ark.home), and CI runners export it. Every
    test here fabricates state under a fake HOME, so an ambient XDG base would
    silently point resolution somewhere else; XDG-order behavior itself is
    covered by the foundation's own suite (test_ark_home.py)."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


@pytest.fixture(autouse=True)
def _allow_plaintext_http_in_tests(monkeypatch):
    """Fixtures are served from a local `http.server`, and production refuses
    plaintext attachment URLs. Opt the suite in via the env var rather than the
    settings file so tests without `fake_home` are covered too; a test that
    writes the knob to its own settings file still wins, since stored values
    are read before the environment."""
    monkeypatch.setenv("A8S_STORAGE_ALLOW_HTTP", "1")


@pytest.fixture(autouse=True)
def _settle_deferred_attachment_retries():
    """Deferred attachment delivery runs on a background pool that outlives the
    test that started it. Left to run, a retry from an earlier test announces
    its failure into a later test's captured stdout — a cross-test leak that
    only shows up when the timing lines up, which is to say on someone else's
    machine."""
    yield
    import network

    network.drain_attachment_retries(timeout_s=10)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect `Path.home()` to `tmp_path` so registry / agent / log files
    land in an isolated location. Resets module-level mutable state in core."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # ntpath.expanduser never consults HOME: it reads USERPROFILE, then
    # HOMEDRIVE+HOMEPATH. Deleting USERPROFILE therefore un-isolates Windows —
    # resolution falls through to the real home and the suite clobbers the
    # developer's live ~/.a8s (field-verified). Point USERPROFILE at the same
    # tmp dir and clear the fallback pair.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    # Don't let a globally-set A8S_HOME leak into tests.
    monkeypatch.delenv("A8S_HOME", raising=False)
    # Prefer legacy ~/.a8s when present so existing path assertions stay stable.
    (tmp_path / ".a8s").mkdir(parents=True, exist_ok=True)
    import json

    settings_path = tmp_path / ".a8s" / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "storage_receive_wait_seconds": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    import core
    # Make sure no prior test left a Lock attached.
    core.PRINT_LOCK = None
    yield tmp_path


@pytest.fixture
def agents_root() -> Path:
    """Existing per-tool agent fixture directories under apps/a8s/tests/agents/."""
    return _PKG_DIR / "tests" / "agents"


@pytest.fixture
def fixtures_dir() -> Path:
    """Pytest-only fixtures (mock-cli, definition JSONs) under apps/a8s/tests/fixtures/."""
    return Path(__file__).resolve().parent / "fixtures"
