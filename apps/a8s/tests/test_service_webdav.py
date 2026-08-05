"""Tests for WebDAV storage (in-process fake server)."""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from network import detect_service_kind
from services import StorageError
from services.webdav import WebdavService


class _DavHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.objects[self.path] = self.rfile.read(length)
        self.send_response(201)
        self.end_headers()

    def do_GET(self):
        data = self.server.objects.get(self.path)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_dav() -> tuple[ThreadingHTTPServer, str]:
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _DavHandler)
    server.objects = {}  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


class TestConfig:
    def test_detect_kind(self):
        assert detect_service_kind("webdav://h/dav") == "webdav"
        assert detect_service_kind("https://tempfile.org") == "tempfile_org"

    def test_requires_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            WebdavService("x", url="webdav://h/dav")


class TestRoundTrip:
    def test_put_returns_public_url_and_gets_back(self, tmp_path):
        server, dav = _start_dav()
        try:
            public = "https://files.example.com/a8s"
            svc = WebdavService(
                "fm",
                url=f"webdav://127.0.0.1:{server.server_address[1]}/dav",
                base_url=public,
                user="u",
                password="p",
            )
            svc._dav_base = f"http://127.0.0.1:{server.server_address[1]}/dav"
            src = tmp_path / "a.bin"
            src.write_bytes(b"webdav-bytes")
            url = svc.store(src)
            assert url.startswith(public + "/")
            dest = tmp_path / "out.bin"
            assert svc.retrieve(url, dest) is True
            assert dest.read_bytes() == b"webdav-bytes"
        finally:
            server.shutdown()
            server.server_close()

    def test_foreign_url_returns_false(self, tmp_path):
        svc = WebdavService(
            "fm",
            url="webdav://127.0.0.1:1/dav",
            base_url="https://files.example.com/a8s",
        )
        assert svc.retrieve("https://other.example/x", tmp_path / "a") is False
