"""The 30-day lab never creates or removes a directory inside the repo."""
import importlib.util
import os
from pathlib import Path

import pytest

CHASSIS = Path(__file__).resolve().parent / "eval" / "chassis" / "sim.py"


@pytest.fixture
def sim():
    spec = importlib.util.spec_from_file_location("chassis_sim", CHASSIS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_runs_root_inside_the_repo_is_refused(sim):
    with pytest.raises(ValueError, match="inside the repo"):
        sim.run_dir("base", root=sim.WT / "apps" / "k7e" / "tests" / "scratch")


def test_a_name_that_leaves_the_runs_root_is_refused(sim, tmp_path):
    for name in ("../victim", "/tmp/victim", "..", "a/b", ""):
        with pytest.raises(ValueError):
            sim.run_dir(name, root=tmp_path)


def test_a_symlinked_run_pointing_into_the_repo_is_refused(sim, tmp_path):
    (tmp_path / "runs").mkdir()
    victim = sim.WT / "apps" / "k7e" / "tests" / "eval"
    os.symlink(victim, tmp_path / "runs" / "base")
    with pytest.raises(ValueError, match="outside"):
        sim.run_dir("base", root=tmp_path)


def test_a_plain_run_under_an_outside_root_is_its_own_child(sim, tmp_path):
    dest = sim.run_dir("base", root=tmp_path)
    assert dest == (tmp_path / "runs" / "base").resolve()
    assert not dest.exists()
