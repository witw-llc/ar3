"""PII regression — unit tests for .github/pii_check.py (CI scan is the pii-check job)."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github"))

import pii_check  # noqa: E402
from pii_check import (  # noqa: E402
    GitUnavailable,
    NoPatterns,
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
    with pytest.raises(NoPatterns):
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

    # Distinct canaries for the two things that must not be echoed: the
    # pattern that fired, and the rest of the line it fired on.
    CANARY_PATTERN = "canary-pattern-value"
    CANARY_REST = "canary-line-value"
    CANARY_LINE = f"ssh {CANARY_PATTERN} --key {CANARY_REST}"

    def test_the_diff_gate_prints_neither_the_pattern_nor_the_line(
        self, tmp_path, monkeypatch, capsys
    ):
        """Run the real entry point rather than re-composing its output.

        This assertion used to build the report string itself from
        `locate_violations` and check *that*, which says nothing about what
        the checker prints: a mutation putting the matched line back into the
        production `print` passed it untouched.
        """
        def build(root):
            (root / "notes.md").write_text("nothing here yet\n")

        root = _controlled_repo(tmp_path / "diff-gate", build)
        (root / "notes.md").write_text(f"{self.CANARY_LINE}\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "two"],
            cwd=root, check=True,
        )
        monkeypatch.setenv("PII_PATTERNS", self.CANARY_PATTERN)
        monkeypatch.setattr(pii_check, "REPO_ROOT", root)

        assert pii_check.main(["--range", "HEAD~1...HEAD"]) == 1
        captured = capsys.readouterr()
        printed = captured.out + captured.err
        assert "notes.md: matches PII pattern #1" in printed
        assert self.CANARY_PATTERN not in printed
        assert self.CANARY_REST not in printed

    def test_the_tree_scanner_prints_neither_the_pattern_nor_the_line(
        self, tmp_path, monkeypatch, capsys
    ):
        def build(root):
            (root / "notes.md").write_text(f"{self.CANARY_LINE}\n")

        scanner = _load_scanner()
        scanner.REPO_ROOT = _controlled_repo(tmp_path / "tree-gate", build)
        monkeypatch.setenv("PII_PATTERNS", self.CANARY_PATTERN)

        assert scanner.main() == 1
        captured = capsys.readouterr()
        printed = captured.out + captured.err
        assert "notes.md:1: matches PII pattern #1" in printed
        assert self.CANARY_PATTERN not in printed
        assert self.CANARY_REST not in printed


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
        # A name the scanner has carried since it was written. The scan reads
        # the *index* blob, so a string this test invented in the working tree
        # would be invisible until someone staged it.
        hits = scanner.scan(["MAX_REPORTED"])
        assert any(h.entry.path == "tools/pii-scan.py" for h in hits)

    def test_an_unreadable_tree_is_refused_rather_than_called_clean(self, tmp_path):
        scanner = _load_scanner()
        scanner.REPO_ROOT = tmp_path  # not a git repository
        with pytest.raises(scanner.GitUnavailable):
            scanner.tracked_entries()


CANARY = "private-canary"


def _controlled_repo(root: Path, build) -> Path:
    """A one-commit repo holding exactly what `build` puts in it.

    Built rather than borrowed: each case below needs a tracked object of a
    particular shape, and the host checkout has none of them — the scan would
    pass for want of anything to catch.
    """
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    build(root)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one"],
        cwd=root, check=True,
    )
    return root


def _scan_controlled(tmp_path: Path, build):
    scanner = _load_scanner()
    scanner.REPO_ROOT = _controlled_repo(tmp_path / "controlled", build)
    return scanner, scanner.scan([CANARY])


class TestTheScanReadsTheTrackedRepresentation:
    """What ships is the tracked representation: the path name and the blob
    git stores for it. Reading the working tree with `read_text` saw neither
    — it never looked at a name, it followed a symlink instead of reading the
    target text git actually ships, and it swallowed `UnicodeDecodeError` on
    anything that is not valid UTF-8. Each case here is a repo the old scan
    called clean.
    """

    def test_a_tracked_path_name_is_scanned(self, tmp_path):
        def build(root):
            (root / f"{CANARY}-notes.md").write_text("nothing sensitive inside\n")

        _scanner, hits = _scan_controlled(tmp_path, build)
        assert [(h.entry.path, h.lineno) for h in hits] == [
            (f"{CANARY}-notes.md", None)
        ]

    @pytest.mark.skipif(
        os.name == "nt", reason="git on Windows stores a symlink as a plain file"
    )
    def test_a_symlinks_stored_target_is_scanned(self, tmp_path):
        def build(root):
            os.symlink(f"/{CANARY}/host", root / "link")

        _scanner, hits = _scan_controlled(tmp_path, build)
        assert [h.entry.path for h in hits] == ["link"]

    @pytest.mark.skipif(
        os.name == "nt", reason="git on Windows stores a symlink as a plain file"
    )
    def test_a_symlinks_target_is_scanned_even_when_its_name_ends_in_a_skipped_suffix(
        self, tmp_path
    ):
        """SKIP_SUFFIXES exists for binary regular-file content — a `.png`'s
        bytes are not text. A symlink's blob is always the target path as
        text, however its own name ends, so a symlink named `link.png` still
        ships PII in its stored target and must still be scanned."""
        def build(root):
            os.symlink(f"/{CANARY}/host", root / "link.png")

        _scanner, hits = _scan_controlled(tmp_path, build)
        assert [h.entry.path for h in hits] == ["link.png"]

    @pytest.mark.skipif(
        os.name == "nt", reason="git on Windows stores a symlink as a plain file"
    )
    def test_a_symlink_with_a_skipped_suffix_and_an_innocent_target_stays_clean(
        self, tmp_path
    ):
        """The negative control for the case above: a symlink whose name ends
        in a skipped suffix is scanned, not flagged outright — only a target
        that actually matches a pattern is a hit."""
        def build(root):
            os.symlink("/somewhere/ordinary", root / "link.png")

        _scanner, hits = _scan_controlled(tmp_path, build)
        assert hits == []

    def test_an_undecodable_blob_is_still_scanned(self, tmp_path):
        def build(root):
            (root / "blobfile").write_bytes(b"\xff" + CANARY.encode() + b"\n")

        _scanner, hits = _scan_controlled(tmp_path, build)
        assert [h.entry.path for h in hits] == ["blobfile"]

    def test_a_repo_with_none_of_the_three_stays_clean(self, tmp_path):
        """The negative control. Without it every assertion above would pass
        on a scanner that flagged everything it read."""
        def build(root):
            (root / "notes.md").write_text("nothing sensitive inside\n")
            (root / "plain").write_bytes(b"\xffalso fine\n")
            if os.name != "nt":
                os.symlink("/somewhere/ordinary", root / "link")

        _scanner, hits = _scan_controlled(tmp_path, build)
        assert hits == []


class TestAMatchedPathIsNeverPrinted:
    """A path that matches is itself the PII. The report used to print
    `<path>:<line>: matches PII pattern #N` for every hit, so a leak in a file
    name was published to the CI log by the job that found it."""

    @staticmethod
    def _build(root):
        (root / f"{CANARY}-notes.md").write_text(f"ssh {CANARY} today\n")

    def test_the_report_names_a_position_instead_of_the_path(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setenv("PII_PATTERNS", CANARY)
        scanner, _hits = _scan_controlled(tmp_path, self._build)
        assert scanner.main() == 1
        captured = capsys.readouterr()
        assert CANARY not in captured.out + captured.err
        assert "tracked file #1: path name matches PII pattern #1" in captured.err
        assert "tracked file #1:1: matches PII pattern #1" in captured.err

    def test_a_safe_path_is_still_named(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("PII_PATTERNS", CANARY)

        def build(root):
            (root / "notes.md").write_text(f"ssh {CANARY} today\n")

        scanner, _hits = _scan_controlled(tmp_path, build)
        assert scanner.main() == 1
        captured = capsys.readouterr()
        assert CANARY not in captured.out + captured.err
        assert "notes.md:1: matches PII pattern #1" in captured.err


class TestAnUnreadableBlobFailsClosed:
    """`read_text` raised `OSError`/`UnicodeDecodeError` on a file it could
    not read and the scan `continue`d — the one file it could not check was
    the one file it declared clean by omission."""

    def test_a_missing_object_raises_instead_of_being_skipped(self, tmp_path):
        def build(root):
            (root / "notes.md").write_text("all fine\n")

        scanner = _load_scanner()
        root = _controlled_repo(tmp_path / "controlled", build)
        scanner.REPO_ROOT = root
        entries = scanner.tracked_entries()
        gone = [e._replace(sha="0" * 40) for e in entries]
        with pytest.raises(scanner.GitUnavailable):
            list(scanner.blob_bytes(gone))


class TestAnEmptyPatternSetIsRefused:
    """A configured source that parses to zero patterns is not a configured
    source. Both gates loaded it, matched nothing against it, and printed
    success — `PII_PATTERNS='# comment only'` took both to exit 0, and the
    whole-tree scan announced "clean across the tracked tree (0 pattern(s))".
    Being unconfigured and being configured with a comment have the same
    consequence, so they get the same refusal."""

    def test_an_env_source_that_parses_to_nothing_raises(self, monkeypatch):
        monkeypatch.setenv("PII_PATTERNS", "# comment only")
        with pytest.raises(NoPatterns):
            load_patterns()

    def test_a_file_source_that_parses_to_nothing_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PII_PATTERNS", raising=False)
        empty = tmp_path / "pii-patterns.local.txt"
        empty.write_text("# nothing but a comment\n\n", encoding="utf-8")
        monkeypatch.setattr(pii_check, "LOCAL_PATTERNS_FILE", empty)
        with pytest.raises(NoPatterns):
            load_patterns()

    def test_a_source_with_one_real_pattern_still_loads(self, monkeypatch):
        """The positive control: the refusal has to be about emptiness, not
        about comments being present at all."""
        monkeypatch.setenv("PII_PATTERNS", "# a comment\nexample-hostname\n")
        assert load_patterns() == ["example-hostname"]

    def test_the_diff_gate_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setenv("PII_PATTERNS", "# comment only")
        assert pii_check.main(["--range", "HEAD~1...HEAD"]) == 1
        assert "no patterns loaded" in capsys.readouterr().err

    def test_the_whole_tree_gate_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setenv("PII_PATTERNS", "# comment only")
        scanner = _load_scanner()
        assert scanner.main() == 1
        captured = capsys.readouterr()
        assert "no patterns loaded" in captured.err
        assert "clean" not in captured.out
