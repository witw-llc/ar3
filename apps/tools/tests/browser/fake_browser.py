"""A stand-in for the browser behind the page facade: playwright-cli and the session, in memory.

It keeps tabs and their URLs, and answers the calls the facade makes. A test says where a goto lands
(`redirects`), where a click leads (`links`), what a selector reads (`texts`), what a fetch returns
(`fetches`) and what the recipe's own code returns (`evaluations`, `run_codes`). Every call and every
code file is recorded, so a test can show that a refused call reached nothing.

The Chrome can be closed, as `session.close_browser` closes it: `closed` counts the closes, and once one
has happened every call fails as playwright-cli fails on a session that is not open. `chrome_stays` makes a
close not take, for the test of a Chrome that will not go.

The addresses of the open pages are also answered the way Chrome's DevTools endpoint answers them
(`session.pages`, and `session.close_page`), so that the check of a step reads them without a driver call.
`devtools = False` makes the endpoint not answer, and the check then asks the driver, as it does in a Chrome
whose port the owner fixed.
"""
import json
import re

from browser import commands
from browser import plc
from browser import recipe
from browser import resolve
from browser import secretstore
from browser import session

OK = "### Result\nok\n"
LITERAL = re.compile(r"JSON\.parse\((\".*\")\)")


class FakeBrowser:
    def __init__(self, monkeypatch, seat="seat"):
        self.seat = seat
        self.tabs = ["about:blank"]
        self.tab = 0
        self.history = {0: []}
        self.redirects = {}
        self.links = {}
        self.texts = {}
        self.fetches = {}
        self.evaluations = []
        self.run_codes = []
        self.calls = []
        self.codes = []
        self.typed = []
        self.shots = []
        self.hooks = {}
        self.timeouts = []
        self.opened = 0
        self.closed = 0
        self.chrome_stays = False
        self.devtools = True
        self.stuck = set()  # the addresses of tabs that Chrome will not close
        self.snapshot_text = "- heading 'Snapshot'"
        monkeypatch.setattr(session, "current_url", self._current_url)
        monkeypatch.setattr(session, "ensure_running", self._ensure_running)
        monkeypatch.setattr(session, "snapshot", self._snapshot)
        monkeypatch.setattr(session, "close_browser", self._close_browser)
        monkeypatch.setattr(session, "chrome_running", lambda seat: self.chrome_stays)
        monkeypatch.setattr(plc, "run", self._run)
        monkeypatch.setattr(plc, "evaluate", self._evaluate)
        monkeypatch.setattr(plc, "run_code_file", self._run_code_file)
        monkeypatch.setattr(plc, "call_in_page", self._call_in_page)
        monkeypatch.setattr(plc, "wait_for_selector", self._wait_for_selector)
        monkeypatch.setattr(session, "pages", self._pages)
        monkeypatch.setattr(session, "close_page", self._close_page)
        monkeypatch.setattr(plc, "screenshot", self._screenshot)
        monkeypatch.setattr(plc, "fill_secret", self._fill_secret)
        monkeypatch.setattr(resolve, "click_target", lambda seat, argument: argument)
        monkeypatch.setattr(resolve, "fill_target", lambda seat, argument: argument)

    @property
    def url(self):
        return self.tabs[self.tab]

    @url.setter
    def url(self, value):
        self.tabs[self.tab] = value

    def verbs(self):
        """The playwright-cli verbs called so far, in order."""
        return [call[0] for call in self.calls]

    def check_open(self):
        if self.closed and not self.chrome_stays:
            raise plc.BrowserError("The browser 'seat' is not open, please run open first")

    def _current_url(self, seat):
        self.check_open()
        return self.url

    def _pages(self, seat):
        if not self.devtools or (self.closed and not self.chrome_stays):
            return None
        # The id of a page is its address here, which is stable while others close; Chrome's own ids are opaque.
        return [(url, url) for url in self.tabs]

    def _close_page(self, seat, page_id):
        if not self.devtools or page_id not in self.tabs or page_id in self.stuck:
            return False
        del self.tabs[self.tabs.index(page_id)]
        self.tab = min(self.tab, len(self.tabs) - 1)
        return True

    def _call_in_page(self, seat, function, values, directory, timeout=30):
        """The calls the commands make with the caller's text as data: the text of a selector, the text of the body."""
        self.check_open()
        self.calls.append(("call_in_page", function, list(values)))
        if "document.querySelector(selector)" in function:
            shown = self.texts.get(values[0], "")
            return shown() if callable(shown) else shown
        if "document.body?.innerText" in function:
            body = self.texts.get("body", "")
            return values[0] in (body() if callable(body) else body)
        raise AssertionError(f"the fake browser does not know this page call: {function[:80]}")

    def _wait_for_selector(self, seat, selector, seconds, directory):
        self.check_open()
        self.calls.append(("wait_for_selector", selector, seconds))

    def _snapshot(self, seat):
        self.check_open()
        return self.snapshot_text() if callable(self.snapshot_text) else self.snapshot_text

    def _close_browser(self, seat):
        self.closed += 1

    def _ensure_running(self, seat, script_epoch=None):
        self.opened += 1

    def _land(self, url):
        self.history.setdefault(self.tab, []).append(self.url)
        self.url = self.redirects.get(url, url)

    def _run(self, seat, *args, timeout=30):
        self.check_open()
        args = tuple(arg for arg in args if arg != "--raw")
        self.calls.append(args)
        verb = args[0]
        if verb == "goto":
            self._land(args[1])
        elif verb == "click" and args[1] in self.links:
            self._land(self.links[args[1]])
        elif verb == "go-back" and self.history.get(self.tab):
            self.url = self.history[self.tab].pop()
        elif verb == "tab-new":
            self.tabs.append("about:blank")
            self.tab = len(self.tabs) - 1
            self.history[self.tab] = []
            if len(args) > 1:
                self._land(args[1])
        elif verb == "tab-select":
            self.tab = int(args[1])
        elif verb == "tab-close":
            index = int(args[1]) if len(args) > 1 else self.tab
            del self.tabs[index]
            self.tab = min(self.tab, len(self.tabs) - 1)
        if verb in self.hooks:
            self.hooks[verb](*args[1:])
        if verb == "tab-list":
            return "### Result\n" + "\n".join(
                f"- {number}: {'(current) ' if number == self.tab else ''}[]({url})"
                for number, url in enumerate(self.tabs)
            ) + "\n"
        return OK

    def _evaluate(self, seat, expression, timeout=30):
        self.check_open()
        match = re.search(r"querySelector\('(.*?)'\)", expression)
        if not match:
            return ""
        shown = self.texts.get(match.group(1), "")
        return shown() if callable(shown) else shown

    def _screenshot(self, seat, path, timeout=60, full_page=False):
        self.check_open()
        self.shots.append((path, full_page))
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG fake")
        return path

    def _fill_secret(self, seat, selector, value, origins, directory, timeout=30):
        self.check_open()
        self.typed.append((selector, value))
        if not secretstore.url_on_origins(self.url, origins):
            raise plc.OriginRefused(self.url)
        return self.url

    def _run_code_file(self, seat, code, directory, timeout=60):
        self.check_open()
        self.codes.append(code)
        self.timeouts.append(timeout)
        if "= await fetch(" in code:
            url, init, limit = json.loads(json.loads(LITERAL.search(code).group(1)))
            self.calls.append(("fetch", url, init))
            answer = self.fetches.get(url)
            answer = answer(url, init) if callable(answer) else answer
            if answer is None:
                answer = {"failed": "Failed to fetch"}
            elif "failed" not in answer:
                answer = {"url": url, "headers": {}, **answer}
            return json.dumps(answer) + "\n"
        values = json.loads(json.loads(LITERAL.search(code).group(1)))
        if "const body = async (page, values) => {" in code:
            source = code.split("const body = async (page, values) => {\n", 1)[1].rsplit("\n  };", 1)[0]
            self.calls.append(("run_code", source, values))
            table = self.run_codes
        else:
            source = code.split("(values) => (\n", 1)[1].split("\n  )(...values)", 1)[0]
            self.calls.append(("evaluate", source, values))
            table = self.evaluations
        for wanted, answer in table:
            if wanted in source:
                answer = answer(*values) if callable(answer) else answer
                return json.dumps({"value": answer}) + "\n"
        return "{}\n"


def static_library(*recipes):
    """A Library of the recipes given, with no file behind them."""

    class Static(recipe.Library):
        def _load(self):
            return {rec.name: recipe.Entry(rec.name, None, "node", recipe=rec) for rec in recipes}

    return Static()


def write_recipes(directory, **sources):
    """Files `<name>.py` holding the sources, in `directory`. Returns the directory."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        (directory / f"{name}.py").write_text(source)
    return directory


def run_script(script, library, allow_eval=False, seat="seat"):
    return commands.run_script(seat, script, allow_eval=allow_eval, recipes=library)
