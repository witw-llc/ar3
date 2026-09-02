"""Tests for the on-demand heavy-dependency mechanism — ar3's tier-2
dependency foundation.

`ar3.deps` fetches boto3/textual-class packages into per-interpreter
directories under `$XDG_DATA_HOME/ar3/deps`. Every test here gets a private
`XDG_DATA_HOME` so nothing touches the real `~/.local/share`, and no test
hits the network — `install_group` is exercised through a stubbed
`subprocess.run` that fakes what `pip --target`/`uv pip install --target`
would leave on disk.
"""
from __future__ import annotations

import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli as ar3
from ar3 import deps as ar3_deps


@pytest.fixture(autouse=True)
def _data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    return tmp_path


def _write_dist(dir_: Path, name: str, version: str) -> None:
    info = dir_ / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )


class TestInterpreterKey:
    def test_folds_in_cache_tag_and_platform(self):
        assert ar3_deps.interpreter_key() == (
            f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}"
        )

    def test_is_a_single_path_safe_segment(self):
        key = ar3_deps.interpreter_key()
        assert "/" not in key
        assert key == key.strip()


class TestDepsRoot:
    def test_honors_xdg_data_home(self, tmp_path):
        assert ar3_deps.deps_root() == tmp_path / "xdg-data" / "ar3" / "deps"

    def test_defaults_to_home_dot_local_share_when_xdg_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        assert ar3_deps.deps_root() == tmp_path / "home" / ".local" / "share" / "ar3" / "deps"

    def test_blank_xdg_data_home_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", "   ")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        assert ar3_deps.deps_root() == tmp_path / "home" / ".local" / "share" / "ar3" / "deps"


class TestGroupDir:
    def test_layout_is_root_then_interpreter_then_group(self):
        assert ar3_deps.group_dir("r4t") == (
            ar3_deps.deps_root() / ar3_deps.interpreter_key() / "r4t"
        )


class TestKnownGroups:
    def test_lists_the_repos_real_groups_and_excludes_test_pins(self):
        groups = ar3_deps.known_groups()
        assert "r4t" in groups
        assert "a8s-s3" in groups
        assert not any(g.endswith("-test") for g in groups)


class TestEnsureGroup:
    def test_missing_dir_returns_none(self):
        assert ar3_deps.ensure_group("no-such-group-at-all") is None

    def test_satisfied_dir_returns_its_path(self, monkeypatch, tmp_path):
        req_dir = tmp_path / "requirements"
        req_dir.mkdir()
        (req_dir / "fakegroup.txt").write_text("fakepkg>=1.0\n", encoding="utf-8")
        monkeypatch.setattr(ar3_deps, "REQUIREMENTS_DIR", req_dir)
        dir_ = ar3_deps.group_dir("fakegroup")
        dir_.mkdir(parents=True)
        _write_dist(dir_, "fakepkg", "1.2.3")
        assert ar3_deps.ensure_group("fakegroup") == dir_

    def test_unsatisfied_pin_returns_none(self, monkeypatch, tmp_path):
        req_dir = tmp_path / "requirements"
        req_dir.mkdir()
        (req_dir / "fakegroup.txt").write_text("fakepkg>=9.0\n", encoding="utf-8")
        monkeypatch.setattr(ar3_deps, "REQUIREMENTS_DIR", req_dir)
        dir_ = ar3_deps.group_dir("fakegroup")
        dir_.mkdir(parents=True)
        _write_dist(dir_, "fakepkg", "1.2.3")
        assert ar3_deps.ensure_group("fakegroup") is None

    def test_missing_package_returns_none(self, monkeypatch, tmp_path):
        req_dir = tmp_path / "requirements"
        req_dir.mkdir()
        (req_dir / "fakegroup.txt").write_text("fakepkg\n", encoding="utf-8")
        monkeypatch.setattr(ar3_deps, "REQUIREMENTS_DIR", req_dir)
        dir_ = ar3_deps.group_dir("fakegroup")
        dir_.mkdir(parents=True)
        assert ar3_deps.ensure_group("fakegroup") is None

    def test_no_pin_file_trusts_an_existing_dir(self, tmp_path):
        # No requirements/no-pins-group.txt exists anywhere in this repo.
        dir_ = ar3_deps.group_dir("no-pins-group")
        dir_.mkdir(parents=True)
        assert ar3_deps.ensure_group("no-pins-group") == dir_


class TestInstallGroup:
    def _fake_group(self, monkeypatch, tmp_path, pin="fakepkg\n"):
        req_dir = tmp_path / "requirements"
        req_dir.mkdir()
        (req_dir / "fakegroup.txt").write_text(pin, encoding="utf-8")
        monkeypatch.setattr(ar3_deps, "REQUIREMENTS_DIR", req_dir)

    def test_missing_requirements_file_raises(self, monkeypatch, tmp_path):
        req_dir = tmp_path / "requirements"
        req_dir.mkdir()
        monkeypatch.setattr(ar3_deps, "REQUIREMENTS_DIR", req_dir)
        with pytest.raises(FileNotFoundError):
            ar3_deps.install_group("no-such-group")

    def test_prefers_uv_when_on_path(self, monkeypatch, tmp_path):
        self._fake_group(monkeypatch, tmp_path)
        monkeypatch.setattr(
            ar3_deps.shutil, "which",
            lambda name: "/opt/homebrew/bin/uv" if name == "uv" else None,
        )
        seen = {}

        def fake_run(argv):
            seen["argv"] = argv
            Path(argv[argv.index("--target") + 1]).mkdir(parents=True)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(ar3_deps.subprocess, "run", fake_run)
        ar3_deps.install_group("fakegroup")
        assert seen["argv"][:3] == ["/opt/homebrew/bin/uv", "pip", "install"]

    def test_falls_back_to_pip_module_when_uv_absent(self, monkeypatch, tmp_path):
        self._fake_group(monkeypatch, tmp_path)
        monkeypatch.setattr(ar3_deps.shutil, "which", lambda _name: None)
        seen = {}

        def fake_run(argv):
            seen["argv"] = argv
            Path(argv[argv.index("--target") + 1]).mkdir(parents=True)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(ar3_deps.subprocess, "run", fake_run)
        ar3_deps.install_group("fakegroup")
        assert seen["argv"][:3] == [sys.executable, "-m", "pip"]

    def test_success_swaps_the_tmp_dir_into_place_and_leaves_no_litter(self, monkeypatch, tmp_path):
        self._fake_group(monkeypatch, tmp_path)
        monkeypatch.setattr(ar3_deps.shutil, "which", lambda _name: None)

        def fake_run(argv):
            target = Path(argv[argv.index("--target") + 1])
            target.mkdir(parents=True)
            (target / "marker.txt").write_text("installed", encoding="utf-8")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(ar3_deps.subprocess, "run", fake_run)
        dest = ar3_deps.install_group("fakegroup")
        assert dest == ar3_deps.group_dir("fakegroup")
        assert (dest / "marker.txt").read_text(encoding="utf-8") == "installed"
        assert [p.name for p in dest.parent.iterdir()] == [dest.name]

    def test_success_replaces_a_previous_install(self, monkeypatch, tmp_path):
        self._fake_group(monkeypatch, tmp_path)
        monkeypatch.setattr(ar3_deps.shutil, "which", lambda _name: None)
        final_dir = ar3_deps.group_dir("fakegroup")
        final_dir.mkdir(parents=True)
        (final_dir / "old.txt").write_text("stale", encoding="utf-8")

        def fake_run(argv):
            target = Path(argv[argv.index("--target") + 1])
            target.mkdir(parents=True)
            (target / "new.txt").write_text("fresh", encoding="utf-8")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(ar3_deps.subprocess, "run", fake_run)
        dest = ar3_deps.install_group("fakegroup")
        assert (dest / "new.txt").is_file()
        assert not (dest / "old.txt").exists()
        assert [p.name for p in dest.parent.iterdir()] == [dest.name]

    def test_failure_keeps_the_previous_install_and_leaves_no_half_populated_dir(
        self, monkeypatch, tmp_path
    ):
        self._fake_group(monkeypatch, tmp_path)
        monkeypatch.setattr(ar3_deps.shutil, "which", lambda _name: None)
        final_dir = ar3_deps.group_dir("fakegroup")
        final_dir.mkdir(parents=True)
        (final_dir / "old.txt").write_text("previous good install", encoding="utf-8")

        def fake_run(argv):
            target = Path(argv[argv.index("--target") + 1])
            target.mkdir(parents=True)
            (target / "partial.txt").write_text("half-installed", encoding="utf-8")
            return SimpleNamespace(returncode=1)

        monkeypatch.setattr(ar3_deps.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError):
            ar3_deps.install_group("fakegroup")

        assert (final_dir / "old.txt").is_file()
        assert not (final_dir / "partial.txt").exists()
        assert [p.name for p in final_dir.parent.iterdir()] == [final_dir.name]

    def test_failure_with_no_previous_install_leaves_the_dir_absent(self, monkeypatch, tmp_path):
        self._fake_group(monkeypatch, tmp_path)
        monkeypatch.setattr(ar3_deps.shutil, "which", lambda _name: None)

        def fake_run(argv):
            target = Path(argv[argv.index("--target") + 1])
            target.mkdir(parents=True)
            return SimpleNamespace(returncode=1)

        monkeypatch.setattr(ar3_deps.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError):
            ar3_deps.install_group("fakegroup")

        final_dir = ar3_deps.group_dir("fakegroup")
        assert not final_dir.exists()
        assert list(final_dir.parent.iterdir()) == []


class TestUseGroup:
    def test_missing_group_warns_and_returns_false(self, capsys):
        assert ar3_deps.use_group("no-such-group-anywhere") is False
        err = capsys.readouterr().err
        assert "ar3 deps no-such-group-anywhere" in err

    def test_present_group_inserts_before_the_first_site_packages_entry(
        self, monkeypatch, tmp_path
    ):
        dir_ = ar3_deps.group_dir("fakegroup")
        dir_.mkdir(parents=True)
        monkeypatch.setattr(ar3_deps, "ensure_group", lambda _g: dir_)
        fake_site_packages = str(tmp_path / "venv" / "lib" / "site-packages")
        original = list(sys.path)
        try:
            sys.path[:] = [original[0], fake_site_packages, *original[1:]]
            before = list(sys.path)
            assert ar3_deps.use_group("fakegroup") is True
            idx = sys.path.index(str(dir_))
            # Ahead of site-packages...
            assert sys.path[idx + 1] == fake_site_packages
            # ...but behind index 0 (stdlib / the vendored dir prepended there).
            assert idx > 0
            assert sys.path[:idx] + sys.path[idx + 1:] == before
        finally:
            sys.path[:] = original

    def test_falls_back_to_append_when_no_site_packages_entry_present(
        self, monkeypatch, tmp_path
    ):
        dir_ = ar3_deps.group_dir("fakegroup")
        dir_.mkdir(parents=True)
        monkeypatch.setattr(ar3_deps, "ensure_group", lambda _g: dir_)
        original = list(sys.path)
        try:
            sys.path[:] = [
                p for p in original
                if "site-packages" not in p and "dist-packages" not in p
            ]
            baseline = list(sys.path)
            assert ar3_deps.use_group("fakegroup") is True
            assert sys.path[-1] == str(dir_)
            assert sys.path[:-1] == baseline
        finally:
            sys.path[:] = original

    def test_fetched_group_shadows_an_older_copy_on_site_packages(self, monkeypatch, tmp_path):
        # Regression: appending used to let an older system/venv copy of a
        # package shadow the fetched one, since site-packages came first in
        # sys.path. The group dir must now win the import.
        site_packages = tmp_path / "fake-site-packages"
        site_packages.mkdir()
        (site_packages / "ar3_deps_probe_mod.py").write_text(
            "SOURCE = 'stale-site-packages'\n", encoding="utf-8"
        )

        dir_ = ar3_deps.group_dir("fakegroup")
        dir_.mkdir(parents=True)
        (dir_ / "ar3_deps_probe_mod.py").write_text(
            "SOURCE = 'fresh-group-dir'\n", encoding="utf-8"
        )
        monkeypatch.setattr(ar3_deps, "ensure_group", lambda _g: dir_)

        original = list(sys.path)
        sys.modules.pop("ar3_deps_probe_mod", None)
        try:
            sys.path[:] = [original[0], str(site_packages), *original[1:]]
            assert ar3_deps.use_group("fakegroup") is True
            idx = sys.path.index(str(dir_))
            assert idx > 0
            assert idx < sys.path.index(str(site_packages))

            import importlib
            importlib.invalidate_caches()
            import ar3_deps_probe_mod

            assert ar3_deps_probe_mod.SOURCE == "fresh-group-dir"
        finally:
            sys.modules.pop("ar3_deps_probe_mod", None)
            sys.path[:] = original

    def test_does_not_duplicate_an_existing_entry(self, monkeypatch, tmp_path):
        dir_ = ar3_deps.group_dir("fakegroup")
        dir_.mkdir(parents=True)
        monkeypatch.setattr(ar3_deps, "ensure_group", lambda _g: dir_)
        original = list(sys.path)
        try:
            ar3_deps.use_group("fakegroup")
            ar3_deps.use_group("fakegroup")
            assert sys.path.count(str(dir_)) == 1
        finally:
            sys.path[:] = original


class TestDepsVerb:
    def test_lists_known_groups_as_not_installed(self, capsys):
        assert ar3.main(["deps"]) == 0
        out = capsys.readouterr().out
        assert "a8s-s3" in out
        assert "r4t" in out
        assert "not installed" in out

    def test_unknown_group_is_a_usage_error(self, capsys):
        assert ar3.main(["deps", "not-a-real-group"]) == 2
        err = capsys.readouterr().err
        assert "no such group" in err
