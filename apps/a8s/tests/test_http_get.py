"""Tests for plain HTTP(S) storage downloads."""
from __future__ import annotations

from pathlib import Path

import pytest

from services import StorageError
from services.http_get import MAX_REDIRECTS, http_get_url_to_path
from _fake_storage import free_port, start_fake_tempfile_server


class TestHttpGetUrlToPath:
    def test_declines_non_http_schemes(self, tmp_path):
        dest = tmp_path / "a.bin"
        assert http_get_url_to_path("stub://x/1", dest) is False
        assert http_get_url_to_path("s3://bucket/key", dest) is False
        assert not dest.exists()

    def test_plaintext_http_is_refused_by_default(self, fake_home, tmp_path):
        """The conftest fixture opts tests into http so they can serve
        fixtures locally. Production does not: a peer picks this URL, and it
        carries its own authorization in the query string."""
        from settings import set_setting

        set_setting("storage_allow_http", "0")
        server, base = start_fake_tempfile_server()
        try:
            server.files["f0001"] = b"hello-bytes"
            dest = tmp_path / "out.bin"
            with pytest.raises(StorageError, match="refusing plaintext http"):
                http_get_url_to_path(f"{base}/f0001/download", dest)
            assert not dest.exists()
        finally:
            server.shutdown()
            server.server_close()

    def test_https_base_url_is_required_by_default(self, fake_home):
        from settings import set_setting
        from services.file_sync import FileSyncService

        set_setting("storage_allow_http", "0")
        with pytest.raises(ValueError, match="base_url must be https"):
            FileSyncService(
                "drive", url="file:///tmp/sync", base_url="http://cdn.example/a8s"
            )

    def test_downloads_from_http_server(self, tmp_path):
        server, base = start_fake_tempfile_server()
        try:
            server.files["f0001"] = b"hello-bytes"
            url = f"{base}/f0001/download"
            dest = tmp_path / "out.bin"
            assert http_get_url_to_path(url, dest) is True
            assert dest.read_bytes() == b"hello-bytes"
        finally:
            server.shutdown()
            server.server_close()

    def test_404_raises_storage_error(self, tmp_path):
        server, base = start_fake_tempfile_server()
        try:
            dest = tmp_path / "nope.bin"
            with pytest.raises(StorageError, match="HTTP 404"):
                http_get_url_to_path(f"{base}/missing/download", dest)
        finally:
            server.shutdown()
            server.server_close()

    def _hop_server(self, hops: int, body: bytes = b"final-bytes", scheme: str = ""):
        """A server that redirects `hops` times and then serves `body`."""
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        port = free_port()
        target_scheme = scheme or "http"

        class Hop(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                n = int(self.path.rsplit("/", 1)[-1])
                if n >= hops:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(302)
                self.send_header(
                    "Location", f"{target_scheme}://127.0.0.1:{port}/hop/{n + 1}"
                )
                self.end_headers()

        server = HTTPServer(("127.0.0.1", port), Hop)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{port}/hop/0"

    @pytest.mark.parametrize("hops", [1, 2, 3])
    def test_follows_redirects_within_the_limit(self, tmp_path, hops):
        """Object stores redirect a share URL to the host holding the bytes."""
        server, url = self._hop_server(hops)
        try:
            dest = tmp_path / f"out{hops}.bin"
            assert http_get_url_to_path(url, dest) is True
            assert dest.read_bytes() == b"final-bytes"
        finally:
            server.shutdown()
            server.server_close()

    def test_refuses_past_the_redirect_limit(self, tmp_path):
        server, url = self._hop_server(MAX_REDIRECTS + 1)
        try:
            dest = tmp_path / "out.bin"
            with pytest.raises(StorageError):
                http_get_url_to_path(url, dest)
            assert not dest.exists()
        finally:
            server.shutdown()
            server.server_close()

    def test_a_redirect_cannot_downgrade_to_plaintext(self, fake_home):
        """Each hop obeys the scheme rule, so a peer cannot use a redirect to
        step around the https requirement on the first URL.

        This drives the handler directly. Serving the https first hop that the
        end-to-end version needs would mean a certificate, and the rule under
        test is one branch."""
        import urllib.request
        from services.http_get import _LimitedRedirectHandler
        from settings import set_setting

        set_setting("storage_allow_http", "0")
        handler = _LimitedRedirectHandler()
        req = urllib.request.Request("https://store.example/object")

        def redirect_to(newurl):
            return handler.redirect_request(
                req, None, 302, "Found", {"location": newurl}, newurl
            )

        assert redirect_to("http://store.example/object") is None
        assert redirect_to("https://cdn.example/object") is not None

        set_setting("storage_allow_http", "1")
        assert redirect_to("http://store.example/object") is not None

    def test_a_redirect_to_a_foreign_scheme_is_refused(self, tmp_path):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        class ToFile(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "file:///etc/passwd")
                self.end_headers()

        port = free_port()
        server = HTTPServer(("127.0.0.1", port), ToFile)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            dest = tmp_path / "out.bin"
            with pytest.raises(StorageError, match="HTTP 302"):
                http_get_url_to_path(f"http://127.0.0.1:{port}/x", dest)
            assert not dest.exists()
        finally:
            server.shutdown()
            server.server_close()

    def test_max_bytes_enforced(self, tmp_path):
        server, base = start_fake_tempfile_server()
        try:
            server.files["big"] = b"x" * 100
            dest = tmp_path / "big.bin"
            with pytest.raises(StorageError, match="max_file_bytes"):
                http_get_url_to_path(
                    f"{base}/big/download", dest, max_bytes=10
                )
            assert not dest.exists()
        finally:
            server.shutdown()
            server.server_close()
