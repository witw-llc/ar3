"""Tests for inbound attachment path guards."""
from __future__ import annotations

from pathlib import Path

from services.attachment_path import bundle_file_path


class TestBundleFilePath:
    def test_accepts_basename(self, tmp_path):
        root = tmp_path / "bundle"
        root.mkdir()
        dest, err = bundle_file_path(root, "doc.txt")
        assert err == ""
        assert dest == (root / "doc.txt").resolve()

    def test_rejects_traversal(self, tmp_path):
        root = tmp_path / "bundle"
        root.mkdir()
        dest, err = bundle_file_path(root, "../../../etc/passwd")
        assert dest is None
        assert "not a basename" in err

    def test_rejects_nested_path(self, tmp_path):
        root = tmp_path / "bundle"
        root.mkdir()
        dest, err = bundle_file_path(root, "sub/doc.txt")
        assert dest is None
        assert "not a basename" in err
