"""Shared in-process fake of TempFile.org's HTTP API for tests.

`POST /api/upload/local` accepts a multipart `files` field and returns
`{"files": [{"id", "name", "size", "url", "expiryTime"}]}` where `url`
points back at this same server. `GET /<id>/download` streams the
stashed bytes. Used by:
  - test_service_tempfile_org.py — round-trip + error-path coverage.
  - test_remote_e2e.py            — cross-cluster FILE: delivery via the
                                    full sender-side route_outboxes →
                                    MQTT → receive_envelope path.
"""
from __future__ import annotations

import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):
        if self.path != "/api/upload/local":
            self.send_error(404)
            return
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=(.+)$", ctype)
        if not m:
            self.send_error(400, "missing boundary")
            return
        boundary = m.group(1).encode()
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        parts = body.split(b"--" + boundary)
        file_bytes: bytes | None = None
        for part in parts:
            if b'name="files"' in part:
                _, _, rest = part.partition(b"\r\n\r\n")
                file_bytes = rest.rsplit(b"\r\n", 1)[0]
                break
        if file_bytes is None:
            self.send_error(400, "missing files field")
            return
        file_id = f"f{len(self.server.files):04d}"
        self.server.files[file_id] = file_bytes
        port = self.server.server_address[1]
        url = f"http://127.0.0.1:{port}/{file_id}/"
        body_out = json.dumps({
            "files": [{
                "id": file_id, "name": "x", "size": len(file_bytes),
                "url": url, "expiryTime": 0,
            }]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

    def do_GET(self):
        m = re.match(r"^/(?P<id>[^/]+)/download$", self.path)
        if not m:
            self.send_error(404)
            return
        file_id = m.group("id")
        if file_id not in self.server.files:
            self.send_error(404, "unknown id")
            return
        data = self.server.files[file_id]
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_fake_tempfile_server() -> tuple[ThreadingHTTPServer, str]:
    """Spin up the fake server on a random port. Returns
    (server_handle, base_url). Caller stops via:
        server.shutdown(); server.server_close()."""
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.files = {}  # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"
