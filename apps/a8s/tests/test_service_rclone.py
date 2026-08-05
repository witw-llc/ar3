"""Tests for the rclone storage service.

A fake `rclone` on disk exercises the real subprocess path — argv, exit codes,
stdout parsing — without needing a configured remote. The link shapes here are
what a live `rclone link` against Google Drive returned.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from network import detect_service_kind
from services import StorageError
from services.rclone import RcloneService, direct_download_url

DRIVE_ID = "1xeIdcBzqqve7R1B-C9aDy24eGYvzKJ_1"
DIRECT = (
    "https://drive.usercontent.google.com/download"
    f"?id={DRIVE_ID}&export=download"
)


def fake_rclone(tmp_path: Path, *, link: str = "", rc: int = 0, err: str = "") -> Path:
    """A stub rclone that logs its argv and prints `link` for the link verb."""
    path = tmp_path / "fake-rclone"
    log = tmp_path / "argv.log"
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f'if [ "$1" = "link" ]; then printf "%s\\n" {link!r}; fi\n'
        f'if [ -n {err!r} ]; then printf "%s\\n" {err!r} >&2; fi\n'
        f"exit {rc}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def argv_lines(tmp_path: Path) -> list[str]:
    log = tmp_path / "argv.log"
    return log.read_text(encoding="utf-8").strip().splitlines() if log.exists() else []


class TestConfig:
    def test_url_dispatches_to_this_kind(self):
        assert detect_service_kind("rclone://gdrive/A8S") == "rclone"

    def test_requires_a_remote_name(self):
        with pytest.raises(ValueError, match="needs a remote name"):
            RcloneService("drive", url="rclone:///A8S")

    def test_rejects_unknown_option(self):
        with pytest.raises(ValueError, match="unknown option"):
            RcloneService("drive", url="rclone://gdrive/A8S", base_url="https://x/y")


class TestStore:
    def test_uploads_then_links_and_returns_a_direct_url(self, tmp_path):
        rclone = fake_rclone(tmp_path, link=f"https://drive.google.com/open?id={DRIVE_ID}")
        src = tmp_path / "memo.m4a"
        src.write_bytes(b"bytes")

        svc = RcloneService(
            "drive", url="rclone://gdrive/A8S", rclone_path=str(rclone)
        )
        assert svc.store(src) == DIRECT

        copyto, link = argv_lines(tmp_path)
        assert copyto.startswith(f"copyto {src} gdrive:A8S/a8s/")
        assert copyto.endswith("/memo.m4a")
        # Same object both times, and the per-file token keeps two attachments
        # with one name apart.
        assert link == f"link {copyto.split()[-1]}"

    def test_a_failing_rclone_reports_its_last_stderr_line(self, tmp_path):
        rclone = fake_rclone(tmp_path, rc=1, err="Failed to copy: quota exceeded")
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        svc = RcloneService("drive", url="rclone://gdrive/A8S", rclone_path=str(rclone))
        with pytest.raises(StorageError, match="quota exceeded"):
            svc.store(src)

    def test_missing_binary_names_the_option_that_fixes_it(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        svc = RcloneService(
            "drive", url="rclone://gdrive/A8S", rclone_path=str(tmp_path / "nope")
        )
        with pytest.raises(StorageError, match="rclone_path"):
            svc.store(src)

    def test_link_that_is_not_a_download_fails_loudly(self, tmp_path):
        """Storing a backend's preview page as the attachment would be silent
        corruption, so an unknown host is an error rather than a pass-through."""
        rclone = fake_rclone(tmp_path, link="https://www.dropbox.com/s/abc/a.txt?dl=0")
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        svc = RcloneService("drive", url="rclone://gdrive/A8S", rclone_path=str(rclone))
        with pytest.raises(StorageError, match="no known direct-download form"):
            svc.store(src)


class TestRetrieve:
    def test_declines_so_the_receiver_uses_a_plain_get(self, tmp_path):
        """The whole point: a receiving node needs no rclone and no credential."""
        svc = RcloneService("drive", url="rclone://gdrive/A8S")
        assert svc.retrieve(DIRECT, tmp_path / "out.bin") is False


class TestDirectDownloadUrl:
    @pytest.mark.parametrize(
        "link",
        [
            f"https://drive.google.com/open?id={DRIVE_ID}",
            f"https://drive.google.com/file/d/{DRIVE_ID}/view?usp=drivesdk",
        ],
    )
    def test_drive_link_shapes(self, link):
        assert direct_download_url(link) == DIRECT
