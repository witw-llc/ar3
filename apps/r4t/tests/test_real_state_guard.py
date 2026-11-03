"""The suite must never write the developer's real r4t state.

Found 2026-11-02: tests that built a copilot rig and a turn env for node
`acme` / member `worker` without the `r4t_home` fixture wrote that node's meta
and day logs into the real `~/.config/r4t/rosters`, and quota tests rewrote the
real `quota/*.json`. The autouse fixture in conftest closes it; the session
guard in conftest fails the run if anything else opens it again.
"""
from __future__ import annotations

import os
from pathlib import Path

import engines
import state
from ar3 import homeguard
from conftest import _HOME_GUARD


def test_the_state_home_is_not_the_real_one_by_default(tmp_path):
    real = _HOME_GUARD.roots["r4t"].resolve()
    assert state.r4t_home().resolve() != real
    assert real not in state.r4t_home().resolve().parents
    xdg = Path(os.environ["XDG_CONFIG_HOME"]).resolve()
    assert xdg != (Path.home() / ".config").resolve()


def test_writes_through_the_product_api_stay_out_of_the_real_tree():
    """The two writers the leak came through: a node's meta and day log, and
    the quota snapshot."""
    real = _HOME_GUARD.roots["r4t"]
    before = homeguard.snapshot(real)
    state.append_log("acme", "r4t: guard canary")
    state.update_meta("acme", "worker", last_spend={"otel": None})
    engines.save_snapshot("codex", {"engine": "codex", "origin": "live", "buckets": []})
    assert homeguard.changes(before, homeguard.snapshot(real)) == []
    assert (state.r4t_home() / "rosters" / "acme" / "agents" / "worker" / "meta.json").is_file()


def test_the_guard_sees_an_add_a_change_and_a_removal(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "keep").write_text("a", encoding="utf-8")
    (root / "edit").write_text("a", encoding="utf-8")
    (root / "gone").write_text("a", encoding="utf-8")
    before = homeguard.snapshot(root)
    (root / "new").write_text("a", encoding="utf-8")
    (root / "edit").write_text("longer", encoding="utf-8")
    (root / "gone").unlink()
    assert homeguard.changes(before, homeguard.snapshot(root)) == [
        "added    new", "removed  gone", "changed  edit",
    ]


def test_a_session_that_moved_a_real_tree_fails_in_strict_modes(tmp_path, monkeypatch, capsys):
    root = tmp_path / "real"
    root.mkdir()
    monkeypatch.setattr(homeguard, "real_home", lambda app: root)
    for name in ("CI", "AR3_TESTS_STRICT_HOME", "AR3_TESTS_ALLOW_REAL_HOME"):
        monkeypatch.delenv(name, raising=False)
    guard = homeguard.HomeGuard(("r4t",))
    (root / "rosters").mkdir()
    (root / "rosters" / "acme.json").write_text("{}", encoding="utf-8")

    class Session:
        exitstatus = 0

    quiet = Session()
    guard.finish(quiet)
    assert quiet.exitstatus == 0 and "warning" in capsys.readouterr().err
    monkeypatch.setenv("AR3_TESTS_STRICT_HOME", "1")
    strict = Session()
    guard.finish(strict)
    assert strict.exitstatus == 1
    assert "REAL STATE TOUCHED" in capsys.readouterr().err
    monkeypatch.setenv("AR3_TESTS_ALLOW_REAL_HOME", "1")
    waived = Session()
    guard.finish(waived)
    assert waived.exitstatus == 0


def test_the_suite_guard_is_armed():
    assert set(_HOME_GUARD.roots) == {"r4t", "k7e"}


# --- an inherited override must not survive fixture setup ---------------------------


def test_inner_every_home_variable_is_a_throwaway():
    """Runs in the normal suite as a plain check, and is the payload the
    launched-with-overrides test below runs under sentinel homes. Each write
    goes through the variable a leaked override would redirect."""
    import subprocess
    import sys

    import knowledge

    assert homeguard.still_launched() == [], "an inherited home survived the fixture"
    for name in homeguard.HOME_VARS:
        assert os.environ.get(name) != homeguard.LAUNCHED[name] or not homeguard.LAUNCHED[name]
    state.append_log("canary", "r4t: canary")
    done = subprocess.run(
        [sys.executable, str(knowledge.K7E_ENTRY), "store", "canary", "--content", "x"],
        env=dict(os.environ), capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr


def test_a_launch_with_inherited_overrides_writes_nothing_there(tmp_path):
    """The launcher's environment names live stores (K7E_HOME and its
    siblings). The suite is started with every home variable set to a sentinel
    dir; the payload test must pass and no file may appear in any sentinel."""
    import sys

    here = Path(__file__).resolve()
    code, output, written = homeguard.sentinel_run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"{here.name}::test_inner_every_home_variable_is_a_throwaway"],
        cwd=here.parent, root=tmp_path / "sentinels",
    )
    assert code == 0, output
    assert written == [], written
