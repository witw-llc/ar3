"""A value that is stored for a seat, or that a script hid, is in no command line of playwright-cli.

`ps` shows every local user the arguments of every process. A value gets into one in two ways. The caller's
text can hold it (`fill #user <value>`, `go https://site/<value>`). Or the page can: a page can make an id, a
class or an address from a value it was given, and a tool that names an element by its id puts the value in the
command line (`click #<value>`). Three layers hold the rule, and each is tested here on its own:

- the text of the caller and the selector of a page call travel in a private file (`plc.call_in_page`), so the
  target the caller typed is in no command line;
- `resolve` names an element by a selector that holds no such value, or by its path of tag names and positions
  when the best one does, or says it has no name for it;
- `plc.run` looks at every argument before it starts the driver, and refuses, naming nothing, when one still
  holds a value.

The live test with a real Chrome and a page that does this is test_output_paths_live.py.
"""
import json
import os
from urllib.parse import quote, quote_plus
import html

import pytest

from browser import commands
from browser import plc
from browser import resolve
from browser import secretstore
from browser import session
from browser.recipe import Recipe

from fake_browser import static_library
from test_commands import FakeCli, ORIGIN, _Completed, _cli

HOSTILE = "SyntheticUserA7492"
TRICKY = '@pa"ss\\wo{rd}: #x\té☃'


def guard(**secrets):
    return secretstore.Scrubber({name: {"value": value, "origins": [ORIGIN]} for name, value in secrets.items()})


class Spawns:
    """Every command line the real `plc.run` would start, at the level of the subprocess."""

    def __init__(self, monkeypatch):
        self.argv = []
        monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
        monkeypatch.setattr(plc.subprocess, "run", self.run)

    def run(self, cmd, **kwargs):
        self.argv.append(list(cmd))
        return _Completed(0, "### Result\nok\n")


@pytest.fixture(autouse=True)
def no_guard_left_over(monkeypatch):
    """A test that registers a scrubber with `plc.protect` must not leave it for the next."""
    monkeypatch.setattr(plc, "_protected", {})


# --- the last gate: plc.run ---------------------------------------------------------------------------------


def test_a_command_line_that_holds_a_stored_value_is_refused_and_nothing_runs(monkeypatch):
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(USER=HOSTILE)):
        with pytest.raises(plc.BrowserError) as raised:
            plc.run("seat", "click", f"#{HOSTILE}")
    assert str(raised.value) == plc.COMMAND_REFUSED
    assert spawns.argv == [], "the driver was never started"
    assert HOSTILE not in str(raised.value) and "USER" not in str(raised.value)


def test_the_refusal_is_the_same_whichever_value_and_argument_caused_it(monkeypatch):
    Spawns(monkeypatch)
    messages = set()
    with plc.protecting("seat", guard(USER=HOSTILE, TOKEN="tok-abc-123-xyz")):
        for args in (("click", f"#{HOSTILE}"), ("fill", "#a", "tok-abc-123-xyz"), ("goto", f"https://x/{HOSTILE}")):
            with pytest.raises(plc.BrowserError) as raised:
                plc.run("seat", *args)
            messages.add(str(raised.value))
    assert messages == {plc.COMMAND_REFUSED}


@pytest.mark.parametrize("form", sorted(secretstore._forms(TRICKY)))
def test_a_value_in_any_form_the_scrubber_knows_is_refused(monkeypatch, form):
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(PASSWORD=TRICKY)):
        with pytest.raises(plc.BrowserError, match="must not be in one"):
            plc.run("seat", "goto", f"https://example.com/?x=1&p={form}&y=2")
    assert spawns.argv == []


def test_the_forms_include_the_ones_a_page_and_a_url_make(monkeypatch):
    """The table above is not empty, and it holds the escapes of a selector, a URL and markup."""
    forms = secretstore._forms(TRICKY)
    for expected in (
        TRICKY, secretstore._css_escaped(TRICKY), quote(TRICKY, safe=""), quote_plus(TRICKY), html.escape(TRICKY),
        json.dumps(TRICKY)[1:-1], secretstore._yaml_quoted(TRICKY),
    ):
        assert expected in forms


def test_any_argument_of_the_line_is_looked_at_not_only_the_first(monkeypatch):
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(USER=HOSTILE)):
        for args in (
            ("fill", "#a", f"say {HOSTILE}"), ("select", "#s", HOSTILE), ("drop", "#t", "--path", f"/tmp/{HOSTILE}"),
            ("tab-new", f"https://example.com/{HOSTILE}"), ("find", HOSTILE), ("type", HOSTILE),
        ):
            with pytest.raises(plc.BrowserError):
                plc.run("seat", *args)
    assert spawns.argv == []


def test_a_line_that_holds_no_value_runs(monkeypatch):
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(USER=HOSTILE)):
        plc.run("seat", "click", "#next")
        plc.run("seat", "fill", "#a", "hello")
    assert [cmd[2] for cmd in spawns.argv] == ["click", "fill"]


def test_what_the_tool_wrote_itself_is_not_looked_at(monkeypatch):
    """A short value such as a year or a PIN is in the paths and the numbers the tool makes. A line of its own is
    marked `Own`, and so are the verb and a flag in front of it, and a snapshot ref says nothing."""
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(YEAR="2026", PIN="1234", WORD="click", FLAG="raw-run")):
        plc.run("seat", "click", "e1234")
        plc.run("seat", "click", "f2e1234")
        plc.run("seat", "eval", plc.Own("() => 1234"), "e1234")
        plc.run("seat", "--raw", "run-code", plc.Own("--filename=/seats/a/20261234T000000-code.js"))
        plc.run("seat", "screenshot", plc.Own("--filename=/seats/a/artifacts/20261025T000000-screen.png"))
        plc.run("seat", "attach", plc.Own("--cdp=http://127.0.0.1:41234"))
        plc.run("seat", "resize", plc.Own("1234"), plc.Own("1024"))
    assert len(spawns.argv) == 7


def test_text_of_the_caller_that_looks_like_what_the_tool_wrote_is_still_looked_at(monkeypatch):
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(PIN="1234")):
        for args in (("goto", "https://example.com/1234"), ("fill", "#a", "e1234x"), ("type", "ee1234")):
            with pytest.raises(plc.BrowserError):
                plc.run("seat", *args)
        plc.run("seat", "press", "Enter")
    assert len(spawns.argv) == 1


def test_the_name_of_the_seat_is_not_looked_at(monkeypatch):
    spawns = Spawns(monkeypatch)
    with plc.protecting("garmin-1234", guard(PIN="1234")):
        plc.run("garmin-1234", "snapshot")
    assert spawns.argv == [["playwright-cli", "-s=garmin-1234", "snapshot"]]


def test_the_protection_is_of_one_seat_and_ends_with_its_block(monkeypatch):
    spawns = Spawns(monkeypatch)
    plc.run("one", "type", HOSTILE)
    with plc.protecting("one", guard(USER=HOSTILE)):
        plc.run("two", "type", HOSTILE)
        with pytest.raises(plc.BrowserError):
            plc.run("one", "type", HOSTILE)
        with plc.protecting("one", guard(OTHER="other-value-1")):
            plc.run("one", "type", HOSTILE)
        with pytest.raises(plc.BrowserError):
            plc.run("one", "type", HOSTILE)
    plc.run("one", "type", HOSTILE)
    assert len(spawns.argv) == 4


def test_a_value_a_script_hides_is_protected_from_then_on(monkeypatch):
    spawns = Spawns(monkeypatch)
    scrubber = secretstore.Scrubber({})
    with plc.protecting("seat", scrubber):
        plc.run("seat", "fill", "#box", "482916")  # nothing is hidden yet
        scrubber.hide("PASSCODE", "482916")
        with pytest.raises(plc.BrowserError):
            plc.run("seat", "fill", "#box", "482916")
        plc.run("seat", "fill", "#box", "48291")
    assert len(spawns.argv) == 2


def test_a_run_that_hides_a_value_protects_it_even_when_it_made_its_scrubber_late(monkeypatch):
    run = commands.Run("seat")
    assert run.scrubber is None
    run.hide("PASSCODE", "482916")
    assert plc.holds_protected("seat", "code 482916")


def test_run_code_keeps_what_is_own_and_looks_at_the_rest(monkeypatch, tmp_path):
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(PIN="1234")):
        plc.run_code("seat", plc.Own("await page.goto('/1234')"))
        with pytest.raises(plc.BrowserError):
            plc.run_code("seat", "await page.goto('/1234')")
        # The same code in a private file is in no command line, so it is no business of the gate.
        plc.run_code_file("seat", "async (page) => '1234'", str(tmp_path))
    assert len(spawns.argv) == 2
    assert [cmd[-1].startswith("--filename=") for cmd in spawns.argv] == [False, True]


def test_the_file_of_a_private_call_is_the_one_place_a_value_may_be(monkeypatch, tmp_path):
    """`fill-secret` types the value through such a file, and a selector that holds a value goes the same way."""
    seen = []

    def run(cmd, **kwargs):
        target = [part for part in cmd if part.startswith("--filename=")][0].split("=", 1)[1]
        with open(target, encoding="utf-8") as handle:
            seen.append(handle.read())
        return _Completed(0, '{"typed":"https://app.example.com"}\n')

    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    monkeypatch.setattr(plc.subprocess, "run", run)
    with plc.protecting("seat", guard(USER=HOSTILE)):
        plc.fill_secret("seat", f"#{HOSTILE}", HOSTILE, [], str(tmp_path))
    assert HOSTILE in seen[0]


def test_no_check_happens_without_a_protected_value(monkeypatch):
    spawns = Spawns(monkeypatch)
    plc.run("seat", "type", HOSTILE)
    with plc.protecting("seat", secretstore.Scrubber({})):
        plc.run("seat", "type", HOSTILE)
    assert len(spawns.argv) == 2


def test_a_text_that_is_not_text_is_not_a_problem_for_the_gate(monkeypatch):
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(USER=HOSTILE)):
        plc.run("seat", "tab-select", 0)
        plc.run("seat", "mousewheel", 0, 400)
    assert len(spawns.argv) == 2


# --- the page chooses the selector: resolve ----------------------------------------------------------------


def _verdict(**overrides):
    base = {
        "selectorValid": False, "selectorCount": 0, "selectorVisibleCount": 0, "selectorTarget": "",
        "selectorPlain": "", "exact": [], "substring": [], "candidates": [],
    }
    base.update(overrides)
    return base


def _page(monkeypatch, verdict):
    monkeypatch.setattr(resolve, "evaluate_json", lambda seat, function, argument: verdict)


def _button(selector, plain="body > button", text="Continue", tag="button"):
    return {"tag": tag, "text": text, "selector": selector, "plain": plain}


def test_the_best_selector_is_used_when_it_holds_no_value(monkeypatch):
    _page(monkeypatch, _verdict(exact=[_button("#next")]))
    with plc.protecting("seat", guard(USER=HOSTILE)):
        assert resolve.click_target("seat", "Continue") == "#next"


def test_a_selector_the_page_made_from_the_value_is_not_used_the_element_is_named_by_its_path(monkeypatch):
    """The reviewed case: the user name typed into a field is the id of the next button."""
    _page(monkeypatch, _verdict(exact=[_button(f"#{HOSTILE}", "body > div:nth-of-type(2) > button")]))
    with plc.protecting("seat", guard(USER=HOSTILE)):
        assert resolve.click_target("seat", "Continue") == "body > div:nth-of-type(2) > button"
    # Without the value stored, the best selector stands: nothing changes for a seat that holds none.
    assert resolve.click_target("seat", "Continue") == f"#{HOSTILE}"


@pytest.mark.parametrize("make", [
    lambda value: f"#{secretstore._css_escaped(value)}",
    lambda value: f"[data-testid={json.dumps(value)}]",
    lambda value: f"button.{secretstore._css_escaped(value)}",
    lambda value: f"div > #{secretstore._css_escaped(value)} > button",
])
def test_the_forms_a_selector_makes_of_a_value_are_known(monkeypatch, make):
    value = "user@example.com"
    _page(monkeypatch, _verdict(exact=[_button(make(value))]))
    with plc.protecting("seat", guard(USER=value)):
        assert resolve.click_target("seat", "Continue") == "body > button"


def test_a_css_selector_of_the_caller_that_matches_is_replaced_the_same_way(monkeypatch):
    _page(monkeypatch, _verdict(
        selectorValid=True, selectorCount=1, selectorVisibleCount=1, selectorTarget=f"#{HOSTILE}",
        selectorPlain="body > button",
    ))
    with plc.protecting("seat", guard(USER=HOSTILE)):
        assert resolve.click_target("seat", "button") == "body > button"


def test_a_fill_target_is_named_the_same_way(monkeypatch):
    _page(monkeypatch, _verdict(exact=[_button(f"#{HOSTILE}", "form > input", "Name", "input")]))
    with plc.protecting("seat", guard(USER=HOSTILE)):
        assert resolve.fill_target("seat", "Name") == "form > input"


def test_a_label_matched_after_normalising_is_named_the_same_way(monkeypatch):
    _page(monkeypatch, _verdict(candidates=[
        {"tag": "input", "text": "Owner’s name", "selector": f"#{HOSTILE}", "plain": "form > input"},
    ]))
    with plc.protecting("seat", guard(USER=HOSTILE)):
        assert resolve.fill_target("seat", "Owner's name") == "form > input"


def test_an_element_with_no_name_free_of_the_value_says_so_and_names_the_fix(monkeypatch):
    _page(monkeypatch, _verdict(exact=[_button(f"#{HOSTILE}", f"body > x-{HOSTILE.lower()}")]))
    with plc.protecting("seat", guard(USER=HOSTILE, LOWER=HOSTILE.lower())):
        with pytest.raises(plc.BrowserError) as raised:
            resolve.click_target("seat", "Continue")
    assert str(raised.value) == (
        "click: no selector for that element is free of a value that must not be in a command line. "
        "Take a `snap` and use the ref of the element"
    )
    assert HOSTILE not in str(raised.value)


def test_an_element_with_no_path_at_all_is_an_error_too(monkeypatch):
    """A verdict from a page that does not give the path (an older answer) names no fallback."""
    _page(monkeypatch, _verdict(exact=[{"tag": "button", "text": "Continue", "selector": f"#{HOSTILE}"}]))
    with plc.protecting("seat", guard(USER=HOSTILE)):
        with pytest.raises(plc.BrowserError, match="no selector for that element"):
            resolve.click_target("seat", "Continue")


def test_a_list_of_elements_to_choose_from_names_each_by_a_selector_free_of_the_value(monkeypatch):
    _page(monkeypatch, _verdict(exact=[
        _button(f"#{HOSTILE}", "body > button:nth-of-type(1)"),
        _button("#other", "body > button:nth-of-type(2)"),
        _button(f"#{HOSTILE}-x", f"body > {HOSTILE}"),
    ]))
    with plc.protecting("seat", guard(USER=HOSTILE)):
        with pytest.raises(plc.BrowserError) as raised:
            resolve.click_target("seat", "Continue")
    message = str(raised.value)
    assert "3 elements match" in message
    assert "body > button:nth-of-type(1)" in message and "#other" in message
    assert HOSTILE not in message
    assert "no selector without a protected value: use a ref from `snap`" in message


def test_the_text_of_a_candidate_is_scrubbed_before_it_is_cut_so_no_half_of_a_value_shows(monkeypatch):
    """A text cut at 60 characters would show the start of a value that begins before the cut."""
    text = "x" * 50 + HOSTILE + " and more text"
    _page(monkeypatch, _verdict(exact=[_button("#a", text=text), _button("#b", text="Continue")]))
    with plc.protecting("seat", guard(USER=HOSTILE)):
        with pytest.raises(plc.BrowserError) as raised:
            resolve.click_target("seat", "Continue")
    message = str(raised.value)
    assert HOSTILE[:6] not in message, "the start of the value would show if the cut came first"
    assert "x" * 50 + "[secret:" in message


def test_a_ref_is_never_named_by_the_page(monkeypatch):
    monkeypatch.setattr(resolve, "evaluate_json", lambda *a: pytest.fail("a ref reached the page-side pass"))
    monkeypatch.setattr(plc, "run", lambda *a, **k: "### Result\ntrue\n")
    with plc.protecting("seat", guard(PIN="1234")):
        assert resolve.click_target("seat", "e1234") == "e1234"


def test_the_target_the_caller_typed_is_sent_to_the_page_in_a_file_not_in_a_command_line(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        plc, "call_in_page",
        lambda seat, function, values, directory, timeout=30: seen.append((function, values, directory)) or _verdict(
            exact=[_button("#next")]
        ),
    )
    monkeypatch.setattr(plc, "run", lambda *a, **k: pytest.fail("the driver was asked on the command line"))
    assert resolve.click_target("seat", "Sign in as alice") == "#next"
    (function, values, directory), = seen
    assert values == ["Sign in as alice"] and "alice" not in function
    assert directory == session.scratch_dir("seat")


# --- every verb, through the driver's command line ---------------------------------------------------------


def _world(monkeypatch, tmp_path):
    """A seat that holds the user name HOSTILE and the password of the other tests, with a driver stand-in
    that records each command line it is started with."""
    cli = _cli(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "USER", HOSTILE, [ORIGIN])
    return cli


def _hostile_page(cli):
    """A page whose best selector for every element is built from the value, and whose path is not."""
    element = {"tag": "button", "text": "Continue", "selector": f"#{HOSTILE}", "plain": "body > button"}
    field = {"tag": "input", "text": "Continue", "selector": f"#{HOSTILE}", "plain": "body > input"}
    cli.verdicts = {
        "click": _verdict(exact=[element]),
        "fill": _verdict(exact=[field]),
    }


def _calls(cli, verb):
    return [cmd[2:] for cmd in cli.calls if cmd[2] == verb]


def test_the_chain_of_the_review_names_the_button_by_its_path_and_the_value_is_in_no_command_line(monkeypatch, tmp_path):
    """`fill-secret #user USER`, then `click Continue`, on a page that makes the id of the button from the value."""
    cli = _world(monkeypatch, tmp_path)
    _hostile_page(cli)
    run = commands.run_script("seat", "fill-secret #user USER\nclick Continue")
    assert run.ok, run.error
    assert run.steps[1] == {"command": "click Continue", "ok": True, "output": "body > button"}
    assert _calls(cli, "click") == [["click", "body > button"]]
    assert HOSTILE not in cli.argv_text()


@pytest.mark.parametrize("line, verb, kind", [
    ("click Continue", "click", "click"),
    ("hover Continue", "hover", "click"),
    ("check Continue", "check", "click"),
    ("uncheck Continue", "uncheck", "click"),
    ("download Continue 0", "click", "click"),
    ("fill Continue some text", "fill", "fill"),
    ("select Continue one", "select", "fill"),
    ("fill-secret Continue PASSWORD", None, "fill"),
])
def test_every_verb_that_names_an_element_by_the_page_uses_a_selector_free_of_the_value(monkeypatch, tmp_path, line, verb, kind):
    cli = _world(monkeypatch, tmp_path)
    _hostile_page(cli)
    run = commands.run_script("seat", line)
    assert run.steps[0]["command"] == line.replace("PASSWORD", "PASSWORD")
    assert HOSTILE not in cli.argv_text(), cli.calls
    assert HOSTILE not in json.dumps(run.steps)
    if verb:
        (call,) = _calls(cli, verb)
        assert call[1] == ("body > input" if kind == "fill" else "body > button")


def test_drop_names_its_target_the_same_way(monkeypatch, tmp_path):
    cli = _world(monkeypatch, tmp_path)
    _hostile_page(cli)
    notes = tmp_path / "notes.txt"
    notes.write_text("x")
    run = commands.run_script("seat", f"drop Continue {notes}")
    assert run.ok, run.error
    assert _calls(cli, "drop") == [["drop", "body > button", "--path", str(notes)]]
    assert HOSTILE not in cli.argv_text()


def test_a_click_on_an_element_with_no_name_free_of_the_value_never_starts_the_driver_for_it(monkeypatch, tmp_path):
    cli = _world(monkeypatch, tmp_path)
    cli.verdicts = {"click": _verdict(exact=[{
        "tag": "button", "text": "Continue", "selector": f"#{HOSTILE}", "plain": f"body > {HOSTILE}",
    }])}
    run = commands.run_script("seat", "click Continue\nurl")
    assert not run.ok and len(run.steps) == 1
    assert "no selector for that element is free of a value" in run.error
    assert _calls(cli, "click") == [] and HOSTILE not in cli.argv_text()


def test_a_ref_of_the_last_snap_is_used_as_it_is(monkeypatch, tmp_path):
    cli = _world(monkeypatch, tmp_path)
    run = commands.run_script("seat", "fill-secret #user USER\nclick e12")
    assert run.ok, run.error
    assert _calls(cli, "click") == [["click", "e12"]]


@pytest.mark.parametrize("line, verb", [
    (f"type {HOSTILE}", "type"),
    (f"press {HOSTILE}", "press"),
    (f"fill Continue {HOSTILE}", "fill"),
    (f"fill Continue say {HOSTILE} twice", "fill"),
    (f"select Continue {HOSTILE}", "select"),
    (f"find {HOSTILE}", "find"),
    (f"scroll {HOSTILE}", "mousewheel"),
    (f"dialog-accept {HOSTILE}", "dialog-accept"),
    (f"console {HOSTILE}", "console"),
    (f"go https://app.example.com/u/{HOSTILE}/home", "goto"),
    (f"video-start {HOSTILE}", "video-start"),
])
def test_the_text_of_the_caller_that_holds_the_value_is_refused_and_the_driver_does_not_run_it(monkeypatch, tmp_path, line, verb):
    """The caller does not hold the value, so this is a guess, or a page's text handed back. It is refused whatever the verb."""
    cli = _world(monkeypatch, tmp_path)
    _hostile_page(cli)
    run = commands.run_script("seat", line)
    assert not run.ok
    assert run.error == plc.COMMAND_REFUSED
    assert HOSTILE not in cli.argv_text() and HOSTILE not in json.dumps(run.steps)
    assert _calls(cli, verb) == []


def test_a_file_the_caller_names_with_the_value_in_its_path_is_refused_for_upload(monkeypatch, tmp_path):
    cli = _world(monkeypatch, tmp_path)
    named = tmp_path / f"{HOSTILE}.txt"
    named.write_text("x")
    run = commands.run_script("seat", f"upload {named}")
    assert run.error == plc.COMMAND_REFUSED and _calls(cli, "upload") == []


def test_the_text_a_verb_types_into_a_field_is_refused_when_it_is_a_stored_password_too(monkeypatch, tmp_path):
    cli = _world(monkeypatch, tmp_path)
    _hostile_page(cli)
    run = commands.run_script("seat", "fill Continue hunter2-value")
    assert run.error == plc.COMMAND_REFUSED
    assert "hunter2-value" not in cli.argv_text()


def test_a_script_that_does_not_name_a_value_is_untouched(monkeypatch, tmp_path):
    cli = _world(monkeypatch, tmp_path)
    run = commands.run_script("seat", "fill #pw hello there\ntype typed text\npress Enter\nscroll 400\nfind Sign in")
    assert run.ok, run.error
    assert [cmd[2] for cmd in cli.calls if cmd[2] != "--raw"] == ["fill", "type", "press", "mousewheel", "find"]


def test_the_refusal_names_the_failing_command_line_scrubbed(monkeypatch, tmp_path):
    _world(monkeypatch, tmp_path)
    run = commands.run_script("seat", f"type {HOSTILE}")
    assert run.steps[0]["command"] == "type [secret:USER]"
    assert HOSTILE not in json.dumps(run.steps) and HOSTILE not in str(run.error)


# --- what the tool wrote itself is no business of the gate -------------------------------------------------


@pytest.mark.parametrize("value", ["2026", "0000", "1000", "5000", "1280", "1024", "click", "scratch", "browser"])
def test_a_short_value_that_is_in_the_paths_and_numbers_of_the_tool_does_not_stop_its_own_calls(monkeypatch, tmp_path, value):
    """A year, a PIN or a word such as `browser` is in the artifact paths, the timestamps, the window size, the
    timeouts and the seat directory. The tool's own calls carry them, and the gate leaves them alone."""
    cli = _cli(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "SHORT", value, [ORIGIN])
    run = commands.run_script(
        "seat",
        "snap\nshot\nsave\nvideo-start clip\nwait-for #pw 3\ntext #pw\nfill-secret #pw PASSWORD\nclick e5\nurl",
    )
    assert run.ok, run.error
    assert any(cmd[2] == "screenshot" for cmd in cli.calls)
    assert any(cmd[2] == "state-save" for cmd in cli.calls)
    assert any(cmd[2] == "video-start" for cmd in cli.calls)


# --- a recipe -----------------------------------------------------------------------------------------------


def test_a_recipe_that_hands_a_hidden_value_to_a_plain_fill_is_refused_and_the_value_goes_by_the_file(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    rec = Recipe("shop", "A shop", origins=[ORIGIN])
    outcomes = []

    @rec.verb("go", help="Hide a code and type it two ways.")
    def go(page, args):
        page.hide("PASSCODE", "482916")
        try:
            page.fill("#box", "482916")
        except plc.BrowserError as exc:
            outcomes.append(str(exc))
        outcomes.append(page.fill_hidden("#box", "482916"))
        return "done"

    run = commands.run_script("seat", "recipe shop go", recipes=static_library(rec))
    assert run.ok, run.error
    assert outcomes == [plc.COMMAND_REFUSED, "#box <- [hidden]"]
    assert "482916" not in cli.argv_text()
    assert "482916" in cli.code[-1]["text"], "the value went by the private file"


def test_a_page_the_recipe_drives_names_its_elements_by_a_path_when_the_id_is_the_value(monkeypatch, tmp_path):
    cli = _world(monkeypatch, tmp_path)
    _hostile_page(cli)
    rec = Recipe("shop", "A shop", origins=["https://example.com"])

    @rec.verb("go", help="Click.")
    def go(page, args):
        return page.click("Continue")

    run = commands.run_script("seat", "recipe shop go", recipes=static_library(rec))
    assert run.ok, run.error
    assert "click Continue -> body > button" in run.steps[0]["output"]
    assert HOSTILE not in cli.argv_text()


# --- the tool's own text calls do not carry the caller's text on a command line ------------------------------


@pytest.mark.parametrize("line", ["text #pw", "assert-text some-words", "wait-for #pw 1"])
def test_the_selector_or_text_the_caller_typed_is_in_no_command_line_for_these_verbs(monkeypatch, tmp_path, line):
    cli = _cli(monkeypatch, tmp_path)
    commands.run_script("seat", line)
    typed = line.split(None, 1)[1].split()[0]
    assert cli.calls and not any(typed in part for cmd in cli.calls for part in cmd), cli.calls


@pytest.mark.parametrize("line", ["click #pw", "fill #pw x", "hover #pw", "select #pw x", "check #pw"])
def test_the_target_of_a_verb_is_classified_in_a_file_and_only_the_selector_that_comes_back_is_sent(monkeypatch, tmp_path, line):
    cli = _cli(monkeypatch, tmp_path)
    commands.run_script("seat", line)
    assert cli.classified, "the target went to the page in a file"
    assert not [cmd for cmd in cli.calls if cmd[2] == "eval"], "no expression of the page-side pass was a command line"


def test_the_private_files_of_these_calls_are_removed_when_the_call_returns(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    commands.run_script("seat", "click #pw\ntext #pw\nassert-text x\nwait-for #pw 1")
    assert cli.classified, "the target went through a file"
    assert os.listdir(session.scratch_dir("seat")) == []


# --- the commands of the CLI --------------------------------------------------------------------------------


def test_the_go_command_of_the_cli_refuses_an_address_that_holds_a_stored_value(monkeypatch, capsys):
    import sys

    from browser import cli

    secretstore.set_secret("s", "USER", HOSTILE, [ORIGIN])
    spawns = Spawns(monkeypatch)
    monkeypatch.setattr(session, "ensure_running", lambda seat, script_epoch=None: None)
    monkeypatch.setattr(sys, "argv", ["browser", "go", f"https://example.com/?u={HOSTILE}", "--seat", "s"])
    assert cli.main() == 1
    out, err = capsys.readouterr()
    assert err == f"s: {plc.COMMAND_REFUSED}\n" and out == ""
    assert spawns.argv == [], "the driver was not started"
    assert HOSTILE not in out + err


def test_the_cli_commands_that_print_a_page_start_the_driver_with_no_value_in_it(monkeypatch, capsys):
    import sys

    from browser import cli

    secretstore.set_secret("s", "USER", HOSTILE, [ORIGIN])
    spawns = Spawns(monkeypatch)
    monkeypatch.setattr(session, "ensure_running", lambda seat, script_epoch=None: None)
    monkeypatch.setattr(commands, "refuse_a_local_page", lambda *a, **k: None)
    monkeypatch.setattr(session, "snapshot", lambda seat: "- page")
    for verb in (["console"], ["snap"]):
        monkeypatch.setattr(sys, "argv", ["browser", *verb, "--seat", "s"])
        assert cli.main() == 0
    capsys.readouterr()
    assert HOSTILE not in "\0".join(part for cmd in spawns.argv for part in cmd)


# --- the one way round the gate -----------------------------------------------------------------------------

# Every place in the tool that marks text as its own. The gate does not look at such text, so each one is a
# claim that no text of the caller or of the page is in it. A new line here is a decision for the reviewer, and
# this list is where it is made: add the line and say in the commit why the text is the tool's alone.
OWN_SITES = {
    "commands.py": [
        'plc.run(seat, "video-start", plc.Own(path), plc.Own("--size"), plc.Own("1280x1024"))',
        'argv += [plc.Own("--path"), path]',
        'plc.run(seat, "goto", plc.Own("about:blank"), timeout=30)',
    ],
    "page.py": [
        'self._drive(lambda: plc.run(self._call.seat, "tab-select", plc.Own(str(int(index)))))',
        "extra = [] if index is None else [plc.Own(str(int(index)))]",
        'self._drive(lambda: plc.run(self._call.seat, "resize", plc.Own(str(int(width))), plc.Own(str(int(height)))))',
    ],
    "plc.py": [
        'return run(seat, "run-code", Own(text) if isinstance(body, Own) else text, timeout=timeout)',
        'return run(seat, "--raw", "run-code", Own(f"--filename={target}"), timeout=timeout)',
        'flags = [Own("--full-page")] if full_page else []',
        'output = run(seat, "screenshot", Own(f"--filename={path}"), *flags, timeout=timeout)',
    ],
    "resolve.py": ['plc.run(seat, "eval", plc.Own("() => true"), ref, timeout=10)'],
    "session.py": [
        "return plc.Own(",
        'plc.run(seat, "attach", plc.Own(f"--cdp={base}"), timeout=15)',
        "plc.Own(",
        "return plc.Own(_route_downloads_js(seat) + (",
        "plc.Own(",
        "plc.Own(",
        'plc.run(seat, "state-save", plc.Own(path), timeout=timeout)',
        'return plc.evaluate(seat, plc.Own("window.location.href"))',
    ],
}


def test_the_tool_marks_text_as_its_own_in_these_places_and_no_others():
    from pathlib import Path

    root = Path(plc.__file__).resolve().parent
    found = {}
    for source in sorted(root.glob("**/*.py")):
        lines = [
            line.strip() for line in source.read_text(encoding="utf-8").splitlines()
            if "Own(" in line and not line.lstrip().startswith(("#", "class Own", '"""'))
        ]
        if lines:
            found[str(source.relative_to(root))] = lines
    assert found == OWN_SITES, "a new place marks text as its own: see OWN_SITES"


def test_a_screenshot_named_after_a_hidden_value_is_refused_before_a_path_is_made(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    rec = Recipe("shop", "A shop", origins=["https://example.com"])
    outcomes = []

    @rec.verb("go", help="Hide a code and name a picture after it.")
    def go(page, args):
        page.hide("PASSCODE", "482916")
        try:
            page.screenshot("code-482916.png")
        except plc.BrowserError as exc:
            outcomes.append(str(exc))
        outcomes.append(os.path.basename(page.screenshot("plain")))
        return "done"

    run = commands.run_script("seat", "recipe shop go", recipes=static_library(rec))
    assert run.ok, run.error
    assert outcomes[0] == plc.COMMAND_REFUSED and outcomes[1].endswith("-plain.png")
    assert "482916" not in cli.argv_text()
    assert not [name for name in os.listdir(session.artifacts_dir("seat")) if "482916" in name]


def test_a_ref_is_refused_only_when_the_value_is_that_very_text(monkeypatch):
    """The number of a ref says nothing, so a PIN that is inside `e1234` does not stop a click on it. A stored value
    that is `e1234` itself is in the command line as it stands."""
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(PIN="1234")):
        plc.run("seat", "click", "e1234")
    with plc.protecting("seat", guard(ODD="e1234")):
        with pytest.raises(plc.BrowserError):
            plc.run("seat", "click", "e1234")
        plc.run("seat", "click", "e12345")
    assert len(spawns.argv) == 2


# --- where a ref is exempt -----------------------------------------------------------------------------------------
#
# The number of a snapshot ref says nothing, so a PIN that is inside `e1234` does not stop a click on the element
# that has it. That holds where the verb takes the element it acts on (`plc.TARGET_PLACES`) and nowhere else: the
# same text in the place of the text that `fill` types, or `type` or `press` or `select` take, is the caller's, and it
# is the PIN with an `e` before it (round 3, finding 2).

PIN = "482916"


@pytest.mark.parametrize("args", [
    ("fill", "#user", f"e{PIN}"),
    ("fill", "e5", f"e{PIN}"),
    ("type", f"e{PIN}"),
    ("press", f"e{PIN}"),
    ("select", "e5", f"e{PIN}"),
    ("goto", f"e{PIN}"),
    ("dialog-accept", f"e{PIN}"),
    ("video-chapter", f"e{PIN}"),
    ("find", f"e{PIN}"),
    ("mousewheel", "0", f"e{PIN}"),
    ("upload", f"e{PIN}"),
    ("eval", f"e{PIN}"),
    ("click", "e5", f"e{PIN}"),
    ("tab-new", f"f1e{PIN}"),
    ("--raw", "type", f"e{PIN}"),
    ("dblclick", f"e{PIN}"),
    ("drag", f"e{PIN}", "e5"),
])
def test_text_that_looks_like_a_ref_and_holds_a_value_is_refused_where_it_is_data(monkeypatch, args):
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(PIN=PIN)):
        with pytest.raises(plc.BrowserError) as raised:
            plc.run("seat", *args)
    assert str(raised.value) == plc.COMMAND_REFUSED
    assert spawns.argv == [], "the driver was never started"


@pytest.mark.parametrize("args", [
    ("click", "e1234"),
    ("click", "f2e1234"),
    ("hover", "e1234"),
    ("check", "e1234"),
    ("uncheck", "e1234"),
    ("fill", "e1234", "some text"),
    ("select", "e1234", "one"),
    ("eval", plc.Own("() => true"), "e1234"),
    ("drop", "e1234", plc.Own("--path"), "/tmp/example.png"),
    ("--raw", "click", "e1234"),
])
def test_a_ref_is_the_target_of_a_verb_even_when_a_stored_pin_is_in_its_number(monkeypatch, args):
    """A page of a thousand elements has an element e1234, and a PIN of 1234 must not stop the click on it."""
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(PIN="1234")):
        plc.run("seat", *args)
    assert len(spawns.argv) == 1


@pytest.mark.parametrize("args", [
    ("fill", "e1234", "e1234"),
    ("select", "e1234", "e1234"),
    ("fill", "e1234", "see e1234 and 1234"),
    ("type", "e1234"),
])
def test_a_ref_that_is_a_target_does_not_make_the_text_after_it_a_target(monkeypatch, args):
    spawns = Spawns(monkeypatch)
    with plc.protecting("seat", guard(PIN="1234")):
        with pytest.raises(plc.BrowserError, match="must not be in one"):
            plc.run("seat", *args)
    assert spawns.argv == []


def test_the_table_of_places_lists_the_verbs_that_take_an_element_and_no_others():
    """A verb in the table has its ref exempt in the places the table names. A verb the tool does not have (`dblclick`,
    `drag`) is not in it: a ref there is data, and for a stored PIN it is the PIN."""
    assert set(plc.TARGET_PLACES) == {"click", "hover", "check", "uncheck", "fill", "select", "drop", "eval"}
    assert plc.TARGET_PLACES["eval"] == (1,) and plc.TARGET_PLACES["fill"] == (0,)


def test_the_probe_of_the_review_fill_and_type_with_a_ref_shaped_pin_are_refused_and_nothing_runs(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "PIN", PIN, [ORIGIN])
    for line in (f"fill #user e{PIN}", f"type e{PIN}"):
        run = commands.run_script("seat", line)
        assert not run.ok and run.error == plc.COMMAND_REFUSED, line
    assert _calls(cli, "fill") == [] and _calls(cli, "type") == []
    assert PIN not in cli.argv_text()


def test_the_verbs_of_the_tool_pass_the_ref_in_the_place_that_the_table_says(monkeypatch, tmp_path):
    """The table of places and the call sites of the tool agree: a ref of a script reaches the driver as the target of
    each verb, for a seat that holds a PIN that is in its number."""
    cli = _cli(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "PIN", "1234", [ORIGIN])
    notes = tmp_path / "notes.txt"
    notes.write_text("a file to drop\n")
    script = "\n".join([
        "click e1234", "fill e1234 hello", "select e1234 one", "hover e1234", "check e1234", "uncheck e1234",
        f"drop e1234 {notes}",
    ])
    run = commands.run_script("seat", script)
    assert run.ok, run.error
    assert _calls(cli, "drop") == [["drop", "e1234", "--path", str(notes)]]
    assert _calls(cli, "click") == [["click", "e1234"]]
    assert _calls(cli, "fill") == [["fill", "e1234", "hello"]]
    assert _calls(cli, "select") == [["select", "e1234", "one"]]
    for verb in ("hover", "check", "uncheck"):
        assert _calls(cli, verb) == [[verb, "e1234"]]
    assert ["eval", "() => true", "e1234"] in [cmd[2:] for cmd in cli.calls], "resolve asks the driver whether the ref exists"
    for verb, places in plc.TARGET_PLACES.items():
        assert all(isinstance(place, int) and place >= 0 for place in places), verb


# --- the strings a selector is made of -------------------------------------------------------------------------------


def _embedded(selector, embeds, plain="body > button"):
    return {"tag": "button", "text": "Continue", "selector": selector, "plain": plain, "embeds": embeds}


def test_a_selector_made_of_the_classes_of_a_phrase_is_not_used_though_the_phrase_is_not_in_it(monkeypatch):
    """`classList` splits a phrase set as a class name, and the selector joins the words with dots. The class attribute
    holds the phrase as it was typed, and the page-side pass gives the strings a selector is made of."""
    phrase = "correct horse battery staple"
    _page(monkeypatch, _verdict(exact=[_embedded("div.correct.horse.battery.staple", ["wrap", phrase])]))
    with plc.protecting("seat", guard(PASS=phrase)):
        assert not plc.holds_protected("seat", "div.correct.horse.battery.staple")
        assert resolve.click_target("seat", "Continue") == "body > button"
    assert resolve.click_target("seat", "Continue") == "div.correct.horse.battery.staple"


def test_a_selector_made_of_the_id_of_a_parent_is_not_used_when_that_id_holds_the_value(monkeypatch):
    _page(monkeypatch, _verdict(exact=[_embedded("#wrap > button", [HOSTILE, "go"])]))
    with plc.protecting("seat", guard(USER=HOSTILE)):
        assert resolve.click_target("seat", "Continue") == "body > button"


def test_the_strings_of_the_element_are_not_looked_at_when_they_hold_no_value(monkeypatch):
    _page(monkeypatch, _verdict(exact=[_embedded("#next", ["next", "btn primary"])]))
    with plc.protecting("seat", guard(USER=HOSTILE)):
        assert resolve.click_target("seat", "Continue") == "#next"


def test_the_strings_of_the_element_of_a_css_selector_and_of_a_label_count_too(monkeypatch):
    _page(monkeypatch, _verdict(
        selectorValid=True, selectorCount=1, selectorVisibleCount=1, selectorTarget="div.some.words.here",
        selectorPlain="body > div", selectorEmbeds=["some words here"],
    ))
    with plc.protecting("seat", guard(PASS="some words here")):
        assert resolve.click_target("seat", "div") == "body > div"
    _page(monkeypatch, _verdict(candidates=[
        {"tag": "input", "text": "Name", "selector": "input.some.words", "plain": "form > input", "embeds": ["some words"]},
    ]))
    with plc.protecting("seat", guard(PASS="some words")):
        assert resolve.fill_target("seat", "Name") == "form > input"


def test_an_id_made_of_a_prefix_and_a_value_that_starts_with_a_digit_is_not_used(monkeypatch):
    """CSS.escape escapes a digit at the start of an identifier and not in the middle: the id `pw-2Fast4U!` is
    `pw-2Fast4U\\!`, and the value alone is `\\32 Fast4U\\!`."""
    value = "2Fast4U!"
    _page(monkeypatch, _verdict(exact=[_embedded("#pw-2Fast4U\\!", ["pw-" + value])]))
    with plc.protecting("seat", guard(PASS=value)):
        assert plc.holds_protected("seat", "#pw-2Fast4U\\!")
        assert resolve.click_target("seat", "Continue") == "body > button"


def test_a_page_that_names_its_elements_by_a_phrase_end_to_end(monkeypatch, tmp_path):
    cli = _world(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "PHRASE", "correct horse battery staple", [ORIGIN])
    cli.verdicts = {"click": _verdict(exact=[_embedded(
        "div.correct.horse.battery.staple", ["correct horse battery staple"], plain="body > div"
    )])}
    run = commands.run_script("seat", "click Continue")
    assert run.ok, run.error
    assert run.steps[0]["output"] == "body > div"
    assert _calls(cli, "click") == [["click", "body > div"]]
    assert "correct" not in cli.argv_text()


def test_the_address_a_page_made_of_a_value_is_scrubbed_in_the_reply_of_a_wake(monkeypatch, tmp_path):
    """The reply of a wake is the transcript, and a browser wrote the value into the address in its own form. (The reply
    reaches `a8s tell` on stdin: see tests/test_runtime.py.)"""
    from browser import handler

    _world(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(handler.rt, "send_reply", lambda recipient, body, attachments=(): seen.append(body) or 0)
    monkeypatch.setattr(commands.session, "current_url", lambda seat: "https://example.com/p?u=p@ss%20w0rd")
    secretstore.set_secret("seat", "PW", "p@ss w0rd", [ORIGIN])
    assert handler.handle("seat", "iris", "url", allow="iris") == 0
    assert "p@ss" not in seen[0] and "w0rd" not in seen[0] and "[secret:PW]" in seen[0]


def test_an_artifact_named_after_a_hidden_value_is_refused_before_a_file_is_made(monkeypatch, tmp_path):
    _cli(monkeypatch, tmp_path)
    rec = Recipe("shop", "A shop", origins=["https://example.com"])
    outcomes = []

    @rec.verb("go", help="Hide a code and name a file after it.")
    def go(page, args):
        page.hide("PASSCODE", "482916")
        try:
            page.artifact("code-482916.txt", "text")
        except plc.BrowserError as exc:
            outcomes.append(str(exc))
        outcomes.append(os.path.basename(page.artifact("plain.txt", "text")))
        return "done"

    run = commands.run_script("seat", "recipe shop go", recipes=static_library(rec))
    assert run.ok, run.error
    assert outcomes[0].startswith("artifact: the name holds a value") and outcomes[1].endswith("-plain.txt")
    assert "482916" not in outcomes[0]
    assert not [name for name in os.listdir(session.artifacts_dir("seat")) if "482916" in name]
    assert not [path for path in run.files if "482916" in path]


def test_the_closest_match_hint_is_scrubbed_though_python_writes_it_with_escapes(monkeypatch, tmp_path):
    """The hint quotes the text of a link with `repr`, which escapes a quote and a backslash. The value is still found."""
    value = "x'Y\"z9!k\\w"
    cli = _world(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "LINK", value, [ORIGIN])
    cli.verdicts = {"click": _verdict(candidates=[{"tag": "a", "text": value, "selector": "#a"}])}
    run = commands.run_script("seat", "click xYz9kw")
    assert not run.ok and "closest:" in run.error
    hint = run.error.split("closest:", 1)[1]
    assert hint.strip() == "'[secret:LINK]'", run.error
