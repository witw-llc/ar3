"""Every ar3 CLI answers `--version` with the suite semver.

This is the one promise that has to hold across all six entry points, and the
only way to check it is to run them. The conftest forbids spawning harnesses,
so the real `subprocess.run` is captured at import time and handed back per
its documented escape hatch.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

import cli as ar3
import ar3ver

_REAL_RUN = subprocess.run
_ROOT = Path(__file__).resolve().parents[3]

_A8S = _ROOT / "apps" / "a8s" / "a8s.py"

# Name to the argv its root shim runs. `tell` and `tells` are shims to
# `a8s tell` / `a8s tells`, not scripts of their own.
ENTRY_POINTS = {
    "ar3": [str(_ROOT / "apps" / "ar3" / "cli.py")],
    "a8s": [str(_A8S)],
    "r4t": [str(_ROOT / "apps" / "r4t" / "r4t.py")],
    "k7e": [str(_ROOT / "apps" / "k7e" / "k7e.py")],
    "tell": [str(_A8S), "tell"],
    "tells": [str(_A8S), "tells"],
}


@pytest.fixture
def real_subprocess(monkeypatch):
    monkeypatch.setattr(ar3.subprocess, "run", _REAL_RUN)


@pytest.mark.parametrize("name,argv", sorted(ENTRY_POINTS.items()))
def test_cli_reports_the_suite_version(name, argv, real_subprocess, tmp_path):
    proc = _REAL_RUN(
        [sys.executable, *argv, "--version"],
        capture_output=True, text=True, timeout=60,
        # A version query must not touch or create real state.
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "A8S_HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert (
        proc.stdout.strip()
        == f"{name} {ar3ver.suite_version()} (ar3, python {platform.python_version()})"
    )


def test_every_top_level_shim_has_an_entry_point_covered():
    """A new CLI added to the repo root must gain a `--version` test with it."""
    shims = {
        p.name for p in _ROOT.iterdir()
        if p.is_file() and p.suffix == "" and p.stat().st_mode & 0o111
        and p.name not in {"install.sh"}
    }
    assert shims == set(ENTRY_POINTS), f"uncovered shims: {shims ^ set(ENTRY_POINTS)}"


def test_a_relocated_entry_point_still_runs(real_subprocess, tmp_path):
    """The isolation container copies `apps/r4t` alone to /opt/r4t, so the repo
    root is not two levels up and `ar3ver` is not importable. A CLI must not
    die on import because the version file moved."""
    import shutil

    staged = tmp_path / "opt" / "r4t"
    shutil.copytree(_ROOT / "apps" / "r4t", staged,
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".venv"))
    proc = _REAL_RUN(
        [sys.executable, str(staged / "r4t.py"), "--version"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "R4T_HOME": str(tmp_path / "r4t")},
    )
    assert proc.returncode == 0, proc.stderr
    assert (
        proc.stdout.strip()
        == f"r4t unknown (ar3, python {platform.python_version()})"
    )


class TestTheBundledLibOutranksAnInstalledPackage:
    """`<repo>/lib` carries `ar3ver` and the `ar3` foundation package. Any
    unrelated distribution named `ar3` that a user happens to have installed
    resolves ahead of an *appended* path, so every CLI would import a
    stranger's code — and all six died on import when that was measured
    against a venv holding a canary `ar3`.

    The shadow goes on `PYTHONPATH` rather than into a venv: PYTHONPATH sits
    ahead of site-packages, so a bundled `lib` that beats it beats an
    installed package too, and no interpreter has to be built to prove it.
    """

    @staticmethod
    def _shadow(tmp_path: Path) -> Path:
        """A directory holding an `ar3` package and an `ar3ver` module that
        both explode on import. Silence would prove nothing: only a raised
        canary distinguishes "the bundled copy won" from "neither loaded"."""
        shadow = tmp_path / "shadow"
        (shadow / "ar3").mkdir(parents=True)
        boom = 'raise RuntimeError("shadow package won resolution")\n'
        (shadow / "ar3" / "__init__.py").write_text(boom, encoding="utf-8")
        (shadow / "ar3ver.py").write_text(boom, encoding="utf-8")
        return shadow

    @pytest.mark.parametrize("name,argv", sorted(ENTRY_POINTS.items()))
    def test_the_cli_still_reports_the_suite_version(
        self, name, argv, real_subprocess, tmp_path
    ):
        proc = _REAL_RUN(
            [sys.executable, *argv, "--version"],
            capture_output=True, text=True, timeout=60,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "A8S_HOME": str(tmp_path),
                "PYTHONPATH": str(self._shadow(tmp_path)),
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert (
            proc.stdout.strip()
            == f"{name} {ar3ver.suite_version()} (ar3, python {platform.python_version()})"
        )

    @pytest.mark.parametrize("name,argv", sorted(ENTRY_POINTS.items()))
    def test_the_cli_still_wins_when_the_shadow_precedes_the_bundled_lib(
        self, name, argv, real_subprocess, tmp_path
    ):
        """A conditional `if _AR3_LIB not in sys.path: insert(0, ...)` only
        protects an *absent* path. When something upstream — here, an earlier
        PYTHONPATH entry — has already put the real `lib` on sys.path, the
        guard sees it present and skips the insert, so the shadow ahead of it
        keeps winning. The fix must remove every existing occurrence of the
        bundled path before inserting at 0, not merely insert when absent."""
        proc = _REAL_RUN(
            [sys.executable, *argv, "--version"],
            capture_output=True, text=True, timeout=60,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "A8S_HOME": str(tmp_path),
                "PYTHONPATH": f"{self._shadow(tmp_path)}{os.pathsep}{_ROOT / 'lib'}",
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert (
            proc.stdout.strip()
            == f"{name} {ar3ver.suite_version()} (ar3, python {platform.python_version()})"
        )

    def test_the_shadow_would_be_loud_if_it_won(self, real_subprocess, tmp_path):
        """The positive control for the control: with nothing to outrank it,
        the shadow does take over. Without this, a shadow that quietly failed
        to build would make every case above pass for the wrong reason."""
        proc = _REAL_RUN(
            [sys.executable, "-c", "import ar3ver"],
            capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                 "PYTHONPATH": str(self._shadow(tmp_path))},
        )
        assert proc.returncode != 0
        assert "shadow package won resolution" in proc.stderr

    def test_this_suite_runs_against_the_bundled_lib(self):
        """The same rule for the conftests: they appended `lib` too, so the
        suites themselves would have exercised an installed `ar3`."""
        lib = str(_ROOT / "lib")
        assert lib in sys.path
        installed = [
            i for i, entry in enumerate(sys.path)
            if "site-packages" in entry or "dist-packages" in entry
        ]
        assert all(sys.path.index(lib) < i for i in installed)
