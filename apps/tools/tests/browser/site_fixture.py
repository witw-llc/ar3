"""A small site for the recipe tests, served from fixtures/site on 127.0.0.1 and a port of its own.

It has a sign-in form (a right user and password set a cookie and go to the list), a list of items
that fills itself from a JSON endpoint, an item page, a sign-out link, a public JSON endpoint, an
endpoint that echoes a POST, a redirect to the site's own public endpoint and one to another origin,
a link to that other origin, and a public page taller than a window. Two sites on two ports are two origins: the second one is the
off-site target. Every request is recorded, so a test can show that a refused call made none.
"""
import html
import http.server
import json
import secrets
import threading
import urllib.parse
from pathlib import Path

SITE = Path(__file__).resolve().parent / "fixtures" / "site"
NODE = Path(__file__).resolve().parent / "fixtures" / "node"
# Made-up values. The user name and the password share no text with a word the pages print.
USER = "testuser-q7"
PASSWORD = "pw-Z4k!9-live"
COOKIE = "fixture_session"


def _page(name, **fields):
    text = (SITE / name).read_text(encoding="utf-8")
    for key, value in fields.items():
        text = text.replace("{{" + key + "}}", value)
    return text.encode("utf-8")


class Site:
    """`other` is the base URL of the site the list page links to."""

    def __init__(self, other="http://127.0.0.1:9", user=USER, password=PASSWORD):
        self.other, self.user, self.password = other, user, password
        self.items = json.loads((SITE / "items.json").read_text(encoding="utf-8"))
        self.hits = []
        self.sessions = set()
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, kind="text/html; charset=utf-8", headers=()):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, location, headers=()):
                self._send(302, b"", headers=[("Location", location), *headers])

            def _signed_in(self):
                for part in self.headers.get("Cookie", "").split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == COOKIE and value in site.sessions:
                        return True
                return False

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                site.hits.append(("GET", path))
                if path == "/":
                    self._send(200, b"<!doctype html><title>Another site</title><h1>Another site</h1>")
                elif path == "/signin":
                    self._send(200, _page("signin.html", NOTICE=""))
                elif path == "/signout":
                    site.sessions.clear()
                    self._redirect("/signin", [("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0")])
                elif path == "/tall":
                    self._send(200, _page("tall.html"))
                elif path == "/public.json":
                    self._send(200, b'{"public": true}', "application/json")
                elif path == "/redirect-in":
                    self._redirect("/public.json")
                elif path == "/redirect-out":
                    self._redirect(site.other + "/")
                elif path in ("/list", "/api/items") or path.startswith("/item/"):
                    self._members(path)
                else:
                    self._send(404, b"not found", "text/plain")

            def _members(self, path):
                if not self._signed_in():
                    if path == "/api/items":
                        self._send(401, b'{"error": "signed out"}', "application/json")
                    else:
                        self._redirect("/signin")
                elif path == "/list":
                    self._send(200, _page("list.html", OTHER=site.other))
                elif path == "/api/items":
                    brief = [{"id": item["id"], "title": item["title"]} for item in site.items]
                    self._send(200, json.dumps({"items": brief}).encode(), "application/json")
                else:
                    found = [item for item in site.items if f"/item/{item['id']}" == path]
                    if not found:
                        self._send(404, b"no such item", "text/plain")
                    else:
                        self._send(200, _page(
                            "item.html", TITLE=html.escape(found[0]["title"]), BODY=html.escape(found[0]["body"]),
                        ))

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                site.hits.append(("POST", path))
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                form = urllib.parse.parse_qs(raw.decode("utf-8"))
                if path == "/echo":
                    self._send(200, json.dumps({
                        "body": raw.decode("utf-8"), "type": self.headers.get("Content-Type"),
                        "token": self.headers.get("X-Token"),
                    }).encode(), "application/json")
                elif path != "/signin":
                    self._send(404, b"not found", "text/plain")
                elif form.get("user") == [site.user] and form.get("pass") == [site.password]:
                    token = secrets.token_hex(8)
                    site.sessions.add(token)
                    self._redirect("/list", [("Set-Cookie", f"{COOKIE}={token}; Path=/; HttpOnly")])
                else:
                    self._send(401, _page("signin.html", NOTICE="Sign in failed."))

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def paths(self):
        return [path for _method, path in self.hits]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
