"""Forbidden-pattern sweep — parsing, sweep, and the stdout/stderr contract."""
from __future__ import annotations

import io
import subprocess

import pytest

import check


def _git_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _track(root, rel, content, *, binary=False):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel], cwd=root, check=True)


def _checklist(home, name, text):
    d = home / "checklists"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _run(node, workplace):
    out, err = io.StringIO(), io.StringIO()
    code = check.run(node, workplace, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# ---------- pattern-file parsing ----------

def test_comments_and_blanks_ignored(r4t_home):
    _checklist(r4t_home, "default.txt", "# a comment\n\n   \nfoo\n")
    patterns = check.load_patterns("acme")
    assert [src for src, _ in patterns] == ["foo"]


def test_per_node_file_adds_to_default(r4t_home):
    _checklist(r4t_home, "default.txt", "foo\n")
    _checklist(r4t_home, "acme.txt", "bar\n")
    assert [src for src, _ in check.load_patterns("acme")] == ["foo", "bar"]


def test_malformed_regex_is_operational_error(r4t_home, tmp_path):
    _checklist(r4t_home, "default.txt", "good\n[unclosed\n")
    repo = _git_repo(tmp_path / "repo")
    code, out, err = _run("acme", repo)
    assert code == 2
    assert "default.txt:2" in err
    assert out == ""


# ---------- sweep ----------

def test_findings_reported_on_stderr_only(r4t_home, tmp_path):
    _checklist(r4t_home, "default.txt", r"\\n")
    repo = _git_repo(tmp_path / "repo")
    _track(repo, "doc.md", "clean line\nhas a \\n escape here\n")
    code, out, err = _run("acme", repo)
    assert code == 1
    assert out == "check failed: 1 finding(s)\n"
    assert "doc.md:2" in err


def test_clean_repo_passes(r4t_home, tmp_path):
    _checklist(r4t_home, "default.txt", "forbidden\n")
    repo = _git_repo(tmp_path / "repo")
    _track(repo, "doc.md", "nothing here\n")
    code, out, err = _run("acme", repo)
    assert code == 0
    assert out == "check passed\n"


def test_binary_files_skipped_silently(r4t_home, tmp_path):
    _checklist(r4t_home, "default.txt", "match\n")
    repo = _git_repo(tmp_path / "repo")
    _track(repo, "blob.bin", b"\xff\xfe match \x00\x01", binary=True)
    code, out, err = _run("acme", repo)
    assert code == 0
    assert out == "check passed\n"


def test_untracked_files_are_not_swept(r4t_home, tmp_path):
    _checklist(r4t_home, "default.txt", "forbidden\n")
    repo = _git_repo(tmp_path / "repo")
    (repo / "loose.md").write_text("forbidden here\n", encoding="utf-8")
    code, out, err = _run("acme", repo)
    assert code == 0
    assert out == "check passed\n"


def test_inline_case_flag_honored(r4t_home, tmp_path):
    _checklist(r4t_home, "default.txt", "(?i)perfectly validated\n")
    repo = _git_repo(tmp_path / "repo")
    _track(repo, "report.md", "the org said it was Perfectly Validated\n")
    code, out, err = _run("acme", repo)
    assert code == 1
    assert "report.md:1" in err


# ---------- no checklists ----------

def test_no_checklists_passes_with_note(r4t_home, tmp_path):
    repo = _git_repo(tmp_path / "repo")
    _track(repo, "doc.md", "anything\n")
    code, out, err = _run("acme", repo)
    assert code == 0
    assert out == "check passed\n"
    assert "no checklists configured" in err


# ---------- stdout opacity ----------

def test_stdout_never_leaks_path_or_pattern(r4t_home, tmp_path):
    _checklist(r4t_home, "default.txt", "secret-codename\n")
    repo = _git_repo(tmp_path / "repo")
    _track(repo, "notes.md", "the plan is secret-codename go\n")
    code, out, err = _run("acme", repo)
    assert code == 1
    assert "secret-codename" not in out
    assert "notes.md" not in out
    assert out == "check failed: 1 finding(s)\n"
    # the detail lands only on the human surface
    assert "secret-codename" in err and "notes.md:1" in err
