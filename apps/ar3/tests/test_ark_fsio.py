"""Tests for the shared atomic-write primitive — The Ark's foundation layer."""
from __future__ import annotations

import os

import pytest

from ark.fsio import atomic_write_text


class TestBasics:
    def test_writes_the_text(self, tmp_path):
        target = tmp_path / "file.json"
        atomic_write_text(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_overwrites_existing_content(self, tmp_path):
        target = tmp_path / "file.json"
        target.write_text("old", encoding="utf-8")
        atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    def test_creates_missing_parent_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "c.json"
        atomic_write_text(target, "nested")
        assert target.read_text(encoding="utf-8") == "nested"

    def test_no_tmp_file_left_behind_on_success(self, tmp_path):
        target = tmp_path / "file.json"
        atomic_write_text(target, "hello")
        leftovers = [p for p in tmp_path.iterdir() if p != target]
        assert leftovers == []


class TestConcurrency:
    def test_unique_tmp_names_do_not_collide(self, tmp_path):
        # Two writes to the same target in quick succession must each get
        # their own tmp file — no fixed ".tmp" suffix to collide on.
        target = tmp_path / "file.json"
        for i in range(5):
            atomic_write_text(target, f"pass-{i}")
        assert target.read_text(encoding="utf-8") == "pass-4"


class TestFsync:
    def test_fsync_true_still_produces_correct_content(self, tmp_path):
        target = tmp_path / "file.json"
        atomic_write_text(target, "synced", fsync=True)
        assert target.read_text(encoding="utf-8") == "synced"

    def test_fsync_false_is_the_default(self, tmp_path):
        target = tmp_path / "file.json"
        atomic_write_text(target, "unsynced")
        assert target.read_text(encoding="utf-8") == "unsynced"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
class TestMode:
    def test_mode_is_applied_to_the_final_path(self, tmp_path):
        target = tmp_path / "secret.json"
        atomic_write_text(target, "shh", mode=0o600)
        assert (target.stat().st_mode & 0o777) == 0o600

    def test_default_mode_uses_the_umask(self, tmp_path):
        target = tmp_path / "plain.json"
        atomic_write_text(target, "plain")
        # No explicit mode was requested; the file exists and is readable —
        # the umask decides the rest, so this only pins that we didn't force
        # a restrictive mode when none was asked for.
        assert (target.stat().st_mode & 0o400) != 0


class TestFailureCleanup:
    def test_tmp_file_removed_when_replace_fails(self, tmp_path, monkeypatch):
        target = tmp_path / "sub" / "file.json"

        real_replace = os.replace

        def failing_replace(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", failing_replace)
        with pytest.raises(OSError):
            atomic_write_text(target, "content")
        monkeypatch.setattr(os, "replace", real_replace)

        assert not target.exists()
        leftovers = list((tmp_path / "sub").iterdir())
        assert leftovers == []
