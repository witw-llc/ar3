"""The extension seam: `cli` loads modules from `A8S_EXT_DIR` (default
`apps/a8s/ext/`), lets their `handle` claim a verb or a flag form first, and
skips a broken one with a single stderr line. Every case runs `a8s.py` as a
subprocess under an isolated `A8S_HOME`, because extensions load at import."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

A8S = Path(__file__).resolve().parent.parent / "a8s.py"

TOY = '''
COMMANDS = [("toy", "[x]", "A toy verb."), ("ls", "--toy", "Toy flag form.")]


def handle(cmd, args):
    if cmd == "toy":
        print("toy:" + ",".join(args))
        return 7
    if cmd == "ls" and "--toy" in args:
        print("toy ls")
        return 0
    return None
'''


def run(tmp_path: Path, *args: str, ext_dir: Path | None | str = None):
    env = {**os.environ, "A8S_HOME": str(tmp_path / "home"), "HOME": str(tmp_path)}
    env.pop("A8S_EXT_DIR", None)
    if ext_dir is not None:
        env["A8S_EXT_DIR"] = str(ext_dir)
    return subprocess.run(
        [sys.executable, str(A8S), *args],
        env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def ext(tmp_path):
    d = tmp_path / "ext"
    d.mkdir()
    (d / "toy.py").write_text(TOY)
    return d


def test_extension_claims_a_new_verb(tmp_path, ext):
    r = run(tmp_path, "toy", "a", "b", ext_dir=ext)
    assert r.stdout.strip() == "toy:a,b"
    assert r.returncode == 7


def test_extension_claims_a_flag_form_and_core_keeps_the_rest(tmp_path, ext):
    claimed = run(tmp_path, "ls", "--toy", ext_dir=ext)
    assert claimed.stdout.strip() == "toy ls"
    plain = run(tmp_path, "ls", ext_dir=ext)
    assert plain.returncode == 0
    assert "toy" not in plain.stdout
    assert "no nodes registered" in plain.stdout


def test_help_lists_extensions_after_the_core_table(tmp_path, ext):
    out = run(tmp_path, "--help", ext_dir=ext).stdout
    assert "Commands:" in out
    assert out.index("Commands:") < out.index("Extensions:")
    tail = out.split("Extensions:")[1]
    assert "toy [x]" in tail
    assert "ls --toy" in tail


def test_no_extensions_means_no_extensions_heading(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    out = run(tmp_path, "--help", ext_dir=empty).stdout
    assert "Commands:" in out
    assert "Extensions:" not in out


def test_missing_ext_dir_loads_nothing_and_a8s_works(tmp_path):
    r = run(tmp_path, "ls", ext_dir=tmp_path / "does-not-exist")
    assert r.returncode == 0
    assert r.stderr == ""


def test_unclaimed_unknown_verb_is_still_unknown(tmp_path, ext):
    r = run(tmp_path, "nope", ext_dir=ext)
    assert r.returncode == 2
    assert "unknown command" in r.stderr


def test_broken_extension_is_skipped_with_one_stderr_line(tmp_path, ext):
    (ext / "a_broken.py").write_text("raise RuntimeError('boom')\n")
    (ext / "b_nohandle.py").write_text("COMMANDS = []\n")
    (ext / "_private.py").write_text("raise RuntimeError('never loaded')\n")
    r = run(tmp_path, "toy", ext_dir=ext)
    assert r.returncode == 7
    lines = r.stderr.strip().splitlines()
    assert lines[0] == "a8s: extension a_broken.py: boom"
    assert lines[1].startswith("a8s: extension b_nohandle.py:")
    assert len(lines) == 2


def test_first_extension_to_claim_wins_in_load_order(tmp_path, ext):
    (ext / "a_first.py").write_text(
        "COMMANDS = []\n"
        "def handle(cmd, args):\n"
        "    if cmd == 'toy':\n"
        "        print('first'); return 3\n"
    )
    r = run(tmp_path, "toy", ext_dir=ext)
    assert r.stdout.strip() == "first"
    assert r.returncode == 3


def test_extension_can_import_core_modules(tmp_path, ext):
    (ext / "uses_core.py").write_text(
        "import registry\n"
        "COMMANDS = [('regsize', '', 'Registry size.')]\n"
        "def handle(cmd, args):\n"
        "    if cmd == 'regsize':\n"
        "        print(len(registry.load_registry())); return 0\n"
    )
    r = run(tmp_path, "regsize", ext_dir=ext)
    assert r.stdout.strip() == "0"


def test_default_directory_absent_leaves_core_unchanged(tmp_path):
    r = run(tmp_path, "--help")
    assert r.returncode == 0
    assert "Commands:" in r.stdout
    assert run(tmp_path, "ls").returncode == 0
