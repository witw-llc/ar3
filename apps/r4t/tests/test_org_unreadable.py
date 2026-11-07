"""An org config that exists but cannot be read refuses dispatch.

Absence and malformed settings keep their behavior (controls); only access and
decoding failures are refused, with the path and a repair hint, before the
verb creates any state."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from org import OrgError, OrgUnreadable, check_org, load_org
from test_access_launch import python_314

R4T = Path(__file__).resolve().parents[1] / "r4t.py"
ROSTER = "### Gerry\n- **Rig:** leader\n- **Leader:** yes\n"
BOOK = "---\nisolation: {}\n---\n## Roster\n\n### Gerry\n- **Rig:** leader\n- **Leader:** yes\n"

not_root = pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="root reads everything"
)


def interpreters() -> list:
    found = [pytest.param("venv", sys.executable, id="venv")]
    for label, python in (("3.12", shutil.which("python3.12")), ("3.14", python_314())):
        found.append(pytest.param(
            label, python,
            id=label,
            marks=() if python else pytest.mark.skip(reason=f"Python {label} is not installed or discoverable"),
        ))
    return found


WITH_INTERPRETER = pytest.mark.parametrize("label,python", interpreters())


def _break_config(root: Path, how: str) -> Path:
    """Make the org config unreadable `how`; return the path the error names."""
    cfg = root / "r4t-org.json"
    if how == "file":
        cfg.write_text('{"comms": "open"}', encoding="utf-8")
        cfg.chmod(0)
    elif how == "parent":
        cfg.write_text('{"comms": "open"}', encoding="utf-8")
        root.chmod(0)
    elif how == "directory":
        cfg.mkdir()
    elif how == "decoding":
        cfg.write_bytes(b'{"comms": "\xff\xfe"}')
    elif how == "runbook-file":
        cfg = root / "r4t.md"
        cfg.write_text(BOOK, encoding="utf-8")
        cfg.chmod(0)
    elif how == "runbook-decoding":
        cfg = root / "r4t.md"
        cfg.write_bytes(b"---\nisolation: {}\n---\n\xff\xfe\n")
    return cfg


def _named(root: Path, how: str, path: Path) -> Path:
    """What the error must contain: with the folder closed it is the folder."""
    return root if how == "parent" else path


def _restore(root: Path) -> None:
    root.chmod(0o700)
    for child in root.iterdir():
        if child.is_file():
            child.chmod(0o600)


HOWS = [
    pytest.param("file", marks=not_root),
    pytest.param("parent", marks=not_root),
    "directory",
    "decoding",
    pytest.param("runbook-file", marks=not_root),
    "runbook-decoding",
]


@pytest.fixture
def org_root(tmp_path):
    root = tmp_path / "org"
    root.mkdir()
    (root / "ROSTER.md").write_text(ROSTER, encoding="utf-8")
    yield root
    if root.exists():
        _restore(root)


@pytest.mark.parametrize("how", HOWS)
class TestLoaderFlagsUnreadable:
    def test_load_org_flags_it_and_names_the_path(self, org_root, how):
        if how.startswith("runbook"):
            (org_root / "ROSTER.md").unlink()
        path = _break_config(org_root, how)
        loaded = load_org(org_root)
        assert loaded.unreadable and str(_named(org_root, how, path)) in loaded.unreadable
        assert "fix" in loaded.unreadable or "permissions" in loaded.unreadable
        assert loaded.errors and loaded.errors[0] == loaded.unreadable

    def test_check_org_still_reports_it(self, org_root, how):
        if how.startswith("runbook"):
            (org_root / "ROSTER.md").unlink()
        path = _break_config(org_root, how)
        problems = check_org(org_root)
        assert problems and str(_named(org_root, how, path)) in problems[0]


def test_unreadable_is_a_typed_org_error():
    assert issubclass(OrgUnreadable, OrgError)


class TestControlsKeepTheirBehavior:
    def test_absent_config_is_defaults_without_error(self, org_root):
        loaded = load_org(org_root)
        assert loaded.unreadable is None and loaded.errors == []

    def test_malformed_json_warns_and_defaults(self, org_root):
        (org_root / "r4t-org.json").write_text("{bad", encoding="utf-8")
        loaded = load_org(org_root)
        assert loaded.unreadable is None
        assert any("cannot read org config" in e for e in loaded.errors)

    def test_non_object_json_warns_and_defaults(self, org_root):
        (org_root / "r4t-org.json").write_text("[1]", encoding="utf-8")
        loaded = load_org(org_root)
        assert loaded.unreadable is None and loaded.errors

    def test_bad_setting_warns_and_defaults(self, org_root):
        (org_root / "r4t-org.json").write_text('{"comms": "sideways"}', encoding="utf-8")
        loaded = load_org(org_root)
        assert loaded.unreadable is None and loaded.errors


def _env(tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("R4T_", "A8S_", "TELL_"))}
    env.update(
        HOME=str(tmp_path / "home"),
        R4T_HOME=str(tmp_path / "r4t-home"),
        A8S_HOME=str(tmp_path / "a8s-home"),
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    return env


def _run(python: str, tmp_path: Path, root: Path, *argv: str):
    return subprocess.run(
        [python, str(R4T), *argv, "--root", str(root)],
        capture_output=True, text=True, env=_env(tmp_path), timeout=60,
    )


def _verbs() -> list[list[str]]:
    return [
        ["dispatch", "--from", "ext", "--to", "acme", "--message", "hi", "--no-drain"],
        ["dispatch", "--batch", '[{"to": "acme", "from": "ext", "message": "hi"}]'],
        ["idle", "--node", "acme"],
        ["clear", "--node", "acme"],
        ["flush", "--node", "acme", "--all"],
        ["resume", "--node", "acme", "Gerry"],
        ["tell", "--node", "acme", "--as", "Gerry", "--simulate-tell", "hi"],
    ]


@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize(
    "verb", _verbs(), ids=lambda v: v[0] + ("-batch" if "--batch" in v else "")
)
@WITH_INTERPRETER
def test_every_dispatching_verb_refuses_before_touching_state(
    label, python, verb, how, tmp_path, org_root
):
    if how.startswith("runbook"):
        (org_root / "ROSTER.md").unlink()
    path = _break_config(org_root, how)
    try:
        done = _run(python, tmp_path, org_root, *verb)
    finally:
        _restore(org_root)
    assert done.returncode == 2, (done.stdout, done.stderr)
    named = _named(org_root, how, path)
    assert "refused" in done.stderr and str(named) in done.stderr
    assert "permissions" in done.stderr or "cannot read" in done.stderr
    assert not (tmp_path / "r4t-home").exists(), "a refused verb must create no state"
    assert not (tmp_path / "a8s-home").exists() or not any((tmp_path / "a8s-home").iterdir())


@WITH_INTERPRETER
def test_stamped_root_is_refused_too(label, python, tmp_path, org_root):
    """No --root: dispatch resolves the root the node was stamped with."""
    import state

    env = _env(tmp_path)
    stamp = subprocess.run(
        [python, "-c",
         f"import sys; sys.path[:0]=[{str(R4T.parent)!r}, {str(R4T.parents[2] / 'lib')!r}];"
         f"import state, pathlib; state.stamp_root('acme', pathlib.Path({str(org_root)!r}))"],
        env=env, capture_output=True, text=True,
    )
    assert stamp.returncode == 0, stamp.stderr
    before = sorted(p.relative_to(tmp_path).as_posix() for p in (tmp_path / "r4t-home").rglob("*"))
    (org_root / "r4t-org.json").mkdir()
    done = subprocess.run(
        [python, str(R4T), "dispatch", "--from", "ext", "--to", "acme", "--message", "hi"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60,
    )
    assert done.returncode == 2 and "refused" in done.stderr
    assert str(org_root / "r4t-org.json") in done.stderr
    after = sorted(p.relative_to(tmp_path).as_posix() for p in (tmp_path / "r4t-home").rglob("*"))
    assert after == before


@WITH_INTERPRETER
class TestCliControls:
    def test_malformed_json_warns_and_proceeds(self, label, python, tmp_path, org_root):
        (org_root / "r4t-org.json").write_text("{bad", encoding="utf-8")
        done = _run(python, tmp_path, org_root, "idle", "--node", "acme")
        assert done.returncode == 0, done.stderr
        assert "warning: cannot read org config" in done.stderr
        assert "refused" not in done.stderr

    def test_absent_config_proceeds_silently(self, label, python, tmp_path, org_root):
        done = _run(python, tmp_path, org_root, "idle", "--node", "acme")
        assert done.returncode == 0, done.stderr
        assert done.stderr == ""

    def test_status_still_reports_instead_of_refusing(self, label, python, tmp_path, org_root):
        (org_root / "r4t-org.json").mkdir()
        done = _run(python, tmp_path, org_root, "status", "--node", "acme")
        assert "refused" not in done.stderr
        assert "warning: cannot read org config" in done.stderr

    def test_roster_check_reports_the_error(self, label, python, tmp_path, org_root):
        (org_root / "r4t-org.json").mkdir()
        done = _run(python, tmp_path, org_root, "roster", "check")
        assert "org: cannot read org config" in done.stdout


@not_root
@WITH_INTERPRETER
def test_unreadable_ancestor_of_stamped_org_never_falls_back_to_cwd(label, python, tmp_path):
    env = _env(tmp_path)
    ancestor = tmp_path / "closed"
    root = ancestor / "org"
    root.mkdir(parents=True)
    (root / "r4t-org.json").write_text('{"isolation": {}}', encoding="utf-8")
    (tmp_path / "ROSTER.md").write_text(ROSTER, encoding="utf-8")
    stamp = subprocess.run(
        [python, "-c",
         f"import sys; sys.path[:0]=[{str(R4T.parent)!r}, {str(R4T.parents[2] / 'lib')!r}];"
         f"import state, pathlib; state.stamp_root('acme', pathlib.Path({str(root)!r}))"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert stamp.returncode == 0, stamp.stderr
    before = {p.relative_to(tmp_path): p.read_bytes() for p in (tmp_path / "r4t-home").rglob("*") if p.is_file()}
    ancestor.chmod(0)
    try:
        done = subprocess.run(
            [python, str(R4T), "idle", "--node", "acme"],
            capture_output=True, text=True, env=env, cwd=tmp_path, timeout=30,
        )
    finally:
        ancestor.chmod(0o700)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "refused" in done.stderr and str(root) in done.stderr
    after = {p.relative_to(tmp_path): p.read_bytes() for p in (tmp_path / "r4t-home").rglob("*") if p.is_file()}
    assert after == before
