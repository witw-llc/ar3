"""The page facade: every call checks the stop epoch, writes a scrubbed transcript line and keeps the
recipe on its origins, and nothing it returns holds a stored secret.

The calls run on a fake browser (fake_browser.py); `http` runs against the fixture site on 127.0.0.1
(site_fixture.py), so a refused call can be shown to have sent no request.
"""
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

from browser import commands
from browser import page as page_module
from browser import plc
from browser import recipe
from browser import secretstore
from browser import session
from browser.recipe import Recipe, UsageError

from fake_browser import FakeBrowser, static_library
from site_fixture import Site
from test_plc import CHECKS, NODE, needs_node

VALUE = "hunter2-value"
MARK = "[secret:PASSWORD]"
ORIGIN = "https://app.example.com"
STORE = {"PASSWORD": {"value": VALUE, "origins": [ORIGIN]}}


def shop(**kwargs):
    kwargs.setdefault("origins", [ORIGIN])
    kwargs.setdefault("env", ["SHOP_BASE"])
    kwargs.setdefault("secrets", ["PASSWORD"])
    return Recipe("shop", "A shop", **kwargs)


def make_page(rec=None, store=None, seat="seat", allow_eval=False):
    """A Page the way the runner builds it, outside any script."""
    rec = rec or shop()
    run = commands.Run(seat, secretstore.Scrubber(store) if store else None)
    call = recipe._Call(seat, run, allow_eval, session.stop_epoch(seat), static_library(rec))
    return page_module.Page(call, rec), call, run


@pytest.fixture
def browser(monkeypatch):
    fake = FakeBrowser(monkeypatch)
    fake.url = f"{ORIGIN}/"
    return fake


@pytest.fixture
def sites(monkeypatch):
    """Two sites, two ports, two origins: the first links to the second. No proxy stands between them and urllib."""
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    other = Site()
    main = Site(other=other.base)
    yield main, other
    main.stop()
    other.stop()


# --- the origin check: the matcher ---------------------------------------------------------------------


def parts(*origins):
    return [secretstore.origin_parts(secretstore.parse_origin(origin)) for origin in origins]


@pytest.mark.parametrize("allowed, seen, matches", CHECKS)
def test_the_python_matcher_agrees_with_the_check_the_page_makes_before_typing_a_secret(allowed, seen, matches):
    """The same table pins both: the in-page check of fill-secret and the check a recipe's URLs get."""
    url = seen + "/some/path?x=1#frag" if seen.startswith(("http://", "https://")) else seen
    assert secretstore.url_on_origins(url, parts(*allowed)) is matches


@pytest.mark.parametrize(
    "url",
    [
        "https://app.example.com@evil.example/",
        "https://evil.example\\@app.example.com/",
        "https://app.example.com\\.evil.example/",
        "https://evil.example/\\@app.example.com",
        "https://app.example.com /x",
        "https://app.example.com\t.evil.example/",
        "https://app.example.com\n/x",
        "https://app.example.com\r\n/x",
        "https://app.example.com\x00/x",
        "https://apр.example.com/",
        "https://app.example.com./",
        "https://%61pp.example.com/",
        "https://app.example.com:99999/",
        "https://app.example.com:abc/",
        "https://[::1/",
        "https:///app.example.com/",
        "https:app.example.com/",
        "//app.example.com/x",
        "/relative",
        "",
        None,
        b"https://app.example.com/",
        7,
    ],
)
def test_a_url_that_a_browser_and_a_library_could_read_two_ways_is_not_on_any_origin(url):
    assert secretstore.url_on_origins(url, parts(ORIGIN)) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://app.example.com",
        "https://app.example.com/",
        "https://app.example.com/a/b?c=d#e",
        "HTTPS://APP.EXAMPLE.COM/x",
        "https://app.example.com:443/x",
        "https://app.example.com/%61",
        "https://app.example.com/a@b",
        "https://app.example.com/?next=https://evil.example",
    ],
)
def test_a_url_on_an_origin_is_on_it_whatever_its_path_query_and_fragment_hold(url):
    assert secretstore.url_on_origins(url, parts(ORIGIN)) is True


def test_a_default_port_is_the_same_origin_with_or_without_it():
    assert secretstore.url_on_origins("http://a.example.com:80/", parts("http://a.example.com"))
    assert secretstore.url_on_origins("http://a.example.com/", parts("http://a.example.com:80"))
    assert not secretstore.url_on_origins("https://a.example.com:80/", parts("https://a.example.com"))
    assert secretstore.url_on_origins("http://[::1]:3000/x", parts("http://[0:0:0:0:0:0:0:1]:3000"))


def test_no_origin_matches_nothing():
    assert secretstore.url_on_origins("https://app.example.com/", []) is False


# --- the origin check: go, fetch, http, new_tab ---------------------------------------------------------------

REFUSED = [
    "https://evil.example/",
    "http://app.example.com/",
    "https://app.example.com:8443/",
    "https://sub.app.example.com/",
    "https://app.example.com.evil.example/",
    "https://evilapp.example.com/",
    "file:///etc/passwd",
    "about:blank",
    "data:text/html,x",
    "javascript:alert(1)",
    "ftp://app.example.com/",
    "https://app.example.com@evil.example/",
    "https://evil.example\\@app.example.com/",
    "http://[::1",
    "/relative",
    "",
]


@pytest.mark.parametrize("url", REFUSED)
def test_go_outside_the_origins_fails_before_any_request(browser, url):
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="go: refusing .*outside the origins of recipe shop"):
        page.go(url)
    assert browser.calls == []
    assert browser.url == f"{ORIGIN}/"


@pytest.mark.parametrize("url", ["https://app.example.com/a", "https://APP.EXAMPLE.COM/x", "https://app.example.com:443/"])
def test_go_on_the_origins_goes(browser, url):
    page, call, run = make_page()
    assert page.go(url) == url
    assert [c[0] for c in browser.calls] == ["goto"]


def test_a_wildcard_origin_covers_the_subdomains_and_not_the_domain(browser):
    page, call, run = make_page(shop(origins=["https://*.example.org"]))
    page.go("https://a.example.org/x")
    page.go("https://a.b.example.org/x")
    with pytest.raises(plc.BrowserError, match="outside the origins"):
        page.go("https://example.org/")


def test_every_origin_of_a_recipe_is_open(browser):
    page, call, run = make_page(shop(origins=[ORIGIN, "http://127.0.0.1:3000"]))
    page.go(f"{ORIGIN}/a")
    page.go("http://127.0.0.1:3000/b")
    with pytest.raises(plc.BrowserError, match=re.escape("(https://app.example.com, http://127.0.0.1:3000)")):
        page.go("http://127.0.0.1:3001/")


@pytest.mark.parametrize("url", [url for url in REFUSED if url not in ("/relative", "")])
def test_fetch_outside_the_origins_fails_before_any_code_runs(browser, url):
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="fetch: refusing .*outside the origins of recipe shop"):
        page.fetch(url)
    assert browser.codes == [] and browser.calls == []


def test_fetch_resolves_a_relative_url_against_the_page_and_checks_the_result(browser):
    browser.url = f"{ORIGIN}/list"
    browser.fetches[f"{ORIGIN}/api/items"] = {"status": 200, "text": '{"a": 1}'}
    page, call, run = make_page()
    assert page.fetch("/api/items").json == {"a": 1}
    assert browser.calls[-1][1] == f"{ORIGIN}/api/items"
    for url in ("//evil.example/api", "https://evil.example/api"):
        with pytest.raises(plc.BrowserError, match="outside the origins"):
            page.fetch(url)
    browser.url = "about:blank"
    with pytest.raises(plc.BrowserError, match="fetch: refusing '/api/items'"):
        page.fetch("/api/items")


def test_fetch_that_ends_on_another_origin_fails_the_call_and_returns_nothing(browser):
    browser.fetches[f"{ORIGIN}/api"] = {"status": 200, "url": "https://evil.example/landing", "text": "secret page"}
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="fetch \\(the response\\): refusing 'https://evil.example/landing'"):
        page.fetch(f"{ORIGIN}/api")


@pytest.mark.parametrize("url", ["https://evil.example/", "about:blank", "file:///etc/hostname", "http://app.example.com/"])
def test_new_tab_outside_the_origins_fails_before_any_call(browser, url):
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="new_tab: refusing"):
        page.new_tab(url)
    assert browser.calls == [] and browser.tabs == [f"{ORIGIN}/"]


def test_new_tab_on_the_origins_opens_and_selects_the_tab(browser):
    page, call, run = make_page()
    assert page.new_tab(f"{ORIGIN}/two") == f"{ORIGIN}/two"
    assert browser.tabs == [f"{ORIGIN}/", f"{ORIGIN}/two"] and browser.tab == 1
    assert browser.calls == [("tab-new", f"{ORIGIN}/two")]


def local_page(site):
    rec = Recipe("local", "Local", origins=[site.base])
    return make_page(rec)


def test_http_gets_a_public_endpoint_with_urllib(sites):
    main, other = sites
    page, call, run = local_page(main)
    reply = page.http(f"{main.base}/public.json")
    assert (reply.status, reply.ok, reply.json, reply.text) == (200, True, {"public": True}, '{"public": true}')
    assert reply.headers["content-type"] == "application/json"
    assert reply.url == f"{main.base}/public.json"
    assert main.paths() == ["/public.json"]
    assert call.lines == [f"  http GET {main.base}/public.json -> 200"]


def test_http_posts_json_text_and_headers(sites):
    main, other = sites
    page, call, run = local_page(main)
    reply = page.http(f"{main.base}/echo", "post", json={"a": [1, "b"]}, headers={"X-Token": "t1"})
    assert reply.json == {"body": '{"a": [1, "b"]}', "type": "application/json", "token": "t1"}
    reply = page.http(f"{main.base}/echo", "POST", body="plain text", headers={"Content-Type": "text/plain"})
    assert reply.json["body"] == "plain text" and reply.json["type"] == "text/plain"
    with pytest.raises(ValueError, match="json or body"):
        page.http(f"{main.base}/echo", "POST", json={}, body="x")


def test_http_returns_an_error_status_instead_of_raising(sites):
    main, other = sites
    page, call, run = local_page(main)
    reply = page.http(f"{main.base}/api/items")
    assert (reply.status, reply.ok, reply.json) == (401, False, {"error": "signed out"})
    missing = page.http(f"{main.base}/nothing-here")
    assert (missing.status, missing.ok, missing.text) == (404, False, "not found")


def test_http_follows_a_redirect_that_stays_on_the_origins(sites):
    main, other = sites
    page, call, run = local_page(main)
    reply = page.http(f"{main.base}/redirect-in")
    assert (reply.status, reply.json, reply.url) == (200, {"public": True}, f"{main.base}/public.json")
    assert main.paths() == ["/redirect-in", "/public.json"]


def test_http_refuses_a_redirect_to_another_origin_before_following_it(sites):
    main, other = sites
    page, call, run = local_page(main)
    with pytest.raises(plc.BrowserError, match="http \\(a redirect\\): refusing"):
        page.http(f"{main.base}/redirect-out")
    assert main.paths() == ["/redirect-out"]
    assert other.hits == []


def test_http_outside_the_origins_sends_nothing(sites):
    main, other = sites
    page, call, run = local_page(main)
    with pytest.raises(plc.BrowserError, match="http: refusing"):
        page.http(f"{other.base}/public.json")
    assert other.hits == [] and main.hits == []


def test_http_to_a_port_nobody_listens_on_is_a_failed_call(sites):
    main, other = sites
    page, call, run = local_page(main)
    other.stop()
    rec = Recipe("local", "Local", origins=[other.base])
    page, call, run = make_page(rec)
    with pytest.raises(plc.BrowserError, match="^http: "):
        page.http(f"{other.base}/public.json", timeout=2)


def test_http_cuts_a_response_over_the_cap(sites, monkeypatch):
    main, other = sites
    page, call, run = local_page(main)
    monkeypatch.setattr(page_module, "MAX_HTTP_BYTES", 5)
    with pytest.raises(plc.BrowserError, match="http: the response is over 5 bytes"):
        page.http(f"{main.base}/public.json")


# --- the origin check: after a call that can navigate ---------------------------------------------------------

OUT = "https://evil.example/landed"


def arrange_click(b):
    b.links["#out"] = OUT


def arrange_hook(verb):
    def arrange(b):
        b.hooks[verb] = lambda *args: setattr(b, "url", OUT)

    return arrange


def arrange_back(b):
    b.history[0] = [OUT]


def arrange_redirect(b):
    b.redirects[f"{ORIGIN}/two"] = OUT


def arrange_tabs(b):
    b.tabs = [OUT, f"{ORIGIN}/"]
    b.tab = 1


def arrange_tabs_other(b):
    b.tabs = [f"{ORIGIN}/", OUT]
    b.tab = 0


NAVIGATIONS = [
    ("go", arrange_redirect, lambda p: p.go(f"{ORIGIN}/two")),
    ("click", arrange_click, lambda p: p.click("#out")),
    ("dblclick", arrange_click, lambda p: p.dblclick("#out")),
    ("press", arrange_hook("press"), lambda p: p.press("Enter")),
    ("select", arrange_hook("select"), lambda p: p.select("#menu", "b")),
    ("check", arrange_hook("check"), lambda p: p.check("#box")),
    ("uncheck", arrange_hook("uncheck"), lambda p: p.uncheck("#box")),
    ("back", arrange_back, lambda p: p.back()),
    ("forward", arrange_hook("go-forward"), lambda p: p.forward()),
    ("reload", arrange_hook("reload"), lambda p: p.reload()),
    ("new_tab", arrange_redirect, lambda p: p.new_tab(f"{ORIGIN}/two")),
    ("select_tab", arrange_tabs_other, lambda p: p.select_tab(1)),
    ("close_tab", arrange_tabs, lambda p: p.close_tab()),
    ("wait_for_url", arrange_hook("none"), lambda p: p.wait_for_url("")),
]


@pytest.mark.parametrize("name, arrange, act", NAVIGATIONS, ids=[row[0] for row in NAVIGATIONS])
def test_a_call_that_ends_on_a_page_outside_the_origins_fails_the_verb(browser, name, arrange, act):
    arrange(browser)
    if name == "wait_for_url":
        browser.url = OUT
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match=f"{name.replace('_', '[_-]')}: the page is at 'https://evil.example/landed', outside the origins of recipe shop"):
        act(page)


@pytest.mark.parametrize("name, arrange, act", NAVIGATIONS, ids=[row[0] for row in NAVIGATIONS])
def test_the_same_calls_that_stay_on_the_origins_succeed(browser, name, arrange, act):
    page, call, run = make_page()
    browser.tabs = [f"{ORIGIN}/", f"{ORIGIN}/other"]
    browser.history[0] = [f"{ORIGIN}/earlier"]
    act(page)


def test_a_blank_page_is_on_no_origin_and_holds_nothing(browser):
    page, call, run = make_page()
    browser.tabs = [f"{ORIGIN}/", "about:blank"]
    assert page.select_tab(1) == "about:blank"
    browser.url = "about:blank"
    assert page.reload() == "about:blank"


def test_a_page_a_navigation_left_in_an_error_page_is_outside(browser):
    page, call, run = make_page()
    browser.hooks["goto"] = lambda *args: setattr(browser, "url", "chrome-error://chromewebdata/")
    with pytest.raises(plc.BrowserError, match="go: the page is at 'chrome-error://chromewebdata/'"):
        page.go(f"{ORIGIN}/a")


def test_the_calls_that_can_navigate_are_the_ones_that_check_where_the_page_ended():
    assert page_module.NAVIGATING == {
        "go", "click", "dblclick", "press", "select", "check", "uncheck", "back", "forward", "reload",
        "download", "upload", "drop",
    }


def test_a_verb_run_through_command_is_checked_like_the_method(browser):
    page, call, run = make_page()
    browser.links["#out"] = OUT
    with pytest.raises(plc.BrowserError, match="click: the page is at"):
        page.command("click", "#out")
    with pytest.raises(plc.BrowserError, match="go: refusing"):
        page.command("go", "https://evil.example/")


def test_fill_secret_keeps_the_origin_check_of_the_secret_itself(browser):
    store = {"PASSWORD": {"value": VALUE, "origins": ["https://login.example.net"]}}
    page, call, run = make_page(store=store)
    with pytest.raises(plc.BrowserError) as refused:
        page.fill_secret("#pw", "PASSWORD")
    assert "secret PASSWORD may not be typed on origin" in str(refused.value)
    assert "allowed origins: https://login.example.net" in str(refused.value)
    assert VALUE not in str(refused.value) and VALUE not in "\n".join(call.lines)
    browser.url = "https://login.example.net/"
    # The recipe's own origins do not matter to the secret: it is typed only where the operator said.
    assert page.fill_secret("#pw", "PASSWORD") == f"#pw <- {MARK}"


# --- the stop epoch ---------------------------------------------------------------------------------------------


def file_to_upload(tmp_path):
    path = tmp_path / "up.txt"
    path.write_text("x")
    return str(path)


CALLS = {
    "go": lambda p, t: p.go(f"{ORIGIN}/a"),
    "click": lambda p, t: p.click("#a"),
    "dblclick": lambda p, t: p.dblclick("#a"),
    "fill": lambda p, t: p.fill("#a", "text"),
    "fill_secret": lambda p, t: p.fill_secret("#a", "PASSWORD"),
    "press": lambda p, t: p.press("Enter"),
    "select": lambda p, t: p.select("#a", "b"),
    "check": lambda p, t: p.check("#a"),
    "uncheck": lambda p, t: p.uncheck("#a"),
    "hover": lambda p, t: p.hover("#a"),
    "wait": lambda p, t: p.wait(1),
    "wait_for": lambda p, t: p.wait_for("#a", 1),
    "wait_for_url": lambda p, t: p.wait_for_url("/a", 1),
    "text": lambda p, t: p.text("#a"),
    "find": lambda p, t: p.find("a"),
    "url": lambda p, t: p.url(),
    "snapshot_text": lambda p, t: p.snapshot_text(),
    "screenshot": lambda p, t: p.screenshot("a"),
    "hide": lambda p, t: p.hide("PASSCODE", "482916"),
    "no_page_files": lambda p, t: p.no_page_files().__enter__(),
    "sensitive": lambda p, t: p.sensitive("passcode", lambda: True).__enter__(),
    "fill_hidden": lambda p, t: p.fill_hidden("#a", "482916"),
    "download": lambda p, t: p.download("#a", 1),
    "upload": lambda p, t: p.upload("#a", file_to_upload(t)),
    "drop": lambda p, t: p.drop("#a", file_to_upload(t)),
    "back": lambda p, t: p.back(),
    "forward": lambda p, t: p.forward(),
    "reload": lambda p, t: p.reload(),
    "evaluate": lambda p, t: p.evaluate("() => 1"),
    "run_code": lambda p, t: p.run_code("return 1;"),
    "fetch": lambda p, t: p.fetch(f"{ORIGIN}/a"),
    "http": lambda p, t: p.http(f"{ORIGIN}/a"),
    "tabs": lambda p, t: p.tabs(),
    "new_tab": lambda p, t: p.new_tab(f"{ORIGIN}/a"),
    "select_tab": lambda p, t: p.select_tab(0),
    "close_tab": lambda p, t: p.close_tab(),
    "local_storage_set": lambda p, t: p.local_storage_set("k", "v"),
    "resize": lambda p, t: p.resize(800, 600),
    "artifact": lambda p, t: p.artifact("a.txt", "text"),
    "log": lambda p, t: p.log("text"),
    "env": lambda p, t: p.env("SHOP_BASE"),
    "recipe": lambda p, t: p.recipe("shop", "x"),
    "command": lambda p, t: p.command("url"),
}


def test_every_public_method_of_the_page_is_covered_by_the_stop_test():
    public = {name for name in dir(page_module.Page) if not name.startswith("_")}
    assert set(CALLS) == public


@pytest.mark.parametrize("name", sorted(CALLS))
def test_after_a_stop_every_call_fails_with_the_stop_text_and_reaches_nothing(browser, monkeypatch, tmp_path, name):
    monkeypatch.setenv("SHOP_BASE", ORIGIN)
    page, call, run = make_page(store=STORE)
    session.bump_stop_epoch("seat")
    with pytest.raises(plc.BrowserError) as stopped:
        CALLS[name](page, tmp_path)
    assert str(stopped.value) == session.STOP_MESSAGE
    assert browser.calls == [] and browser.codes == [] and browser.typed == []
    artifacts = session.artifacts_dir("seat")
    assert not os.path.isdir(artifacts) or os.listdir(artifacts) == []


def test_a_wait_sleeps_in_slices_so_a_stop_is_seen_within_one(browser, monkeypatch):
    page, call, run = make_page()
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            session.bump_stop_epoch("seat")

    monkeypatch.setattr(page_module.time, "sleep", sleep)
    with pytest.raises(plc.BrowserError, match=session.STOP_MESSAGE):
        page.wait(60)
    assert len(slept) == 3 and max(slept) <= page_module.WAIT_SLICE_SECONDS


def test_a_wait_waits_the_time_asked_and_reads_a_large_number_as_milliseconds(browser):
    page, call, run = make_page()
    started = time.monotonic()
    page.wait(0.3)
    assert 0.25 <= time.monotonic() - started < 2
    started = time.monotonic()
    page.wait(400)
    assert time.monotonic() - started < 2
    assert call.lines == ["  wait 0.3s", "  wait 0.4s"]


def test_wait_for_url_polls_until_the_url_holds_the_text_or_the_time_is_up(browser, monkeypatch):
    page, call, run = make_page()
    urls = iter([f"{ORIGIN}/a", f"{ORIGIN}/b", f"{ORIGIN}/done", f"{ORIGIN}/done"])
    monkeypatch.setattr(session, "current_url", lambda seat: next(urls))
    monkeypatch.setattr(page_module.time, "sleep", lambda seconds: None)
    assert page.wait_for_url("/done", 30) == f"{ORIGIN}/done"
    browser.url = f"{ORIGIN}/other"
    monkeypatch.setattr(session, "current_url", lambda seat: f"{ORIGIN}/other")
    with pytest.raises(plc.BrowserError, match=f"wait-for-url: still at {ORIGIN}/other"):
        page.wait_for_url("/done", 0)


# --- scrubbing --------------------------------------------------------------------------------------------------


def leaky(browser):
    """A browser whose every answer holds the stored value in raw, JSON, URL and markup forms."""
    browser.url = f"{ORIGIN}/?pw={VALUE}"
    browser.texts["#a"] = f"typed {VALUE}"
    browser.snapshot_text = f'- textbox "Password": {VALUE}'
    browser.evaluations.append(("() =>", lambda *values: {"seen": VALUE, "list": [VALUE, 1, True, None]}))
    browser.run_codes.append(("return", lambda *values: VALUE))
    browser.fetches[f"{ORIGIN}/api"] = {
        "status": 200, "url": f"{ORIGIN}/api?pw={VALUE}", "headers": {"X-Echo": VALUE},
        "text": json.dumps({"password": VALUE}),
    }


def test_what_the_page_returns_to_a_recipe_never_holds_a_stored_value(browser):
    leaky(browser)
    page, call, run = make_page(store=STORE)
    returned = {
        "url": page.url(),
        "text": page.text("#a"),
        "snapshot": page.snapshot_text(),
        "evaluate": page.evaluate("() => 1"),
        "run_code": page.run_code("return 1;"),
        "go": page.go(f"{ORIGIN}/?pw={VALUE}"),
        "command": page.command("url"),
    }
    reply = page.fetch(f"{ORIGIN}/api")
    returned["fetch"] = [reply.url, reply.text, reply.headers, reply.json]
    flat = json.dumps(returned, default=str)
    assert VALUE not in flat
    assert MARK in flat
    assert returned["evaluate"] == {"seen": MARK, "list": [MARK, 1, True, None]}
    assert reply.json == {"password": MARK}
    assert reply.headers == {"x-echo": MARK}
    assert VALUE not in "\n".join(call.lines)


def test_a_number_that_is_a_stored_value_is_replaced_too(browser):
    store = {"PIN": {"value": "123456", "origins": [ORIGIN]}}
    browser.evaluations.append(("() =>", lambda *values: {"n": 123456, "m": 12, "f": 99.5}))
    page, call, run = make_page(store=store)
    assert page.evaluate("() => 1") == {"n": "[secret:PIN]", "m": 12, "f": 99.5}


def test_the_text_of_a_failed_call_is_scrubbed_before_a_recipe_can_catch_it(browser):
    page, call, run = make_page(store=STORE)

    def broken(*args):
        raise plc.BrowserError(f"fill: call log shows {VALUE}")

    browser.hooks["fill"] = broken
    with pytest.raises(plc.BrowserError) as failed:
        page.fill("#pw", VALUE)
    assert VALUE not in str(failed.value) and MARK in str(failed.value)
    assert VALUE not in "\n".join(call.lines)
    assert call.lines == [f"  fill #pw {MARK}"]


def test_a_code_error_is_scrubbed_and_loses_the_driver_header(browser, monkeypatch):
    page, call, run = make_page(store=STORE)

    def boom(*args, **kwargs):
        raise plc.BrowserError(f"### Error\nError: failed on {VALUE}")

    monkeypatch.setattr(plc, "run_code_file", boom)
    with pytest.raises(plc.BrowserError) as failed:
        page.evaluate("() => 1")
    assert str(failed.value) == f"Error: failed on {MARK}"


def test_every_transcript_line_is_scrubbed(browser):
    page, call, run = make_page(store=STORE)
    page.log(f"password is {VALUE}")
    page.fill("#q", f"{VALUE} again")
    page.go(f"{ORIGIN}/?pw={VALUE}")
    page.evaluate(f"() => '{VALUE}'")
    assert VALUE not in "\n".join(call.lines)
    assert call.lines[0] == f"  log: password is {MARK}"


def test_an_artifact_is_scrubbed_when_it_is_text(browser):
    page, call, run = make_page(store=STORE)
    for data in (f"line with {VALUE}\n", f"line with {VALUE}\n".encode()):
        path = page.artifact("note.txt", data)
        content = Path(path).read_bytes()
        assert VALUE.encode() not in content and MARK.encode() in content
    clean = bytes([0xff, 0xfe, 0x00, 0x80])
    assert Path(page.artifact("blob.bin", clean)).read_bytes() == clean, "bytes with no value are written as they are"


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le"])
def test_text_in_utf_16_or_utf_32_that_holds_a_value_is_refused_as_an_artifact(browser, codec):
    """A Windows export is UTF-16 or Windows-1252. UTF-16 of ASCII text is valid UTF-8 (with a NUL between the letters), so
    it is not text to the scrub, and is read as the encodings it can be."""
    page, call, run = make_page(store=STORE)
    data = f"name,password\nalex,{VALUE}\n".encode(codec)
    with pytest.raises(plc.BrowserError, match="not UTF-8 text"):
        page.artifact("export.csv", data)
    clean = "name,password\nalex,nothing here\n".encode(codec)
    assert Path(page.artifact("export.csv", clean)).read_bytes() == clean


def test_data_that_is_not_utf_8_text_is_read_as_other_encodings_when_it_is_not_too_large(browser, monkeypatch):
    page, call, run = make_page(store=STORE)
    data = f"name,password\nalex,{VALUE}\n".encode("utf-16")
    with pytest.raises(plc.BrowserError, match="not UTF-8 text"):
        page.artifact("export.csv", data)
    monkeypatch.setattr(page_module, "MAX_READ_AS_OTHER_TEXT", len(data) - 1)
    assert Path(page.artifact("export.csv", data)).read_bytes() == data, "past the size, UTF-16 and UTF-32 are not read"
    assert page_module._utf8_text(b"abc\xef\xbb\xbf") == "abc\ufeff" and page_module._utf8_text(b"a\x00b") is None
    assert page_module._utf8_text("caf\u00e9".encode("utf-8")) == "caf\u00e9" and page_module._utf8_text(b"caf\xe9") is None
    path = page.artifact("nul.txt", "has a NUL \x00 inside")
    assert Path(path).read_bytes() == "has a NUL \x00 inside".encode("utf-8"), "text given as a str is text"


def test_a_value_with_a_non_ascii_character_in_a_file_in_latin_1_is_refused_as_an_artifact(browser, monkeypatch):
    page, call, run = make_page(store={"PASSWORD": {"value": "caf\u00e9-pass7", "origins": [ORIGIN]}})
    monkeypatch.setattr(page_module, "MAX_READ_AS_OTHER_TEXT", 0)  # UTF-16 and UTF-32 are not read at this size, ISO-8859-1 is
    with pytest.raises(plc.BrowserError, match="not UTF-8 text"):
        page.artifact("export.csv", "user,caf\u00e9-pass7\n".encode("latin-1"))
    with pytest.raises(plc.BrowserError, match="not UTF-8 text"):
        page.artifact("export.csv", "user,caf\u00e9-pass7\n".encode("cp1252"))
    control, call, run = make_page(store={"PASSWORD": {"value": "x\x81y-pass7", "origins": [ORIGIN]}})
    with pytest.raises(plc.BrowserError, match="not UTF-8 text"):
        control.artifact("export.csv", "user,x\x81y-pass7\n".encode("latin-1")), "a byte that Windows-1252 has no character for"
    euro, call, run = make_page(store={"PASSWORD": {"value": "\u20acuro-pass7", "origins": [ORIGIN]}})
    with pytest.raises(plc.BrowserError, match="not UTF-8 text"):
        euro.artifact("export.csv", "user,\u20acuro-pass7\n".encode("cp1252"))
    assert Path(page.artifact("export.csv", "user,caf\u00e9-pass7\n".encode("utf-16"))).exists(), "beyond the size, not read"
    monkeypatch.setattr(page_module, "MAX_READ_AS_OTHER_TEXT", 10**9)
    with pytest.raises(plc.BrowserError, match="not UTF-8 text"):
        page.artifact("export.csv", "user,caf\u00e9-pass7\n".encode("utf-16"))
    assert Path(page.artifact("export.csv", "user,caf\u00e9-pass7\n")).read_text(encoding="utf-8") == f"user,{MARK}\n"


def test_bytes_that_are_not_utf8_text_and_hold_a_value_are_refused(browser):
    """A scrub changes text. A file in another encoding that has the value in its ASCII text (Latin-1 is that) is still a
    file the reply would show the value in; binary that has it cannot be changed and kept whole. Neither is written."""
    page, call, run = make_page(store=STORE)
    for data in (
        bytes([0xff, 0xfe, 0x00]) + VALUE.encode(),
        f"caf\xe9 {VALUE}\n".encode("latin-1"),
        VALUE.encode() + bytes([0x80]),
    ):
        before = set(os.listdir(commands.session.artifacts_dir("seat"))) if os.path.isdir(commands.session.artifacts_dir("seat")) else set()
        with pytest.raises(plc.BrowserError, match="not UTF-8 text"):
            page.artifact("blob.bin", data)
        after = set(os.listdir(commands.session.artifacts_dir("seat"))) if os.path.isdir(commands.session.artifacts_dir("seat")) else set()
        assert before == after, "nothing was written"
    sheet = page.artifact("latin.txt", "caf\xe9 no secret here\n".encode("latin-1"))
    assert Path(sheet).read_bytes() == "caf\xe9 no secret here\n".encode("latin-1")


def test_the_snapshot_files_playwright_writes_are_scrubbed_around_every_call(browser, monkeypatch):
    page, call, run = make_page(store=STORE)
    written = []
    real = commands.scrubbed_outputs

    def spy(seat, scrubber):
        written.append(bool(scrubber))
        return real(seat, scrubber)

    monkeypatch.setattr(commands, "scrubbed_outputs", spy)
    browser.fetches[f"{ORIGIN}/api"] = {"status": 200, "text": ""}
    page.click("#a")
    page.fetch(f"{ORIGIN}/api")
    page.new_tab(f"{ORIGIN}/b")
    assert written == [True, True, True]


# --- no method gives a secret back ------------------------------------------------------------------------------


def test_no_method_of_the_page_reads_a_secret():
    names = [name for name in dir(page_module.Page) if not name.startswith("_")]
    assert [name for name in names if "secret" in name.lower() or "password" in name.lower()] == ["fill_secret"]
    assert [name for name in names if name.startswith(("get", "read", "load"))] == []


def test_fill_secret_names_the_secret_and_gives_back_the_name_only(browser):
    page, call, run = make_page(store=STORE)
    assert page.fill_secret("#pw", "PASSWORD") == f"#pw <- {MARK}"
    assert browser.typed == [("#pw", VALUE)]
    assert VALUE not in "\n".join(call.lines)
    assert call.lines == [f"  fill-secret #pw PASSWORD -> #pw <- {MARK}"]


def test_a_recipe_can_type_only_the_secrets_it_declares(browser):
    page, call, run = make_page(shop(secrets=["OTHER"]), store=STORE)
    with pytest.raises(plc.BrowserError, match="fill_secret: PASSWORD is not among the secrets recipe shop declares"):
        page.fill_secret("#pw", "PASSWORD")
    assert browser.typed == []


def test_a_secret_that_is_not_stored_fails_the_fill_and_names_the_fix(browser):
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="secret PASSWORD is not set for seat seat"):
        page.fill_secret("#pw", "PASSWORD")


def test_the_env_of_a_recipe_does_not_reach_a_secret(browser, monkeypatch):
    monkeypatch.setenv("PASSWORD", VALUE)
    page, call, run = make_page(store=STORE)
    with pytest.raises(UsageError, match="PASSWORD is not an env name recipe shop declares"):
        page.env("PASSWORD")


# --- a value the recipe read from a page ------------------------------------------------------------------------

CODE = "482916"
CODE_MARK = "[secret:PASSCODE]"


def scratch_file(name, text):
    """A file as playwright-cli leaves one under the seat's scratch directory."""
    directory = session.cli_output_dir("seat")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    Path(path).write_text(text)
    return path


def test_a_hidden_value_is_replaced_in_all_that_follows_even_with_no_stored_secret(browser):
    page, call, run = make_page()
    browser.url = f"{ORIGIN}/?code={CODE}"
    browser.snapshot_text = f"- generic: Your code is {CODE}"
    browser.texts["#a"] = f"typed {CODE}"
    assert page.snapshot_text() == f"- generic: Your code is {CODE}"
    page.hide("PASSCODE", CODE)
    assert page.snapshot_text() == f"- generic: Your code is {CODE_MARK}"
    assert page.url() == f"{ORIGIN}/?code={CODE_MARK}"
    assert page.text("#a") == f"typed {CODE_MARK}"
    page.log(f"the code is {CODE}")
    page.go(f"{ORIGIN}/?code={CODE}")
    assert CODE not in "\n".join(call.lines)
    assert call.lines[0] == "  snapshot"
    assert "  hide PASSCODE" in call.lines
    assert f"  log: the code is {CODE_MARK}" in call.lines


def test_hiding_a_value_scrubs_the_files_playwright_cli_has_already_written(browser):
    page, call, run = make_page()
    seen = scratch_file("page-1.yml", f"- generic: Your code is {CODE}\n")
    other = scratch_file("page-2.yml", "- generic: nothing\n")
    page.hide("PASSCODE", CODE)
    assert Path(seen).read_text() == f"- generic: Your code is {CODE_MARK}\n"
    assert Path(other).read_text() == "- generic: nothing\n"


def test_the_files_written_after_a_value_is_hidden_are_scrubbed_around_every_call(browser):
    page, call, run = make_page()
    page.hide("PASSCODE", CODE)
    target = os.path.join(session.cli_output_dir("seat"), "page-3.yml")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    browser.hooks["click"] = lambda *args: Path(target).write_text(f"- generic: {CODE}\n")
    page.click("#a")
    assert Path(target).read_text() == f"- generic: {CODE_MARK}\n"


def test_a_hidden_value_comes_back_marked_from_what_the_page_returns(browser):
    page, call, run = make_page(store=STORE)
    browser.evaluations.append(("() =>", lambda *values: {"code": CODE, "n": int(CODE), "pw": VALUE}))
    page.hide("PASSCODE", CODE)
    assert page.evaluate("() => 1") == {"code": CODE_MARK, "n": CODE_MARK, "pw": MARK}


def test_a_hidden_value_is_not_a_secret_a_recipe_can_type(browser):
    page, call, run = make_page(shop(secrets=["PASSCODE"]))
    page.hide("PASSCODE", CODE)
    with pytest.raises(plc.BrowserError, match="secret PASSCODE is not set for seat seat"):
        page.fill_secret("#a", "PASSCODE")
    assert browser.typed == []
    assert run.secret("PASSCODE") is None


@pytest.mark.parametrize("value", ["", "123", 482916, None])
def test_a_value_too_short_to_scrub_is_refused_without_repeating_it(browser, value):
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="^hide: a secret value is at least 4 characters"):
        page.hide("PASSCODE", value)
    assert not run.scrubber


def test_a_hidden_value_needs_a_name_like_a_secret_name(browser):
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="^hide: a secret name starts with a capital letter"):
        page.hide("passcode", CODE)
    assert CODE not in "\n".join(call.lines)


def test_a_stop_that_has_landed_does_not_keep_hide_from_hiding(browser):
    page, call, run = make_page()
    session.bump_stop_epoch("seat")
    with pytest.raises(plc.BrowserError, match=session.STOP_MESSAGE):
        page.hide("PASSCODE", CODE)
    assert run.clean(f"code {CODE}") == f"code {CODE_MARK}"
    scratch = scratch_file("page-1.yml", f"- generic: {CODE}\n")
    with pytest.raises(plc.BrowserError, match=session.STOP_MESSAGE):
        page.hide("OTHER", "135790")
    assert Path(scratch).read_text() == f"- generic: {CODE_MARK}\n"


def test_the_page_files_written_in_a_block_are_deleted_and_the_ones_before_it_stay(browser):
    page, call, run = make_page()
    before = scratch_file("page-1.yml", "- textbox: kept\n")
    with page.no_page_files():
        during = scratch_file("page-2.yml", "- textbox [ref=e4]: 4\n")
        log = scratch_file("console-2.log", "[LOG] shown\n")
    assert Path(before).read_text() == "- textbox: kept\n"
    assert not Path(during).exists() and not Path(log).exists()


def test_the_page_files_are_deleted_when_the_block_fails_too(browser):
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="the click failed"):
        with page.no_page_files():
            during = scratch_file("page-2.yml", "- textbox [ref=e4]: 4\n")
            raise plc.BrowserError("the click failed")
    assert not Path(during).exists()


def test_a_file_that_was_there_and_grew_in_the_block_stays(browser):
    """A console log is one file for the whole session; deleting it would take what came before the block."""
    page, call, run = make_page()
    log = scratch_file("console-1.log", "[LOG] before\n")
    with page.no_page_files():
        with open(log, "a") as handle:
            handle.write("[LOG] during\n")
    assert Path(log).read_text() == "[LOG] before\n[LOG] during\n"


def test_a_file_that_is_gone_when_the_block_ends_is_no_error(browser):
    page, call, run = make_page()
    with page.no_page_files():
        os.remove(scratch_file("page-2.yml", "x"))


def test_the_block_touches_only_the_page_file_directory(browser, tmp_path):
    page, call, run = make_page()
    elsewhere = tmp_path / "keep.txt"
    with page.no_page_files():
        elsewhere.write_text("x")
        artifact = page.artifact("note.txt", "kept")
    assert elsewhere.exists() and Path(artifact).exists()


def test_a_private_code_file_that_cannot_be_written_is_a_browser_error_without_the_text(browser, monkeypatch):
    import errno

    page, call, run = make_page()

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(plc, "fill_secret", full)
    with pytest.raises(plc.BrowserError) as failed:
        page.fill_hidden("#code", CODE)
    assert str(failed.value) == "fill_hidden: cannot write its private code file: No space left on device"


def test_fill_hidden_types_the_text_and_the_transcript_names_the_field_only(browser):
    page, call, run = make_page()
    shown = page.fill_hidden("#code", CODE)
    assert browser.typed == [("#code", CODE)]
    assert shown == "#code <- [hidden]"
    assert call.lines == ["  fill_hidden #code -> #code <- [hidden]"]
    assert CODE not in json.dumps(browser.calls, default=str) + "\n".join(call.lines)


def test_fill_hidden_reaches_a_snapshot_ref_the_way_the_secret_fill_does(browser):
    page, call, run = make_page()
    page.fill_hidden("e12", "4")
    page.fill_hidden("f1e3", "")
    assert browser.typed == [("aria-ref=e12", "4"), ("aria-ref=f1e3", "")]
    assert call.lines == ["  fill_hidden e12 -> e12 <- [hidden]", "  fill_hidden f1e3 -> f1e3 <- [hidden]"]


def test_fill_hidden_types_only_on_the_origins_of_the_recipe(browser):
    page, call, run = make_page()
    browser.url = "https://elsewhere.example.net/signin"
    with pytest.raises(plc.BrowserError) as refused:
        page.fill_hidden("#code", CODE)
    assert str(refused.value) == (
        "fill_hidden: the field is in a page at 'https://elsewhere.example.net/signin', "
        f"outside the origins of recipe shop ({ORIGIN})"
    )
    assert CODE not in str(refused.value) + "\n".join(call.lines)


def test_a_failed_hidden_fill_says_what_failed_and_not_what_was_typed(browser, monkeypatch):
    page, call, run = make_page()
    log = (
        "Error: locator.fill: Timeout 5000ms exceeded.\nCall log:\n"
        '\x1b[2m  - waiting for locator("#code")\x1b[22m\n\x1b[2m  -   locator resolved to <div></div>\x1b[22m\n'
        f'\x1b[2m  - fill("{CODE}")\x1b[22m\n'
    )

    def fail(*args, **kwargs):
        raise plc.BrowserError(log)

    monkeypatch.setattr(plc, "fill_secret", fail)
    with pytest.raises(plc.BrowserError) as failed:
        page.fill_hidden("#code", CODE)
    assert str(failed.value) == "fill_hidden: Error: locator.fill: Timeout 5000ms exceeded."
    assert CODE not in str(failed.value) + "\n".join(call.lines)


def test_the_headline_of_a_failed_hidden_fill_is_scrubbed_too(browser, monkeypatch):
    page, call, run = make_page()
    page.hide("PASSCODE", CODE)

    def fail(*args, **kwargs):
        raise plc.BrowserError(f"### Error\nError: field already holds {CODE}\nCall log:\n  - fill(\"{CODE}\")")

    monkeypatch.setattr(plc, "fill_secret", fail)
    with pytest.raises(plc.BrowserError) as failed:
        page.fill_hidden("#code", CODE)
    assert str(failed.value) == f"fill_hidden: Error: field already holds {CODE_MARK}"


def test_a_target_that_cannot_be_found_fails_a_hidden_fill_without_the_text(browser, monkeypatch):
    page, call, run = make_page()

    def nothing(seat, argument):
        raise plc.BrowserError(f"fill: nothing visible matching {argument!r}")

    monkeypatch.setattr(page_module.resolve, "fill_target", nothing)
    with pytest.raises(plc.BrowserError) as failed:
        page.fill_hidden("Code", CODE)
    assert str(failed.value) == "fill_hidden: fill: nothing visible matching 'Code'"
    assert browser.typed == []


def test_fill_hidden_takes_text_and_scrubs_the_files_the_driver_wrote(browser, monkeypatch):
    page, call, run = make_page()
    page.hide("PASSCODE", CODE)
    target = os.path.join(session.cli_output_dir("seat"), "page-9.yml")
    os.makedirs(os.path.dirname(target), exist_ok=True)

    def type_it(seat, selector, value, origins, directory, timeout=30):
        Path(target).write_text(f'- textbox "Code" [ref=e4]: {value}\n')
        return ORIGIN

    monkeypatch.setattr(plc, "fill_secret", type_it)
    page.fill_hidden("#code", CODE)
    assert Path(target).read_text() == f'- textbox "Code" [ref=e4]: {CODE_MARK}\n'
    with pytest.raises(plc.BrowserError, match="the text is a str"):
        page.fill_hidden("#code", 482916)


# --- env ------------------------------------------------------------------------------------------------------------


def test_env_reads_a_declared_name_and_logs_the_name_only(browser, monkeypatch):
    monkeypatch.setenv("SHOP_BASE", "https://app.example.com/base")
    page, call, run = make_page()
    assert page.env("SHOP_BASE") == "https://app.example.com/base"
    assert call.lines == ["  env SHOP_BASE"]


def test_env_of_an_undeclared_or_unset_or_empty_name_is_a_usage_failure_that_names_it(browser, monkeypatch):
    monkeypatch.setenv("PATH_ELSEWHERE", "x")
    monkeypatch.delenv("SHOP_BASE", raising=False)
    page, call, run = make_page()
    with pytest.raises(UsageError, match="PATH_ELSEWHERE is not an env name recipe shop declares"):
        page.env("PATH_ELSEWHERE")
    with pytest.raises(UsageError, match=r"SHOP_BASE is not set\. Set it on the node with `a8s vars <node> set env.SHOP_BASE <value>`"):
        page.env("SHOP_BASE")
    monkeypatch.setenv("SHOP_BASE", "")
    with pytest.raises(UsageError, match="SHOP_BASE is not set"):
        page.env("SHOP_BASE")


# --- the recipe's own code ---------------------------------------------------------------------------------------------

HOSTILE = ['x"); process.exit(1); ("', "line one\nline two", "back\\slash", "`${1+1}`", "</script>", "café   ☃", "'; await x; //"]
LITERAL = re.compile(r"JSON\.parse\((\".*\")\)")


def values_in(code):
    return json.loads(json.loads(LITERAL.search(code).group(1)))


@pytest.mark.parametrize("value", HOSTILE)
def test_evaluate_passes_values_as_one_json_literal_and_never_as_code(value):
    code = page_module.evaluate_code("(a) => a", [value, 7, {"k": [value]}])
    assert values_in(code) == [value, 7, {"k": [value]}]
    outside = LITERAL.sub("", code)
    assert value not in outside
    assert code.count("\n") == page_module.evaluate_code("(a) => a", ["x"]).count("\n")


@pytest.mark.parametrize("value", HOSTILE)
def test_run_code_passes_values_as_one_json_literal_and_never_as_code(value):
    code = page_module.run_code_text("return values[0];", [value])
    assert values_in(code) == [value]
    assert value not in LITERAL.sub("", code)


@pytest.mark.parametrize("value", HOSTILE)
def test_fetch_passes_the_url_and_the_request_as_one_json_literal(value):
    code = page_module.fetch_code("https://app.example.com/a", {"method": "POST", "body": value}, 5000)
    assert values_in(code) == ["https://app.example.com/a", {"method": "POST", "body": value}, 5000]
    assert value not in LITERAL.sub("", code)


def test_evaluate_hands_the_function_and_its_values_to_the_code_file(browser):
    browser.evaluations.append(("a + b", lambda a, b: a + b))
    page, call, run = make_page()
    assert page.evaluate("(a, b) => a + b", 2, 3) == 5
    assert browser.calls[-1] == ("evaluate", "(a, b) => a + b", [2, 3])
    assert call.lines == ["  evaluate (a, b) => a + b"]


def test_evaluate_that_returns_nothing_returns_none(browser):
    page, call, run = make_page()
    assert page.evaluate("() => undefined") is None


def test_run_code_gets_page_and_values_and_returns_json(browser):
    browser.run_codes.append(("values[0]", lambda *values: {"first": values[0]}))
    page, call, run = make_page()
    assert page.run_code("const t = await page.title();\nreturn {first: values[0]};", "v1") == {"first": "v1"}
    assert browser.calls[-1][2] == ["v1"]
    assert call.lines == ["  run_code const t = await page.title();"]


def test_the_recipes_own_code_needs_no_opt_in_and_the_guard_on_a_seat_with_secrets_does_not_apply(browser):
    page, call, run = make_page(store=STORE, allow_eval=False)
    browser.evaluations.append(("() => 2", 2))
    assert page.evaluate("() => 2") == 2
    assert page.run_code("return 1;") is None
    with pytest.raises(plc.BrowserError, match="eval and run-code are off on a seat that holds secrets"):
        page.command("eval", "1 + 1")
    quiet, call, run = make_page(allow_eval=False)
    with pytest.raises(plc.BrowserError, match="eval refused: this seat has not opted in"):
        quiet.command("eval", "1 + 1")
    with pytest.raises(plc.BrowserError, match="run-code refused: this seat has not opted in"):
        quiet.command("run-code", "return 1;")


def test_a_caller_verb_run_through_command_keeps_the_callers_opt_in(browser, monkeypatch):
    monkeypatch.setattr(plc, "evaluate", lambda seat, expression, timeout=30: "2")
    page, call, run = make_page(allow_eval=True)
    assert page.command("eval", "1 + 1") == "2"


def test_local_storage_set_sets_the_item_through_the_values_of_a_call(browser):
    page, call, run = make_page()
    page.local_storage_set("flag", "on\nline two")
    kind, source, values = browser.calls[-1]
    assert kind == "evaluate" and "localStorage.setItem" in source
    assert values == ["flag", "on\nline two"]
    assert call.lines == ["  local_storage_set flag"]


def test_the_code_a_page_runs_goes_through_a_private_file_and_gets_a_long_timeout(browser):
    page, call, run = make_page()
    browser.fetches[f"{ORIGIN}/a"] = {"status": 200, "text": ""}
    page.evaluate("() => 1")
    page.run_code("return 1;")
    page.fetch(f"{ORIGIN}/a", timeout=7)
    assert browser.timeouts == [60, 60, 22]


# --- fetch --------------------------------------------------------------------------------------------------------------


def test_fetch_sends_the_page_cookies_and_returns_status_headers_text_and_json(browser):
    browser.fetches[f"{ORIGIN}/api"] = {
        "status": 201, "headers": {"Content-Type": "application/json", "X-Id": "9"}, "text": '{"id": 9}',
    }
    page, call, run = make_page()
    reply = page.fetch(f"{ORIGIN}/api", "post", json={"a": 1}, headers={"X-Mode": "m"}, timeout=3)
    assert (reply.status, reply.ok, reply.text, reply.json) == (201, True, '{"id": 9}', {"id": 9})
    assert reply.headers == {"content-type": "application/json", "x-id": "9"}
    assert reply.url == f"{ORIGIN}/api"
    url, init = browser.calls[-1][1:]
    assert url == f"{ORIGIN}/api"
    assert init == {
        "method": "POST", "credentials": "include", "body": '{"a": 1}',
        "headers": {"X-Mode": "m", "Content-Type": "application/json"},
    }
    assert call.lines == [f"  fetch POST {ORIGIN}/api -> 201"]


def test_fetch_keeps_a_content_type_the_recipe_set_and_sends_text_as_given(browser):
    browser.fetches[f"{ORIGIN}/api"] = {"status": 200, "text": "ok"}
    page, call, run = make_page()
    page.fetch(f"{ORIGIN}/api", "PUT", body="a=1", headers={"content-type": "application/x-www-form-urlencoded"})
    init = browser.calls[-1][2]
    assert init["body"] == "a=1" and init["headers"] == {"content-type": "application/x-www-form-urlencoded"}
    page.fetch(f"{ORIGIN}/api")
    assert "body" not in browser.calls[-1][2] and browser.calls[-1][2]["method"] == "GET"
    with pytest.raises(ValueError, match="json or body"):
        page.fetch(f"{ORIGIN}/api", "POST", json={}, body="x")


@pytest.mark.parametrize("path", ["/redirect-in", "/redirect-out", "/moved"])
def test_a_fetch_that_is_answered_with_a_redirect_fails_with_a_clear_message(browser, path):
    browser.fetches[f"{ORIGIN}{path}"] = {"redirect": True}
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError) as failed:
        page.fetch(path)
    message = str(failed.value)
    assert message.startswith(f"fetch: {ORIGIN}{path} answered with a redirect. A recipe's fetch follows none")
    assert "before the recipe's origins could be checked" in message
    assert message.endswith("Fetch the address it points to, or open the page with go")
    assert call.lines == [f"  fetch GET {ORIGIN}{path}"]


def test_a_redirect_message_is_scrubbed_like_every_other_error(browser):
    url = f"{ORIGIN}/moved?token={VALUE}"
    browser.fetches[url] = {"redirect": True}
    page, call, run = make_page(store=STORE)
    with pytest.raises(plc.BrowserError) as failed:
        page.fetch(url)
    assert VALUE not in str(failed.value) and MARK in str(failed.value)


def test_a_response_that_is_not_json_has_no_json_and_a_bad_status_is_not_ok(browser):
    browser.fetches[f"{ORIGIN}/api"] = {"status": 500, "text": "<html>oops</html>"}
    page, call, run = make_page()
    reply = page.fetch(f"{ORIGIN}/api")
    assert (reply.ok, reply.json, reply.status) == (False, None, 500)


@pytest.mark.parametrize("status, ok", [(199, False), (200, True), (299, True), (300, False), (404, False)])
def test_ok_is_a_2xx_status(status, ok):
    assert page_module.Response(status, "u", {}, "").ok is ok


def test_a_response_parses_its_json_once_and_only_when_asked():
    reply = page_module.Response(200, "u", {}, '{"a": [1]}')
    assert reply.json == {"a": [1]}
    assert reply.json is reply.json
    assert page_module.Response(200, "u", {}, "").json is None


def test_a_fetch_the_page_could_not_make_is_a_failed_call(browser):
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="^fetch: Failed to fetch$"):
        page.fetch(f"{ORIGIN}/nothing")


def test_a_fetch_that_returns_nothing_usable_is_a_failed_call(browser, monkeypatch):
    page, call, run = make_page()
    for output in ("", "[]\n", '{"other": 1}\n'):
        monkeypatch.setattr(plc, "run_code_file", lambda *a, **k: output)
        with pytest.raises(plc.BrowserError, match="fetch: the page returned no response"):
            page.fetch(f"{ORIGIN}/api")


def test_fetch_takes_text_for_a_url(browser):
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="fetch: the URL is text"):
        page.fetch(None)


# --- tabs, window ------------------------------------------------------------------------------------------------------


def test_tabs_open_select_and_close_through_the_driver(browser):
    page, call, run = make_page()
    page.new_tab(f"{ORIGIN}/two")
    assert page.select_tab(0) == f"{ORIGIN}/"
    assert page.select_tab("1") == f"{ORIGIN}/two"
    assert page.close_tab() == f"{ORIGIN}/"
    page.new_tab(f"{ORIGIN}/three")
    page.new_tab(f"{ORIGIN}/four")
    assert page.close_tab(0) == f"{ORIGIN}/four"
    assert browser.tabs == [f"{ORIGIN}/three", f"{ORIGIN}/four"]
    assert [c for c in browser.calls if c[0].startswith("tab")] == [
        ("tab-new", f"{ORIGIN}/two"), ("tab-select", "0"), ("tab-select", "1"), ("tab-close",),
        ("tab-new", f"{ORIGIN}/three"), ("tab-new", f"{ORIGIN}/four"), ("tab-close", "0"),
    ]


def test_resize_passes_whole_numbers(browser):
    page, call, run = make_page()
    page.resize(800.0, "600")
    assert browser.calls == [("resize", "800", "600")]
    assert call.lines == ["  resize 800.0x600"]


# --- artifacts and files -------------------------------------------------------------------------------------------------


def test_an_artifact_is_a_file_of_the_reply_and_is_attached_once(browser, tmp_path):
    page, call, run = make_page()
    path = page.artifact("report.txt", "hello")
    assert os.path.dirname(path) == session.artifacts_dir("seat")
    assert os.path.basename(path).endswith("-report.txt") and Path(path).read_text() == "hello"
    assert run.files == [path]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    source = tmp_path / "copy-me.bin"
    source.write_bytes(b"\x00\x01data")
    copy = page.artifact("copy.bin", source)
    assert Path(copy).read_bytes() == b"\x00\x01data"
    assert run.files == [path, copy]
    assert Path(page.artifact("a.txt", bytearray(b"bytes"))).read_bytes() == b"bytes"


def test_an_artifact_is_written_aside_and_renamed(browser, monkeypatch):
    page, call, run = make_page()
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((a, b)), real(a, b))[1])
    page.artifact("a.txt", "text")
    (source, target), = replaced
    assert os.path.dirname(source) == os.path.dirname(target)
    assert source.startswith(target) and source.endswith(".tmp")
    assert f".{os.getpid()}." in source


@pytest.mark.parametrize("name", ["", "../x", "a/b", "a\\b", ".hidden", "x\0y", None, 7])
def test_an_artifact_name_is_a_plain_file_name(browser, name):
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="the name is a plain file name"):
        page.artifact(name, "x")


def test_an_artifact_is_not_made_from_the_browser_state_directory_or_other_things(browser, tmp_path):
    secretstore.set_secret("seat", "PASSWORD", VALUE, [ORIGIN])
    page, call, run = make_page(store=STORE)
    with pytest.raises(plc.BrowserError, match="refusing a file inside the browser state directory"):
        page.artifact("copy.json", Path(secretstore.path("seat")))
    with pytest.raises(plc.BrowserError, match="give text, bytes or a pathlib.Path"):
        page.artifact("x.txt", {"a": 1})
    with pytest.raises(FileNotFoundError):
        page.artifact("x.txt", tmp_path / "missing")
    assert run.files == []


def test_an_artifact_over_the_cap_is_refused(browser, monkeypatch):
    monkeypatch.setattr(page_module, "MAX_ARTIFACT_BYTES", 4)
    page, call, run = make_page()
    with pytest.raises(plc.BrowserError, match="artifact: over 4 bytes"):
        page.artifact("x.txt", "12345")


def test_a_screenshot_is_a_png_artifact_that_comes_with_the_reply(browser):
    page, call, run = make_page()
    path = page.screenshot()
    assert path.endswith("-screen.png") and Path(path).read_bytes().startswith(b"\x89PNG")
    named = page.screenshot("home")
    assert named.endswith("-home.png")
    assert page.screenshot("wide.jpg").endswith("-wide.jpg")
    assert run.files == [path, named, run.files[2]]
    with pytest.raises(plc.BrowserError, match="the name is a plain file name"):
        page.screenshot("../x")


def test_a_screenshot_takes_the_whole_page_only_when_asked(browser):
    page, call, run = make_page()
    view = page.screenshot("view")
    whole = page.screenshot("whole", full_page=True)
    assert browser.shots == [(view, False), (whole, True)]
    assert run.files == [view, whole]
    assert call.lines == ["  screenshot view.png", "  screenshot whole.png (full page)"]


def test_the_snapshot_is_text_through_the_scrub(browser):
    browser.snapshot_text = f'- textbox "Password": {VALUE}'
    page, call, run = make_page(store=STORE)
    assert page.snapshot_text() == f'- textbox "Password": {MARK}'
    assert page.snapshot_text(target="#form", depth=2, boxes=True) == f'- textbox "Password": {MARK}'
    assert browser.calls[-1] == ("snapshot", "#form", "2", True)
    with pytest.raises(plc.BrowserError, match="depth is 0 to 999"):
        page.snapshot_text(depth=-1)


def test_upload_clicks_the_target_then_hands_the_chooser_the_file(browser, tmp_path):
    page, call, run = make_page()
    path = file_to_upload(tmp_path)
    second = tmp_path / "second.txt"
    second.write_text("y")
    page.upload("#choose", path, str(second))
    assert [c[0] for c in browser.calls] == ["click", "upload"]
    assert browser.calls[1] == ("upload", path, str(second))
    with pytest.raises(plc.BrowserError, match="usage: upload"):
        page.upload("#choose")


def test_upload_and_drop_refuse_a_path_inside_the_state_directory_as_the_verbs_do(browser):
    page, call, run = make_page()
    inside = os.path.join(session.state_root(), "seat", "artifacts", "x.txt")
    os.makedirs(os.path.dirname(inside), exist_ok=True)
    Path(inside).write_text("x")
    with pytest.raises(plc.BrowserError, match="refusing .* inside the browser state directory"):
        page.drop("#zone", inside)
    with pytest.raises(plc.BrowserError, match="refusing .* inside the browser state directory"):
        page.command("upload", inside)


# --- command and the transcript lines -------------------------------------------------------------------------------------


def test_command_runs_any_verb_of_the_command_language_by_name(browser):
    page, call, run = make_page()
    page.command("scroll", 400)
    assert browser.calls == [("mousewheel", "0", "400")]
    with pytest.raises(plc.BrowserError, match="unknown command: nope"):
        page.command("nope")


def test_command_with_recipe_needs_a_site_and_a_verb(browser):
    page, call, run = make_page()
    with pytest.raises(UsageError, match="recipe <site> <verb>"):
        page.command("recipe", "shop")


def test_a_transcript_line_names_the_call_and_what_came_back(browser):
    browser.texts["h1"] = "Title"
    page, call, run = make_page()
    page.go(f"{ORIGIN}/a")
    page.text("h1")
    assert call.lines == [f"  go {ORIGIN}/a -> {ORIGIN}/a", "  text h1 -> Title"]


def test_a_call_that_failed_leaves_its_line_without_a_result(browser):
    page, call, run = make_page()
    browser.hooks["click"] = lambda *a: (_ for _ in ()).throw(plc.BrowserError("nothing there"))
    with pytest.raises(plc.BrowserError):
        page.click("#a")
    assert call.lines == ["  click #a"]


def test_a_transcript_line_is_one_short_line(browser):
    page, call, run = make_page()
    page.log("x" * 500)
    page.log("first\nsecond")
    assert len(call.lines[0]) == 2 + recipe.CALL_LINE_WIDTH and call.lines[0].endswith("...")
    assert call.lines[1] == "  log: first"


def test_a_nested_recipes_lines_sit_one_step_further_in(browser):
    page, call, run = make_page()
    inner = recipe._Call("seat", run, False, call.epoch, call.library, call.lines, 1)
    page_module.Page(inner, shop()).log("deeper")
    page.log("outer")
    assert call.lines == ["    log: deeper", "  log: outer"]


# --- the generated code, run as playwright-cli runs it ------------------------------------------------------------------

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8');
const answer = JSON.parse(process.argv[3]);
const calls = [];
const page = {
  evaluate: async (fn, arg) => { calls.push(['evaluate', arg]); return fn(arg); },
  title: async () => 'The title',
};
const context = {
  page, JSON, Object, String, Error, AbortController, setTimeout, clearTimeout,
  document: { title: 'T' }, localStorage: { setItem: (k, v) => calls.push(['setItem', k, v]) },
  fetch: async (url, init) => {
    calls.push(['fetch', url, Object.assign({}, init, { signal: typeof init.signal })]);
    if (answer.reject) throw new Error(answer.reject);
    return { type: answer.type || 'basic', status: answer.status, url: answer.url || url, text: async () => answer.text,
             headers: { forEach: (fn) => { for (const k of Object.keys(answer.headers || {})) fn(answer.headers[k], k); } } };
  },
};
vm.createContext(context);
const fn = vm.runInContext('(' + code + ')', context);
fn(page).then(
  (result) => process.stdout.write(JSON.stringify({ result: JSON.parse(JSON.stringify(result === undefined ? null : result)), calls })),
  (error) => process.stdout.write(JSON.stringify({ error: String(error), calls })),
);
"""


def run_in_node(tmp_path, code, answer=None):
    (tmp_path / "harness.js").write_text(HARNESS)
    (tmp_path / "code.js").write_text(code)
    done = subprocess.run(
        [NODE, str(tmp_path / "harness.js"), str(tmp_path / "code.js"), json.dumps(answer or {})],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
@pytest.mark.parametrize("value", HOSTILE)
def test_the_page_function_receives_exactly_the_values_given(tmp_path, value):
    out = run_in_node(tmp_path, page_module.evaluate_code("(a, b) => ({a, b, title: document.title})", [value, {"k": [value]}]))
    assert out["result"] == {"value": {"a": value, "b": {"k": [value]}, "title": "T"}}


@needs_node
def test_a_function_that_uses_a_comment_at_its_end_still_closes(tmp_path):
    out = run_in_node(tmp_path, page_module.evaluate_code("() => 41 + 1 // the answer", []))
    assert out["result"] == {"value": 42}


@needs_node
def test_run_code_runs_the_body_with_page_and_values(tmp_path):
    body = "const title = await page.title();\nreturn [title, values[0]]; // done"
    out = run_in_node(tmp_path, page_module.run_code_text(body, ["v\n1"]))
    assert out["result"] == {"value": ["The title", "v\n1"]}


@needs_node
def test_run_code_without_a_return_gives_nothing_back(tmp_path):
    out = run_in_node(tmp_path, page_module.run_code_text("const x = 1;", []))
    assert out["result"] == {}


@needs_node
def test_fetch_code_makes_one_fetch_with_the_request_and_returns_the_response(tmp_path):
    init = {"method": "POST", "headers": {"X-A": "b"}, "credentials": "include", "body": 'q"\n'}
    answer = {"status": 202, "url": "https://app.example.com/final", "text": "t x", "headers": {"x-id": "5"}}
    out = run_in_node(tmp_path, page_module.fetch_code("https://app.example.com/a", init, 4000), answer)
    assert out["result"] == {
        "status": 202, "url": "https://app.example.com/final", "headers": {"x-id": "5"}, "text": "t x",
    }
    fetched = [c for c in out["calls"] if c[0] == "fetch"]
    assert fetched == [["fetch", "https://app.example.com/a", {**init, "signal": "object", "redirect": "manual"}]]


@needs_node
@pytest.mark.parametrize("init", [{}, {"redirect": "follow"}, {"redirect": "error"}, {"method": "POST", "redirect": "manual"}])
def test_fetch_code_follows_no_redirect_whatever_the_request_says(tmp_path, init):
    out = run_in_node(tmp_path, page_module.fetch_code("https://app.example.com/a", init, 4000), {"status": 200, "text": ""})
    (fetched,) = [c for c in out["calls"] if c[0] == "fetch"]
    assert fetched[2]["redirect"] == "manual"


@needs_node
def test_fetch_code_reports_a_redirect_instead_of_going_on_with_it(tmp_path):
    answer = {"type": "opaqueredirect", "status": 0, "text": "never read", "url": "https://app.example.com/a"}
    out = run_in_node(tmp_path, page_module.fetch_code("https://app.example.com/a", {"method": "GET"}, 4000), answer)
    assert out["result"] == {"redirect": True}


def test_the_generated_fetch_asks_for_manual_redirects_and_lets_nothing_else_set_it():
    code = page_module.fetch_code("https://app.example.com/a", {"redirect": "follow"}, 4000)
    assert "redirect: 'manual'" in code
    assert code.index("Object.assign({}, init, {signal: controller.signal, redirect: 'manual'})") > 0
    assert "opaqueredirect" in code


@needs_node
def test_fetch_code_reports_a_network_error_instead_of_throwing(tmp_path):
    out = run_in_node(tmp_path, page_module.fetch_code("https://app.example.com/a", {"method": "GET"}, 4000),
                      {"reject": "Failed to fetch"})
    assert out["result"] == {"failed": "Failed to fetch"}


@needs_node
def test_every_generated_file_is_one_async_function_of_page(tmp_path):
    for code in (
        page_module.evaluate_code("() => 1", []),
        page_module.run_code_text("return 1;", []),
        page_module.fetch_code("https://app.example.com/", {}, 1000),
    ):
        assert code.startswith("async (page) => {\n") and code.endswith("}\n")
        run_in_node(tmp_path, code, {"status": 200, "text": ""})
