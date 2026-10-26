"""The sensitive step of the page facade: `with page.sensitive("passcode", cleanup): ...`.

A recipe that reads a value from one page and types it into another (a one-time passcode) holds a value that
the scrub cannot find once it is one digit to a box. The step keeps the failure paths of the tool from carrying
it out, and it does not rely on the recipe's cleanup for that: when the cleanup cannot say that the page is
clear, the tool closes the seat's Chrome.

These tests run on the fake browser. test_givebacks.py holds the cases of the bundled recipe (the two cases of the review),
and test_sensitive_live.py runs a step in a real Chrome.
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
from browser.recipe import NeedsPerson, Recipe, UsageError

from fake_browser import FakeBrowser, run_script, static_library

ORIGIN = "https://app.example.com"
VALUE = "482916"
GENERIC = "shop go: the passcode step failed"
CLOSED = GENERIC + ". The page could not be confirmed clear, so Chrome was closed."


@pytest.fixture
def browser(monkeypatch):
    fake = FakeBrowser(monkeypatch)
    monkeypatch.setattr(commands, "CLOSE_WAIT_SECONDS", 0.2)
    fake.url = f"{ORIGIN}/"
    fake.snapshot_text = lambda: f"- generic: the page says {VALUE}"
    return fake


def shop():
    return Recipe("shop", "A shop", origins=[ORIGIN])


def make_page():
    rec = shop()
    run = commands.Run("seat")
    call = recipe._Call("seat", run, False, session.stop_epoch("seat"), static_library(rec))
    return page_module.Page(call, rec), run


def run_verb(function, **kwargs):
    """A one-verb recipe `shop go` whose code is `function(page)`, run as a script."""
    rec = shop()

    @rec.verb("go", help="Go.")
    def go(page, args):
        return function(page)

    return run_script("recipe shop go", static_library(rec), **kwargs)


def cli_file(name, text):
    directory = session.cli_output_dir("seat")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    Path(path).write_text(text)
    return path


def everything(result):
    parts = [result.transcript(), json.dumps(result.steps), str(result.error)]
    for directory in (session.artifacts_dir("seat"), session.scratch_dir("seat")):
        for folder, _dirs, names in os.walk(directory):
            parts.extend(Path(folder, name).read_text(errors="replace") for name in names)
    return "\n".join(parts)


# --- the shape of a step -----------------------------------------------------------------------------------------


def test_a_step_that_ends_well_with_a_confirmed_cleanup_leaves_nothing_behind(browser):
    page, run = make_page()
    with page.sensitive("passcode", lambda: True):
        assert run.sensitive == ["passcode"] and not run.capturing
    assert run.sensitive == [] and run.uncleared == [] and run.capturing
    assert browser.closed == 0


def test_the_cleanup_runs_when_the_block_ends_and_sees_the_step_still_open(browser):
    page, run = make_page()
    states = []

    def cleanup():
        states.append((list(run.sensitive), list(run.uncleared), run.capturing))
        return True

    with page.sensitive("passcode", cleanup):
        assert states == []
    assert states == [(["passcode"], ["passcode"], False)]


def test_the_cleanup_runs_after_an_error_too_and_the_error_is_one_generic_line(browser):
    page, run = make_page()
    ran = []
    with pytest.raises(recipe.SensitiveFailure) as raised:
        with page.sensitive("passcode", lambda: ran.append(1) or True):
            raise plc.BrowserError(f"TimeoutError: fill('{VALUE}')")
    assert ran == [1]
    assert str(raised.value) == "the passcode step failed"
    assert VALUE not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__, "no traceback shows the original"
    assert run.sensitive == [] and run.capturing and browser.closed == 0


@pytest.mark.parametrize("error", [ValueError(VALUE), KeyError(VALUE), RuntimeError(VALUE), plc.BrowserError(VALUE),
                                    recipe.RecipeFailure(VALUE), OSError(VALUE)])
def test_any_exception_becomes_the_generic_line(browser, error):
    page, _run = make_page()
    with pytest.raises(recipe.SensitiveFailure) as raised:
        with page.sensitive("passcode", lambda: True):
            raise error
    assert str(raised.value) == "the passcode step failed" and VALUE not in repr(raised.value)


def test_a_failure_in_a_recipe_leaves_as_the_recipe_and_the_verb_and_the_step(browser):
    def code(page):
        with page.sensitive("passcode", lambda: True):
            raise plc.BrowserError(f"TimeoutError: fill('{VALUE}') Call log: {VALUE}")

    result = run_verb(code)
    assert not result.ok and result.error == GENERIC
    assert VALUE not in result.transcript() and VALUE not in json.dumps(result.steps)


def test_a_needs_a_person_and_a_usage_error_keep_their_text_when_the_cleanup_confirms(browser):
    def person(page):
        with page.sensitive("passcode", lambda: True):
            raise NeedsPerson("Outlook web is signed out", "Sign in by hand")

    result = run_verb(person)
    assert result.error == "needs a person: Outlook web is signed out. Sign in by hand."

    def usage(page):
        with page.sensitive("passcode", lambda: True):
            raise UsageError("GIVEBACKS_BASE is not set")

    result = run_verb(usage)
    assert result.error.splitlines()[0] == "GIVEBACKS_BASE is not set"
    assert browser.closed == 0


def test_a_needs_a_person_gives_way_to_the_generic_line_when_the_page_is_not_confirmed_clear(browser):
    def person(page):
        with page.sensitive("passcode", lambda: False):
            raise NeedsPerson("Outlook web is signed out", "Sign in by hand")

    result = run_verb(person)
    assert result.error == CLOSED and browser.closed == 1


def test_a_keyboard_interrupt_passes_through_after_the_cleanup_and_the_page_is_cleared(browser):
    page, run = make_page()
    ran = []
    with pytest.raises(KeyboardInterrupt):
        with page.sensitive("passcode", lambda: ran.append(1) or True):
            raise KeyboardInterrupt
    assert ran == [1] and run.sensitive == [] and run.uncleared == []


def test_a_keyboard_interrupt_with_a_cleanup_that_fails_closes_chrome_and_still_passes_through(browser):
    page, run = make_page()
    with pytest.raises(KeyboardInterrupt):
        with page.sensitive("passcode", lambda: False):
            raise KeyboardInterrupt
    assert browser.closed == 1 and run.uncleared == ["passcode"]


def test_the_name_and_the_cleanup_are_checked_before_anything_runs(browser):
    page, run = make_page()
    for name in ("", "Passcode", "pass code", "x" * 40, None, 7):
        with pytest.raises(ValueError):
            with page.sensitive(name, lambda: True):
                pass
    with pytest.raises(TypeError):
        with page.sensitive("passcode", None):
            pass
    assert run.sensitive == [] and browser.calls == []


# --- the cleanup must confirm ------------------------------------------------------------------------------------


@pytest.mark.parametrize("answer", [None, False, 0, 1, "yes", "True", [True], object()])
def test_only_true_confirms(browser, answer):
    page, run = make_page()
    with pytest.raises(recipe.SensitiveFailure) as raised:
        with page.sensitive("passcode", lambda: answer):
            pass
    assert str(raised.value) == "the passcode step failed. The page could not be confirmed clear, so Chrome was closed."
    assert browser.closed == 1 and run.uncleared == ["passcode"]


def test_a_cleanup_that_raises_is_not_a_confirmation_and_its_error_does_not_leave(browser):
    page, _run = make_page()

    def cleanup():
        raise plc.BrowserError(f"the page still says {VALUE}")

    with pytest.raises(recipe.SensitiveFailure) as raised:
        with page.sensitive("passcode", cleanup):
            pass
    assert VALUE not in str(raised.value) and browser.closed == 1


def test_a_block_that_worked_but_whose_cleanup_did_not_confirm_is_a_failure(browser):
    def code(page):
        with page.sensitive("passcode", lambda: False):
            page.log("everything worked")
        return "not reached"

    result = run_verb(code)
    assert not result.ok and result.error == CLOSED and browser.closed == 1


def test_chrome_is_closed_before_the_failure_is_reported(browser, monkeypatch):
    order = []
    page, _run = make_page()
    monkeypatch.setattr(session, "close_browser", lambda seat: order.append("close"))
    try:
        with page.sensitive("passcode", lambda: order.append("cleanup") or False):
            order.append("block")
    except recipe.SensitiveFailure:
        order.append("reported")
    assert order == ["block", "cleanup", "close", "reported"]


def test_a_chrome_that_will_not_close_is_said_and_capture_stays_off(browser):
    browser.chrome_stays = True
    page, run = make_page()
    with pytest.raises(recipe.SensitiveFailure) as raised:
        with page.sensitive("passcode", lambda: False):
            pass
    assert str(raised.value) == (
        "the passcode step failed. The page could not be confirmed clear, and Chrome could not be closed. "
        "Close it with `browser close --seat seat`."
    )
    assert run.sensitive == [] and run.uncleared == ["passcode"] and not run.capturing


def test_a_close_that_raises_counts_as_not_closed(browser, monkeypatch):
    def stuck(seat):
        raise plc.BrowserError("cannot reach the process")

    monkeypatch.setattr(session, "close_browser", stuck)
    page, run = make_page()
    with pytest.raises(recipe.SensitiveFailure, match="could not be closed"):
        with page.sensitive("passcode", lambda: False):
            pass
    assert not run.capturing


def test_after_a_close_capture_stays_off_for_the_rest_of_the_run(browser):
    """Chrome is gone, so there is nothing left to capture, and nothing is tried."""
    page, run = make_page()
    with pytest.raises(recipe.SensitiveFailure):
        with page.sensitive("passcode", lambda: False):
            pass
    assert browser.closed == 1 and run.uncleared == ["passcode"] and not run.capturing


# --- nothing of the page is captured ----------------------------------------------------------------------------


def test_the_failure_snapshot_is_not_taken_while_the_page_is_not_known_to_be_clear(browser):
    browser.chrome_stays = True

    def code(page):
        with page.sensitive("passcode", lambda: False):
            raise plc.BrowserError("boom")

    result = run_verb(code)
    assert not result.ok
    assert [path for path in result.files if path.endswith("failure.txt")] == []
    assert VALUE not in everything(result)


def test_the_failure_snapshot_is_taken_after_a_step_whose_page_is_confirmed_clear_of_the_page_it_left(browser):
    browser.snapshot_text = "- generic: the site"

    def code(page):
        with page.sensitive("passcode", lambda: True):
            raise plc.BrowserError("boom")

    result = run_verb(code)
    (failure,) = [path for path in result.files if path.endswith("failure.txt")]
    assert Path(failure).read_text() == "- generic: the site"


def test_the_failure_snapshot_is_not_even_tried_when_chrome_was_closed(browser, monkeypatch):
    """The page is gone with the Chrome. A snapshot would fail anyway; it must not be tried on a new one."""
    tried = []
    monkeypatch.setattr(commands, "_write_snapshot", lambda *args, **kwargs: tried.append(args))

    def code(page):
        with page.sensitive("passcode", lambda: False):
            raise plc.BrowserError("boom")

    result = run_verb(code)
    assert result.error == CLOSED and browser.closed == 1
    assert tried == [] and [path for path in result.files if path.endswith("failure.txt")] == []


def test_snapshot_screenshot_and_console_are_refused_while_the_step_is_open(browser):
    page, run = make_page()
    with page.sensitive("passcode", lambda: True):
        for verb in ("snap", "shot", "console"):
            with pytest.raises(plc.BrowserError, match="nothing of the page is captured while a sensitive step is open"):
                page.command(verb)
        with pytest.raises(plc.BrowserError, match="nothing of the page is captured"):
            page.screenshot("view")
        assert page.snapshot_text() == f"- generic: the page says {VALUE}", "reading the page into the recipe is allowed"
    assert browser.shots == []
    assert page.command("snap")  # and after the step they work again
    assert page.screenshot("view").endswith("view.png")


def test_the_capture_verbs_of_a_script_are_refused_while_the_page_is_not_known_clear(browser):
    run = commands.Run("seat")
    run.uncleared.append("passcode")
    for verb in ("snap", "shot", "console"):
        with pytest.raises(plc.BrowserError, match="nothing of the page is captured"):
            commands._step("seat", verb, [], run, False)
    assert browser.shots == []
    run.uncleared.clear()
    assert commands._step("seat", "snap", [], run, False)


def test_a_step_that_is_open_blocks_capture_for_a_nested_page_too(browser):
    rec = shop()
    inner = Recipe("inner", "Inner", origins=[ORIGIN])
    seen = []

    @inner.verb("look", help="Look.")
    def look(page, args):
        seen.append(page._call.run.capturing)
        return "looked"

    @rec.verb("go", help="Go.")
    def go(page, args):
        with page.sensitive("passcode", lambda: True):
            page.recipe("inner", "look")
        return "done"

    result = run_script("recipe shop go", static_library(rec, inner))
    assert result.ok, result.error
    assert seen == [False]


# --- video --------------------------------------------------------------------------------------------------------


def start_recording():
    session.save_video_state("seat", {"path": "/tmp/never-written.webm", "started_at": time.time(), "chapters": 0})


def test_a_step_does_not_open_while_a_video_is_recording(browser):
    """A recording keeps the pages a step shows, the mail with the passcode in it among them, and it is not scrubbed."""
    start_recording()
    page, run = make_page()
    with pytest.raises(plc.BrowserError, match="a video is recording") as raised:
        with page.sensitive("passcode", lambda: pytest.fail("the cleanup ran")):
            pytest.fail("the block ran")
    assert "video-stop" in str(raised.value)
    assert run.sensitive == [] and run.uncleared == [] and browser.calls == []


def test_a_script_that_films_the_login_gets_no_login(browser):
    """A sender sends: video-start, recipe gb login, video-stop. The login stops before it shows a page."""
    def code(page):
        with page.sensitive("passcode", lambda: True):
            page.log("shown")

    start_recording()
    result = run_verb(code)
    assert not result.ok and result.error.startswith("sensitive: a video is recording")
    assert "shown" not in result.transcript()


def test_a_video_cannot_be_started_while_the_step_is_open(browser):
    page, run = make_page()
    with page.sensitive("passcode", lambda: True):
        with pytest.raises(plc.BrowserError, match="nothing of the page is captured"):
            page.command("video-start")
    assert session.video_state("seat") is None
    assert "video-start" not in browser.verbs()


# --- page files --------------------------------------------------------------------------------------------------


def test_the_page_files_of_the_step_and_its_cleanup_are_deleted_and_older_ones_stay(browser):
    page, _run = make_page()
    before = cli_file("page-1.yml", "- textbox: before\n")
    written = {}

    def cleanup():
        written["cleanup"] = cli_file("page-3.yml", f"- textbox [ref=e4]: {VALUE}\n")
        return True

    with page.sensitive("passcode", cleanup):
        written["block"] = cli_file("page-2.yml", f"- generic: {VALUE}\n")
    assert os.path.exists(before)
    assert not os.path.exists(written["block"]) and not os.path.exists(written["cleanup"])


def test_the_page_files_are_deleted_when_the_step_fails_and_when_the_cleanup_fails(browser):
    page, _run = make_page()
    written = []

    def cleanup():
        written.append(cli_file("page-5.yml", f"- generic: {VALUE}\n"))
        raise plc.BrowserError("no")

    with pytest.raises(recipe.SensitiveFailure):
        with page.sensitive("passcode", cleanup):
            written.append(cli_file("page-4.yml", f"- generic: {VALUE}\n"))
            raise plc.BrowserError("no")
    assert len(written) == 2 and not any(os.path.exists(path) for path in written)


def test_the_page_files_are_deleted_when_the_run_is_interrupted(browser):
    page, _run = make_page()
    with pytest.raises(KeyboardInterrupt):
        with page.sensitive("passcode", lambda: True):
            written = cli_file("page-4.yml", f"- generic: {VALUE}\n")
            raise KeyboardInterrupt
    assert not os.path.exists(written)


def test_a_file_that_was_there_and_grew_stays(browser):
    page, _run = make_page()
    log = cli_file("console-1.log", "[LOG] before\n")
    with page.sensitive("passcode", lambda: True):
        with open(log, "a") as handle:
            handle.write("[LOG] during\n")
    assert Path(log).read_text() == "[LOG] before\n[LOG] during\n"


# --- steps inside steps, and a stop -----------------------------------------------------------------------------


def test_steps_nest_capture_stays_off_until_the_outer_one_ends(browser):
    page, run = make_page()
    with page.sensitive("outer", lambda: True):
        with page.sensitive("inner", lambda: True):
            assert run.sensitive == ["outer", "inner"]
        assert run.sensitive == ["outer"] and not run.capturing
    assert run.sensitive == [] and run.capturing


def test_a_failed_inner_step_reaches_the_outer_one_as_a_generic_line_and_the_outer_makes_it_its_own(browser):
    page, _run = make_page()
    with pytest.raises(recipe.SensitiveFailure) as raised:
        with page.sensitive("outer", lambda: True):
            with page.sensitive("inner", lambda: True):
                raise plc.BrowserError(VALUE)
    assert str(raised.value) == "the outer step failed" and VALUE not in repr(raised.value)


def test_an_unconfirmed_inner_step_closes_chrome_once_and_the_outer_goes_on_to_fail(browser):
    page, run = make_page()
    with pytest.raises(recipe.SensitiveFailure):
        with page.sensitive("outer", lambda: True):
            with page.sensitive("inner", lambda: False):
                pass
    assert browser.closed == 1 and run.sensitive == [] and run.uncleared == ["inner"] and not run.capturing


def test_a_stop_in_a_step_is_reported_as_the_stop(browser):
    def code(page):
        with page.sensitive("passcode", lambda: True):
            session.bump_stop_epoch("seat")
            page.wait(1)

    result = run_verb(code)
    assert not result.ok and result.error == session.STOP_MESSAGE
    assert [path for path in result.files if path.endswith("failure.txt")] == []


def test_a_stop_that_lands_before_the_step_opens_stops_it_before_anything_is_touched(browser):
    page, run = make_page()
    session.bump_stop_epoch("seat")
    with pytest.raises(plc.BrowserError, match=session.STOP_MESSAGE):
        with page.sensitive("passcode", lambda: pytest.fail("the cleanup ran")):
            pytest.fail("the block ran")
    assert run.sensitive == [] and browser.calls == []


# --- tabs -------------------------------------------------------------------------------------------------------


def test_tabs_are_the_addresses_in_order_without_the_titles(browser):
    page, _run = make_page()
    browser.tabs = [f"{ORIGIN}/", "https://outlook.office.com/mail/", "about:blank"]
    browser.tab = 1
    assert page.tabs() == [f"{ORIGIN}/", "https://outlook.office.com/mail/", "about:blank"]


def test_a_title_with_brackets_does_not_confuse_the_list(browser, monkeypatch):
    page, _run = make_page()
    monkeypatch.setattr(
        plc, "run", lambda seat, *args, **kwargs: "### Result\n- 0: (current) [Inbox [3] (work)](https://outlook.office.com/mail/)\n- 1: [](about:blank)\n",
    )
    assert page.tabs() == ["https://outlook.office.com/mail/", "about:blank"]


@pytest.mark.parametrize("output", [
    "### Result\n", "### Result\nno tabs\n", "### Result\n- 1: [](about:blank)\n",
    "### Result\n- 0: [](about:blank)\nstray\n", "### Result\n- 0: [](about:blank)\n- 0: [](about:blank)\n",
])
def test_a_list_that_cannot_be_read_is_an_error_and_not_an_empty_list(browser, monkeypatch, output):
    page, _run = make_page()
    monkeypatch.setattr(plc, "run", lambda seat, *args, **kwargs: output)
    with pytest.raises(plc.BrowserError, match="tabs:"):
        page.tabs()


def test_a_tab_address_is_scrubbed(browser):
    page, run = make_page()
    run.hide("PASSCODE", VALUE)
    browser.tabs = [f"{ORIGIN}/?code={VALUE}"]
    assert page.tabs() == [f"{ORIGIN}/?code=[secret:PASSCODE]"]


# --- the lines under the step ------------------------------------------------------------------------------------
#
# A recipe's step shows, under its own line, one line for each page call it made. Outside a sensitive step a line
# ends with what the call returned (`click Read mail -> a > b`). Inside one, a page call may have read the mail,
# so its line shows the verb and its target, and nothing the page or the server said.

MAIL = "Your code is 482916"


def test_a_line_of_a_sensitive_step_shows_the_verb_and_the_target_and_no_result(browser):
    browser.texts["#message"] = MAIL
    browser.links["Read mail"] = f"{ORIGIN}/mail/482916"

    def code(page):
        with page.sensitive("passcode", lambda: True):
            page.click("Read mail")
            page.text("#message")
            page.url()
            page.check("Trust")
            page.hover("Trust")
        return "done"

    result = run_verb(code)
    assert result.ok, result.error
    lines = [line.strip() for line in result.steps[0]["output"].splitlines()]
    assert lines[:4] == ["click Read mail", "text #message", "check Trust", "hover Trust"]
    assert "482916" not in result.transcript() and MAIL not in result.transcript()


def test_the_same_calls_outside_a_sensitive_step_still_show_what_came_back(browser):
    browser.texts["#message"] = MAIL
    result = run_verb(lambda page: page.text("#message"))
    assert "text #message -> Your code is 482916" in result.steps[0]["output"]


def test_text_given_to_a_call_after_its_target_is_left_off_inside_a_sensitive_step(browser):
    def code(page):
        with page.sensitive("passcode", lambda: True):
            page.fill("#box", "typed text from the page")
            page.select("#menu", "a value from the page")
            page.press("Enter")
            page.find("a phrase from the page")
        return "done"

    result = run_verb(code)
    lines = [line.strip() for line in result.steps[0]["output"].splitlines()]
    assert lines[:4] == ["fill #box", "select #menu", "press", "find"]
    assert "from the page" not in result.transcript()


def test_the_address_a_wait_found_is_not_shown_inside_a_sensitive_step(browser):
    browser.url = f"{ORIGIN}/mail/482916"

    def code(page):
        with page.sensitive("passcode", lambda: True):
            page.wait_for_url("/mail", 1)
        page.wait_for_url("/mail", 1)
        return "done"

    result = run_verb(code)
    lines = [line.strip() for line in result.steps[0]["output"].splitlines()]
    assert lines[0] == "wait-for-url /mail"
    assert lines[1] == f"wait-for-url /mail -> {ORIGIN}/mail/482916", "outside the step the line is as it was"


def test_what_a_fetch_and_a_list_of_tabs_answered_is_not_shown_inside_a_sensitive_step(browser):
    browser.fetches[f"{ORIGIN}/api"] = {"status": 200, "text": "{}"}

    def code(page):
        with page.sensitive("passcode", lambda: True):
            page.fetch(f"{ORIGIN}/api")
            page.tabs()
        page.tabs()
        return "done"

    result = run_verb(code)
    lines = [line.strip() for line in result.steps[0]["output"].splitlines()]
    assert lines[:2] == [f"fetch GET {ORIGIN}/api", "tabs"]
    assert lines[2] == "tabs -> 1", "outside the step the line is as it was"


def test_a_failed_sensitive_step_shows_the_lines_of_its_calls_without_results(browser):
    browser.texts["#message"] = MAIL

    def code(page):
        with page.sensitive("passcode", lambda: True):
            page.text("#message")
            raise plc.BrowserError(f"TimeoutError: {MAIL}")

    result = run_verb(code)
    assert not result.ok and result.error == GENERIC
    assert result.steps[0]["output"].strip() == "text #message"
    assert "482916" not in result.transcript() and "482916" not in json.dumps(result.steps)


def test_the_lines_of_a_nested_step_show_no_result_either(browser):
    browser.texts["#message"] = MAIL

    def code(page):
        with page.sensitive("outer", lambda: True):
            with page.sensitive("inner", lambda: True):
                page.text("#message")
            page.text("#message")
        return "done"

    result = run_verb(code)
    lines = [line.strip() for line in result.steps[0]["output"].splitlines()]
    assert lines[:2] == ["text #message", "text #message"]
