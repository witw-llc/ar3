"""Changelog gate — unit tests for tools/changelog-heading.py and
tools/changelog-closes.py.

Each test builds a miniature CHANGELOG.md body in-memory, so nothing here
depends on what the real changelog happens to say today.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    path = REPO_ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


heading = _load("changelog-heading")
closes = _load("changelog-closes")


class TestChangelogHeading:
    def test_missing_version_heading_fails(self):
        text = "## Unreleased\n\n## 0.1.95 — 2026-09-24\n\n### Fixed\n\n- old\n"
        message = heading.check(text, "0.1.96")
        assert message is not None
        assert "0.1.96" in message
        assert "rename" in message

    def test_unreleased_with_content_fails_even_when_heading_exists(self):
        text = (
            "## Unreleased\n\n### Added\n\n- new thing\n\n"
            "## 0.1.96 — 2026-09-25\n\n### Changed\n\n- something\n"
        )
        message = heading.check(text, "0.1.96")
        assert message is not None
        assert "Unreleased" in message

    def test_heading_present_and_unreleased_empty_passes(self):
        text = (
            "## Unreleased\n\n## 0.1.96 — 2026-09-25\n\n### Changed\n\n- something\n"
        )
        assert heading.check(text, "0.1.96") is None

    def test_heading_present_with_no_unreleased_section_at_all_passes(self):
        text = "## 0.1.96 — 2026-09-25\n\n### Changed\n\n- something\n"
        assert heading.check(text, "0.1.96") is None

    def test_heading_without_date_suffix_still_matches(self):
        text = "## Unreleased\n\n## 0.1.96\n\n### Changed\n\n- something\n"
        assert heading.check(text, "0.1.96") is None

    def test_cli_exit_code(self, tmp_path, capsys):
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("## Unreleased\n\n## 0.1.95 — 2026-09-24\n", encoding="utf-8")
        assert heading.main(["0.1.96", "--changelog", str(changelog)]) == 1
        assert "rename" in capsys.readouterr().err
        assert heading.main(["0.1.95", "--changelog", str(changelog)]) == 0


class TestChangelogCloses:
    def test_no_section_returns_nothing(self):
        text = "## 0.1.95 — 2026-09-24\n\n### Fixed\n\n- fix. Closes #1\n"
        assert closes.closes_for_version(text, "0.1.96") == []

    def test_collects_and_dedupes_issue_numbers(self):
        text = (
            "## 0.1.96 — 2026-09-25\n\n"
            "### Changed\n\n"
            "- one thing. Closes #246\n"
            "- another. Closes #272 and also Closes #246\n\n"
            "## 0.1.95 — 2026-09-24\n\n"
            "### Fixed\n\n"
            "- older. Closes #1\n"
        )
        assert closes.closes_for_version(text, "0.1.96") == [246, 272]
        assert closes.closes_for_version(text, "0.1.95") == [1]

    def test_closes_wrapped_before_its_number_is_read(self):
        # A hard-wrapped entry can put the number on the next line; the
        # publish job closes only what this reads, so a miss leaves the
        # issue open with nothing saying so.
        text = (
            "## 0.1.96 — 2026-09-25\n\n"
            "- a long entry that ends the line with the word. Closes\n"
            "  #272\n"
        )
        assert closes.closes_for_version(text, "0.1.96") == [272]

    def test_section_with_no_closes_lines_is_empty(self):
        text = "## 0.1.96 — 2026-09-25\n\n### Added\n\n- something with no issue\n"
        assert closes.closes_for_version(text, "0.1.96") == []

    def test_cli_prints_one_per_line(self, tmp_path, capsys):
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "## 0.1.96 — 2026-09-25\n\n- a. Closes #282\n- b. Closes #246\n",
            encoding="utf-8",
        )
        assert closes.main(["0.1.96", "--changelog", str(changelog)]) == 0
        assert capsys.readouterr().out.splitlines() == ["246", "282"]
