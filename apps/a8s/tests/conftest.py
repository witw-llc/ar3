"""pytest scaffolding for a8s.

- Adds `apps/a8s/` to `sys.path` so tests can `import core, registry, ...`
  the same way `a8s.py` does at runtime.
- Provides a `fake_home` fixture that redirects `Path.home()` to a per-test
  tmp dir so tests never touch the real `~/.a8s/`.
- Provides an `agents_root` fixture pointing at the existing fixture dirs
  under `apps/a8s/tests/agents/`.
"""
from __future__ import annotations

import os
import stat
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


@pytest.fixture
def zone(monkeypatch):
    """Force the zone every rendered stamp reads in, for the whole suite.

    Not via `TZ`. `time.tzset()` does not exist on Windows and the C library
    there never consults `TZ`, so a fixture built that way raises
    `AttributeError` before a single assertion runs — it took out sixty tests
    on the Windows seat, the largest cause left in this suite.

    `ark.clock`'s two conversion points are redirected instead, which is what
    these tests are actually after: that a heading or a log line renders in a
    known zone. That the zone *comes from the machine* is `ark.clock`'s own
    contract and is tested there, where `TZ` is the thing under test rather
    than the way to set one up.

    `to_local` is wrapped rather than replaced, so the stored-stamp parsing
    the caller depends on stays the real one and only the final conversion
    moves. Zone names are IANA keys resolved through `zoneinfo`, so `%Z`
    comes from the tz database and reads `PDT` on every platform instead of
    Windows' `Pacific Daylight Time`.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from ark import clock

    real_to_local = clock.to_local

    def use(name: str) -> None:
        tz = ZoneInfo(name)
        monkeypatch.setattr(
            clock, "local_now", lambda: datetime.now(timezone.utc).astimezone(tz)
        )
        monkeypatch.setattr(
            clock,
            "to_local",
            lambda ts: (lambda dt: None if dt is None else dt.astimezone(tz))(
                real_to_local(ts)
            ),
        )

    return use


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


@pytest.fixture(autouse=True, scope="session")
def _a8s_home_floor(tmp_path_factory):
    """Session-wide A8S_HOME floor under pytest's tmp root.

    A test that redirects nothing must still be unable to resolve the
    developer's live state. `fake_home` and `set_home` delete the variable
    per-test, so properly isolated tests are unaffected — the floor exists
    for the future bypass, turning a live-state write (a clobbered registry,
    fixture envelopes published to real remotes — both field-observed) into
    a write to a throwaway directory."""
    os.environ["A8S_HOME"] = str(tmp_path_factory.mktemp("a8s-home-floor"))


def set_home(monkeypatch, home) -> None:
    """Point every home-resolution path at `home`, on every platform.

    `ntpath.expanduser` never consults HOME — it reads USERPROFILE, then
    HOMEDRIVE+HOMEPATH — so a site that sets HOME alone is isolated on POSIX
    and un-isolated on Windows: resolution falls through to the real home,
    and the suite writes the developer's live ~/.a8s (field-verified). Every
    test that redirects home goes through here or `fake_home`."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.delenv("A8S_HOME", raising=False)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect `Path.home()` to `tmp_path` so registry / agent / log files
    land in an isolated location. Resets module-level mutable state in core."""
    set_home(monkeypatch, tmp_path)
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


@pytest.fixture
def agents_root() -> Path:
    """Existing per-tool agent fixture directories under apps/a8s/tests/agents/."""
    return _PKG_DIR / "tests" / "agents"


@pytest.fixture
def fixtures_dir() -> Path:
    """Pytest-only fixtures (mock_cli.py, definition JSONs) under apps/a8s/tests/fixtures/."""
    return Path(__file__).resolve().parent / "fixtures"
