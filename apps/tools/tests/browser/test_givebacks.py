"""The bundled GiveBacks recipe, on a fake browser. No test reaches the real site, its API or Outlook web:
`fake_browser.py` stands in for the browser, and `Vendor` below stands in for the three sites, as far as the
recipe sees them (what a page says, where a click leads, who is signed in).

The recipe has the verbs and flags that people type for `gb`, so a test pins them. It signs in with the
stored secrets, and with a one-time passcode that is not stored: the tests show that the passcode appears
in no output and no file, however the sign-in ends.
"""
import contextlib
import json
import os
import re
from pathlib import Path

import pytest

from browser import commands
from browser import page as page_module
from browser import plc
from browser import recipe
from browser import secretstore
from browser import session
from browser.recipe import Arg, Opt

from fake_browser import FakeBrowser, run_script

BASE = "https://club.example.com"
CAUSE = "00000000-0000-4000-8000-0000000000c1"
MESSAGE = "00000000-0000-4000-8000-0000000000a1"
API = "https://api.givebacks.com/services/communication/messages"
OUTLOOK = "https://outlook.office.com/mail/"
OUTLOOK_LANDING = "https://outlook.cloud.microsoft/mail/"
MICROSOFT = "https://login.microsoftonline.com/common/oauth2/authorize?client_id=example"
STRAY_MAIL = "https://mail.example.net/inbox"
USER = "member-q7@example.com"
PASSWORD = "pw-Z4k!9-gb"
CODE = "482916"
SIX_DIGITS = re.compile(r"\b\d{6}\b")
BOX_DIGIT = re.compile(r'PinInput" \[ref=e\d+\]: *\d')
PIN_SELECTORS = [f"aria-ref=e{number}" for number in range(11, 17)]
# What leaves a failed passcode step: the step, and nothing the page or the driver said.
PASSCODE_FAILED = "givebacks login: the passcode step failed"
PASSCODE_FAILED_AND_CLOSED = PASSCODE_FAILED + ". The page could not be confirmed clear, so Chrome was closed."
# What playwright-cli follows with a snapshot file of the page: the commands that act on the page.
PAGE_FILE_VERBS = {
    "goto", "click", "press", "hover", "check", "uncheck", "select", "fill", "type",
    "tab-new", "tab-select", "tab-close", "go-back", "go-forward", "reload",
}


def leaks(text):
    """True when the text would show the code: the code, any six digits, or a digit in a code box."""
    return bool(CODE in text or SIX_DIGITS.search(text) or BOX_DIGIT.search(text))

SIGN_IN_PAGE = """\
- heading "Sign in" [level=1] [ref=e1]
- textbox "Email" [ref=e2]
- textbox "Password" [ref=e3]
- button "Sign in with Google" [ref=e4]
- button "Sign in with Apple" [ref=e5]
- button "Sign In" [ref=e6]
"""
MESSAGES_PAGE = """\
- heading "Messages" [level=1] [ref=e1]
- link "Weekly update" [ref=e2]
"""
OUTLOOK_LIST = f"""\
- listbox "Message list" [ref=e29]:
  - option "Weekly update Newsletter 8:00 AM Hello there" [ref=e31]
  - option "Your one-time passcode Support 9:41 AM Your code is {CODE}" [ref=e30]
"""
OUTLOOK_PANE = f"""\
- listbox "Message list" [ref=e29]:
  - option "Your one-time passcode Support 9:41 AM Your code is {CODE}" [ref=e30]
- heading "Your one-time passcode" [level=2] [ref=e39]
- generic [ref=e40]: Your one-time passcode is {CODE}. It expires in 10 minutes.
- generic [ref=e41]: Phone 555 0100
"""
OUTLOOK_NO_MAIL = """\
- listbox "Message list" [ref=e29]:
  - option "Weekly update Newsletter 8:00 AM Hello there" [ref=e31]
"""


class Vendor:
    """GiveBacks, Outlook web and Microsoft's sign-in, driven through the fake browser's hooks.

    `stage` is where the site's session is: "out", "otp" (the code page waits) or "in". `outlook` says
    what the second tab finds: "open" (the code mail is there), "signed_out", "empty" (no such mail),
    "elsewhere" (a page of another site on the recipe's origins). The pin boxes show the digits typed
    since the page last loaded, as a snapshot would.
    """

    def __init__(self, browser, monkeypatch, stage="out", needs_code=False, code=CODE, outlook="open"):
        self.browser = browser
        self.stage, self.needs_code, self.code, self.outlook = stage, needs_code, code, outlook
        self.opened = False
        self.loaded = 0
        self.written = 0
        self.leaky_files = []
        self.wrong = 0
        browser.url = "about:blank"
        monkeypatch.setattr(session, "snapshot", self.snapshot)
        monkeypatch.setattr(plc, "run", self.run_cli)
        self.cli = browser._run
        browser.hooks["goto"] = self.on_goto
        browser.hooks["tab-new"] = self.on_goto
        browser.hooks["click"] = self.on_click
        browser.hooks["press"] = self.on_press
        self.route()

    # -- where a page request lands

    def route(self):
        self.browser.redirects[f"{BASE}/messages"] = {
            "out": f"{BASE}/users/sign_in", "otp": f"{BASE}/one-time-passcode", "in": f"{BASE}/messages",
        }[self.stage]
        self.browser.redirects[OUTLOOK] = {
            "open": OUTLOOK_LANDING, "empty": OUTLOOK_LANDING, "signed_out": MICROSOFT,
            "elsewhere": "https://api.givebacks.com/", "stray": STRAY_MAIL,
        }[self.outlook]

    def move(self, stage):
        self.stage = stage
        self.route()
        self.browser.url = self.browser.redirects[f"{BASE}/messages"]
        self.on_goto()

    def on_goto(self, *args):
        self.loaded = len(self.browser.typed)

    # -- playwright-cli: a snapshot file of the page after each command that acts on it

    def run_cli(self, seat, *args, timeout=30):
        out = self.cli(seat, *args, timeout=timeout)
        verb = args[0] if args[0] != "--raw" else args[1]
        if "--raw" not in args and verb in PAGE_FILE_VERBS:
            self.write_page_file(seat)
        return out

    def write_page_file(self, seat):
        self.written += 1
        directory = session.cli_output_dir(seat)
        os.makedirs(directory, exist_ok=True)
        path = Path(directory, f"page-{self.written}.yml")
        text = f"### Snapshot\n{self.page_text()}"
        path.write_text(text)
        if leaks(text):
            self.leaky_files.append(path)

    # -- the clicks and keys that matter

    def typed_here(self):
        return dict(self.browser.typed[self.loaded:])

    def on_click(self, target):
        url = self.browser.url
        if url.startswith(f"{BASE}/users/sign_in") and target == "e6":
            typed = self.typed_here()
            if typed.get("aria-ref=e2") == USER and typed.get("aria-ref=e3") == PASSWORD:
                self.move("otp" if self.needs_code else "in")
        elif url.startswith(f"{BASE}/one-time-passcode") and target == "e23":
            self.move("out")
        elif url.startswith(f"{BASE}/one-time-passcode") and target == "e21":
            self.submit()
        elif url.startswith("https://outlook.") and target == "e30":
            self.opened = True

    def on_press(self, key):
        if key == "Enter" and self.browser.url.startswith(f"{BASE}/one-time-passcode"):
            self.submit()

    def submit(self):
        digits = "".join(value for selector, value in self.browser.typed[self.loaded:] if selector in PIN_SELECTORS)
        if digits == self.code:
            self.move("in")
        else:
            self.wrong += 1

    # -- what the pages say

    def passcode_page(self):
        typed = self.typed_here()
        boxes = []
        for number in range(11, 17):
            digit = typed.get(f"aria-ref=e{number}")
            boxes.append(f'- textbox "PinInput" [ref=e{number}]' + (f": {digit}" if digit else ""))
        return "\n".join([
            '- heading "Enter your code" [level=1] [ref=e1]',
            "- paragraph [ref=e2]: We sent a 6-digit code to a***@example.com.",
            *boxes,
            '- checkbox "Trust this browser" [ref=e20]',
            '- button "Submit" [disabled] [ref=e21]',
            '- button "Resend Code" [ref=e22]',
            '- button "Sign Out" [ref=e23]',
        ]) + "\n"

    def page_text(self):
        url = self.browser.url
        if url.startswith(f"{BASE}/users/sign_in"):
            return SIGN_IN_PAGE
        if url.startswith(f"{BASE}/one-time-passcode"):
            return self.passcode_page()
        if url.startswith(BASE):
            return MESSAGES_PAGE
        if url.startswith("https://outlook."):
            return (OUTLOOK_PANE if self.opened else OUTLOOK_LIST) if self.outlook == "open" else OUTLOOK_NO_MAIL
        return "- generic [ref=e1]: another page\n"

    def snapshot(self, seat):
        """What `playwright-cli snapshot` prints: the page, inline, with no file."""
        self.browser.check_open()
        return self.page_text()


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    """The waits the recipe asks for, without the time. Each is recorded."""
    waited = []

    def wait(self, seconds):
        self._tick()
        waited.append(seconds)
        self._say(f"wait {seconds:g}s")

    monkeypatch.setattr(page_module.Page, "wait", wait)
    return waited


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("GIVEBACKS_BASE", BASE)
    monkeypatch.setenv("GIVEBACKS_CAUSE_ID", CAUSE)


@pytest.fixture
def stored():
    secretstore.set_secret("seat", "GIVEBACKS_USER", USER, [BASE])
    secretstore.set_secret("seat", "GIVEBACKS_PASS", PASSWORD, [BASE])


@pytest.fixture
def browser(monkeypatch, settings, stored):
    return FakeBrowser(monkeypatch)


@pytest.fixture
def vendor(browser, monkeypatch):
    return Vendor(browser, monkeypatch, stage="in")


def run(script):
    """A script on the bundled recipes, which are read now: after the settings are in the environment."""
    return run_script(script, recipe.Library())


def output(result, index=0):
    return result.steps[index].get("output", "")


def lines(result, index=0):
    return output(result, index).splitlines()


def given(browser, url, payload, status=200):
    browser.fetches[url] = {"status": status, "text": json.dumps(payload)}


def the_recipe():
    (entry,) = recipe.Library().entries()
    assert entry.error is None, entry.error
    return entry.recipe


def files_of_the_seat():
    """The text of every file in the seat's artifacts and scratch directories."""
    found = []
    for directory in (session.artifacts_dir("seat"), session.scratch_dir("seat")):
        for folder, _dirs, names in os.walk(directory):
            found.extend(Path(folder, name).read_bytes().decode("utf-8", "replace") for name in names)
    return found


def everything_in(result):
    """All the text a run hands back or leaves in the seat's working files, as one string."""
    parts = [result.transcript(), json.dumps(result.steps), str(result.error)]
    for path in result.files:
        parts.append(Path(path).read_bytes().decode("utf-8", "replace"))
    for directory in (session.artifacts_dir("seat"), session.scratch_dir("seat")):
        for folder, _dirs, names in os.walk(directory):
            parts.extend(Path(folder, name).read_bytes().decode("utf-8", "replace") for name in names)
    return "\n".join(parts)


# --- the recipe as the loader and the listing show it ---------------------------------------------------------


def test_the_recipe_is_bundled_and_goes_by_its_name_and_its_alias(settings):
    library = recipe.Library()
    (entry,) = library.entries()
    assert (entry.name, entry.source, entry.error, entry.replaces) == ("givebacks", "bundled", None, False)
    assert library.find("gb") is entry and library.find("givebacks") is entry and library.find("GB") is entry
    assert entry.path == recipe.BUNDLED_DIR / "givebacks.py"


def test_the_listing_and_the_help_show_the_recipe_and_its_alias(browser):
    result = run("recipe\nrecipe gb\nrecipe givebacks help list\nrecipe gb help pull")
    assert result.ok, result.error
    assert lines(result, 0) == ["givebacks (gb)  GiveBacks newsletters"]
    described = lines(result, 1)
    assert described[0] == "givebacks (gb): GiveBacks newsletters"
    assert f"origins: {BASE}, https://api.givebacks.com, https://outlook.office.com, https://outlook.cloud.microsoft" in described
    assert "env: GIVEBACKS_BASE, GIVEBACKS_CAUSE_ID" in described
    assert "secrets: GIVEBACKS_USER, GIVEBACKS_PASS" in described
    assert "sign-in probe: yes" in described
    for verb in ("login (read)", "list (read, needs sign-in)", "open (read, needs sign-in)",
                 "pull (read, needs sign-in)", "screenshot (read, needs sign-in)"):
        assert verb in described, verb
    assert lines(result, 2)[:3] == [
        "list (read, needs sign-in)",
        "  List recent newsletter drafts: id, status, send date and subject. The rows also come back as data.",
        "  usage: recipe givebacks list [--limit LIMIT]",
    ]
    assert "  usage: recipe givebacks pull --id ID [-o OUTPUT]" in lines(result, 3)
    assert browser.calls == [] and browser.opened == 0, "listing and help opened a browser"


def test_the_recipe_declares_five_read_verbs_and_no_write_verb_yet(settings):
    rec = the_recipe()
    assert list(rec.verbs) == ["login", "list", "open", "pull", "screenshot"]
    assert {verb.kind for verb in rec.verbs.values()} == {"read"}
    assert {name: verb.signin for name, verb in rec.verbs.items()} == {
        "login": False, "list": True, "open": True, "pull": True, "screenshot": True,
    }
    assert rec.probe is not None
    assert (rec.aliases, rec.env, rec.secrets) == (["gb"], ["GIVEBACKS_BASE", "GIVEBACKS_CAUSE_ID"], ["GIVEBACKS_USER", "GIVEBACKS_PASS"])


# --- the arguments are the ones of the gb commands -----------------------------------------------------------

# Written out here as literals, so the suite needs nothing from outside this repository:
# verb -> [(flags, required, default, type)] in the order the `gb` commands declare them.
GB_ARGUMENTS = {
    "login": [],
    "list": [(("--limit",), False, 10, "int")],
    "open": [(("--id",), True, None, None)],
    "pull": [(("--id",), True, None, None), (("-o", "--output"), False, None, None)],
    "screenshot": [(("--id",), True, None, None), (("--dir",), False, ".", None)],
}


def shape(verb):
    found = []
    for spec in verb.specs:
        assert isinstance(spec, Opt) and not isinstance(spec, Arg), "the gb commands take options only"
        kind = spec.keywords.get("type")
        found.append((
            tuple(spec.flags), bool(spec.keywords.get("required", False)), spec.keywords.get("default"),
            kind.__name__ if kind else None,
        ))
    return found


def test_each_verb_keeps_the_flags_the_requirement_and_the_default_of_its_gb_command(settings):
    rec = the_recipe()
    assert {name: shape(verb) for name, verb in rec.verbs.items()} == GB_ARGUMENTS


def test_the_arguments_parse_into_the_names_the_gb_commands_use(settings):
    verbs = the_recipe().verbs
    assert vars(verbs["login"].parse([])) == {}
    assert vars(verbs["list"].parse([])) == {"limit": 10}
    assert vars(verbs["list"].parse(["--limit", "25"])) == {"limit": 25}
    assert vars(verbs["open"].parse(["--id", MESSAGE])) == {"id": MESSAGE}
    assert vars(verbs["pull"].parse(["--id", MESSAGE])) == {"id": MESSAGE, "output": None}
    assert vars(verbs["pull"].parse(["--id", MESSAGE, "-o", "design.json"])) == {"id": MESSAGE, "output": "design.json"}
    assert vars(verbs["pull"].parse(["--id", MESSAGE, "--output", "design.json"]))["output"] == "design.json"
    assert vars(verbs["screenshot"].parse(["--id", MESSAGE])) == {"id": MESSAGE, "dir": "."}
    assert vars(verbs["screenshot"].parse(["--id", MESSAGE, "--dir", "shots"]))["dir"] == "shots"


@pytest.mark.parametrize(
    "verb, words, message",
    [
        ("list", ["--limit", "many"], "argument --limit: invalid int value: 'many'"),
        ("open", [], "the following arguments are required: --id"),
        ("pull", ["-o", "x.json"], "the following arguments are required: --id"),
        ("screenshot", ["--dir", "x"], "the following arguments are required: --id"),
        ("login", ["--id", "x"], "unrecognized arguments: --id x"),
    ],
)
def test_a_wrong_argument_fails_with_the_usage_of_the_verb_and_touches_no_browser(browser, verb, words, message):
    result = run(f"recipe gb {verb} {' '.join(words)}")
    assert not result.ok
    assert result.error.splitlines()[0] == message
    assert result.error.splitlines()[1].startswith(f"usage: recipe givebacks {verb}")
    assert browser.calls == [] and browser.opened == 0


# --- the site, from the node's settings ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, origin",
    [
        (BASE, BASE),
        (BASE + "/", BASE),
        ("  " + BASE + "  ", BASE),
        ("HTTPS://Club.Example.COM", BASE),
        ("https://club.example.com:8443", "https://club.example.com:8443"),
        ("http://127.0.0.1:3000", "http://127.0.0.1:3000"),
        ("https://club.example.com:443", BASE),
        ("http://club.example.com:80", "http://club.example.com"),
        ("https://club.example.com:80", "https://club.example.com:80"),
    ],
)
def test_the_site_origin_comes_from_the_giveback_base_setting(monkeypatch, value, origin):
    monkeypatch.setenv("GIVEBACKS_BASE", value)
    assert the_recipe().origins == [origin, "https://api.givebacks.com", "https://outlook.office.com", "https://outlook.cloud.microsoft"]


@pytest.mark.parametrize(
    "value",
    [
        None, "", "   ", "club.example.com", "ftp://club.example.com", "https://club.example.com/messages",
        "https://club.example.com?x=1", "https://club.example.com#top", "https://user@club.example.com",
        "https://user:secret@club.example.com", "https://club.example.com:99999", "https://club.example.com:abc",
        "https://club..example.com", "https://club.example.com.", "https://-club.example.com", "https://[::1]:3000",
        "https://*.example.com", "https:///club.example.com", "javascript:alert(1)", "https://clüb.example.com",
    ],
)
def test_a_site_address_that_is_not_one_adds_no_origin_and_breaks_nothing(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("GIVEBACKS_BASE", raising=False)
    else:
        monkeypatch.setenv("GIVEBACKS_BASE", value)
    rec = the_recipe()
    assert rec.origins == ["https://api.givebacks.com", "https://outlook.office.com", "https://outlook.cloud.microsoft"]
    assert sorted(rec.verbs) == sorted(GB_ARGUMENTS)


def test_a_default_port_in_the_setting_is_dropped_as_the_browser_drops_it(browser, monkeypatch):
    """The page address the browser reports has no default port. The recipe compares it with its own."""
    Vendor(browser, monkeypatch, stage="out")
    monkeypatch.setenv("GIVEBACKS_BASE", "https://club.example.com:443")
    result = run("recipe gb status")
    assert result.ok, result.error
    assert lines(result)[-1] == "out"
    assert ("goto", f"{BASE}/messages") in browser.calls


def test_the_site_origin_is_read_when_the_recipe_loads_so_a_new_run_follows_the_setting(monkeypatch):
    monkeypatch.setenv("GIVEBACKS_BASE", "https://one.example.com")
    assert the_recipe().origins[0] == "https://one.example.com"
    monkeypatch.setenv("GIVEBACKS_BASE", "https://two.example.com")
    assert the_recipe().origins[0] == "https://two.example.com"


def test_a_site_that_sends_the_page_to_another_site_fails_the_verb_before_anything_is_read(browser, vendor):
    browser.redirects[f"{BASE}/messages"] = "https://elsewhere.example.net/"
    result = run("recipe gb list")
    assert not result.ok
    assert "outside the origins of recipe givebacks" in result.error
    assert [call for call in browser.calls if call[0] == "fetch"] == []


# --- the two settings every verb needs -----------------------------------------------------------------------

VERB_LINES = {
    "login": "recipe gb login", "list": "recipe gb list", "open": f"recipe gb open --id {MESSAGE}",
    "pull": f"recipe gb pull --id {MESSAGE}", "screenshot": f"recipe gb screenshot --id {MESSAGE}",
}


@pytest.mark.parametrize("verb", sorted(VERB_LINES))
@pytest.mark.parametrize(
    "missing, named",
    [
        (("GIVEBACKS_BASE",), "GIVEBACKS_BASE is not set."),
        (("GIVEBACKS_CAUSE_ID",), "GIVEBACKS_CAUSE_ID is not set."),
        (("GIVEBACKS_BASE", "GIVEBACKS_CAUSE_ID"), "GIVEBACKS_BASE and GIVEBACKS_CAUSE_ID are not set."),
    ],
)
def test_a_missing_setting_is_a_usage_failure_that_names_it(browser, monkeypatch, verb, missing, named):
    for name in missing:
        monkeypatch.delenv(name)
    result = run(VERB_LINES[verb])
    assert not result.ok
    message, usage = result.error.splitlines()[0], result.error.splitlines()[-1]
    assert message == f"{named} Set each on the node with `a8s vars <node> set env.NAME <value>`"
    assert usage.startswith(f"usage: recipe givebacks {verb}")
    assert not [call for call in browser.calls if call[0] in ("fetch", "click", "screenshot")] and browser.typed == []


def test_a_site_address_that_is_not_an_address_is_a_usage_failure_too(browser, monkeypatch):
    monkeypatch.setenv("GIVEBACKS_BASE", "club.example.com")
    result = run("recipe gb status")
    assert not result.ok
    assert result.error == "GIVEBACKS_BASE is the address of the GiveBacks site, such as https://yourgroup.givebacks.com"


# --- the sign-in state ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "landing, state",
    [
        ("/messages", "in"),
        ("/messages?page=2", "in"),
        ("/messages/", "in"),
        ("/users/sign_in", "out"),
        ("/users/sign-in", "out"),
        ("/sign-in?next=%2Fmessages", "out"),
        ("/one-time-passcode", "out"),
        ("/dashboard", "unknown"),
        ("/", "unknown"),
    ],
)
def test_the_status_probe_answers_in_out_or_unknown_from_where_the_site_lands(browser, vendor, landing, state):
    browser.redirects[f"{BASE}/messages"] = BASE + landing
    result = run("recipe gb status")
    assert result.ok, result.error
    assert lines(result)[-1] == state


def test_the_status_probe_opens_the_page_and_does_nothing_else(browser, vendor, pauses):
    result = run("recipe gb status")
    assert result.ok, result.error
    # The one call before the page opens clears the "leave this page?" prompt of the page being left.
    assert browser.verbs() == ["evaluate", "goto"]
    assert browser.calls[0][1] == "() => { window.onbeforeunload = null; return true; }"
    assert browser.calls[1] == ("goto", f"{BASE}/messages")
    assert browser.typed == [] and browser.codes[0].count("fetch") == 0 and len(browser.codes) == 1
    assert pauses == [3], "the site sends a signed-out page on to its sign-in a moment after it loads"
    assert lines(result) == [
        "  env GIVEBACKS_BASE", "  env GIVEBACKS_CAUSE_ID",
        "  evaluate () => { window.onbeforeunload = null; return true; }",
        f"  go {BASE}/messages -> {BASE}/messages", "  wait 3s", "in",
    ]


SIGNED_OUT = (
    "needs a person: givebacks is signed out on seat seat. "
    "Run `recipe givebacks login`, or sign in by hand in the Chrome of seat seat."
)
UNSURE = (
    "needs a person: cannot tell whether givebacks is signed in on seat seat. "
    "Run `recipe givebacks login`, or sign in by hand in the Chrome of seat seat."
)


@pytest.mark.parametrize("verb", ["list", "open", "pull", "screenshot"])
@pytest.mark.parametrize(
    "stage, landing, message",
    [("out", "/users/sign_in", SIGNED_OUT), ("otp", "/one-time-passcode", SIGNED_OUT), ("in", "/dashboard", UNSURE)],
)
def test_every_verb_that_needs_a_sign_in_stops_for_a_person_when_the_site_is_not_signed_in(
    browser, monkeypatch, verb, stage, landing, message,
):
    vendor = Vendor(browser, monkeypatch, stage=stage)
    browser.redirects[f"{BASE}/messages"] = BASE + landing
    result = run(VERB_LINES[verb])
    assert not result.ok and result.error == message
    assert [call for call in browser.calls if call[0] in ("fetch", "click", "state-save")] == [] and browser.shots == []
    assert browser.typed == [] and vendor.stage == stage


def test_login_does_not_run_the_probe_it_is_how_a_person_signs_in(browser, vendor):
    result = run("recipe gb login")
    assert result.ok, result.error
    assert not [line for line in lines(result) if line.startswith("  status ->")]


# --- list ----------------------------------------------------------------------------------------------------

ROWS = [
    {"id": "m-1", "subject": "First", "status": "sent", "sent_at": "2026-10-20T08:00:00Z"},
    {"uuid": "m-2", "name": "Second", "status": "draft"},
    {"subject": "Third"},
]


def test_list_prints_one_line_per_draft_and_returns_the_same_rows_as_data(browser, vendor):
    given(browser, f"{API}?cause_id={CAUSE}&per_page=10", {"messages": ROWS})
    result = run("recipe gb list")
    assert result.ok, result.error
    assert lines(result)[-3:] == [
        "  m-1  [sent]  2026-10-20  First",
        "  m-2  [draft]    Second",
        "  ?  [?]    Third",
    ]
    (data,) = [path for path in result.files if path.endswith("result.json")]
    assert json.loads(Path(data).read_text()) == {"messages": [
        {"id": "m-1", "status": "sent", "sent": "2026-10-20", "subject": "First"},
        {"id": "m-2", "status": "draft", "sent": "", "subject": "Second"},
        {"id": "?", "status": "?", "sent": "", "subject": "Third"},
    ]}
    assert browser.calls[-1][:2] == ("fetch", f"{API}?cause_id={CAUSE}&per_page=10")
    assert browser.calls[-1][2]["credentials"] == "include" and browser.calls[-1][2]["method"] == "GET"


@pytest.mark.parametrize(
    "words, per_page",
    [("", 10), ("--limit 3", 3), ("--limit 0", 10), ("--limit -4", 1), ("--limit 250", 250)],
)
def test_list_asks_for_as_many_rows_as_the_gb_command_does(browser, vendor, words, per_page):
    given(browser, f"{API}?cause_id={CAUSE}&per_page={per_page}", {"messages": ROWS})
    result = run(f"recipe gb list {words}")
    assert result.ok, result.error
    assert browser.calls[-1][1] == f"{API}?cause_id={CAUSE}&per_page={per_page}"
    (data,) = [path for path in result.files if path.endswith("result.json")]
    assert len(json.loads(Path(data).read_text())["messages"]) == min(per_page, len(ROWS))


def test_list_keeps_no_more_rows_than_the_limit_even_when_the_api_sent_more(browser, vendor):
    given(browser, f"{API}?cause_id={CAUSE}&per_page=2", {"messages": ROWS})
    result = run("recipe gb list --limit 2")
    (data,) = [path for path in result.files if path.endswith("result.json")]
    assert [row["id"] for row in json.loads(Path(data).read_text())["messages"]] == ["m-1", "m-2"]
    assert not [line for line in lines(result) if "Third" in line]


@pytest.mark.parametrize(
    "payload, ids",
    [
        ({"data": [{"id": "d-1"}]}, ["d-1"]),
        ([{"id": "l-1"}, {"id": "l-2"}], ["l-1", "l-2"]),
        ({"messages": [], "data": [{"id": "ignored"}]}, []),
        ({"messages": None, "data": [{"id": "d-2"}]}, ["d-2"]),
        ({"messages": 0, "data": [{"id": "d-3"}]}, ["d-3"]),
        ({"messages": "", "data": [{"id": "d-4"}]}, ["d-4"]),
    ],
)
def test_list_reads_the_rows_where_the_api_keeps_them(browser, vendor, payload, ids):
    given(browser, f"{API}?cause_id={CAUSE}&per_page=10", payload)
    result = run("recipe gb list")
    assert result.ok, result.error
    if not ids:
        assert lines(result)[-1] == "No messages."
        return
    (data,) = [path for path in result.files if path.endswith("result.json")]
    assert [row["id"] for row in json.loads(Path(data).read_text())["messages"]] == ids


@pytest.mark.parametrize(
    "answer",
    [
        {"status": 200, "text": "not json"},
        {"status": 200, "text": json.dumps({"error": "nope"})},
        {"status": 200, "text": json.dumps({"messages": ["a string"]})},
        {"status": 200, "text": json.dumps("text")},
        {"status": 500, "text": "oops"},
    ],
)
def test_list_that_cannot_read_the_answer_says_so_with_what_the_api_sent(browser, vendor, answer):
    browser.fetches[f"{API}?cause_id={CAUSE}&per_page=10"] = answer
    result = run("recipe gb list")
    assert not result.ok
    assert result.error.startswith(f"Could not parse messages list. The API answered {answer['status']}: ")
    assert answer["text"][:500] in result.error


def test_the_cause_id_is_one_value_in_the_query_whatever_it_holds(browser, vendor, monkeypatch):
    monkeypatch.setenv("GIVEBACKS_CAUSE_ID", "a&per_page=999 b#c")
    given(browser, f"{API}?cause_id=a%26per_page%3D999%20b%23c&per_page=10", {"messages": ROWS})
    result = run("recipe gb list")
    assert result.ok, result.error


def test_list_gets_only_from_the_api_host(browser, vendor):
    given(browser, f"{API}?cause_id={CAUSE}&per_page=10", {"messages": ROWS})
    run("recipe gb list --limit 10")
    urls = [call[1] for call in browser.calls if call[0] == "fetch"]
    assert urls == [f"{API}?cause_id={CAUSE}&per_page=10"]


# --- open ----------------------------------------------------------------------------------------------------


def test_open_goes_to_the_editor_and_says_where(browser, vendor):
    result = run(f"recipe gb open --id {MESSAGE}")
    assert result.ok, result.error
    assert lines(result)[-1] == f"Opening editor: {BASE}/messages/{MESSAGE}/design"
    assert browser.calls[-1] == ("goto", f"{BASE}/messages/{MESSAGE}/design")
    assert browser.url == f"{BASE}/messages/{MESSAGE}/design"


def test_an_id_is_one_path_segment_whatever_it_holds(browser, vendor):
    result = run("recipe gb open --id ../../x?y=1#z")
    assert result.ok, result.error
    assert browser.calls[-1] == ("goto", f"{BASE}/messages/..%2F..%2Fx%3Fy%3D1%23z/design")


def test_open_leaves_a_page_that_asks_to_stay_and_retries_a_navigation_a_prompt_held_up(browser, vendor):
    attempts = []

    def hold(*args):
        attempts.append(args)
        if len(attempts) == 2:  # the status probe went first; this is the editor's navigation
            raise page_module.plc.BrowserError("playwright-cli timed out after 60s")

    browser.hooks["goto"] = lambda *args: (vendor.on_goto(*args), hold(*args))
    result = run(f"recipe gb open --id {MESSAGE}")
    assert result.ok, result.error
    verbs = browser.verbs()
    assert verbs.count("dialog-accept") == 1 and verbs.index("dialog-accept") > verbs.index("goto", 2)
    assert [call for call in browser.calls if call[0] == "goto"][-2:] == [("goto", f"{BASE}/messages/{MESSAGE}/design")] * 2


def test_a_page_that_cannot_clear_its_leave_prompt_is_left_all_the_same(browser, vendor, monkeypatch):
    real = browser._run_code_file

    def refuse(seat, code, directory, timeout=60):
        if "onbeforeunload" in code:
            raise plc.BrowserError("does not handle the modal state")
        return real(seat, code, directory, timeout)

    monkeypatch.setattr(plc, "run_code_file", refuse)
    result = run(f"recipe gb open --id {MESSAGE}")
    assert result.ok, result.error
    assert browser.calls[-1] == ("goto", f"{BASE}/messages/{MESSAGE}/design")


def test_a_prompt_that_already_stands_is_answered_and_the_navigation_tried_again(browser, vendor):
    refused = []

    def stand(*args):
        vendor.on_goto(*args)
        if len(refused) == 0 and browser.url.endswith("/design"):
            refused.append(args)
            raise plc.BrowserError("### Error\nThe tool does not handle the modal state")

    browser.hooks["goto"] = stand
    result = run(f"recipe gb open --id {MESSAGE}")
    assert result.ok, result.error
    verbs = browser.verbs()
    assert verbs.count("dialog-accept") == 1 and verbs.count("goto") == 3


def test_a_navigation_that_fails_for_another_reason_is_not_retried(browser, vendor):
    def broken(*args):
        raise page_module.plc.BrowserError("net::ERR_NAME_NOT_RESOLVED")

    browser.hooks["goto"] = broken
    result = run(f"recipe gb open --id {MESSAGE}")
    assert not result.ok and "ERR_NAME_NOT_RESOLVED" in result.error
    assert "dialog-accept" not in [call[0] for call in browser.calls]


# --- pull ----------------------------------------------------------------------------------------------------

DESIGN = {"counters": {"u_row": 2}, "body": {"id": "b1", "rows": [{"id": "r1"}, {"id": "r2"}]}}


def api_message(browser, template=None, status=200, message=MESSAGE, **extra):
    template = json.dumps(DESIGN) if template is None else template
    given(browser, f"{API}/{message}?cause_id={CAUSE}", {"message": {"template": template, **extra}}, status)


def attached(result, suffix):
    (path,) = [path for path in result.files if path.endswith(suffix)]
    return Path(path)


def test_pull_attaches_the_design_json_and_says_what_it_pulled(browser, vendor):
    api_message(browser)
    result = run(f"recipe gb pull --id {MESSAGE}")
    assert result.ok, result.error
    assert lines(result)[-3:] == [f"Pulling design for {MESSAGE}...", "Got design: 2 rows", "Attached givebacks-design.json"]
    assert attached(result, "-givebacks-design.json").read_text() == json.dumps(DESIGN, indent=2)
    assert browser.calls[-1][0] == "fetch" and browser.calls[-1][1] == f"{API}/{MESSAGE}?cause_id={CAUSE}"


@pytest.mark.parametrize(
    "given_name, attached_name",
    [
        ("design.json", "design.json"),
        ("{dir}/out/design.json", "design.json"),
        ("{dir}/sub dir/new design.json", "new design.json"),
        ("C:\\Users\\someone\\design.json", "design.json"),
        ("{dir}/out/", None),
        (".hidden.json", None),
        ("..", None),
    ],
)
def test_pull_names_the_attachment_with_the_base_name_of_the_output_file(
    browser, vendor, tmp_path, given_name, attached_name,
):
    api_message(browser)
    work = tmp_path / "work"
    word = given_name.replace("{dir}", str(work))
    result = run(f"recipe gb pull --id {MESSAGE} -o '{word}'")
    assert not work.exists() and not Path(attached_name or "design.json").exists(), "pull wrote on this machine"
    if attached_name is None:
        assert not result.ok and result.error.splitlines()[0] == "-o names the attached file, such as design.json"
        return
    assert result.ok, result.error
    assert lines(result)[-1] == f"Attached {attached_name}"
    assert attached(result, f"-{attached_name}").read_text() == json.dumps(DESIGN, indent=2)


def test_pull_writes_no_file_at_the_path_it_was_given(browser, vendor, tmp_path):
    api_message(browser)
    path = tmp_path / "out" / "design.json"
    result = run(f"recipe gb pull --id {MESSAGE} -o {path}")
    assert result.ok, result.error
    assert not path.exists() and not path.parent.exists()
    assert attached(result, "-design.json").read_text() == json.dumps(DESIGN, indent=2)


def test_pull_is_refused_before_any_request_when_the_file_name_is_not_one(browser, vendor):
    result = run(f"recipe gb pull --id {MESSAGE} -o ..")
    assert not result.ok
    assert [call for call in browser.calls if call[0] == "fetch"] == []


@pytest.mark.parametrize(
    "answer",
    [
        {"status": 200, "text": json.dumps({"message": {}})},
        {"status": 200, "text": json.dumps({"message": None})},
        {"status": 200, "text": json.dumps({"message": "text"})},
        {"status": 200, "text": json.dumps([{"message": {"template": "counters"}}])},
        {"status": 200, "text": json.dumps({"message": {"template": "{}"}})},
        {"status": 200, "text": json.dumps({"message": {"template": "counters but {not json"}})},
        {"status": 200, "text": json.dumps({"message": {"template": '"counters"'}})},
        {"status": 200, "text": json.dumps({"message": {"template": {"counters": {}}}})},
        {"status": 404, "text": json.dumps({"error": "no such message"})},
        {"status": 200, "text": "<html>"},
    ],
)
def test_pull_that_cannot_read_a_design_says_so_with_what_the_api_sent(browser, vendor, answer):
    browser.fetches[f"{API}/{MESSAGE}?cause_id={CAUSE}"] = answer
    result = run(f"recipe gb pull --id {MESSAGE}")
    assert not result.ok
    assert result.error.startswith(f"Could not parse design from response. The API answered {answer['status']}: ")
    assert result.files == [] or all(not path.endswith("givebacks-design.json") for path in result.files)


@pytest.mark.parametrize("design", [{"counters": {}}, {"counters": {}, "body": None}, {"counters": {}, "body": {"rows": "x"}}])
def test_pull_counts_no_rows_in_a_design_without_a_list_of_rows(browser, vendor, design):
    api_message(browser, template=json.dumps(design))
    result = run(f"recipe gb pull --id {MESSAGE}")
    assert result.ok, result.error
    assert "Got design: 0 rows" in lines(result)


# --- screenshot ----------------------------------------------------------------------------------------------


def test_screenshot_attaches_a_full_page_image_and_says_it_saved(browser, vendor, pauses):
    result = run(f"recipe gb screenshot --id {MESSAGE}")
    assert result.ok, result.error
    assert lines(result)[-1] == "Screenshot saved."
    ((path, full_page),) = browser.shots
    assert full_page is True and path.endswith("-newsletter-preview.png") and path in result.files
    assert Path(path).read_bytes().startswith(b"\x89PNG")
    assert ("goto", f"{BASE}/messages/{MESSAGE}") in browser.calls
    assert pauses[-1] == 4, "the page gets a moment to draw before the picture"


def test_screenshot_accepts_the_dir_flag_and_ignores_it(browser, vendor, tmp_path):
    folder = tmp_path / "shots" / "deeper"
    result = run(f"recipe gb screenshot --id {MESSAGE} --dir {folder}")
    assert result.ok, result.error
    assert not folder.exists() and not folder.parent.exists()
    assert len(browser.shots) == 1


def test_screenshot_tries_the_editor_when_the_message_page_gives_no_picture(browser, vendor, pauses, monkeypatch):
    tries = []
    real = browser._screenshot

    def shoot(seat, path, timeout=60, full_page=False):
        tries.append(browser.url)
        if len(tries) == 1:
            raise page_module.plc.BrowserError("screenshot timed out")
        return real(seat, path, timeout, full_page)

    monkeypatch.setattr(page_module.plc, "screenshot", shoot)
    result = run(f"recipe gb screenshot --id {MESSAGE}")
    assert result.ok, result.error
    assert tries == [f"{BASE}/messages/{MESSAGE}", f"{BASE}/messages/{MESSAGE}/design"]
    assert pauses[-2:] == [4, 5]


def test_screenshot_that_fails_twice_says_so(browser, vendor, monkeypatch):
    def shoot(seat, path, timeout=60, full_page=False):
        raise page_module.plc.BrowserError("screenshot timed out")

    monkeypatch.setattr(page_module.plc, "screenshot", shoot)
    result = run(f"recipe gb screenshot --id {MESSAGE}")
    assert not result.ok and result.error == "Screenshot failed: screenshot timed out"
    assert result.files == [] or all(not path.endswith(".png") for path in result.files)


# --- login: the profile first ---------------------------------------------------------------------------------


def test_login_with_a_signed_in_profile_types_nothing(browser, vendor):
    result = run("recipe gb login")
    assert result.ok, result.error
    assert lines(result)[-1] == "Already signed in."
    assert browser.typed == [] and "state-save" not in browser.verbs()


def test_login_on_a_page_it_cannot_read_stops_for_a_person_and_types_nothing(browser, vendor):
    browser.redirects[f"{BASE}/messages"] = f"{BASE}/dashboard"
    result = run("recipe gb login")
    assert not result.ok
    assert result.error == (
        "needs a person: cannot tell whether givebacks is signed in. "
        "Look at the page in this seat's Chrome, and sign in by hand if it asks."
    )
    assert browser.typed == []


# --- login: the form ------------------------------------------------------------------------------------------


def test_login_types_the_stored_secrets_into_the_form_and_submits(browser, monkeypatch):
    site = Vendor(browser, monkeypatch, stage="out", needs_code=False)
    result = run("recipe gb login")
    assert result.ok, result.error
    assert browser.typed == [("aria-ref=e2", USER), ("aria-ref=e3", PASSWORD)]
    out = lines(result)
    assert "  fill-secret e2 GIVEBACKS_USER -> e2 <- [secret:GIVEBACKS_USER]" in out
    assert "  fill-secret e3 GIVEBACKS_PASS -> e3 <- [secret:GIVEBACKS_PASS]" in out
    assert "  click e6 -> e6" in out, "the Sign In button, not the Google or Apple one"
    assert out[-2].startswith("  save -> ") and out[-1] == "Login successful."
    assert site.stage == "in" and browser.url == f"{BASE}/messages"
    assert USER not in everything_in(result) and PASSWORD not in everything_in(result)
    assert browser.verbs().count("state-save") == 1, "the state is saved once the sign-in worked"


def test_login_waits_the_times_the_site_needs(browser, monkeypatch, pauses):
    Vendor(browser, monkeypatch, stage="out", needs_code=False)
    run("recipe gb login")
    assert pauses == [3, 4]


def test_login_without_the_stored_secrets_names_the_fix(browser, monkeypatch):
    Vendor(browser, monkeypatch, stage="out")
    secretstore.remove("seat", "GIVEBACKS_PASS")
    result = run("recipe gb login")
    assert not result.ok
    assert result.error == (
        "secret GIVEBACKS_PASS is not set for seat seat; the operator sets it with "
        "`ar3 tool browser --node seat secret set GIVEBACKS_PASS - --origin ORIGIN`"
    )


def test_login_with_a_wrong_password_ends_on_the_form_and_says_so(browser, monkeypatch):
    site = Vendor(browser, monkeypatch, stage="out")
    secretstore.set_secret("seat", "GIVEBACKS_PASS", "wrong-pw-1234", [BASE])
    result = run("recipe gb login")
    assert not result.ok and result.error == "Login failed - still not on the messages page."
    assert site.stage == "out"


def test_login_that_cannot_find_the_form_fields_says_so(browser, monkeypatch):
    Vendor(browser, monkeypatch, stage="out")
    monkeypatch.setattr(session, "snapshot", lambda seat: '- heading "Sign in" [ref=e1]\n- button "Sign In" [ref=e6]\n')
    result = run("recipe gb login")
    assert not result.ok and result.error == "Cannot find login form fields."
    assert browser.typed == []


def test_login_presses_enter_in_the_password_box_when_the_form_has_no_sign_in_button(browser, monkeypatch):
    site = Vendor(browser, monkeypatch, stage="out")
    monkeypatch.setattr(
        session, "snapshot",
        lambda seat: '- textbox "Email" [ref=e2]\n- textbox "Password" [ref=e3]\n- button "Sign in with Google" [ref=e4]\n',
    )
    browser.hooks["press"] = lambda key: site.move("in") if key == "Enter" else None
    result = run("recipe gb login")
    assert result.ok, result.error
    assert [call for call in browser.calls if call[0] in ("click", "press")] == [("click", "e3"), ("press", "Enter")]


# --- login: the one-time passcode -----------------------------------------------------------------------------


@pytest.fixture
def coded(browser, monkeypatch):
    return Vendor(browser, monkeypatch, stage="out", needs_code=True)


def the_page_files_hold_no_code():
    """No file the seat keeps shows the code, whole or in a box."""
    return [text for text in files_of_the_seat() if leaks(text)] == []


def test_login_reads_the_passcode_from_outlook_and_types_it_without_showing_it(browser, coded, pauses):
    result = run("recipe gb login")
    assert result.ok, result.error
    assert coded.stage == "in" and browser.url == f"{BASE}/messages"
    digits = [value for selector, value in browser.typed if selector in PIN_SELECTORS]
    assert "".join(digits) == CODE and [s for s, v in browser.typed if s in PIN_SELECTORS] == PIN_SELECTORS
    out = lines(result)
    assert [line for line in out if line.startswith("  fill_hidden ")] == [
        f"  fill_hidden e{number} -> e{number} <- [hidden]" for number in range(11, 17)
    ]
    assert out[-1] == "Login successful."
    assert pauses == [3, 4, 5, 5, 2, 1, 1] + [0.2] * 6 + [0.5, 3, 3]


def test_login_follows_the_second_tab_and_comes_back_to_the_first(browser, coded):
    result = run("recipe gb login")
    assert result.ok, result.error
    assert browser.tabs == [f"{BASE}/messages"] and browser.tab == 0
    verbs = browser.verbs()
    assert verbs.index("tab-new") < verbs.index("tab-close") < verbs.index("tab-select")
    assert ("tab-new", OUTLOOK) in browser.calls and ("tab-select", "0") in browser.calls
    assert ("click", "e30") in browser.calls, "the message that holds the code is opened"
    assert ("check", "e20") in browser.calls, "'trust this browser' is ticked"
    assert ("press", "Enter") in browser.calls, "the submit button is disabled before the code is in, so Enter submits"


def test_the_passcode_is_in_no_output_when_the_login_succeeds(browser, coded):
    result = run("recipe gb login")
    assert result.ok, result.error
    seen = everything_in(result)
    assert not leaks(seen), leaks(seen)
    assert "  hide PASSCODE" in lines(result)
    assert USER not in seen and PASSWORD not in seen


def test_the_passcode_is_in_no_command_line_or_code_the_driver_was_given(browser, coded):
    run("recipe gb login")
    flat = json.dumps(browser.calls, default=str) + "\n".join(browser.codes)
    assert CODE not in flat


def test_the_page_files_of_the_passcode_section_are_deleted(browser, coded):
    """The control: playwright-cli follows a click or a key with a snapshot of the page, and during the
    section those hold the mail and the digits, one to a box, which no scrub could find."""
    result = run("recipe gb login")
    assert result.ok, result.error
    assert len(coded.leaky_files) >= 4, "the fake wrote no page file that holds the code: the check below proves nothing"
    assert [path for path in coded.leaky_files if path.exists()] == []
    assert the_page_files_hold_no_code()


def test_the_page_files_before_the_section_stay(browser, coded):
    run("recipe gb login")
    kept = sorted(path.name for path in Path(session.cli_output_dir("seat")).glob("page-*.yml"))
    assert kept and all(name.startswith("page-") for name in kept)
    held = [path.read_text() for path in Path(session.cli_output_dir("seat")).glob("page-*.yml")]
    assert any("Sign In" in text for text in held), "the page file of the sign-in form is not the section's"


def test_the_passcode_is_in_no_output_when_the_code_is_wrong(browser, monkeypatch):
    # The mail says one code and the site wants another: the recipe types what the mail says.
    site = Vendor(browser, monkeypatch, stage="out", needs_code=True, code="999999")
    result = run("recipe gb login")
    assert not result.ok and result.error == "Login failed - still not on the messages page."
    assert site.wrong == 1 and site.stage == "otp"
    seen = everything_in(result)
    assert not leaks(seen), leaks(seen)
    failure = Path([path for path in result.files if path.endswith("failure.txt")][0]).read_text()
    assert "PinInput" in failure, "the failure snapshot is of the code page"
    assert site.leaky_files and the_page_files_hold_no_code()


def test_the_code_page_is_loaded_again_after_a_failed_login_so_no_digit_is_left_on_it(browser, monkeypatch):
    site = Vendor(browser, monkeypatch, stage="out", needs_code=True, code="999999")
    run("recipe gb login")
    gotos = [call for call in browser.calls if call[0] == "goto"]
    assert gotos[-1] == ("goto", f"{BASE}/messages"), "the last navigation reloads the site"
    assert site.typed_here() == {}


def test_the_boxes_are_emptied_when_the_page_cannot_be_loaded_again(browser, monkeypatch):
    site = Vendor(browser, monkeypatch, stage="out", needs_code=True, code="999999")

    def broken(*args):
        site.on_goto(*args)
        if site.wrong:  # the submit has been refused: the reload fails
            raise plc.BrowserError("net::ERR_INTERNET_DISCONNECTED")

    browser.hooks["goto"] = broken
    result = run("recipe gb login")
    assert not result.ok and result.error == "Login failed - still not on the messages page."
    assert [value for selector, value in browser.typed[-6:]] == [""] * 6
    assert [selector for selector, value in browser.typed[-6:]] == PIN_SELECTORS
    failure = Path([path for path in result.files if path.endswith("failure.txt")][0]).read_text()
    assert "PinInput" in failure and not leaks(failure), "the failure snapshot is of the code page, boxes empty"
    assert not leaks(everything_in(result))


def test_the_passcode_is_in_no_output_when_the_login_fails_while_it_is_typed(browser, coded, monkeypatch):
    real = browser._fill_secret

    def fail_on_the_fourth(seat, selector, value, origins, directory, timeout=30):
        if selector == "aria-ref=e14":
            raise plc.BrowserError(f'Error: locator.fill: Timeout 5000ms exceeded.\nCall log:\n  - fill("{value}")')
        return real(seat, selector, value, origins, directory, timeout)

    monkeypatch.setattr(plc, "fill_secret", fail_on_the_fourth)
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED
    assert not leaks(everything_in(result))
    assert coded.typed_here() == {}, "the cleanup loaded the page again: the digits typed before the failure are gone"
    assert browser.closed == 0, "the page was confirmed clear, so Chrome stays"


def test_the_passcode_is_in_no_output_when_the_failure_comes_after_the_code_is_typed(browser, coded, monkeypatch):
    real = browser._run

    def refuse_the_tick(seat, *args, **kwargs):
        if args and args[0] == "check":
            raise plc.BrowserError("check: the box is covered")
        return real(seat, *args, **kwargs)

    coded.cli = refuse_the_tick
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED
    assert not leaks(everything_in(result))
    assert coded.leaky_files and the_page_files_hold_no_code()
    assert coded.typed_here() == {} and browser.closed == 0


def test_the_passcode_is_in_no_output_when_the_page_cannot_be_read_after_the_submit(browser, coded, monkeypatch):
    """The failure comes after the digits are in the boxes and the code has gone to the site."""
    real = session.current_url

    def gone_after_the_key(seat):
        if browser.calls and browser.calls[-1][0] == "press":
            raise plc.BrowserError("the page is gone")
        return real(seat)

    monkeypatch.setattr(session, "current_url", gone_after_the_key)
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED
    assert not leaks(everything_in(result))
    assert coded.typed_here() == {}


def test_the_passcode_is_in_no_output_when_the_outlook_tab_cannot_be_closed(browser, coded, monkeypatch):
    """The code is read and hidden, and the tab that shows it will not close, however often it is asked. The
    cleanup cannot confirm the page clear, so Chrome is closed and the failure snapshot is not taken."""
    real = browser._run

    def stuck(seat, *args, **kwargs):
        if args and args[0] == "tab-close":
            raise plc.BrowserError("tab-close: the tab will not close")
        return real(seat, *args, **kwargs)

    coded.cli = stuck
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED_AND_CLOSED
    assert browser.url.startswith("https://outlook."), "the mail is still on screen, in a Chrome that is now closed"
    assert browser.closed == 1
    assert [path for path in result.files if path.endswith("failure.txt")] == []
    assert not leaks(everything_in(result))


def test_a_stop_before_the_code_is_read_leaves_no_file_that_holds_it(browser, coded, pauses, monkeypatch):
    """The mail is open and shows the code, and the stop lands before the recipe has read it, so it is not
    hidden yet. The page files of the section are deleted all the same."""
    def stop_after_the_click(self, seconds):
        self._tick()
        pauses.append(seconds)
        if seconds == 2:
            session.bump_stop_epoch("seat")

    monkeypatch.setattr(page_module.Page, "wait", stop_after_the_click)
    result = run("recipe gb login")
    assert not result.ok and result.error == session.STOP_MESSAGE
    assert coded.leaky_files and [path for path in coded.leaky_files if path.exists()] == []
    assert not leaks(everything_in(result))
    assert "  hide PASSCODE" not in output(result, 0)


def test_a_signed_out_outlook_stops_for_a_person_and_the_tab_is_closed(browser, monkeypatch):
    site = Vendor(browser, monkeypatch, stage="out", needs_code=True, outlook="signed_out")
    result = run("recipe gb login")
    assert not result.ok
    assert result.error == (
        "needs a person: Outlook web is signed out in this seat, and GiveBacks wants a code from it. "
        "Sign in to Outlook web by hand in this seat's Chrome, then log in again."
    )
    assert browser.tabs == [f"{BASE}/one-time-passcode"] and browser.tab == 0
    assert site.stage == "otp" and [s for s, v in browser.typed if s in PIN_SELECTORS] == []
    failure = Path([path for path in result.files if path.endswith("failure.txt")][0]).read_text()
    assert "PinInput" in failure, "the failure snapshot is of the code page, not of Microsoft's"


def test_a_tab_that_passes_through_microsofts_sign_in_on_its_way_to_outlook_is_not_a_signed_out_mailbox(
    browser, monkeypatch, pauses,
):
    """The tab can be on Microsoft's sign-in for a moment when it opens, and on the mail when the wait is over."""
    site = Vendor(browser, monkeypatch, stage="out", needs_code=True, outlook="signed_out")

    def arrive(self, seconds):
        self._tick()
        pauses.append(seconds)
        if seconds == 5 and browser.url == MICROSOFT:
            browser.url = OUTLOOK_LANDING
            site.outlook = "open"

    monkeypatch.setattr(page_module.Page, "wait", arrive)
    result = run("recipe gb login")
    assert result.ok, result.error
    assert site.stage == "in"


def test_a_mailbox_with_no_code_stops_for_a_person_and_the_tab_is_closed(browser, monkeypatch):
    Vendor(browser, monkeypatch, stage="out", needs_code=True, outlook="empty")
    result = run("recipe gb login")
    assert not result.ok
    assert result.error == (
        "needs a person: GiveBacks sent a one-time passcode and none could be read from Outlook web. "
        "Read the mail by hand and sign in by hand in this seat's Chrome."
    )
    assert browser.tabs == [f"{BASE}/one-time-passcode"] and browser.tab == 0
    assert [s for s, v in browser.typed if s in PIN_SELECTORS] == []


def test_an_outlook_tab_that_opens_another_page_fails_and_the_tab_is_closed(browser, monkeypatch):
    Vendor(browser, monkeypatch, stage="out", needs_code=True, outlook="elsewhere")
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED
    assert browser.tabs == [f"{BASE}/one-time-passcode"]
    assert browser.closed == 0


def test_an_outlook_tab_that_cannot_be_opened_fails_the_step_and_leaves_the_site_tab_open(browser, coded, monkeypatch):
    real = browser._run

    def refuse(seat, *args, **kwargs):
        if args and args[0] == "tab-new":
            raise plc.BrowserError("tab-new: the browser would not open a tab")
        return real(seat, *args, **kwargs)

    coded.cli = refuse
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED
    assert browser.tabs == [f"{BASE}/one-time-passcode"], "the tab of the site was not closed"
    assert "tab-close" not in browser.verbs()


def test_the_phone_and_fax_lines_do_not_give_a_code(settings):
    find = the_recipe().probe.__globals__["_find_code"]
    assert find("- generic: Phone 555 123456\n- generic [ref=e2]: code 482916") == "482916"
    assert find("- generic: Fax 123456") is None
    assert find("- generic [ref=e2]: Fax 123456") == "123456", "the second pass takes any generic line"
    assert find("- option: 1234567 and 12345 and a1234567") is None
    assert find("- text: Your code is 482916.") == "482916"
    assert find("") is None


def test_login_that_finds_fewer_than_six_boxes_says_so_and_types_nothing(browser, coded, monkeypatch):
    real = coded.passcode_page
    coded.passcode_page = lambda: "\n".join(real().splitlines()[:5]) + "\n"
    result = run("recipe gb login")
    assert not result.ok and result.error == "Found 3 pin inputs, need 6."
    assert [s for s, v in browser.typed if s in PIN_SELECTORS] == []
    assert browser.tabs == [f"{BASE}/one-time-passcode"]
    assert not leaks(everything_in(result))


def test_login_from_a_code_page_left_open_signs_out_first_to_get_a_fresh_code(browser, monkeypatch):
    site = Vendor(browser, monkeypatch, stage="otp", needs_code=True)
    result = run("recipe gb login")
    assert result.ok, result.error
    assert ("click", "e23") in browser.calls, "the Sign Out button"
    assert browser.calls.index(("click", "e23")) < browser.calls.index(("click", "e6"))
    assert site.stage == "in"
    assert "  log: A passcode page is already open. Signing out to get a fresh code." in lines(result)


def test_login_mentions_what_it_does_in_words_that_hold_no_secret_and_no_address(browser, coded):
    result = run("recipe gb login")
    logged = [line for line in lines(result) if "log:" in line]
    assert logged == [
        "  log: Logging in to GiveBacks...",
        "  log: A one-time passcode is required. Reading it from Outlook web.",
    ]
    assert "a***@example.com" not in result.transcript(), "the address the code was sent to is not repeated"


def test_the_whole_login_fits_the_transcript_without_a_cut(browser, coded):
    result = run("recipe gb login")
    assert "more lines" not in output(result)


def test_a_stop_during_the_login_ends_it_at_the_next_call(browser, coded, monkeypatch):
    seen = []

    def wait(self, seconds):
        self._tick()
        seen.append(seconds)
        if len(seen) == 3:
            session.bump_stop_epoch("seat")

    monkeypatch.setattr(page_module.Page, "wait", wait)
    result = run("recipe gb login")
    assert not result.ok and result.error == session.STOP_MESSAGE
    assert [s for s, v in browser.typed if s in PIN_SELECTORS] == []


# --- login: the passcode step cannot leak when its cleanup fails ----------------------------------------------
#
# The review found that the failure snapshot of a failed sign-in escaped the page-file guard once the recipe's
# own cleanup failed too. A best-effort cleanup cannot be the gate. The passcode step is a sensitive step
# (`page.sensitive`): nothing of the page is captured while it is open, the error is one line that holds none
# of the page, and the step ends with a cleanup that must confirm the browser holds nothing of the code.
# Failing to confirm closes Chrome. Each case runs twice: with the step, and with the step taken out
# (`without`, the control), which must leak, or the case proves nothing.


@pytest.fixture(params=["with", "without", "lying"])
def section(request, monkeypatch):
    """The recipe as it is, and two controls that must leak. `without`: the same recipe with the sensitive step
    taken out, so that the page files are still deleted (what the recipe had alone) and nothing else guards the
    failure. `lying`: the step is there and its cleanup says yes without looking, which shows that the honest
    answer of the cleanup is what keeps Chrome closed and the snapshot untaken."""
    if request.param == "without":
        @contextlib.contextmanager
        def only_the_page_files(self, name, cleanup):
            with self.no_page_files():
                yield

        monkeypatch.setattr(page_module.Page, "sensitive", only_the_page_files, raising=False)
    elif request.param == "lying":
        real = page_module.Page.sensitive
        monkeypatch.setattr(page_module.Page, "sensitive", lambda self, name, cleanup: real(self, name, lambda: True))
    return request.param


@pytest.fixture
def snapshots(monkeypatch):
    """The labels of the snapshots the tool tried to write, whatever came of them."""
    tried = []
    real = commands._write_snapshot

    def spy(seat, run, label="snapshot"):
        tried.append(label)
        return real(seat, run, label)

    monkeypatch.setattr(commands, "_write_snapshot", spy)
    return tried


def failure_text(result):
    found = [path for path in result.files if path.endswith("failure.txt")]
    return Path(found[0]).read_text() if found else None


def test_a_click_on_the_code_message_fails_and_its_tab_will_not_close(browser, coded, monkeypatch, section, snapshots):
    """Case (a) of the review. The click on the mail that holds the code fails, with a driver message that quotes the
    mail, and closing the tab fails too. The mail is open on screen with the code in its preview, and the
    code has not been hidden yet."""
    real = browser._run

    def broken(seat, *args, **kwargs):
        if args and args[0] == "click" and args[1] == "e30":
            raise plc.BrowserError(
                "TimeoutError: locator.click: Timeout 5000ms exceeded.\nCall log:\n"
                f"  - locator resolved to <div role=option aria-label='Your one-time passcode Support 9:41 AM Your code is {CODE}'>"
            )
        if args and args[0] == "tab-close":
            raise plc.BrowserError("tab-close: the tab will not close")
        return real(seat, *args, **kwargs)

    coded.cli = broken
    result = run("recipe gb login")
    assert not result.ok
    seen = everything_in(result)
    assert browser.url.startswith("https://outlook."), "the mail is still the page"
    if section != "with":
        assert leaks(seen), f"the control ({section}): the failure snapshot attaches the code"
        assert "failure" in snapshots and browser.closed == 0
        return
    assert result.error == PASSCODE_FAILED_AND_CLOSED
    assert browser.closed == 1, "the page could not be confirmed clear, so Chrome was closed"
    assert "failure" not in snapshots, "no failure snapshot is even tried once the page is not known to be clear"
    assert failure_text(result) is None
    assert not leaks(seen), leaks(seen)
    assert coded.leaky_files and [path for path in coded.leaky_files if path.exists()] == []
    assert CODE not in json.dumps(result.steps) and CODE not in result.transcript()


def test_the_code_is_typed_and_the_check_fails_and_the_reload_and_the_clearing_fail(
    browser, coded, monkeypatch, section, snapshots
):
    """Case (b) of the review. All six digits are in the boxes. Ticking "trust this browser" fails, loading the page
    again fails, and emptying the boxes fails. The failure snapshot would show the digits, one to a box."""
    real = browser._run
    real_fill = browser._fill_secret

    def broken(seat, *args, **kwargs):
        if args and args[0] == "check":
            raise plc.BrowserError("check: the box is covered")
        if args and args[0] == "goto" and coded.typed_here():
            raise plc.BrowserError("net::ERR_INTERNET_DISCONNECTED")
        return real(seat, *args, **kwargs)

    def cannot_clear(seat, selector, value, origins, directory, timeout=30):
        if value == "":
            raise plc.BrowserError('TimeoutError: locator.fill: Timeout 5000ms exceeded.\nCall log:\n  - fill("")')
        return real_fill(seat, selector, value, origins, directory, timeout)

    coded.cli = broken
    monkeypatch.setattr(plc, "fill_secret", cannot_clear)
    result = run("recipe gb login")
    assert not result.ok
    seen = everything_in(result)
    assert len(coded.typed_here()) == 6, "all six digits are in the boxes"
    if section != "with":
        assert leaks(seen), f"the control ({section}): the failure snapshot attaches the digits"
        assert BOX_DIGIT.search(failure_text(result)) and browser.closed == 0
        return
    assert result.error == PASSCODE_FAILED_AND_CLOSED
    assert browser.closed == 1, "the boxes could not be emptied and the page could not be left: Chrome was closed"
    assert "failure" not in snapshots and failure_text(result) is None
    assert not leaks(seen), leaks(seen)
    assert coded.leaky_files and [path for path in coded.leaky_files if path.exists()] == []


def test_a_chrome_that_will_not_close_is_said_and_nothing_is_captured_after(browser, coded, monkeypatch):
    """The page is not known to be clear and Chrome does not go: the error says what to run, and capture stays off."""
    real = browser._run

    def broken(seat, *args, **kwargs):
        if args and args[0] == "tab-close":
            raise plc.BrowserError("tab-close: the tab will not close")
        return real(seat, *args, **kwargs)

    coded.cli = broken
    browser.chrome_stays = True
    monkeypatch.setattr(commands, "CLOSE_WAIT_SECONDS", 0.2)
    result = run("recipe gb login")
    assert not result.ok
    assert result.error == (
        PASSCODE_FAILED + ". The page could not be confirmed clear, and Chrome could not be closed. "
        "Close it with `ar3 tool browser --seat seat close`."
    )
    assert browser.closed == 1
    assert failure_text(result) is None, "capture stays off until the page is known to be clear"
    assert not leaks(everything_in(result))


def test_a_tab_that_would_not_close_once_is_closed_by_the_cleanup_and_chrome_stays(browser, coded, monkeypatch):
    """The step failed, and the cleanup found the mail's tab still open, closed it and confirmed. The page is
    clear, so Chrome stays, and the failure snapshot is taken of the site and shows no code."""
    real = browser._run
    refused = []

    def once(seat, *args, **kwargs):
        if args and args[0] == "tab-close" and not refused:
            refused.append(args)
            raise plc.BrowserError("tab-close: the tab will not close")
        return real(seat, *args, **kwargs)

    coded.cli = once
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED
    assert browser.closed == 0
    assert browser.tabs == [f"{BASE}/one-time-passcode"] and browser.tab == 0
    failure = failure_text(result)
    assert failure is not None and "PinInput" in failure and not leaks(failure)
    assert not leaks(everything_in(result))


def test_a_mail_tab_at_an_address_nobody_listed_is_closed_by_the_cleanup(browser, monkeypatch):
    """Outlook web can end on a host the recipe does not know. The tab the step opened is closed all the same."""
    site = Vendor(browser, monkeypatch, stage="out", needs_code=True, outlook="stray")
    real = site.cli
    refused = []

    def once(seat, *args, **kwargs):
        if args and args[0] == "tab-close" and not refused:
            refused.append(args)
            raise plc.BrowserError("tab-close: the tab will not close")
        return real(seat, *args, **kwargs)

    site.cli = once
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED
    assert browser.tabs == [f"{BASE}/one-time-passcode"] and browser.closed == 0
    assert refused, "the recipe's own close failed, so the cleanup had a tab to close"


def test_a_tab_the_seat_had_before_the_step_stays_even_when_it_shows_outlook(browser, coded):
    """The cleanup closes the tabs the step opened, and not a tab that was open when it began."""
    browser.tabs.append("https://outlook.office.com/mail/the-own-inbox-of-the-seat")
    browser.history[1] = []
    result = run("recipe gb login")
    assert result.ok, result.error
    assert browser.tabs == [f"{BASE}/messages", "https://outlook.office.com/mail/the-own-inbox-of-the-seat"]
    assert browser.closed == 0


def test_the_cleanup_closes_the_tab_the_step_opened_and_not_the_one_the_seat_had(browser, coded):
    """Three tabs: the sign-in tab, a tab of the seat that shows Outlook, and the mail tab the step opened. The close
    of the mail tab fails once. The cleanup closes it, by its place, and leaves the tab of the seat."""
    browser.tabs.append("https://outlook.office.com/mail/the-own-inbox-of-the-seat")
    browser.history[1] = []
    real = coded.cli
    refused = []

    def once(seat, *args, **kwargs):
        if args and args[0] == "tab-close" and not refused:
            refused.append(args)
            raise plc.BrowserError("tab-close: the tab will not close")
        return real(seat, *args, **kwargs)

    coded.cli = once
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED
    assert browser.tabs == [f"{BASE}/one-time-passcode", "https://outlook.office.com/mail/the-own-inbox-of-the-seat"]
    assert browser.closed == 0 and refused


def test_a_code_page_that_shows_no_boxes_is_not_known_to_be_empty(browser, monkeypatch):
    """The wrong code is refused, and the page then shows a modal over its boxes, so the snapshot lists none."""
    site = Vendor(browser, monkeypatch, stage="out", needs_code=True, code="999999")
    real = site.passcode_page
    site.passcode_page = lambda: real() if not site.wrong else '- dialog "Something went wrong" [ref=e50]\n'
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED_AND_CLOSED
    assert browser.closed == 1, "no box was seen empty, so the page is not known to be clear"
    assert not leaks(everything_in(result))


def test_a_reload_that_works_but_leaves_the_digits_on_the_page_does_not_confirm(browser, monkeypatch):
    """The cleanup looks again after it has acted. A page that shows the digits after the reload is not clear."""
    site = Vendor(browser, monkeypatch, stage="out", needs_code=True, code="999999")
    browser.hooks["goto"] = lambda *args: None  # the page is loaded again and keeps what was typed
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED_AND_CLOSED
    assert site.typed_here() and browser.closed == 1
    assert failure_text(result) is None and not leaks(everything_in(result))


def test_boxes_that_are_emptied_in_name_only_do_not_confirm(browser, monkeypatch):
    """The reload fails, the call that empties a box succeeds and does nothing, and the boxes still show the digits."""
    site = Vendor(browser, monkeypatch, stage="out", needs_code=True, code="999999")
    real = browser._fill_secret
    cli = browser._run

    def reload_fails(seat, *args, **kwargs):
        if args and args[0] == "goto" and site.typed_here():
            raise plc.BrowserError("net::ERR_INTERNET_DISCONNECTED")
        return cli(seat, *args, **kwargs)

    def empties_nothing(seat, selector, value, origins, directory, timeout=30):
        if value == "":
            return BASE
        return real(seat, selector, value, origins, directory, timeout)

    site.cli = reload_fails
    monkeypatch.setattr(plc, "fill_secret", empties_nothing)
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED_AND_CLOSED
    assert len(site.typed_here()) == 6 and browser.closed == 1
    assert failure_text(result) is None and not leaks(everything_in(result))


def test_a_login_is_not_filmed(browser, coded):
    """A sender sends `video-start`, `recipe gb login`, `video-stop`. A recording is pixels, and a scrub does not
    reach pixels: the login does not open the mail while one is on."""
    session.save_video_state("seat", {"path": "/tmp/never-written.webm", "started_at": 0, "chapters": 0})
    result = run("recipe gb login")
    assert not result.ok and result.error.startswith("sensitive: a video is recording")
    assert "tab-new" not in browser.verbs() and browser.tabs == [f"{BASE}/one-time-passcode"]
    assert [(selector, value) for selector, value in browser.typed if selector in PIN_SELECTORS] == []
    assert not leaks(everything_in(result))


def test_a_wrong_code_ends_with_the_boxes_empty_and_chrome_stays(browser, monkeypatch):
    site = Vendor(browser, monkeypatch, stage="out", needs_code=True, code="999999")
    result = run("recipe gb login")
    assert not result.ok and result.error == "Login failed - still not on the messages page."
    assert site.wrong == 1 and site.typed_here() == {}
    assert browser.closed == 0
    failure = failure_text(result)
    assert failure is not None and "PinInput" in failure and not leaks(failure)
    assert not leaks(everything_in(result))


def test_a_login_that_works_asks_the_browser_whether_it_is_clear_and_leaves_chrome_alone(browser, coded):
    result = run("recipe gb login")
    assert result.ok, result.error
    assert browser.closed == 0 and "tab-list" in browser.verbs()
    assert result.capturing and result.sensitive == [] and result.uncleared == []


def test_the_cleanup_looks_before_it_acts_on_a_clear_page(browser, monkeypatch):
    """A step that fails before any digit is typed finds the boxes empty, so the page is not loaded again."""
    Vendor(browser, monkeypatch, stage="out", needs_code=True, outlook="empty")
    result = run("recipe gb login")
    assert not result.ok and result.error.startswith("needs a person: GiveBacks sent a one-time passcode")
    assert [call for call in browser.calls if call[0] == "goto"] == [("goto", f"{BASE}/messages")]
    assert browser.closed == 0


def test_the_passcode_step_is_open_before_the_mail_is_opened_and_closed_after_the_code_page_is_left(
    browser, coded, monkeypatch
):
    seen = []
    real = page_module.Page.sensitive

    @contextlib.contextmanager
    def watch(self, name, cleanup):
        seen.append(("open", name, len(browser.tabs), browser.url))
        with real(self, name, cleanup):
            yield
        seen.append(("closed", name, len(browser.tabs), browser.url))

    monkeypatch.setattr(page_module.Page, "sensitive", watch)
    result = run("recipe gb login")
    assert result.ok, result.error
    assert seen == [
        ("open", "passcode", 1, f"{BASE}/one-time-passcode"),
        ("closed", "passcode", 1, f"{BASE}/messages"),
    ], "the mail's tab is opened inside the step, and the step ends on a page that is not the code page"
    verbs = browser.verbs()
    assert verbs.count("tab-new") == 1


def test_no_page_of_the_passcode_step_is_captured_when_a_failure_comes_from_the_mail(browser, coded, monkeypatch):
    """A failure inside the step takes no failure snapshot even when the cleanup confirms: the snapshot is taken
    after the step has ended, of the page the cleanup left."""
    real = browser._run

    def broken(seat, *args, **kwargs):
        if args and args[0] == "click" and args[1] == "e30":
            raise plc.BrowserError("click: the message would not open")
        return real(seat, *args, **kwargs)

    coded.cli = broken
    result = run("recipe gb login")
    assert not result.ok and result.error == PASSCODE_FAILED
    failure = failure_text(result)
    assert failure is not None and "Your one-time passcode" not in failure, "the snapshot is of the site, not of the mail"
    assert browser.closed == 0 and browser.tabs == [f"{BASE}/one-time-passcode"]


# --- check ---------------------------------------------------------------------------------------------------


def test_check_runs_the_probe_and_the_list_and_never_the_login(browser, vendor):
    given(browser, f"{API}?cause_id={CAUSE}&per_page=3", {"messages": ROWS})
    result = run("recipe gb check")
    assert result.ok, result.error
    assert lines(result)[-3:] == ["status: in", "list: healthy", "check: healthy (2)"]
    assert browser.typed == []


def test_check_fails_when_the_site_is_signed_out(browser, monkeypatch):
    Vendor(browser, monkeypatch, stage="out")
    result = run("recipe gb check")
    assert not result.ok and result.error == "check failed: list"
    assert "status: out" in output(result)


# --- the alias goes by the same words ------------------------------------------------------------------------


def test_the_alias_and_the_name_run_the_same_verb(browser, vendor):
    given(browser, f"{API}?cause_id={CAUSE}&per_page=1", {"messages": ROWS})
    by_alias = run("recipe gb list --limit 1")
    by_name = run("recipe givebacks list --limit 1")
    assert by_alias.ok and by_name.ok
    assert lines(by_alias)[-1] == lines(by_name)[-1] == "  m-1  [sent]  2026-10-20  First"
