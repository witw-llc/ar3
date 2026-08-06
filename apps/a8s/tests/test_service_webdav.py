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
    """A fake that keeps the two rules a real WebDAV server enforces.

    PUT does not create parent collections — it answers 409 when the
    directory is absent — and MKCOL answers 405 when it is already there.
    A fake that accepts any PUT lets a client ship that is broken against
    every real server.
    """

    def log_message(self, format, *args):
        return

    def _parent(self) -> str:
        return self.path.rsplit("/", 1)[0]

    def do_MKCOL(self):
        if self._parent() not in self.server.collections:
            self.send_error(409)
            return
        if self.path in self.server.collections:
            self.send_error(405)
            return
        self.server.collections.add(self.path)
        self.send_response(201)
        self.end_headers()

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self._parent() not in self.server.collections:
            self.send_error(409)
            return
        self.server.objects[self.path] = body
        self.send_response(201)
        self.end_headers()

    def do_DELETE(self):
        if self.path in self.server.objects:
            del self.server.objects[self.path]
        elif self.path.rstrip("/") in self.server.collections:
            self.server.collections.discard(self.path.rstrip("/"))
        else:
            self.send_error(404)
            return
        self.send_response(204)
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
    server.collections = {"/dav"}  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def _svc(server, **opts) -> WebdavService:
    port = server.server_address[1]
    svc = WebdavService(
        "fm",
        url=f"webdav://127.0.0.1:{port}/dav",
        base_url=opts.pop("base_url", "https://files.example.com/a8s"),
        user="u",
        password="p",
        **opts,
    )
    svc._dav_base = f"http://127.0.0.1:{port}/dav"
    return svc


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
            svc = _svc(server, base_url=public)
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

    def test_store_creates_the_missing_collections(self, tmp_path):
        # Every key carries a fresh random directory, so a client that leaves
        # the collections to the server uploads nothing at all.
        server, dav = _start_dav()
        try:
            svc = _svc(server, prefix="deep/nested")
            src = tmp_path / "a.bin"
            src.write_bytes(b"x")
            svc.store(src)
            made = [c for c in server.collections if c != "/dav"]
            assert "/dav/deep" in made and "/dav/deep/nested" in made
            assert len(made) == 3  # prefix, its parent, and the random one
        finally:
            server.shutdown()
            server.server_close()

    def test_a_collection_is_made_once_per_process(self, tmp_path):
        server, dav = _start_dav()
        try:
            svc = _svc(server)
            src = tmp_path / "a.bin"
            src.write_bytes(b"x")
            svc.store(src)
            svc.store(src)
            # Shared prefix made once; one random directory per upload.
            assert len([c for c in server.collections if c != "/dav"]) == 3
        finally:
            server.shutdown()
            server.server_close()

    def test_a_filename_with_spaces_round_trips(self, tmp_path):
        # Voice memos and screenshots arrive named with spaces. An unescaped
        # space is not a legal request target, so the PUT never leaves.
        server, dav = _start_dav()
        try:
            svc = _svc(server)
            src = tmp_path / "72nd Ave NE 52.m4a"
            src.write_bytes(b"memo")
            url = svc.store(src)
            assert url.endswith("/72nd%20Ave%20NE%2052.m4a")
            dest = tmp_path / "out" / "72nd Ave NE 52.m4a"
            assert svc.retrieve(url, dest) is True
            assert dest.read_bytes() == b"memo"
        finally:
            server.shutdown()
            server.server_close()

    def test_a_blank_prefix_puts_objects_at_the_top(self, tmp_path):
        # The operator pointed the service at a folder that is already for
        # a8s alone; a second `a8s` level below it is noise.
        server, dav = _start_dav()
        try:
            svc = _svc(server, prefix="")
            src = tmp_path / "a.bin"
            src.write_bytes(b"x")
            url = svc.store(src)
            key = url.removeprefix("https://files.example.com/a8s/")
            assert key.count("/") == 1 and not key.startswith("a8s/")
        finally:
            server.shutdown()
            server.server_close()

    def test_delete_removes_the_object_and_its_directory(self, tmp_path):
        # `a8s health` runs often. Leaving the empty per-object collection
        # behind would grow the folder forever even with the file gone.
        server, dav = _start_dav()
        try:
            svc = _svc(server)
            src = tmp_path / "a.bin"
            src.write_bytes(b"x")
            url = svc.store(src)
            assert svc.delete(url) is True
            assert server.objects == {}
            assert [c for c in server.collections if c != "/dav"] == ["/dav/a8s"]
        finally:
            server.shutdown()
            server.server_close()

    def test_delete_declines_a_foreign_url(self, tmp_path):
        server, dav = _start_dav()
        try:
            assert _svc(server).delete("https://other.example/x/y.bin") is False
        finally:
            server.shutdown()
            server.server_close()

    def test_a_deleted_directory_is_remade_on_the_next_store(self, tmp_path):
        # The created-collections cache must forget what delete removed.
        server, dav = _start_dav()
        try:
            svc = _svc(server, prefix="p")
            src = tmp_path / "a.bin"
            src.write_bytes(b"x")
            svc.delete(svc.store(src))
            url = svc.store(src)
            assert svc.retrieve(url, tmp_path / "out" / "a.bin") is True
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


class TestProbeCleanup:
    def test_a_store_that_expires_needs_no_delete(self):
        from services.tempfile_org import TempFileOrgService

        assert TempFileOrgService("t", url="https://tempfile.org").objects_expire is True

    def test_an_operator_owned_store_does_not_expire(self):
        server, dav = _start_dav()
        try:
            assert _svc(server).objects_expire is False
        finally:
            server.shutdown()
            server.server_close()
