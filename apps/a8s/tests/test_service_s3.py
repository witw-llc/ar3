"""Tests for the S3 storage service.

boto3 is an optional dependency and is not installed in CI, so every test
here drives a fake client injected into the service's client cache. That
keeps the suite honest about our own logic — key naming, URL matching,
error translation — without pretending to test AWS.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from network import detect_service_kind
from services import StorageError
from services.s3 import S3Service


class FakeClient:
    """Records calls and lets a test force a failure."""

    def __init__(self, *, fail: str = "") -> None:
        self.uploads: list[tuple[str, str, str]] = []
        self.downloads: list[tuple[str, str, str]] = []
        self.presigned: list[dict] = []
        self.fail = fail

    def upload_file(self, src, bucket, key):
        if self.fail == "upload":
            raise RuntimeError("boom")
        self.uploads.append((src, bucket, key))

    def generate_presigned_url(self, op, *, Params, ExpiresIn):
        self.presigned.append({"op": op, "params": Params, "expires": ExpiresIn})
        return (
            f"https://{Params['Bucket']}.s3.us-east-1.amazonaws.com/"
            f"{Params['Key']}?X-Amz-Signature=deadbeef"
        )

    def download_file(self, bucket, key, dest):
        if self.fail == "download":
            raise RuntimeError("nope")
        self.downloads.append((bucket, key, dest))
        Path(dest).write_text("payload", encoding="utf-8")


def _service(**opts) -> tuple[S3Service, FakeClient]:
    fail = opts.pop("_fail", "")
    svc = S3Service("mybucket", url=opts.pop("url", "s3://my-bucket"), **opts)
    client = FakeClient(fail=fail)
    svc._client_cache = client
    return svc, client


class TestConfigUrl:
    def test_accepts_s3_scheme(self):
        assert S3Service.supports_config_url("s3://my-bucket")
        assert S3Service.supports_config_url("  s3://my-bucket/prefix  ")

    def test_rejects_other_schemes(self):
        assert not S3Service.supports_config_url("https://tempfile.org")
        assert not S3Service.supports_config_url("my-bucket")

    def test_detect_service_kind_routes_to_s3(self):
        assert detect_service_kind("s3://my-bucket") == "s3"
        assert detect_service_kind("https://tempfile.org") == "tempfile_org"

    def test_url_without_bucket_is_rejected(self):
        with pytest.raises(ValueError, match="must name a bucket"):
            S3Service("x", url="s3://")

    def test_unknown_option_raises(self):
        with pytest.raises(ValueError, match="unknown option"):
            S3Service("x", url="s3://b", regionn="us-east-1")

    def test_presign_hours_bounds(self):
        with pytest.raises(ValueError, match="presign_hours"):
            S3Service("x", url="s3://b", presign_hours=0)
        with pytest.raises(ValueError, match="presign_hours"):
            S3Service("x", url="s3://b", presign_hours=169)

    def test_timeout_must_be_positive(self):
        with pytest.raises(ValueError, match="timeout_s"):
            S3Service("x", url="s3://b", timeout_s=0)

    def test_id_is_the_configured_name(self):
        svc, _ = _service()
        assert svc.id == "mybucket"


class TestKeyNaming:
    def test_prefix_comes_from_the_url_path(self, tmp_path):
        svc, client = _service(url="s3://my-bucket/team/files")
        src = tmp_path / "a.m4a"
        src.write_text("x", encoding="utf-8")
        svc.store(src)
        assert client.uploads[0][2].startswith("team/files/")
        assert client.uploads[0][2].endswith("/a.m4a")

    def test_explicit_prefix_wins_over_the_url(self, tmp_path):
        svc, client = _service(url="s3://my-bucket/ignored", prefix="chosen")
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        svc.store(src)
        assert client.uploads[0][2].startswith("chosen/")

    def test_default_prefix_when_url_has_none(self, tmp_path):
        svc, client = _service()
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        svc.store(src)
        assert client.uploads[0][2].startswith("a8s/")

    def test_keys_are_unique_per_upload(self, tmp_path):
        svc, client = _service()
        src = tmp_path / "same.txt"
        src.write_text("x", encoding="utf-8")
        svc.store(src)
        svc.store(src)
        assert client.uploads[0][2] != client.uploads[1][2]

    def test_directory_components_are_stripped_from_the_name(self, tmp_path):
        svc, client = _service()
        nested = tmp_path / "sub"
        nested.mkdir()
        src = nested / "b.txt"
        src.write_text("x", encoding="utf-8")
        svc.store(src)
        assert client.uploads[0][2].endswith("/b.txt")
        assert "sub" not in client.uploads[0][2]


class TestStore:
    def test_returns_a_presigned_url(self, tmp_path):
        svc, client = _service()
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        url = svc.store(src)
        assert url.startswith("https://my-bucket.s3.")
        assert "X-Amz-Signature" in url
        assert client.presigned[0]["expires"] == 24 * 3600

    def test_presign_hours_option_is_honored(self, tmp_path):
        svc, client = _service(presign_hours=6)
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        svc.store(src)
        assert client.presigned[0]["expires"] == 6 * 3600

    def test_upload_failure_becomes_storage_error(self, tmp_path):
        svc, _ = _service(_fail="upload")
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        with pytest.raises(StorageError, match="s3 upload failed"):
            svc.store(src)


class TestRetrieve:
    def test_round_trips_its_own_presigned_url(self, tmp_path):
        svc, _ = _service()
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        url = svc.store(src)
        dest = tmp_path / "out" / "a.txt"
        assert svc.retrieve(url, dest) is True
        assert dest.read_text(encoding="utf-8") == "payload"

    def test_accepts_an_s3_uri(self, tmp_path):
        svc, client = _service()
        dest = tmp_path / "a.txt"
        assert svc.retrieve("s3://my-bucket/a8s/abc/a.txt", dest) is True
        assert client.downloads[0][1] == "a8s/abc/a.txt"

    def test_accepts_path_style_urls(self, tmp_path):
        svc, client = _service()
        dest = tmp_path / "a.txt"
        url = "https://s3.us-west-2.amazonaws.com/my-bucket/a8s/abc/a.txt"
        assert svc.retrieve(url, dest) is True
        assert client.downloads[0][1] == "a8s/abc/a.txt"

    def test_percent_escapes_are_decoded(self, tmp_path):
        svc, client = _service()
        dest = tmp_path / "a.m4a"
        svc.retrieve("s3://my-bucket/a8s/abc/my%20memo.m4a", dest)
        assert client.downloads[0][1] == "a8s/abc/my memo.m4a"

    def test_declines_another_bucket(self, tmp_path):
        svc, client = _service()
        assert svc.retrieve("s3://other-bucket/k", tmp_path / "a") is False
        assert svc.retrieve(
            "https://other.s3.us-east-1.amazonaws.com/k", tmp_path / "a"
        ) is False
        assert client.downloads == []

    def test_declines_a_foreign_url(self, tmp_path):
        svc, _ = _service()
        assert svc.retrieve("https://tempfile.org/abc/download", tmp_path / "a") is False

    def test_declines_a_bucket_url_with_no_key(self, tmp_path):
        svc, _ = _service()
        assert svc.retrieve("s3://my-bucket/", tmp_path / "a") is False

    def test_download_failure_becomes_storage_error(self, tmp_path):
        svc, _ = _service(_fail="download")
        with pytest.raises(StorageError, match="s3 download failed"):
            svc.retrieve("s3://my-bucket/a8s/abc/a.txt", tmp_path / "a")

    def test_creates_the_destination_directory(self, tmp_path):
        svc, _ = _service()
        dest = tmp_path / "deep" / "nested" / "a.txt"
        assert svc.retrieve("s3://my-bucket/a8s/abc/a.txt", dest) is True
        assert dest.is_file()


class TestMissingBoto3:
    def test_names_the_install_command(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "boto3", None)
        svc = S3Service("x", url="s3://my-bucket")
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        with pytest.raises(StorageError, match="requirements/a8s-s3.txt"):
            svc.store(src)
