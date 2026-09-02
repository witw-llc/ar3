"""pytest scaffolding for ar3."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))
# `ar3ver` and the `ar3` foundation package live under `lib/`, shared by every
# CLI. Put that on the path here rather than relying on the app's own import
# having run first.
sys.path.append(str(_PKG.parent.parent / "lib"))

# The app's entry module is `cli`, not `ar3`: a top-level package named `ar3`
# cannot share a sys.path entry with a module of the same name, and the
# package is the one that has to win.
import cli  # noqa: E402


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
