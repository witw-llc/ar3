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

from mqtt_cluster import mqtt_broker  # noqa: E402 — re-export for pytest


@pytest.fixture(autouse=True)
def _allow_plaintext_http_in_tests(monkeypatch):
    """Fixtures are served from a local `http.server`, and production refuses
    plaintext attachment URLs. Opt the suite in via the env var rather than the
    settings file so tests without `fake_home` are covered too; a test that
    writes the knob to its own settings file still wins, since stored values
    are read before the environment."""
    monkeypatch.setenv("A8S_STORAGE_ALLOW_HTTP", "1")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect `Path.home()` to `tmp_path` so registry / agent / log files
    land in an isolated location. Resets module-level mutable state in core."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Some platforms also honor USERPROFILE / HOMEPATH. Set HOME and clear
    # the others to be safe.
    monkeypatch.delenv("USERPROFILE", raising=False)
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
