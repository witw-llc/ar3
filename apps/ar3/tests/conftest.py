"""pytest scaffolding for ar3."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))
# `ar3ver` and the `ar3` foundation package live under `lib/`, shared by every
# CLI. Put that on the path here rather than relying on the app's own import
# having run first, and at the front rather than the end: appended, it loses
# to the site-packages of whatever interpreter runs the suite, so an unrelated
# `ar3` distribution would be what the tests exercise.
sys.path.insert(0, str(_PKG.parent.parent / "lib"))

# The app's entry module is `cli`, not `ar3`: a top-level package named `ar3`
# cannot share a sys.path entry with a module of the same name, and the
# package is the one that has to win.
import autoupdate_trigger  # noqa: E402
import cli  # noqa: E402
from ar3 import crontab  # noqa: E402
from _memory_trigger import STORE, MemoryTrigger  # noqa: E402


from ar3 import homeguard  # noqa: E402
from ar3.homeguard import HomeGuard  # noqa: E402

# Recorded at import, before any fixture redirects a home: the real trees.
_HOME_GUARD = HomeGuard(("r4t", "k7e"))


def pytest_sessionfinish(session, exitstatus):
    _HOME_GUARD.finish(session)


@pytest.fixture(autouse=True)
def _isolated_suite_homes(tmp_path_factory, monkeypatch):
    """No test may reach the developer's real a8s, r4t or k7e state, whether or
    not it asked for `homes`: every home variable starts each test at a
    throwaway dir, so an override inherited from the launcher cannot survive.
    The `homes` fixture and tests that set their own still override these."""
    root = tmp_path_factory.mktemp("suite-homes")
    for name, value in homeguard.throwaway_homes(root).items():
        monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """No test may execute a real harness CLI. Probes that want to exercise
    subprocess behaviour re-patch `cli.subprocess.run` themselves."""
    def forbidden(*args, **kwargs):
        raise AssertionError(f"tests must not spawn real processes: {args!r}")

    monkeypatch.setattr(cli.subprocess, "run", forbidden)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Hermetic suite homes. Every probe path in the greeter resolves under
    tmp_path, so no test can read or touch a real ~/.config/a8s, ~/.config/r4t
    or ~/.config/k7e."""
    roots = {}
    for env, name in (("A8S_HOME", "a8s"), ("R4T_HOME", "r4t"), ("K7E_HOME", "k7e")):
        root = tmp_path / name
        root.mkdir()
        monkeypatch.setenv(env, str(root))
        roots[name] = root
    monkeypatch.setattr(cli.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path / "absent-bin")
    return roots


@pytest.fixture(autouse=True)
def _hermetic_auto_update(monkeypatch, tmp_path_factory):
    """No test may read or write the real ar3 update state, the account's lock
    directory, or the host's scheduler. Tests of the scheduler entries build
    their own trigger over fakes."""
    monkeypatch.setenv("AR3_HOME", str(tmp_path_factory.mktemp("ar3-home")))
    monkeypatch.delenv("AR3_AUTO_UPDATE", raising=False)
    STORE.clear()
    MemoryTrigger.fail_install = MemoryTrigger.unavailable = False
    lock_root = tmp_path_factory.mktemp("account-locks")
    monkeypatch.setattr(crontab, "account_lock_root", lambda: lock_root)
    monkeypatch.setattr(autoupdate_trigger, "select", lambda scratch: MemoryTrigger())
    yield
    STORE.clear()
