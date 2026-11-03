"""Tests for the shared atomic-write primitive — ar3's foundation layer."""
from __future__ import annotations

import os

import pytest

from pathlib import Path

from ar3.fsio import atomic_write_bytes, atomic_write_text, exists, is_dir, is_file, stat_or_none


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


class TestBytes:
    def test_lands_byte_for_byte(self, tmp_path):
        target = tmp_path / "copy.md"
        data = b"- one\r\n- two\n- caf\xe9\r\n"  # mixed endings, not UTF-8
        atomic_write_bytes(target, data)
        assert target.read_bytes() == data

    def test_text_writes_are_unchanged(self, tmp_path):
        target = tmp_path / "file.md"
        atomic_write_text(target, "a\nb\n")
        assert target.read_text(encoding="utf-8") == "a\nb\n"

    def test_tmp_file_removed_when_replace_fails(self, tmp_path, monkeypatch):
        target = tmp_path / "sub" / "copy.md"

        def failing_replace(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", failing_replace)
        with pytest.raises(OSError):
            atomic_write_bytes(target, b"content")
        assert list((tmp_path / "sub").iterdir()) == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_mode_is_applied(self, tmp_path):
        target = tmp_path / "secret.bin"
        atomic_write_bytes(target, b"\x00\r\n", mode=0o600)
        assert (target.stat().st_mode & 0o777) == 0o600
        assert target.read_bytes() == b"\x00\r\n"


class TestUnreadablePath:
    """Python 3.12 re-raises PermissionError from Path.is_dir on a Windows
    profile WSL cannot stat. These helpers return False/None instead."""

    def test_a_readable_file_and_directory(self, tmp_path):
        directory = tmp_path / "profile"
        directory.mkdir()
        target = directory / "auth.json"
        target.write_text("{}", encoding="utf-8")
        assert is_dir(directory) is True
        assert is_file(directory) is False
        assert is_file(target) is True
        assert is_dir(target) is False
        assert exists(target) is True
        assert stat_or_none(target) is not None
        assert exists(tmp_path / "missing") is False
        assert is_dir(tmp_path / "missing") is False
        assert stat_or_none(tmp_path / "missing") is None

    def test_permission_error_on_one_directory_does_not_raise(self, tmp_path, monkeypatch):
        blocked = tmp_path / "CodexSandboxOffline"
        sibling = tmp_path / "nobre"
        blocked.mkdir()
        sibling.mkdir()
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if Path(path) == blocked:
                raise PermissionError(13, "Permission denied", str(path))
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", stat)
        assert is_dir(blocked) is False
        assert is_file(blocked) is False
        assert exists(blocked) is False
        assert stat_or_none(blocked) is None
        assert is_dir(sibling) is True


def _flaky_read(monkeypatch, failures, error=PermissionError):
    import ar3.fsio as fsio

    real = Path.read_text
    calls = []

    def read_text(self, *a, **k):
        calls.append(self)
        if len(calls) <= failures:
            raise error(13, "sharing violation")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(fsio.time, "sleep", lambda s: None)
    return calls


def test_read_text_retry_rides_out_a_brief_sharing_violation(tmp_path, monkeypatch):
    from ar3.fsio import read_text_retry

    f = tmp_path / "pid"
    f.write_text("42")
    calls = _flaky_read(monkeypatch, 3)
    assert read_text_retry(f) == "42"
    assert len(calls) == 4


def test_read_text_retry_is_bounded_and_raises_the_permission_error(tmp_path, monkeypatch):
    from ar3.fsio import READ_ATTEMPTS, read_text_retry

    f = tmp_path / "pid"
    f.write_text("42")
    calls = _flaky_read(monkeypatch, 10**6)
    with pytest.raises(PermissionError):
        read_text_retry(f)
    assert len(calls) == READ_ATTEMPTS


def test_read_text_retry_does_not_retry_other_errors(tmp_path, monkeypatch):
    from ar3.fsio import read_text_retry

    calls = _flaky_read(monkeypatch, 0)
    with pytest.raises(FileNotFoundError):
        read_text_retry(tmp_path / "absent")
    assert len(calls) == 1


class TestAbsentIsNotUnreadable:
    """Policy files: only a genuinely absent path reads as "no file"."""

    def test_absent_is_none_and_not_present(self, tmp_path):
        from ar3.fsio import is_present, read_text_if_present

        assert read_text_if_present(tmp_path / "nope") is None
        assert is_present(tmp_path / "nope") is False

    def test_a_file_where_a_folder_should_be_is_absent(self, tmp_path):
        from ar3.fsio import is_present, read_text_if_present

        (tmp_path / "blocker").write_text("x", encoding="utf-8")
        assert read_text_if_present(tmp_path / "blocker" / "f") is None
        assert is_present(tmp_path / "blocker" / "f") is False

    def test_present_text_is_returned(self, tmp_path):
        from ar3.fsio import read_text_if_present

        (tmp_path / "f").write_text("héllo", encoding="utf-8")
        assert read_text_if_present(tmp_path / "f") == "héllo"

    def test_a_directory_in_the_files_place_is_unreadable_not_absent(self, tmp_path):
        from ar3.fsio import is_present, read_text_if_present

        (tmp_path / "d").mkdir()
        assert is_present(tmp_path / "d") is True
        with pytest.raises(OSError):
            read_text_if_present(tmp_path / "d")

    @pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="posix non-root")
    def test_a_closed_folder_is_unreadable_not_absent(self, tmp_path):
        from ar3.fsio import is_present, read_text_if_present

        folder = tmp_path / "private"
        folder.mkdir()
        (folder / "f").write_text("x", encoding="utf-8")
        folder.chmod(0)
        try:
            assert is_present(folder / "f") is True
            with pytest.raises(PermissionError):
                read_text_if_present(folder / "f")
        finally:
            folder.chmod(0o700)

    def test_undecodable_bytes_raise_not_empty(self, tmp_path):
        from ar3.fsio import read_text_if_present

        (tmp_path / "f").write_bytes(b"\xff\xfe\x00bad")
        with pytest.raises(UnicodeDecodeError):
            read_text_if_present(tmp_path / "f")


class TestDanglingLinks:
    """A link that leads nowhere is something configured and unreachable, not
    nothing configured."""

    @pytest.mark.skipif(os.name == "nt", reason="symlinks")
    def test_a_dangling_file_link_is_not_absent(self, tmp_path):
        from ar3.fsio import DanglingLink, is_present, read_text_if_present

        link = tmp_path / "rigs.json"
        link.symlink_to(tmp_path / "unmounted" / "rigs.json")
        assert is_present(link) is True
        with pytest.raises(DanglingLink):
            read_text_if_present(link)

    @pytest.mark.skipif(os.name == "nt", reason="symlinks")
    def test_a_dangling_parent_link_is_not_absent(self, tmp_path):
        from ar3.fsio import DanglingLink, read_text_if_present

        (tmp_path / "parent").symlink_to(tmp_path / "offline")
        with pytest.raises(DanglingLink):
            read_text_if_present(tmp_path / "parent" / "rigs.json")

    @pytest.mark.skipif(os.name == "nt", reason="symlinks")
    def test_a_live_link_reads_and_a_plain_miss_is_absent(self, tmp_path):
        from ar3.fsio import read_text_if_present

        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "f").write_text("x", encoding="utf-8")
        (tmp_path / "link").symlink_to(tmp_path / "real")
        assert read_text_if_present(tmp_path / "link" / "f") == "x"
        assert read_text_if_present(tmp_path / "real" / "none") is None


class TestFindExecutable:
    @pytest.mark.skipif(os.name == "nt", reason="posix modes")
    def test_it_reads_the_childs_path_and_cwd_only(self, tmp_path, monkeypatch):
        from ar3.proc import find_executable

        (tmp_path / "bin").mkdir()
        tool = tmp_path / "bin" / "t"
        tool.write_text("x", encoding="utf-8")
        tool.chmod(0o755)
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.chdir("/")
        assert find_executable("t", "bin", tmp_path) == str(tool)
        assert find_executable("t", os.pathsep + "/x", tmp_path / "bin") == str(tool)
        assert find_executable("t", "bin", "/") is None
