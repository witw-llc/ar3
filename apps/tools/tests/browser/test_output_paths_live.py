"""A stored user name reaches nothing it should not, through a page that makes ids from it, remembers it in its own
storage and logs it seconds later. Live: needs Chrome and playwright-cli, and runs only with
AR3_TOOL_BROWSER_LIVE=1. State lives in temp XDG directories, the server is on 127.0.0.1, and the user name is made up.

The page copies what is typed into its user field to the ids of the elements that follow, to its local storage
at once, and to its console a few seconds later. That is ordinary site behaviour, and the three ways it carries
the value are the three that were found in review:

- a tool that names an element by its id puts the value in the command line of the next call (`click #<value>`);
- a `save` writes the local storage of the site, and the value with it, into the seat's state file;
- the console log is written by the driver's daemon after the call has returned, and a later call never looked at it.

Each is held here against a real page: `ps` is read every 10 ms while the scripts run, and the files of the seat
are read when they are over.

Two more were found in the next round, and are held the same way:

- a page that prints the value as JSON in an element, which a snapshot then quotes: the value is there under two layers
  of escapes, and a caller decodes it back (`test_a_page_that_prints_the_value_as_json_...`);
- text that looks like a ref and holds a PIN, which a plain `fill` or `type` would put in a command line, and a real
  ref that holds the PIN in its number, which must still be clicked (`test_text_that_looks_like_a_ref_...`).

And one in the round after that:

- a page that shows the value under two layers of JSON in a button, with an overlay over it, so that a click times out and
  Playwright's error previews the button with its text cut at 50 characters: the cut value, under two layers, is one the
  scrub cannot know, so the error of the driver is cut to its first line on a seat that holds a secret
  (`test_the_error_of_a_click_that_an_overlay_stops_...`).
"""
import json
import os
import re
import shlex
import stat
import threading
import time
import http.server
from pathlib import Path

import pytest

from test_secret_live import ArgvWatcher, _env, _files_holding, _gone, _mode, _script, _set, _tool

pytestmark = pytest.mark.skipif(
    os.environ.get("AR3_TOOL_BROWSER_LIVE") != "1",
    reason="live: set AR3_TOOL_BROWSER_LIVE=1",
)

VALUE = "SyntheticUserA7492"
CONTROL = "argv-control-7731"
REFUSED = "playwright-cli was not run: an argument of its command line held a value that must not be in one"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Sign in</title></head><body>
<form onsubmit="return false">
  <label for="user">Username</label><input id="user" type="text">
  <button id="next" type="button">Continue</button>
  <input id="agree" type="checkbox" aria-label="Agree">
  <select id="menu"><option>one</option><option>two</option></select>
  <a id="get" href="/report.txt" download="report.txt">Download</a>
  <div id="zone" class="zone" style="width:140px;height:40px;border:1px solid #888">Drop here</div>
</form>
<script>
const delay = Number(new URLSearchParams(location.search).get('delay') || 1500);
const zone = document.querySelector('.zone');
zone.addEventListener('dragover', (e) => e.preventDefault());
zone.addEventListener('drop', (e) => { e.preventDefault(); zone.textContent = 'dropped ' + e.dataTransfer.files.length; });
const user = document.getElementById('user');
user.addEventListener('input', () => {
  const v = user.value;
  document.getElementById('next').id = v;
  document.getElementById('agree').id = v + '-agree';
  document.getElementById('menu').id = v + '-menu';
  document.getElementById('get').id = v + '-get';
  document.getElementById('zone').id = v + '-zone';
  localStorage.setItem('remembered_user', v);
  setTimeout(() => console.log('remembered user ' + v), delay);
});
</script></body></html>
"""


# The page of the review: it prints what is typed as JSON in an element and in the console. A snapshot shows the text
# of the element in the double quotes of YAML, so the value is there under two layers of escapes.
NESTED = """<!doctype html>
<html><head><meta charset="utf-8"><title>Debug</title></head><body>
<input id="user" type="text"><pre id="debug"></pre>
<script>
const user = document.getElementById('user');
user.addEventListener('input', () => {
  const text = JSON.stringify({user: user.value});
  document.getElementById('debug').textContent = text;
  setTimeout(() => console.log('debug ' + text), 1500);
});
</script></body></html>
"""

# The page of the chain of the review of the third round: the value, under two layers of JSON, in the text of a button; and a
# layer over the page, so that a click on the button times out and Playwright's error previews the button.
OVERLAY = """<!doctype html>
<html><head><meta charset="utf-8"><title>Overlay</title></head><body>
<input id="user" type="text"><button id="b" type="button"></button>
<div id="shield" style="position:fixed;left:0;top:0;width:100%;height:100%"></div>
<script>
const user = document.getElementById('user');
user.addEventListener('input', () => {
  document.getElementById('b').textContent = JSON.stringify(JSON.stringify({user: user.value}));
});
</script></body></html>
"""

# A page that holds a note with a line feed and a tab in an attribute of a button, with a layer over the page: a click on the
# button times out, and the preview of the button in the error shows the note with U+21B5 for the line feed and U+21C6 for the tab.
NOTE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Note</title></head><body>
<button id="b" type="button" data-note="Marsh&#10;Lantern&#9;5047-Zephyr">Send</button>
<div id="shield" style="position:fixed;left:0;top:0;width:100%;height:100%"></div>
</body></html>
"""

# A page with so many elements that a snapshot gives refs up to e1200 and more.
REFS = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Refs</title></head><body><input id="user" type="text">'
    '<div id="out">none</div>'
    + "".join(f"<button onclick=\"document.getElementById('out').textContent='b{n}'\">b{n}</button>" for n in range(1, 1201))
    + "</body></html>"
)

FORM = """<!doctype html>
<html><head><meta charset="utf-8"><title>Search</title></head><body>
<form method="get" action="/form"><label for="q">Query</label><input id="q" name="q" type="text"><button id="go" type="submit">Go</button></form>
</body></html>
"""


class Site:
    def __init__(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/report.txt"):
                    body, kind = b"a report\n", "application/octet-stream"
                elif self.path.startswith("/form"):
                    body, kind = FORM.encode(), "text/html; charset=utf-8"
                elif self.path.startswith("/nested"):
                    body, kind = NESTED.encode(), "text/html; charset=utf-8"
                elif self.path.startswith("/overlay"):
                    body, kind = OVERLAY.encode(), "text/html; charset=utf-8"
                elif self.path.startswith("/note"):
                    body, kind = NOTE.encode(), "text/html; charset=utf-8"
                elif self.path.startswith("/refs"):
                    body, kind = REFS.encode(), "text/html; charset=utf-8"
                else:
                    body, kind = PAGE.encode(), "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()


def _cli_files(home):
    directory = home / "scratch" / ".playwright-cli"
    return sorted(directory.iterdir()) if directory.exists() else []


def _holding(paths, value):
    return [path.name for path in paths if path.is_file() and value.encode() in path.read_bytes()]


def test_a_page_that_makes_ids_from_the_stored_value_puts_it_in_no_command_line_and_no_state_file_of_the_reply(tmp_path):
    seat = f"ar3scratch{os.getpid()}paths"
    env = _env(tmp_path)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    site = Site()
    notes = tmp_path / "notes.txt"
    notes.write_text("a file to drop\n")
    try:
        _set(env, seat, "USER", VALUE, site.origin)

        # A positive control for the watcher: a plain `fill` of a text that is no secret is in a command line.
        with ArgvWatcher([CONTROL]) as control:
            plain, payload = _script(env, seat, f"go {site.origin}/\nfill #user {CONTROL}\n")
        assert payload["ok"], payload
        assert control.seen == {CONTROL}, "the watcher cannot see a command line, so it proves nothing"

        # The page does what the review found: every element after the field is named by the value.
        with ArgvWatcher([VALUE]) as watch:
            done, payload = _script(env, seat, "\n".join([
                f"go {site.origin}/",
                "fill-secret #user USER",
                "click Continue",
                "hover Continue",
                "check input[type=checkbox]",
                "uncheck input[type=checkbox]",
                "select select two",
                f"drop div.zone {notes}",
                "download Download 10",
                "text body",
                "save",
                "url",
            ]) + "\n")
        assert done.returncode == 0 and payload["ok"], (payload, done.stderr)
        assert watch.seen == set(), "the value was in a command line"
        by_command = {step["command"]: step for step in payload["steps"]}
        # Each element was named by its path, because its id is the user name; none of the output holds the name.
        for command in ("click Continue", "hover Continue", "check input[type=checkbox]", "uncheck input[type=checkbox]"):
            assert by_command[command]["output"].startswith("body > "), (command, by_command[command])
        assert VALUE not in done.stdout
        assert by_command["save"]["output"] == "saved"
        assert not [path for path in payload["files"] if path.endswith("state.json")]

        # state.json is the site's storage as Chrome keeps it: the site's own copy of the name is in it, in a
        # file only the OS user can open, and no step returned it.
        state = home / "state.json"
        assert _mode(state) == 0o600 and _mode(home) == 0o700 and _mode(home / "scratch") == 0o700
        assert VALUE in state.read_text(), "the control: the site kept its own copy in its storage"

        # A plain `fill` of the name would put it in a command line, and the tool refuses it before the driver runs.
        with ArgvWatcher([VALUE]) as typed:
            refused, payload = _script(env, seat, f"fill input[type=text] {VALUE}\n")
        assert refused.returncode == 1 and payload["steps"][0]["error"] == REFUSED
        assert typed.seen == set(), "the refusal came before the driver was started"
        assert VALUE not in refused.stdout

        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
        found = _files_holding([home], [VALUE])
        allowed = {str(home / "secrets.json"), str(state)}
        outside = [path for path in found if path not in allowed and not path.startswith(str(profile) + os.sep)]
        assert outside == [], f"the value is in a file outside the store, the state file and the profile: {outside}"
    finally:
        site.stop()
        _tool(env, seat, "close")
        _gone(profile)


def test_the_console_log_the_daemon_writes_after_the_call_is_scrubbed_by_the_next_call(tmp_path):
    seat = f"ar3scratch{os.getpid()}console"
    env = _env(tmp_path)
    home = tmp_path / "data/ar3/browser/" / seat
    profile = home / "chrome"
    site = Site()
    try:
        _set(env, seat, "USER", VALUE, site.origin)
        done, payload = _script(env, seat, f"go {site.origin}/?delay=6000\nfill-secret #user USER\n")
        assert done.returncode == 0 and payload["ok"], (payload, done.stderr)
        assert _holding(_cli_files(home), VALUE) == [], "at the end of the script the log did not hold it yet"

        # The page logs the name six seconds after the fill, with the script over: the daemon writes the line now.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not _holding(_cli_files(home), VALUE):
            time.sleep(0.25)
        logs = _holding(_cli_files(home), VALUE)
        assert logs and logs[0].startswith("console-"), "the control: between two scripts the log holds the value"
        assert _mode(home / "scratch") == 0o700, "between two scripts it is in a directory only the OS user can enter"

        # The next script, of any kind, begins by scrubbing it, and nothing it returns or attaches holds the name.
        later, payload = _script(env, seat, "url\nconsole\nsnap\n")
        assert later.returncode == 0 and payload["ok"], (payload, later.stderr)
        assert _holding(_cli_files(home), VALUE) == [], "the next call scrubbed the file"
        assert VALUE not in later.stdout
        assert "[secret:USER]" in (home / "scratch" / ".playwright-cli" / logs[0]).read_text()
        assert _mode(home / "scratch" / ".playwright-cli" / logs[0]) == 0o600
        for path in payload["files"]:
            assert VALUE.encode() not in Path(path).read_bytes()
        console_step = [step for step in payload["steps"] if step["command"] == "console"][0]
        assert "remembered user [secret:USER]" in console_step["output"]

        # The browser command of the CLI sweeps first too.
        again, payload = _script(env, seat, f"go {site.origin}/?delay=2500\nfill-secret #user USER\nwait 4\n")
        assert again.returncode == 0, payload
        assert _tool(env, seat, "console").returncode == 0
        assert _holding(_cli_files(home), VALUE) == []

        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
    finally:
        site.stop()
        _tool(env, seat, "close")
        _gone(profile)


@pytest.mark.parametrize("short", ["2026", "0000", "browser"])
def test_a_short_stored_value_that_is_in_the_paths_and_numbers_of_the_tool_stops_none_of_its_own_calls(tmp_path, short):
    """The artifacts are named by the date (`2026...`), the window and the timeouts are numbers, and the seat's
    directory has words in it. A PIN of four digits or a word is in all of them, and the tool's calls still run."""
    seat = f"ar3scratch{os.getpid()}short"
    env = _env(tmp_path)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    site = Site()
    try:
        _set(env, seat, "SHORT", short, site.origin)
        done, payload = _script(env, seat, "\n".join([
            f"go {site.origin}/", "snap", "shot", "save", "wait-for #user 5", "text body", "assert-text Username",
            "fill #user hello", "click Continue", "url",
        ]) + "\n")
        assert done.returncode == 0 and payload["ok"], (payload, done.stderr)
        assert [path for path in payload["files"] if path.endswith("screen.png")]
        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
    finally:
        site.stop()
        _tool(env, seat, "close")
        _gone(profile)


def test_css_escape_of_the_tool_is_what_chrome_makes(tmp_path):
    """The form of a value in an id selector is the one the browser computes. The tool's table is checked against it."""
    from browser import secretstore

    seat = f"ar3scratch{os.getpid()}css"
    env = _env(tmp_path)
    profile = tmp_path / "data/ar3/browser" / seat / "chrome"
    site = Site()
    samples = [
        "SyntheticUserA7492", "user@example.com", "1234abcd", "-5x", "-", "--x", "a b", "a\tb", "x.y#z", "café",
        "ab\x01cd", "ab\x7fcd", "a\\b", "a'b\"c", "a:b", "a/b", "0", "_x_", "a(b)c", "pass word 1234",
        "it's <b> & \"q\"", "line1\nline2 end", "\U0001F600 pin 4242", "\u65e5\u672c\u8a9e-pass", "no\u00a0break x", "100%41&amp;x",
    ]
    try:
        script = (
            f"go {site.origin}/\nrun-code <<END\n"
            f"return await page.evaluate((items) => items.map((s) => [CSS.escape(s), CSS.escape('pw-' + s), "
            f"encodeURIComponent(s), encodeURI(s), new URLSearchParams({{q: s}}).toString().slice(2), "
            f"new URL('http://example.com/').searchParams.has('x') ? '' : (() => {{ const u = new URL('http://example.com/'); "
            f"u.searchParams.set('x', s); return u.search.slice(3); }})(), "
            f"JSON.stringify(s).slice(1, -1), escape(s), "
            f"(() => {{ const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }})()]), {json.dumps(samples)});\nEND\n"
        )
        done, payload = _script({**env, "AR3_BROWSER_ALLOW_EVAL": "1"}, seat, script)
        assert done.returncode == 0 and payload["ok"], (payload, done.stderr)
        shown = json.loads(payload["steps"][1]["output"])
        assert [row[0] for row in shown] == [secretstore._css_escaped(sample) for sample in samples]
        from test_layers import css_escape

        assert [row[0] for row in shown] == [css_escape(sample) for sample in samples], "the encoder of test_layers is Chrome's"
        assert [row[1] for row in shown] == [css_escape("pw-" + sample) for sample in samples]
        # and the other encoders of that file are the ones the browser has: JSON.stringify, encodeURIComponent, escape, innerHTML
        from test_layers import html_text, js_escape, json_utf8, url_component

        assert [row[6] for row in shown] == [json_utf8(sample) for sample in samples], "JSON.stringify"
        assert [row[2] for row in shown] == [url_component(sample) for sample in samples], "encodeURIComponent"
        assert [row[7] for row in shown] == [js_escape(sample) for sample in samples], "escape"
        assert [row[8] for row in shown] == [html_text(sample) for sample in samples], "the HTML of a text node"
        # Every text the browser made of a value, in a position and a context of its own, is the value to the scrub.
        for sample, row in zip(samples, shown):
            if len(sample) < secretstore.MIN_VALUE_LENGTH:
                continue
            scrubber = secretstore.Scrubber({"USER": {"value": sample, "origins": ["https://example.com"]}})
            for text in row:
                assert scrubber.holds(text), (sample, text)
        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
    finally:
        site.stop()
        _tool(env, seat, "close")
        _gone(profile)


@pytest.mark.parametrize("value", ["my pass!", "p@ss w0rd~1", "Tr0ub4dor&3!", "caf\u00e9 latte+x", "a b/c:d=e;f"])
def test_the_address_a_browser_writes_for_a_value_is_scrubbed_in_every_form(tmp_path, value):
    """A GET form puts what was typed into the address, in the form of a browser: `my+pass%21`, `p%40ss+w0rd%7E1`. The
    control is the same run on a seat that holds no secret, which shows the address as the browser wrote it."""
    from urllib.parse import quote, quote_plus, urlsplit

    site = Site()
    env = _env(tmp_path)
    control_seat, seat = f"ar3scratch{os.getpid()}ctl", f"ar3scratch{os.getpid()}url"
    try:
        control, payload = _script(env, control_seat, f"go {site.origin}/form\nfill #q {shlex.quote(value)}\nclick #go\nurl\n")
        assert control.returncode == 0 and payload["ok"], (payload, control.stderr)
        written = payload["steps"][-1]["output"]
        query = urlsplit(written).query
        assert query.startswith("q="), written

        _set(env, seat, "Q", value, site.origin)
        done, payload = _script(env, seat, f"go {site.origin}/form\nfill-secret #q Q\nclick #go\nurl\nsnap\nback\nforward\nurl\nwait-for-url q= 5\n")
        assert done.returncode == 0 and payload["ok"], (payload, done.stderr)
        forms = {value, quote(value, safe=""), quote_plus(value), query[2:], query[2:].replace("+", "%20")}
        everything = done.stdout + "".join(Path(path).read_text(errors="replace") for path in payload["files"])
        for form in forms:
            assert form not in everything, (form, everything[-600:])
        assert f"{site.origin}/form?q=[secret:Q]" in payload["steps"][3]["output"], payload["steps"][3]
    finally:
        site.stop()
        for name in (control_seat, seat):
            _tool(env, name, "close")
            _gone(tmp_path / "data/ar3/browser" / name / "chrome")


NESTED_VALUE = 'Quartz"Ledger\\K5819'  # the shape of the value of the review; the words are this test's own


def test_a_page_that_prints_the_value_as_json_in_an_element_is_scrubbed_in_every_snapshot_and_text(tmp_path):
    """The probe of the review, live. The element holds `{"user":"Quartz\\"Ledger\\\\K5819"}`, and the snapshot of it
    holds that text in the double quotes of YAML. A caller decoded the attachment back, YAML and then JSON, and had the
    value. The control is the same page on a seat that holds no secret: it shows the encoders of test_layers are the
    ones Playwright and the page use. (The value is not the one of the review, whose words other processes of a busy
    machine hold in their command lines; the unit test has that one.)"""
    import shlex

    from test_layers import json_ascii, yaml_playwright

    seat, control_seat = f"ar3scratch{os.getpid()}nested", f"ar3scratch{os.getpid()}nestedctl"
    env = _env(tmp_path)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    site = Site()
    typed = '{"user":"' + json_ascii(NESTED_VALUE) + '"}'
    try:
        control, payload = _script(env, control_seat, f"go {site.origin}/nested\nfill #user {shlex.quote(NESTED_VALUE)}\nsnap\ntext body\n")
        assert control.returncode == 0 and payload["ok"], (payload, control.stderr)
        shown = Path(payload["files"][0]).read_text()
        assert yaml_playwright(typed) in shown, "the control: Playwright quotes the JSON the way test_layers does"
        assert payload["steps"][-1]["output"] == typed

        _set(env, seat, "USER", NESTED_VALUE, site.origin)
        with ArgvWatcher([NESTED_VALUE, "Ledger", "K5819"]) as watch:
            done, payload = _script(env, seat, "\n".join([
                f"go {site.origin}/nested", "fill-secret #user USER", "snap", "text body", "wait 2.5", "console", "snap", "url",
            ]) + "\n")
        assert done.returncode == 0 and payload["ok"], (payload, done.stderr)
        assert watch.seen == set(), "the value was in a command line"
        by_command = {step["command"]: step for step in payload["steps"]}
        assert by_command["text body"]["output"] == '{"user":"[secret:USER]"}'
        assert payload["files"], payload
        for path in payload["files"]:
            attached = Path(path).read_text(errors="replace")
            assert "Quartz" not in attached and "Ledger" not in attached and "K5819" not in attached, (path, attached)
        snapshots = [Path(path).read_text() for path in payload["files"] if path.endswith("snapshot.txt")]
        line = next(line for line in snapshots[0].splitlines() if "generic [ref=" in line and "user" in line)
        recovered = json.loads(json.loads(line.split(": ", 1)[1]))["user"]  # the decoding of the review
        assert recovered == "[secret:USER]"
        assert "Quartz" not in done.stdout and "K5819" not in done.stdout

        # What the driver wrote on its own, a snapshot of the page for each call and the console, holds no part of it.
        for needle in ("Quartz", "Ledger", "K5819"):
            assert _holding(_cli_files(home), needle) == [], needle
        # The browser command of the CLI prints the page through the same scrub.
        shown = _tool(env, seat, "snap")
        assert shown.returncode == 0 and "Quartz" not in shown.stdout + shown.stderr and "[secret:USER]" in shown.stdout

        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
    finally:
        site.stop()
        for name in (seat, control_seat):
            _tool(env, name, "close")
            _gone(tmp_path / "data/ar3/browser" / name / "chrome")


NOTE_VALUE = "Marsh\nLantern\t5047-Zephyr"


def test_a_line_feed_and_a_tab_in_an_attribute_are_the_arrows_of_the_preview_and_the_scrub_knows_them(tmp_path):
    """Playwright's preview of an element passes through `oneLine`: a line feed is U+21B5 and a tab is U+21C6. The control
    is a seat that holds no secret: the error is whole, and the preview of the button holds the note in that shape, so
    the shape is what the driver prints and not what the tests assume. That text, as the driver wrote it, goes through the
    scrub of the note: nothing of the note is left. On a seat that holds the note the error is its first line and a note
    in its place (the call log is withheld), and nothing of the note is anywhere."""
    from browser import plc, secretstore

    seat, control_seat = f"ar3scratch{os.getpid()}arrow", f"ar3scratch{os.getpid()}arrowctl"
    env = _env(tmp_path)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    site = Site()
    words = ("Marsh", "Lantern", "5047", "Zephyr")
    try:
        control, payload = _script(env, control_seat, f"go {site.origin}/note\nclick #b\n")
        assert control.returncode == 1 and not payload["ok"], (payload, control.stderr)
        whole = payload["steps"][-1]["error"]
        assert 'data-note="Marsh\u21b5Lantern\u21c65047-Zephyr"' in whole, "the control: the driver prints the arrows"
        assert "Marsh\nLantern" not in whole and "\t5047" not in whole
        scrubber = secretstore.Scrubber({"NOTE": {"value": NOTE_VALUE, "origins": [site.origin]}})
        scrubbed = scrubber.scrub(whole)
        assert 'data-note="[secret:NOTE]"' in scrubbed and scrubber.holds(whole)
        assert not any(word in scrubbed for word in words), scrubbed

        _set(env, seat, "NOTE", NOTE_VALUE, site.origin)
        done, payload = _script(env, seat, f"go {site.origin}/note\nclick #b\n")
        assert done.returncode == 1 and not payload["ok"], (payload, done.stderr)
        error = payload["steps"][-1]["error"]
        assert error == "### Error\nTimeoutError: " + plc.DRIVER_MESSAGE_WITHHELD, error
        everything = done.stdout + done.stderr + "".join(Path(path).read_text(errors="replace") for path in payload["files"])
        for word in words + ("\u21b5", "\u21c6"):
            assert word not in everything, word
            assert _holding(_cli_files(home), word) == [], word

        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
    finally:
        site.stop()
        for name in (seat, control_seat):
            _tool(env, name, "close")
            _gone(tmp_path / "data/ar3/browser" / name / "chrome")


OVERLAY_VALUE = 'Quartz"Ledger\\Kestrel5819Zeta4472Omega'


def test_the_error_of_a_click_that_an_overlay_stops_shows_no_part_of_a_value_on_a_seat_that_holds_a_secret(tmp_path):
    """The chain of the review, live. The button holds the value under two layers of JSON (`JSON.stringify` of the
    `JSON.stringify` of an object with it), the shield stops the click, and the error of Playwright says `locator resolved
    to <button id="b" type="button">` and the text of the button cut at 50 characters. The control is the same page on a seat that holds
    no secret: the error is whole, call log and preview and the start of the value under two layers, so the driver does
    print it. On the seat that holds the secret the error is its first line and a note, and the failure snapshot, which the
    scrub reads whole, is attached. (The value is not the one of the review, whose words other processes of a busy machine
    hold in their command lines; the unit test has that one.)"""
    from browser import plc

    seat, control_seat = f"ar3scratch{os.getpid()}shield", f"ar3scratch{os.getpid()}shieldctl"
    env = _env(tmp_path)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    site = Site()
    words = ("Quartz", "Ledger", "Kestrel", "5819Zeta", "4472Omega")
    try:
        control, payload = _script(env, control_seat, f"go {site.origin}/overlay\nfill #user {shlex.quote(OVERLAY_VALUE)}\nclick #b\n")
        assert control.returncode == 1 and not payload["ok"], (payload, control.stderr)
        whole = payload["steps"][-1]["error"]
        assert "Call log:" in whole and 'locator resolved to <button id="b" type="button">' in whole and "intercepts pointer events" in whole
        assert "Kestrel5819" in whole, "the control: the driver previews the start of the value, cut, under two layers of JSON"
        assert plc.DRIVER_MESSAGE_WITHHELD not in whole

        _set(env, seat, "USER", OVERLAY_VALUE, site.origin)
        done, payload = _script(env, seat, f"go {site.origin}/overlay\nfill-secret #user USER\nclick #b\n")
        assert done.returncode == 1 and not payload["ok"], (payload, done.stderr)
        error = payload["steps"][-1]["error"]
        assert error == "### Error\nTimeoutError: " + plc.DRIVER_MESSAGE_WITHHELD, error
        failure = [path for path in payload["files"] if path.endswith("failure.txt")]
        assert failure, ("the failure snapshot is attached", payload["files"])
        everything = done.stdout + done.stderr + "".join(Path(path).read_text(errors="replace") for path in payload["files"])
        for word in words:
            assert word not in everything, word
            assert _holding(_cli_files(home), word) == [], word
        assert "[secret:USER]" in Path(failure[0]).read_text()

        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
    finally:
        site.stop()
        for name in (seat, control_seat):
            _tool(env, name, "close")
            _gone(tmp_path / "data/ar3/browser" / name / "chrome")


def test_text_that_looks_like_a_ref_and_holds_a_pin_is_refused_and_a_real_ref_that_holds_it_is_clicked(tmp_path):
    """The probe of the review, live, for a PIN of 482916: `fill #user e482916` and `type e482916` are the PIN with an
    `e` before it, and the driver is never started for them. A page of 1200 buttons has the ref e1000, and a stored PIN
    of 1000 is in its number: the click on it runs, and the `type` of the same text is refused."""
    seat, control_seat = f"ar3scratch{os.getpid()}ref", f"ar3scratch{os.getpid()}refctl"
    env = _env(tmp_path)
    profile = tmp_path / "data/ar3/browser" / seat / "chrome"
    site = Site()
    try:
        # The control: with no secret on the seat, the text is in a command line, so the watcher can see it.
        with ArgvWatcher(["e482916"]) as control:
            plain, payload = _script(env, control_seat, f"go {site.origin}/\nfill #user e482916\ntype e482916\n")
        assert plain.returncode == 0 and payload["ok"], (payload, plain.stderr)
        assert control.seen == {"e482916"}, "the watcher cannot see a command line, so it proves nothing"

        _set(env, seat, "PIN", "482916", site.origin)
        for line in ("fill #user e482916", "type e482916", "press e482916"):
            with ArgvWatcher(["e482916", "482916"]) as watch:
                refused, payload = _script(env, seat, f"go {site.origin}/\n{line}\n")
            assert refused.returncode == 1 and payload["steps"][-1]["error"] == REFUSED, (line, payload)
            assert watch.seen == set(), f"the refusal of `{line}` came after the driver was started"
            assert "482916" not in refused.stdout

        # A ref that is a target: the PIN is in its number, the driver is started, the element is clicked. After a
        # few pages the driver puts the number of the frame before a ref (`f2e1000`): the snapshot says which.
        _set(env, seat, "PIN", "1000", site.origin)
        done, payload = _script(env, seat, f"go {site.origin}/refs\nsnap\n")
        assert done.returncode == 0 and payload["ok"], (payload, done.stderr)
        snapshot = Path(payload["files"][0]).read_text()
        prefix = re.search(r"\[ref=(f\d+)?e990\]", snapshot).group(1) or ""
        with ArgvWatcher([f"{prefix}e1000"]) as watch:
            done, payload = _script(env, seat, f"click {prefix}e1000\ntext #out\n")
        assert done.returncode == 0 and payload["ok"], (payload, done.stderr)
        assert watch.seen == {f"{prefix}e1000"}, "the driver was started with the ref, whose number holds the PIN"
        assert re.fullmatch(r"b\d+", payload["steps"][-1]["output"]), payload["steps"][-1]
        with ArgvWatcher(["e1000"]) as watch:
            refused, payload = _script(env, seat, "type e1000\n")
        assert refused.returncode == 1 and payload["steps"][-1]["error"] == REFUSED
        assert watch.seen == set()

        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
    finally:
        site.stop()
        for name in (seat, control_seat):
            _tool(env, name, "close")
            _gone(tmp_path / "data/ar3/browser" / name / "chrome")
