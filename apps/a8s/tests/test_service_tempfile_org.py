"""Tests for the TempFile.org storage service.

The fake HTTP server (`_fake_storage`) mimics tempfile.org's two
endpoints (`POST /api/upload/local` and `GET /<id>/download`) so the
round-trip behavior is exercised without real network calls. A separate
marker test verifies that an upload to a deliberately-bogus URL surfaces
`StorageError`."""
from __future__ import annotations

from pathlib import Path

import pytest

from services import StorageError
from services.tempfile_org import TempFileOrgService

from _fake_storage import free_port, start_fake_tempfile_server


@pytest.fixture
def fake_tempfile():
    """Spin up an in-process tempfile.org clone on a random port. Yields
    its base URL (e.g. `http://127.0.0.1:<port>`)."""
    server, base_url = start_fake_tempfile_server()
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()


# ---------- option-bag handling ----------


class TestTempFileOrgServiceOptions:
    def test_defaults(self):
        s = TempFileOrgService("t", url="https://tempfile.org")
        assert s.id == "t"
        assert s._expiry_hours == 24
        assert s._timeout_s == 30.0

    def test_custom_expiry(self):
        s = TempFileOrgService("t", url="https://tempfile.org", expiry_hours=6)
        assert s._expiry_hours == 6

    def test_string_expiry_coerced(self):
        # `network.json` values come through as strings (CLI parsing).
        s = TempFileOrgService("t", url="https://tempfile.org", expiry_hours="48")
        assert s._expiry_hours == 48

    def test_bad_expiry_raises(self):
        with pytest.raises(ValueError, match="expiry_hours must be one of"):
            TempFileOrgService("t", url="https://tempfile.org", expiry_hours=12)

    def test_unknown_option_raises(self):
        with pytest.raises(ValueError, match="unknown option"):
            TempFileOrgService("t", url="https://tempfile.org", boguskey="x")

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="unsupported scheme"):
            TempFileOrgService("t", url="ftp://tempfile.org")

    def test_missing_host_raises(self):
        with pytest.raises(ValueError, match="missing host"):
            TempFileOrgService("t", url="https://")


# ---------- supports_config_url dispatch ----------


class TestSupportsConfigUrl:
    def test_canonical_host(self):
        assert TempFileOrgService.supports_config_url("https://tempfile.org") is True

    def test_path_tolerated(self):
        assert TempFileOrgService.supports_config_url("https://tempfile.org/api") is True

    def test_www_subdomain(self):
        assert TempFileOrgService.supports_config_url("https://www.tempfile.org") is True

    def test_other_host(self):
        assert TempFileOrgService.supports_config_url("https://example.com") is False

    def test_non_http_scheme(self):
        assert TempFileOrgService.supports_config_url("ftp://tempfile.org") is False


# ---------- store / retrieve round-trip ----------


class TestRoundTrip:
    def test_upload_then_download_match(self, fake_tempfile, tmp_path):
        s = TempFileOrgService("t", url=fake_tempfile)
        src = tmp_path / "payload.bin"
        src.write_bytes(b"hello payload \x00\x01\x02")
        url = s.store(src)
        assert url.startswith(fake_tempfile)
        dest = tmp_path / "downloaded.bin"
        assert s.retrieve(url, dest) is True
        assert dest.read_bytes() == src.read_bytes()


class TestRetrieveDispatch:
    def test_foreign_host_returns_false(self, fake_tempfile, tmp_path):
        s = TempFileOrgService("t", url=fake_tempfile)
        # URL belongs to a host this service isn't configured for.
        dest = tmp_path / "downloaded.bin"
        assert s.retrieve("https://other.example/abc/", dest) is False
        assert not dest.exists()

    def test_already_has_download_suffix(self, fake_tempfile, tmp_path):
        # Tolerate either the trailing-slash form (what tempfile.org returns)
        # or an explicit `/download` suffix (operator-typed for testing).
        s = TempFileOrgService("t", url=fake_tempfile)
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x")
        landing_url = s.store(src)
        explicit = landing_url.rstrip("/") + "/download"
        dest = tmp_path / "downloaded.bin"
        assert s.retrieve(explicit, dest) is True
        assert dest.read_bytes() == b"x"


class TestErrorPaths:
    def test_unreachable_upload_raises(self, tmp_path):
        # No server on this port — `urlopen` raises URLError quickly.
        s = TempFileOrgService(
            "t", url=f"http://127.0.0.1:{free_port()}", timeout_s=0.5,
        )
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x")
        with pytest.raises(StorageError):
            s.store(src)

    def test_404_download_raises(self, fake_tempfile, tmp_path):
        # URL host matches the service but the file doesn't exist server-side.
        s = TempFileOrgService("t", url=fake_tempfile)
        dest = tmp_path / "downloaded.bin"
        with pytest.raises(StorageError, match="download HTTP 404"):
            s.retrieve(f"{fake_tempfile}/no-such-id/", dest)

    def test_missing_source_raises(self, fake_tempfile, tmp_path):
        s = TempFileOrgService("t", url=fake_tempfile)
        with pytest.raises(StorageError, match="cannot read"):
            s.store(tmp_path / "no-such-file.bin")
