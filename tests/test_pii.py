"""PII regression — unit tests for .github/pii_check.py (CI scan is the pii-check job)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github"))

import pii_check  # noqa: E402
from pii_check import (  # noqa: E402
    GitUnavailable,
    check_diff,
    diff_range,
    diff_vs_main,
    load_patterns,
    locate_violations,
    parse_patterns,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_scanner():
    """tools/pii-scan.py has a hyphen in its name, so it cannot be imported."""
    spec = importlib.util.spec_from_file_location(
        "pii_scan_tool", REPO_ROOT / "tools" / "pii-scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAMPLE_PATTERNS = "example-agent-name\nexample-hostname\nexample\\.host\\.example\n"


@pytest.fixture(autouse=True)
def _pii_patterns_env(monkeypatch):
    monkeypatch.setenv("PII_PATTERNS", SAMPLE_PATTERNS)


def test_example_agent_name_is_registered_pii_pattern():
    patterns = load_patterns()
    assert "example-agent-name" in patterns


def test_pii_check_catches_example_agent_name_in_added_line():
    diff = "\n".join(
        [
            "diff --git a/example.md b/example.md",
            "+++ b/example.md",
            "+export TELL_OUTBOX_DIR=/var/mailboxes/example-agent-name/.outbox",
        ]
    )
    hits = check_diff(diff, parse_patterns(SAMPLE_PATTERNS))
    assert any(p == "example-agent-name" for p, _ in hits)


def test_load_patterns_requires_env_or_local_file(monkeypatch, tmp_path):
    # Point the local file somewhere that cannot exist rather than skipping
    # when it does: a machine that happens to carry the file must still run
    # this assertion, or the test is green by not running.
    monkeypatch.delenv("PII_PATTERNS", raising=False)
    monkeypatch.setattr(pii_check, "LOCAL_PATTERNS_FILE", tmp_path / "absent.txt")
    with pytest.raises(FileNotFoundError):
        load_patterns()


class TestFailureOutputCarriesNoPII:
    """The report is written into CI logs. A pattern is a bare name or
    hostname, and GitHub masks a secret's whole value rather than the
    individual lines inside it, so neither the pattern nor the matched line
    may appear in what the checker prints."""

    DIFF = "\n".join(
        [
            "diff --git a/notes.md b/notes.md",
            "+++ b/notes.md",
            "+ssh example-hostname today",
        ]
    )

    def test_it_still_finds_the_violation(self):
        located = locate_violations(self.DIFF, parse_patterns(SAMPLE_PATTERNS))
        assert located == [("notes.md", 2)]

    def test_a_clean_diff_locates_nothing(self):
        clean = "diff --git a/x.md b/x.md\n+++ b/x.md\n+all fine\n"
        assert locate_violations(clean, parse_patterns(SAMPLE_PATTERNS)) == []

    def test_the_report_line_repeats_neither_the_pattern_nor_the_line(self):
        patterns = parse_patterns(SAMPLE_PATTERNS)
        report = "\n".join(
            f"{path}: matches PII pattern #{index}"
            for path, index in locate_violations(self.DIFF, patterns)
        )
        assert "notes.md" in report
        for pattern in patterns:
            assert pattern not in report
        assert "ssh example-hostname today" not in report


class TestTheGatesFailClosed:
    """"git could not answer" must never be reported as "nothing to report".
    Both gates converted a git failure into empty input and then printed a
    clean result, so an unknown ref or an unreadable checkout passed the
    branch it could not read."""

    def test_an_unknown_range_raises_instead_of_returning_an_empty_diff(self):
        with pytest.raises(GitUnavailable):
            diff_range("definitely-not-a-ref")

    def test_a_repo_with_no_main_ref_raises(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("hello\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "one"],
            cwd=tmp_path, check=True,
        )
        subprocess.run(["git", "branch", "-m", "not-main"], cwd=tmp_path, check=True)
        with pytest.raises(GitUnavailable):
            diff_vs_main(tmp_path)

    def test_the_cli_exits_nonzero_on_an_unknown_range(self, monkeypatch):
        monkeypatch.setenv("PII_PATTERNS", SAMPLE_PATTERNS)
        assert pii_check.main(["--range", "definitely-not-a-ref"]) == 1

    def test_a_valid_range_still_works(self, monkeypatch, tmp_path):
        # A private two-commit repo rather than the host checkout: CI clones
        # shallow, so HEAD~1 does not exist there and the gate (correctly)
        # refuses — which is this class's other tests, not this one.
        for i, text in enumerate(["one\n", "two\n"]):
            (tmp_path / "f.txt").write_text(text)
            subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
            subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", f"c{i}"],
                cwd=tmp_path, check=True,
            )
        monkeypatch.setenv("PII_PATTERNS", "zzz-absent-from-this-repo")
        monkeypatch.setattr(pii_check, "REPO_ROOT", tmp_path)
        assert pii_check.main(["--range", "HEAD~1...HEAD"]) == 0


class TestTheScannerIsNotExemptFromItself:
    """The whole-tree scanner used to list its own path in SKIP_PATHS while
    its docstring named a real machine as the worked example. It could not
    have found its own leak."""

    def test_the_scanner_does_not_skip_itself(self):
        scanner = _load_scanner()
        assert "tools/pii-scan.py" not in scanner.SKIP_PATHS
        # Only the pattern lists stay exempt: they are the strings themselves.
        assert scanner.SKIP_PATHS == {
            ".github/pii-patterns.example.txt",
            ".github/pii-patterns.local.txt",
        }

    def test_a_pattern_planted_in_the_scanner_is_found(self, monkeypatch):
        scanner = _load_scanner()
        monkeypatch.setenv("PII_PATTERNS", "pii-scan")
        hits = scanner.scan(["def tracked_files"])
        assert any(name == "tools/pii-scan.py" for name, _, _ in hits)

    def test_an_unreadable_tree_is_refused_rather_than_called_clean(self, tmp_path):
        scanner = _load_scanner()
        scanner.REPO_ROOT = tmp_path  # not a git repository
        with pytest.raises(scanner.GitUnavailable):
            scanner.tracked_files()
