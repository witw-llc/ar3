"""Tests for the shared suite-version module.

`ar3ver` lives at the repo root because all six CLIs answer `--version` from
it. The update check reaches the public mirror, so every test here stubs the
network: a test suite that phones GitHub is a test suite that fails on a plane.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import ar3ver


@pytest.fixture
def fake_tags(monkeypatch):
    """Serve a tag list to `latest_public_version` without a network call."""
    def install(names, error=None):
        def fake_urlopen(req, timeout=None):
            if error is not None:
                raise error
            body = json.dumps([{"name": n} for n in names]).encode()
            return io.BytesIO(body)
        monkeypatch.setattr(ar3ver.urllib.request, "urlopen", fake_urlopen)
    return install


class TestSuiteVersion:
    def test_reads_the_repo_version_file(self):
        expected = (ar3ver.repo_root() / "VERSION").read_text().strip()
        assert ar3ver.suite_version() == expected

    def test_version_line_names_the_app_and_the_suite(self):
        line = ar3ver.version_line("a8s")
        assert line.startswith("a8s ")
        assert ar3ver.suite_version() in line

    def test_missing_version_file_is_not_a_crash(self, monkeypatch, tmp_path):
        # A user running from a copied-out directory still gets a version.
        monkeypatch.setattr(ar3ver, "repo_root", lambda: tmp_path)
        assert ar3ver.suite_version() == ar3ver.UNKNOWN


class TestParseVersion:
    @pytest.mark.parametrize("text,expected", [
        ("0.1.58", (0, 1, 58)),
        ("v0.1.58", (0, 1, 58)),
        ("  v1.0.0  ", (1, 0, 0)),
        ("1.2", (1, 2)),
    ])
    def test_parses(self, text, expected):
        assert ar3ver.parse_version(text) == expected

    @pytest.mark.parametrize("text", ["", "   ", "main", "v", "1.x.3", None])
    def test_rejects(self, text):
        assert ar3ver.parse_version(text) is None

    def test_orders_numerically_not_lexically(self):
        # "0.1.9" > "0.1.58" as strings, and that is the bug this prevents.
        assert ar3ver.parse_version("0.1.58") > ar3ver.parse_version("0.1.9")


class TestLatestPublicVersion:
    def test_picks_the_highest_tag_not_the_first(self, fake_tags):
        fake_tags(["v0.1.9", "v0.1.58", "v0.1.57"])
        assert ar3ver.latest_public_version() == "0.1.58"

    def test_ignores_tags_that_are_not_versions(self, fake_tags):
        fake_tags(["latest", "release-candidate", "v0.2.0"])
        assert ar3ver.latest_public_version() == "0.2.0"

    def test_no_usable_tags_is_none(self, fake_tags):
        fake_tags(["latest", "nightly"])
        assert ar3ver.latest_public_version() is None

    def test_an_unreachable_mirror_is_none_not_an_exception(self, fake_tags):
        fake_tags([], error=urllib.error.URLError("offline"))
        assert ar3ver.latest_public_version() is None

    def test_a_timeout_is_none_not_an_exception(self, fake_tags):
        fake_tags([], error=TimeoutError())
        assert ar3ver.latest_public_version() is None


class TestUpdateNote:
    def test_names_the_newer_version_when_behind(self, monkeypatch):
        monkeypatch.setattr(ar3ver, "suite_version", lambda: "0.1.57")
        monkeypatch.setattr(ar3ver, "latest_public_version", lambda timeout_s=3.0: "0.1.58")
        note = ar3ver.update_note()
        assert "0.1.57" in note and "0.1.58 is available" in note

    def test_says_latest_when_current(self, monkeypatch):
        monkeypatch.setattr(ar3ver, "suite_version", lambda: "0.1.58")
        monkeypatch.setattr(ar3ver, "latest_public_version", lambda timeout_s=3.0: "0.1.58")
        assert ar3ver.update_note() == "0.1.58 (latest)"

    def test_a_working_copy_ahead_of_the_mirror_is_not_an_update(self, monkeypatch):
        # The normal state on this repo: main is bumped before the mirror push.
        monkeypatch.setattr(ar3ver, "suite_version", lambda: "0.1.58")
        monkeypatch.setattr(ar3ver, "latest_public_version", lambda timeout_s=3.0: "0.1.57")
        assert ar3ver.update_note() == "0.1.58 (latest)"

    def test_offline_says_so_without_claiming_to_be_current(self, monkeypatch):
        monkeypatch.setattr(ar3ver, "suite_version", lambda: "0.1.58")
        monkeypatch.setattr(ar3ver, "latest_public_version", lambda timeout_s=3.0: None)
        note = ar3ver.update_note()
        assert "could not reach" in note and "latest)" not in note
