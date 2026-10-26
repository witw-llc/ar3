"""The addresses of the open pages, read from Chrome's DevTools endpoint (`session.pages`).

A step looks at every page after it ran. Asking the driver for the address costs a call of about a second, and
the endpoint answers in a millisecond. What answers is the seat's own Chrome when the path of the browser
endpoint it reports is the one Chrome wrote into the seat's profile (`DevToolsActivePort`): no other process
knows it. These tests run a small server that answers as Chrome does. test_local_files_live.py asks a real one.
"""
import http.server
import json
import threading
import time

import pytest

from browser import session

BROWSER_PATH = "/devtools/browser/6f9c1d85-4343-4e63-8caa-60806119722e"


class FakeChrome:
    """Answers `/json/version`, `/json` and `/json/close/<id>` on a loopback port of its own."""

    def __init__(self, path=BROWSER_PATH):
        self.path = path
        self.targets = [
            {"id": "AAA1", "type": "page", "url": "https://app.example.com/a", "title": "a"},
            {"id": "BBB2", "type": "browser_ui", "url": "chrome://omnibox-popup.top-chrome/"},
            {"id": "CCC3", "type": "service_worker", "url": "chrome-extension://x/service_worker.js"},
            {"id": "DDD4", "type": "page", "url": "file:///etc/hostname"},
        ]
        self.requests = []
        self.delay = 0
        self.raw = None  # a body to send for `/json` instead of the targets
        chrome = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                chrome.requests.append(self.path)
                time.sleep(chrome.delay)
                if self.path == "/json/version":
                    body = json.dumps({"Browser": "Chrome/155", "webSocketDebuggerUrl": f"ws://127.0.0.1:{chrome.port}{chrome.path}"})
                elif self.path == "/json":
                    body = chrome.raw if chrome.raw is not None else json.dumps(chrome.targets)
                elif self.path.startswith("/json/close/"):
                    wanted = self.path.rsplit("/", 1)[1]
                    chrome.targets = [target for target in chrome.targets if target["id"] != wanted]
                    body = "Target is closing"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def record(self, seat="seat", lines=None):
        """Write the file Chrome writes in its profile: the port, then the path of the browser endpoint."""
        import os

        os.makedirs(session.profile_dir(seat), exist_ok=True)
        with open(session._port_file(seat), "w") as handle:
            handle.write("\n".join([str(self.port), self.path] if lines is None else lines) + "\n")

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def chrome():
    fake = FakeChrome()
    fake.record()
    yield fake
    fake.stop()


def test_the_pages_are_the_targets_of_type_page_with_their_ids_and_addresses(chrome):
    assert session.pages("seat") == [("AAA1", "https://app.example.com/a"), ("DDD4", "file:///etc/hostname")]


def test_it_costs_two_requests_and_no_process(chrome, monkeypatch):
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: pytest.fail("a process was started"))
    assert session.pages("seat")
    assert chrome.requests == ["/json/version", "/json"]


def test_it_is_fast_enough_to_run_after_every_step(chrome):
    session.pages("seat")
    started = time.perf_counter()
    for _ in range(20):
        session.pages("seat")
    assert (time.perf_counter() - started) / 20 < 0.1


def test_a_proxy_in_the_environment_is_not_asked_about_the_loopback(chrome, monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert session.pages("seat") is not None


def test_a_seat_with_no_record_of_a_port_has_no_answer():
    assert session.pages("seat") is None


@pytest.mark.parametrize("lines", [[], ["not a number"], ["41234"], ["41234", ""], ["0", BROWSER_PATH]])
def test_a_record_that_does_not_give_a_port_and_a_path_has_no_answer(chrome, lines):
    chrome.record(lines=lines)
    assert session.pages("seat") is None


def test_what_answers_with_another_browser_path_is_not_the_seats_chrome(chrome):
    """A process that took the port when the Chrome of the seat was gone cannot know the path in the profile."""
    chrome.path = "/devtools/browser/00000000-0000-0000-0000-000000000000"
    chrome.record(lines=[str(chrome.port), BROWSER_PATH])
    assert session.pages("seat") is None
    assert chrome.requests == ["/json/version"], "the list of pages was never asked for"


def test_nothing_listening_on_the_recorded_port_has_no_answer():
    gone = FakeChrome()
    gone.record()
    gone.stop()
    assert session.pages("seat") is None


def test_a_slow_answer_is_no_answer(chrome, monkeypatch):
    monkeypatch.setattr(session, "CDP_ANSWER_SECONDS", 0.2)
    chrome.delay = 1.0
    started = time.monotonic()
    assert session.pages("seat") is None
    assert time.monotonic() - started < 0.9


@pytest.mark.parametrize("raw", ["not json", "{}", "null", '"text"', "[1, 2]", "[]"])
def test_an_answer_that_is_not_a_list_of_targets_gives_no_page_it_cannot_read(chrome, raw):
    chrome.raw = raw
    found = session.pages("seat")
    assert found in (None, [])


def test_a_target_without_an_address_is_left_out(chrome):
    chrome.raw = json.dumps([{"id": "A", "type": "page"}, {"id": "B", "type": "page", "url": 7}, {"id": "C", "type": "page", "url": "about:blank"}])
    assert session.pages("seat") == [("C", "about:blank")]


def test_a_page_is_closed_by_its_id_through_the_same_endpoint(chrome):
    assert session.close_page("seat", "DDD4") is True
    assert chrome.requests[-1] == "/json/close/DDD4"
    assert [page_id for page_id, _url in session.pages("seat")] == ["AAA1"]


def test_a_page_is_not_closed_when_the_endpoint_is_not_the_seats_chrome(chrome):
    chrome.path = "/devtools/browser/other"
    assert session.close_page("seat", "DDD4") is False
    assert not any(request.startswith("/json/close") for request in chrome.requests)


@pytest.mark.parametrize("page_id", ["", "../json/version", "A/../B", "a b", None, "A?x=1"])
def test_an_id_that_is_not_an_id_is_never_sent(chrome, page_id):
    assert session.close_page("seat", page_id) is False
    assert chrome.requests == ["/json/version"] or chrome.requests == []


def test_a_close_that_the_endpoint_cannot_take_is_false():
    assert session.close_page("seat", "DDD4") is False


# --- a port the owner fixed: Chrome writes no record, and the proof is the one the attach uses ------------------


def test_with_a_fixed_port_and_no_record_the_attach_proof_names_the_chrome(chrome, monkeypatch):
    import os

    os.remove(session._port_file("seat"))
    monkeypatch.setenv("AR3_BROWSER_PORT", str(chrome.port))
    monkeypatch.setattr(session, "cdp_base", lambda seat: f"http://127.0.0.1:{chrome.port}")
    assert [url for _page_id, url in session.pages("seat")] == ["https://app.example.com/a", "file:///etc/hostname"]
    assert session.close_page("seat", "DDD4") is True


def test_with_a_fixed_port_that_nobody_of_the_seat_holds_there_is_no_answer(chrome, monkeypatch):
    import os

    os.remove(session._port_file("seat"))
    monkeypatch.setenv("AR3_BROWSER_PORT", str(chrome.port))
    monkeypatch.setattr(session, "cdp_base", lambda seat: None)
    assert session.pages("seat") is None


def test_a_fixed_port_that_clashes_with_the_record_gives_no_answer_and_no_error(chrome, monkeypatch):
    monkeypatch.setenv("AR3_BROWSER_PORT", str(chrome.port + 1))

    def clash(seat):
        raise session.plc.BrowserError("seat runs Chrome on another port")

    monkeypatch.setattr(session, "cdp_base", clash)
    chrome.record(lines=[])  # an empty record is no record
    assert session.pages("seat") is None


def test_a_record_that_does_not_check_out_is_not_rescued_by_the_fixed_port(chrome, monkeypatch):
    """The record names a Chrome that is not there. A fixed port does not make the other answer trusted."""
    chrome.path = "/devtools/browser/other"
    chrome.record(lines=[str(chrome.port), BROWSER_PATH])
    monkeypatch.setenv("AR3_BROWSER_PORT", str(chrome.port))
    monkeypatch.setattr(session, "cdp_base", lambda seat: pytest.fail("the full proof was asked after a bad record"))
    assert session.pages("seat") is None


class _Garbage:
    """A server on a loopback port that answers with bytes that are not HTTP, then closes."""

    def __init__(self, reply):
        import socket

        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        self.reply = reply
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                connection, _address = self.listener.accept()
            except OSError:
                return
            try:
                connection.recv(4096)
                connection.sendall(self.reply)
            except OSError:
                pass
            finally:
                connection.close()

    def stop(self):
        self.listener.close()


@pytest.mark.parametrize("reply", [b"not http at all\r\n\r\n", b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\nshort", b"\xff\xfe\x00", b""])
def test_an_endpoint_that_speaks_something_other_than_http_is_no_answer_and_no_error(reply):
    garbage = _Garbage(reply)
    try:
        import os

        os.makedirs(session.profile_dir("seat"), exist_ok=True)
        with open(session._port_file("seat"), "w") as handle:
            handle.write(f"{garbage.port}\n{BROWSER_PATH}\n")
        assert session.pages("seat") is None
        assert session.close_page("seat", "DDD4") is False
    finally:
        garbage.stop()


def test_a_body_that_is_not_text_is_no_answer_and_no_error(chrome):
    chrome.raw = None
    chrome.targets = None  # json.dumps(None) is "null": a body that is not a list

    assert session.pages("seat") is None
