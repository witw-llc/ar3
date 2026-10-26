"""The tool opens no local file.

A seat's Chrome can show the store of another seat as a page: a `file:` page that a link leads to, read with
`text body`, attached by `snap`, shot by `shot`. The scrub of this seat does not know the other seat's secrets,
and a screenshot is never scrubbed. Three layers hold, and each is tested here on its own:

- the tool never passes playwright-cli the switch that lets its driver open local files, so a driver the tool
  starts has none (`plc.cli_env`);
- `go` refuses every `file:` URL (test_commands.py has the table of spellings);
- a step that ends on a `file:` page fails, the page is sent to about:blank, and what the step wrote is removed.
  The facade does the same after each call that can navigate. This holds for a driver started some other way,
  and for a page left from before.

The live test with a real Chrome is test_local_files_live.py.
"""
import json
import os
import time
from pathlib import Path

import pytest

from browser import commands
from browser import page as page_module
from browser import plc
from browser import recipe
from browser import session
from browser.recipe import Recipe

from fake_browser import FakeBrowser, run_script, static_library

ORIGIN = "https://app.example.com"
MARKER = "SYNTHETIC-PROTECTED-MARKER-2f9c41"
OUTSIDE = "file:///outside/page.html"
PROTECTED = "file:///state/other-seat/state-marker.txt"
LOCAL = "local files are not opened"
SENT_AWAY = "The page was sent to about:blank."


@pytest.fixture(params=["devtools", "driver"])
def browser(monkeypatch, request):
    """A fake browser whose page shows the marker whenever it is a local file, as the real one would. The
    addresses of its pages are read from its DevTools endpoint, or, when that does not answer (a Chrome whose
    port the owner fixed), by asking the driver for the page it drives. Every test here holds on both."""
    fake = FakeBrowser(monkeypatch)
    fake.devtools = request.param == "devtools"
    fake.url = f"{ORIGIN}/"
    fake.links["protected"] = PROTECTED
    fake.links["outside"] = OUTSIDE
    fake.snapshot_text = lambda: f"- generic: {MARKER}" if fake.url.startswith("file:") else "- generic: a page"
    fake.texts["body"] = lambda: MARKER if fake.url.startswith("file:") else "a page"
    return fake


def run(script, **kwargs):
    return run_script(script, static_library(), **kwargs)


def seat_files():
    """The text of every file in the seat's artifacts and scratch directories."""
    found = []
    for directory in (session.artifacts_dir("seat"), session.scratch_dir("seat")):
        for folder, _dirs, names in os.walk(directory):
            found.extend(Path(folder, name).read_bytes().decode("utf-8", "replace") for name in names)
    return found


def everything(result):
    return "\n".join([result.transcript(), json.dumps(result.steps), str(result.error), *seat_files()])


def cli_file(name, text):
    directory = session.cli_output_dir("seat")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    Path(path).write_text(text)
    return path


# --- the switch ---------------------------------------------------------------------------------------------------


def test_the_environment_of_a_playwright_cli_call_has_no_file_access_switch(monkeypatch):
    monkeypatch.setenv(plc.FILE_ACCESS_ENV, "1")
    monkeypatch.setenv("KEPT", "yes")
    env = plc.cli_env()
    assert plc.FILE_ACCESS_ENV not in env and env["KEPT"] == "yes"
    assert plc.FILE_ACCESS_ENV == "PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS"


@pytest.mark.parametrize("value", ["1", "true", "0", ""])
def test_every_value_of_the_switch_is_dropped(monkeypatch, value):
    monkeypatch.setenv(plc.FILE_ACCESS_ENV, value)
    assert plc.FILE_ACCESS_ENV not in plc.cli_env()


class _Done:
    returncode, stdout, stderr = 0, "### Result\nok\n", ""


def test_run_hands_the_driver_an_environment_without_the_switch(monkeypatch):
    monkeypatch.setenv(plc.FILE_ACCESS_ENV, "1")
    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    seen = []
    monkeypatch.setattr(plc.subprocess, "run", lambda cmd, **kwargs: seen.append(kwargs) or _Done())
    plc.run("seat", "attach", "--cdp=http://127.0.0.1:1")
    assert len(seen) == 1 and plc.FILE_ACCESS_ENV not in seen[0]["env"]
    assert seen[0]["env"]["PATH"] == os.environ["PATH"], "the rest of the environment is passed on"


def test_the_list_of_sessions_is_asked_without_the_switch_too(monkeypatch):
    monkeypatch.setenv(plc.FILE_ACCESS_ENV, "1")
    monkeypatch.setattr(plc.shutil, "which", lambda name: "/usr/bin/playwright-cli")
    seen = []
    monkeypatch.setattr(plc.subprocess, "run", lambda cmd, **kwargs: seen.append(kwargs) or _Done())
    plc.sessions()
    assert len(seen) == 1 and plc.FILE_ACCESS_ENV not in seen[0]["env"]


def test_the_private_code_calls_go_through_the_same_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(plc.FILE_ACCESS_ENV, "1")
    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    seen = []
    monkeypatch.setattr(plc.subprocess, "run", lambda cmd, **kwargs: seen.append(kwargs) or _Done())
    plc.run_code_file("seat", "async (page) => 1", str(tmp_path))
    assert len(seen) == 1 and plc.FILE_ACCESS_ENV not in seen[0]["env"]


def test_the_variable_in_the_callers_environment_does_not_change_the_refusal_of_go(browser, monkeypatch):
    """The reviewed chain starts with `go` to a page of its own. With the switch in the caller's environment, the
    tool still opens nothing and the chain returns no content."""
    monkeypatch.setenv(plc.FILE_ACCESS_ENV, "1")
    result = run(f"go {OUTSIDE}\nclick protected\ntext body\nsnap")
    assert not result.ok and len(result.steps) == 1 and LOCAL in result.error
    assert not [call for call in browser.calls if call[0] in ("goto", "click")]
    assert MARKER not in everything(result)


# --- what counts as a local file ----------------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "file:///etc/hostname", "FILE:///x", " file:///x", "\tfi\nle:x", "\x00file:/x",
    "view-source:file:///etc/hostname", "VIEW-SOURCE:FILE:///x", "filesystem:file:///x",
    "view-source:view-source:file:///x", " view-source: file:///x", " " * 500 + "file:///x", "\n" * 300 + "FILE:/x",
])
def test_these_are_local_files(url):
    assert commands.is_local_file(url)


@pytest.mark.parametrize("url", [
    "https://example.com/", "http://127.0.0.1:8000/file:", "about:blank", "", "view-source:https://example.com/file:",
    "filesystem:https://example.com/temporary/x", "data:text/html,file:", None, 7, b"file:///x", "f i l e:///x",
    " " * 500 + "https://example.com/",
])
def test_these_are_not(url):
    assert not commands.is_local_file(url)


def test_a_page_that_ends_behind_view_source_is_a_local_file_too(browser):
    browser.links["protected"] = "view-source:" + PROTECTED
    result = run("click protected")
    assert not result.ok and LOCAL in result.error
    assert browser.url == "about:blank"


# --- a step that ends on a local file ----------------------------------------------------------------------------


def test_a_click_into_a_local_file_fails_the_step_and_the_page_is_sent_away(browser):
    """The chain with a driver that allows local files: a page of the outside, a link, the text of the body."""
    browser.url = OUTSIDE  # a driver started some other way, or a page left from before
    result = run("click protected\ntext body\nsnap")
    assert not result.ok
    assert len(result.steps) == 1, "the script stops at the first failure: `text body` and `snap` never ran"
    assert result.error == f"click: the page is a local file, and {LOCAL}. {SENT_AWAY}"
    assert browser.url == "about:blank" and ("goto", "about:blank") in browser.calls
    assert MARKER not in everything(result)


def test_the_text_of_a_local_page_is_not_returned(browser):
    browser.url = PROTECTED
    result = run("text body")
    assert not result.ok and result.error == f"text: the page is a local file, and {LOCAL}. {SENT_AWAY}"
    assert MARKER not in everything(result)
    assert result.steps[0]["command"] == "text body" and "output" not in result.steps[0]


def test_a_snapshot_of_a_local_page_is_not_attached_and_its_file_is_removed(browser):
    browser.url = PROTECTED
    result = run("snap")
    assert not result.ok and LOCAL in result.error
    assert [path for path in result.files if path.endswith("snapshot.txt")] == []
    assert not [name for name in os.listdir(session.artifacts_dir("seat")) if name.endswith("-snapshot.txt")]
    assert MARKER not in everything(result)


def test_a_screenshot_of_a_local_page_is_not_attached_and_its_file_is_removed(browser):
    browser.url = PROTECTED
    result = run("shot")
    assert not result.ok and LOCAL in result.error
    assert [path for path in result.files if path.endswith("screen.png")] == []
    assert not [name for name in os.listdir(session.artifacts_dir("seat")) if name.endswith("screen.png")]


def test_the_page_files_the_step_wrote_are_removed_and_older_ones_stay(browser):
    old = cli_file("page-old.yml", "- generic: an older page\n")
    hour_ago = time.time() - 3600
    os.utime(old, (hour_ago, hour_ago))

    def write_page_file(*args):
        cli_file("page-protected.yml", f"- generic: {MARKER}\n")

    browser.hooks["click"] = write_page_file
    result = run("click protected")
    assert not result.ok and LOCAL in result.error
    assert not os.path.exists(os.path.join(session.cli_output_dir("seat"), "page-protected.yml"))
    assert os.path.exists(old)
    assert MARKER not in everything(result)


def test_the_files_an_earlier_step_attached_stay(browser):
    result = run("snap\nclick protected")
    assert not result.ok and result.steps[0]["ok"] is True
    assert [path for path in result.files if path.endswith("-snapshot.txt") and os.path.exists(path)]


def test_a_step_that_ends_on_an_ordinary_page_is_left_alone(browser):
    browser.links["next"] = f"{ORIGIN}/next"
    result = run("click next\ntext body\nsnap")
    assert result.ok, result.error
    assert browser.url == f"{ORIGIN}/next" and ("goto", "about:blank") not in browser.calls


def test_a_blank_page_is_no_local_file(browser):
    browser.url = "about:blank"
    result = run("url")
    assert result.ok and result.steps[0]["output"] == "about:blank"


def test_every_step_that_uses_the_page_is_looked_at(browser):
    browser.url = PROTECTED
    for line in ("url", "text body", "find x", "wait-for-url file", "press Enter", "scroll 10", "reload"):
        browser.url = PROTECTED
        result = run(line)
        assert not result.ok and LOCAL in result.error, line
        assert browser.url == "about:blank", line


def test_a_step_that_does_not_use_the_page_asks_nothing_of_it(browser, monkeypatch):
    monkeypatch.setattr(session, "current_url", lambda seat: pytest.fail("the address was read"))
    result = run("close")
    assert result.ok and browser.closed == 1
    result = run_script("recipe", static_library())
    assert result.ok, result.error


def test_an_address_that_cannot_be_read_leaves_the_step_to_its_own_result(browser, monkeypatch):
    def unreadable(seat):
        raise plc.BrowserError("the page is busy with a dialog")

    browser.links["next"] = f"{ORIGIN}/next"
    monkeypatch.setattr(session, "current_url", unreadable)
    result = run("click next")
    assert result.ok, result.error


def test_when_the_page_cannot_be_sent_away_chrome_is_closed(browser):
    def refuse(url):
        if url == "about:blank":
            raise plc.BrowserError("Target page, context or browser has been closed")

    browser.hooks["goto"] = refuse
    browser.url = PROTECTED
    result = run("url")
    assert not result.ok
    assert result.error == (
        f"url: the page is a local file, and {LOCAL}. The page could not be sent to about:blank, so Chrome was closed."
    )
    assert browser.closed == 1


def test_when_neither_works_the_step_says_so(browser, monkeypatch):
    def refuse(url):
        raise plc.BrowserError("no")

    def stuck(seat):
        raise plc.BrowserError("no")

    browser.hooks["goto"] = refuse
    monkeypatch.setattr(session, "close_browser", stuck)
    browser.url = PROTECTED
    result = run("url")
    assert not result.ok
    assert result.error.endswith("The page could not be sent to about:blank, and Chrome could not be closed.")


def test_the_failure_snapshot_is_never_of_a_local_page(browser):
    """A step that fails while the page is a local file: the page is sent away first, and the snapshot is of what is left."""
    browser.url = PROTECTED
    result = run("assert-url zzz")
    assert not result.ok and LOCAL in result.error
    assert browser.url == "about:blank"
    for path in result.files:
        assert MARKER not in Path(path).read_text()
    assert MARKER not in everything(result)


def test_a_failure_that_comes_before_any_step_uses_the_page_still_does_not_snapshot_a_local_page(browser):
    """The first step of the script fails without touching the page, and the seat was left on a local page."""
    browser.url = PROTECTED
    result = run("video-stop")
    assert not result.ok
    assert browser.url == "about:blank"
    assert MARKER not in everything(result)


def test_a_recipe_that_reads_a_local_page_and_then_fails_leaves_none_of_it_in_its_failure(browser):
    """The page calls a failed recipe step reports are its output. A page left from before is a local file, and
    a call that read it must not report what it read."""
    rec = Recipe("peek", "Peeks", origins=[ORIGIN])

    @rec.verb("go", help="Read the page, then fail.")
    def go(page, args):
        page.text("body")
        page.find("anything")
        raise plc.BrowserError("not signed in")

    browser.url = PROTECTED
    result = run_script("recipe peek go", static_library(rec))
    assert not result.ok
    assert result.error == f"recipe: the page is a local file, and {LOCAL}. {SENT_AWAY}", "the local page replaces the failure"
    assert MARKER not in everything(result)
    assert browser.url == "about:blank"


def start_recording():
    """A recording that `video-start` began: its state, and the file the driver is filling."""
    directory = session.artifacts_dir("seat")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "20260101T000000-recording.webm")
    Path(path).write_bytes(b"frames of a local page")
    session.save_video_state("seat", {"path": path, "started_at": time.time(), "chapters": 0})
    return path


def test_a_recording_that_runs_when_the_page_turns_out_to_be_a_local_file_is_stopped_and_discarded(browser):
    """The frames of a local page are pixels, and a later `video-stop` would attach them."""
    path = start_recording()
    result = run("click protected")
    assert not result.ok
    assert result.error == (
        f"click: the page is a local file, and {LOCAL}. {SENT_AWAY} The recording was stopped and discarded."
    )
    assert ("video-stop",) in browser.calls
    assert not os.path.exists(path) and session.video_state("seat") is None
    later = run("video-stop")
    assert not later.ok and "no recording in progress" in later.error


def test_a_recording_is_left_alone_when_the_page_is_not_a_local_file(browser):
    path = start_recording()
    browser.links["next"] = f"{ORIGIN}/next"
    result = run("click next")
    assert result.ok, result.error
    assert os.path.exists(path) and session.video_state("seat") is not None and ("video-stop",) not in browser.calls


def test_only_a_recording_inside_the_artifacts_of_the_seat_is_removed(browser, tmp_path):
    outside = tmp_path / "elsewhere.webm"
    outside.write_bytes(b"not ours")
    session.save_video_state("seat", {"path": str(outside), "started_at": 0, "chapters": 0})
    browser.url = PROTECTED
    result = run("url")
    assert not result.ok and outside.exists()
    assert session.video_state("seat") is None, "the state is forgotten all the same"


def test_a_failing_step_on_an_ordinary_page_keeps_its_own_failure_and_its_output(browser):
    rec = Recipe("peek", "Peeks", origins=[ORIGIN])

    @rec.verb("go", help="Read the page, then fail.")
    def go(page, args):
        page.text("body")
        raise plc.BrowserError("not signed in")

    result = run_script("recipe peek go", static_library(rec))
    assert not result.ok and result.error == "not signed in"
    assert "text body -> a page" in result.steps[0]["output"]


def test_the_failure_snapshot_of_an_ordinary_page_is_still_taken(browser):
    result = run("assert-url zzz")
    assert not result.ok
    (failure,) = [path for path in result.files if path.endswith("failure.txt")]
    assert "a page" in Path(failure).read_text()


# --- a page other than the one the driver drives ------------------------------------------------------------------


@pytest.fixture
def tabs(monkeypatch):
    """A fake browser that answers the DevTools endpoint, with the driven page on an ordinary address."""
    fake = FakeBrowser(monkeypatch)
    fake.url = f"{ORIGIN}/"
    fake.snapshot_text = lambda: f"- generic: {MARKER}" if fake.url.startswith("file:") else "- generic: a page"
    return fake


def test_a_local_page_in_another_tab_fails_the_step_and_the_tab_is_closed(tabs):
    """Any page of Chrome counts, not only the one the driver drives: a tab left from before, or opened by hand."""
    tabs.tabs.append(PROTECTED)
    result = run("url")
    assert not result.ok
    assert result.error == f"url: the page is a local file, and {LOCAL}. {SENT_AWAY} 1 other local page was closed."
    assert tabs.tabs == ["about:blank"], "the driven page was sent away and the other tab is gone"
    assert MARKER not in everything(result)


def test_every_local_tab_is_closed_and_the_ordinary_ones_stay(tabs):
    tabs.tabs += [PROTECTED, f"{ORIGIN}/b", OUTSIDE, "view-source:" + PROTECTED]
    result = run("url")
    assert result.error == f"url: the page is a local file, and {LOCAL}. {SENT_AWAY} 3 other local pages were closed."
    assert tabs.tabs == ["about:blank", f"{ORIGIN}/b"]


def test_a_tab_that_will_not_close_takes_chrome_with_it(tabs):
    tabs.tabs.append(PROTECTED)
    tabs.stuck = {PROTECTED}
    result = run("url")
    assert result.error == (
        f"url: the page is a local file, and {LOCAL}. A page in another tab could not be closed, so Chrome was closed."
    )
    assert tabs.closed == 1


def test_a_tab_that_will_not_close_and_a_chrome_that_will_not_go_are_both_said(tabs, monkeypatch):
    def stuck(seat):
        raise plc.BrowserError("no")

    tabs.tabs.append(PROTECTED)
    tabs.stuck = {PROTECTED}
    monkeypatch.setattr(session, "close_browser", stuck)
    result = run("url")
    assert result.error.endswith("A page in another tab could not be closed, and Chrome could not be closed.")


def test_the_page_the_driver_drives_is_sent_away_whichever_tab_holds_the_local_page(tabs):
    tabs.tabs.append(PROTECTED)
    tabs.tab = 1
    result = run("url")
    assert not result.ok and LOCAL in result.error
    assert ("goto", "about:blank") in tabs.calls and tabs.tabs == [f"{ORIGIN}/", "about:blank"]


def test_the_check_of_a_step_reads_the_endpoint_and_does_not_ask_the_driver(tabs, monkeypatch):
    tabs.links["next"] = f"{ORIGIN}/next"
    monkeypatch.setattr(session, "current_url", lambda seat: pytest.fail("the driver was asked for the address"))
    result = run("click next")
    assert result.ok, result.error


def test_the_failure_snapshot_is_not_taken_while_another_tab_holds_a_local_page(tabs):
    tabs.tabs.append(PROTECTED)
    result = run("assert-url zzz")
    assert not result.ok and LOCAL in result.error
    assert MARKER not in everything(result)


def test_when_the_endpoint_does_not_answer_the_driver_is_asked_for_the_page_it_drives(tabs, monkeypatch):
    """A Chrome whose port the owner fixed writes no record of it. The check falls back to the one page the driver
    drives, and a local page in another tab is then not seen (the limit of that fallback)."""
    tabs.devtools = False
    tabs.tabs.append(PROTECTED)
    seen = []
    real = session.current_url
    monkeypatch.setattr(session, "current_url", lambda seat: seen.append(1) or real(seat))
    result = run("click next")
    assert result.ok or LOCAL not in str(result.error)
    assert seen, "the driver was asked"
    tabs.url = PROTECTED
    result = run("url")
    assert not result.ok and LOCAL in result.error


# --- the facade ---------------------------------------------------------------------------------------------------


def shop():
    return Recipe("shop", "A shop", origins=[ORIGIN])


def make_page():
    rec = shop()
    result = commands.Run("seat")
    call = recipe._Call("seat", result, False, session.stop_epoch("seat"), static_library(rec))
    result.begin_step()
    return page_module.Page(call, rec), result


def go_local(browser):
    def land(*args):
        browser.url = PROTECTED

    return land


CALLS = {
    "click": (lambda p: p.click("#a"), "click"),
    "press": (lambda p: p.press("Enter"), "press"),
    "select": (lambda p: p.select("#a", "b"), "select"),
    "check": (lambda p: p.check("#a"), "check"),
    "uncheck": (lambda p: p.uncheck("#a"), "uncheck"),
    "back": (lambda p: p.back(), "go-back"),
    "forward": (lambda p: p.forward(), "go-forward"),
    "reload": (lambda p: p.reload(), "reload"),
    "new_tab": (lambda p: p.new_tab(f"{ORIGIN}/a"), "tab-new"),
    "select_tab": (lambda p: p.select_tab(0), "tab-select"),
    "close_tab": (lambda p: p.close_tab(), "tab-close"),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_call_that_lands_on_a_local_file_fails_and_the_page_is_sent_away(browser, name):
    page, _result = make_page()
    call, verb = CALLS[name]
    browser.tabs.append(f"{ORIGIN}/b")  # a tab to close and a tab to go back to
    browser.history[1] = []
    browser.tab = 1
    browser.hooks[verb] = go_local(browser)
    with pytest.raises(plc.BrowserError) as raised:
        call(page)
    assert str(raised.value) == f"{name}: the page is a local file, and {LOCAL}. {SENT_AWAY}"
    assert browser.url == "about:blank"


def test_a_wait_for_url_that_lands_on_a_local_file_fails_too(browser):
    page, _result = make_page()
    browser.url = PROTECTED
    with pytest.raises(plc.BrowserError, match=LOCAL):
        page.wait_for_url("file:", 1)
    assert browser.url == "about:blank"


def test_a_recipe_that_catches_the_failure_finds_the_page_blank(browser):
    """The failure is not the recipe's to swallow: by the time it can, the page is gone."""
    page, _result = make_page()
    browser.links["a"] = PROTECTED
    try:
        page.click("a")
    except plc.BrowserError:
        pass
    assert page.url() == "about:blank"
    assert page.snapshot_text() == "- generic: a page"
    assert page.text("body") == "a page"


def test_the_facade_removes_the_files_the_step_wrote_before_the_failure(browser):
    page, result = make_page()
    browser.links["a"] = PROTECTED
    kept = cli_file("page-before.yml", "- generic: before\n")
    hour_ago = time.time() - 3600
    os.utime(kept, (hour_ago, hour_ago))
    browser.hooks["click"] = lambda *args: cli_file("page-protected.yml", f"- generic: {MARKER}\n")
    with pytest.raises(plc.BrowserError):
        page.click("a")
    assert os.path.exists(kept)
    assert not os.path.exists(os.path.join(session.cli_output_dir("seat"), "page-protected.yml"))
    assert MARKER not in "\n".join(seat_files())


def test_a_recipe_that_swallows_the_error_of_a_local_page_reads_a_blank_page_and_nothing_leaves(browser):
    rec = Recipe("swallow", "Swallows", origins=[ORIGIN])

    @rec.verb("go", help="Click, ignore the failure, read the page.")
    def go(page, args):
        try:
            page.click("protected")
        except plc.BrowserError:
            pass
        return page.text("body")

    result = run_script("recipe swallow go", static_library(rec))
    assert result.ok, result.error
    assert result.steps[0]["output"].splitlines()[-1] == "a page"
    assert MARKER not in everything(result)
