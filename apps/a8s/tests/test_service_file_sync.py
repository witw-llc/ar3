"""Tests for file_sync storage."""
from __future__ import annotations

from pathlib import Path

import pytest

from network import detect_service_kind
from services.file_sync import FileSyncService


class TestConfig:
    def test_detect_kind(self):
        assert detect_service_kind("file:///tmp/sync") == "file_sync"
        assert detect_service_kind("s3://b") == "s3"

    def test_requires_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            FileSyncService("x", url="file:///tmp/x")

    def test_unknown_option(self):
        with pytest.raises(ValueError, match="unknown option"):
            FileSyncService(
                "x",
                url="file:///tmp/x",
                base_url="https://cdn.example/a8s",
                nope=1,
            )


class TestRoundTrip:
    def test_store_and_local_retrieve(self, tmp_path):
        root = tmp_path / "sync"
        root.mkdir()
        public = "http://127.0.0.1:9/a8s"
        svc = FileSyncService(
            "drive",
            url=f"file://{root}",
            base_url=public,
        )
        src = tmp_path / "memo.txt"
        src.write_text("payload", encoding="utf-8")
        url = svc.store(src)
        assert url.startswith(public + "/")
        dest = tmp_path / "out" / "memo.txt"
        assert svc.retrieve(url, dest) is True
        assert dest.read_text(encoding="utf-8") == "payload"
