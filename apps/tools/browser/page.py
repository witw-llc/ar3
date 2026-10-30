"""The page facade: what a recipe's code verb drives the seat through.

A code verb never calls playwright-cli or a subprocess. It holds a `Page`, whose methods wrap the
command language's own verbs (`go`, `click`, `fill`, `download` and the rest, so a call behaves as the
verb does) and add what a site driver needs: the snapshot as text, tabs, an in-page `fetch` that
carries the page's cookies, plain HTTP, and code of the recipe's own run in the page.

Every call does the same three things.

- It checks the stop epoch as `run_script` does, so `a8s stop` ends a recipe in the middle of a verb.
- It writes a scrubbed line to the transcript, indented under the `recipe` step.
- It keeps the recipe on its origins. `go`, `fetch`, `http` and `new_tab` refuse a URL outside them
  before any request is made, and after a call that can navigate the page's URL is checked, so a
  link or a redirect that leaves the origins fails the verb.

No method returns the value of a stored secret: the recipe names a secret (`fill_secret`) and the tool
types it. What the page returns to the recipe passes the scrubber first, so a stored value shows as
`[secret:NAME]`. A value the recipe reads from a page and must not show, such as a one-time passcode, is
handed to `hide`, which makes it a scrubbed value from then on, and typed with `fill_hidden`. The page files
playwright-cli writes while the recipe reads and types such a value are kept by `no_page_files` only for
the length of the block. `sensitive` is the form for a part of a verb that must stay safe even when the
recipe's own cleanup fails: it also keeps every capture of the page, the failure snapshot included, off
until the recipe confirms that the page is clear, and closes Chrome when it cannot.

The recipe's own code (`evaluate`, `run_code`) is not a caller's: ALLOW_EVAL and the eval guard do not
apply to it. It is reviewed code, and its in-page JavaScript is not checked against the origins.
"""
from __future__ import annotations

import contextlib
import json as _json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePath

from . import commands
from . import plc
from . import resolve
from . import secretstore
from . import session
from .recipe import NeedsPerson, SensitiveFailure, UsageError

# Verbs after which the page may be somewhere else. A page outside the recipe's origins fails the call.
NAVIGATING = frozenset({
    "go", "click", "press", "select", "check", "uncheck", "back", "forward", "reload",
    "download", "upload", "drop",
})
# Verbs whose first argument is the target they act on. A line of a sensitive step shows the verb and this one
# argument, and no text the call was given after it.
TARGETED = frozenset({
    "click", "hover", "check", "uncheck", "fill", "fill-secret", "select", "drop", "download", "go", "wait-for",
    "text", "upload",
})
WAIT_SLICE_SECONDS = 0.25
MAX_HTTP_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
# Data that is not UTF-8 text is read as UTF-16 and UTF-32 too, to see whether it holds a value, when it is this large or smaller
# (a pass over it for each of four readings). ISO-8859-1 and Windows-1252 take a pass each, and are read at any size.
MAX_READ_AS_OTHER_TEXT = 16 * 1024 * 1024


def _utf8_text(data):
    """The text of `data` when it is UTF-8 and has no NUL (a file of UTF-16 or UTF-32 that is ASCII is valid UTF-8, and has
    them between its letters), and None otherwise."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _readings(data):
    """The texts that `data`, which is not UTF-8 text, can be read as: UTF-8 with the bytes that are none kept apart,
    ISO-8859-1 and Windows-1252, and, for data of at most MAX_READ_AS_OTHER_TEXT bytes, UTF-16 and UTF-32."""
    yield data.decode("utf-8", "surrogateescape")
    yield data.decode("latin-1")
    yield data.decode("cp1252", "replace")
    if len(data) <= MAX_READ_AS_OTHER_TEXT:
        for codec in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            yield data.decode(codec, "replace")


# A sensitive step is named for what it guards, in the words of a sentence: "the passcode step failed".
SECTION_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


class Response:
    """What `fetch` and `http` return: `status`, `url` (where it ended up), `headers` (names in lower
    case), `text`, `json` (the parsed body, or None when the body is not JSON) and `ok` (2xx)."""

    def __init__(self, status, url, headers, text):
        self.status, self.url, self.headers, self.text = status, url, headers, text
        self._parsed = False
        self._json = None

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def json(self):
        if not self._parsed:
            self._parsed = True
            try:
                self._json = _json.loads(self.text)
            except ValueError:
                self._json = None
        return self._json


# The builders of the code files live in plc; the facade's `evaluate` and the tool's own page calls share them.
_literal = plc.literal
evaluate_code = plc.evaluate_code


def run_code_text(body, values):
    """The file `run_code` runs: `body` in an async function of `page` and `values`."""
    return (
        "async (page) => {\n"
        f"  const values = JSON.parse({_literal(list(values))});\n"
        "  const body = async (page, values) => {\n"
        f"{body}\n"
        "  };\n"
        "  return {value: await body(page, values)};\n"
        "}\n"
    )


def fetch_code(url, init, limit_ms):
    """The file `fetch` runs: one `fetch` in the page, with its cookies, and what came back. It
    follows no redirect (`redirect: 'manual'`, which no `init` can override): a followed redirect
    is a request to an address the recipe's origins were never asked about. A redirect comes back
    as `{redirect: true}`."""
    return (
        "async (page) => {\n"
        f"  const request = JSON.parse({_literal([url, init, limit_ms])});\n"
        "  return await page.evaluate(async ([url, init, limit]) => {\n"
        "    const controller = new AbortController();\n"
        "    const timer = setTimeout(() => controller.abort(), limit);\n"
        "    try {\n"
        "      const response = await fetch(\n"
        "        url, Object.assign({}, init, {signal: controller.signal, redirect: 'manual'}));\n"
        "      if (response.type === 'opaqueredirect') return {redirect: true};\n"
        "      const text = await response.text();\n"
        "      const headers = {};\n"
        "      response.headers.forEach((value, name) => { headers[name] = value; });\n"
        "      return {status: response.status, url: response.url, headers, text};\n"
        "    } catch (error) {\n"
        "      return {failed: String((error && error.message) || error)};\n"
        "    } finally {\n"
        "      clearTimeout(timer);\n"
        "    }\n"
        "  }, request);\n"
        "}\n"
    )


def _error_text(exc):
    text = str(exc)
    return text[len("### Error"):].strip() if text.startswith("### Error") else text


def _headline(exc):
    """The first line of a driver error: what failed. The lines after it are a call log, which prints
    the text of a failed `fill`."""
    for line in _error_text(exc).splitlines():
        if line.strip():
            return line.strip()
    return "the field could not be filled"


def _payload(json, body, headers):
    """(body, headers) for a request: `json` is encoded and sets the content type; `body` is text."""
    if json is not None and body is not None:
        raise ValueError("give json or body, not both")
    sent = {str(name): str(value) for name, value in (headers or {}).items()}
    data = None
    if json is not None:
        data = _json.dumps(json)
        if not any(name.lower() == "content-type" for name in sent):
            sent["Content-Type"] = "application/json"
    elif body is not None:
        data = body
    return data, sent


def _artifact_name(name):
    if not isinstance(name, str) or not name or name != os.path.basename(name) or "\\" in name \
            or name.startswith(".") or "\0" in name:
        raise plc.BrowserError("artifact: the name is a plain file name, such as report.txt")
    return name


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to a URL the recipe may reach."""

    def __init__(self, check):
        self._check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Page:
    """The seat's current page, as a recipe's code sees it. Built by the runner for each verb."""

    def __init__(self, call, recipe):
        self._call = call
        self._recipe = recipe

    # -- the three things every call does

    def _tick(self):
        if session.stop_epoch(self._call.seat) != self._call.epoch:
            raise plc.BrowserError(session.STOP_MESSAGE)

    def _clean(self, text):
        return self._call.run.clean(text)

    def _say(self, text):
        return self._call.say(text)

    def _amend(self, index, text):
        """Finish the line a call began with what the call returned. Inside a sensitive step a line shows the
        verb and its target and nothing that the page or the server answered, so it is left as it began."""
        if not self._call.run.sensitive:
            self._call.amend(index, text)

    def _guard(self, url, what):
        if not secretstore.url_on_origins(url, self._recipe.origin_parts):
            raise plc.BrowserError(
                f"{what}: refusing {self._clean(str(url))!r}: it is outside the origins of recipe "
                f"{self._recipe.name} ({', '.join(self._recipe.origins)})"
            )

    def _landed(self, what):
        """Fail unless the page is on the recipe's origins (a blank page is on none and holds nothing). A page
        that is a local file fails the call whatever the origins say: it is sent away and what the step wrote
        of it is removed before the failure reaches the recipe, which may catch it and go on."""
        url = session.current_url(self._call.seat)
        if commands.is_local_file(url):
            raise commands.leave_local_file(self._call.seat, self._call.run, what)
        if url in ("", "about:blank") or secretstore.url_on_origins(url, self._recipe.origin_parts):
            return url
        raise plc.BrowserError(
            f"{what}: the page is at {self._clean(url)!r}, outside the origins of recipe "
            f"{self._recipe.name} ({', '.join(self._recipe.origins)})"
        )

    def _drive(self, action):
        """Run `action`, a call to the driver, under the stop check and the scrub of the files the
        driver writes. A failure's text is scrubbed before it leaves."""
        self._tick()
        try:
            with commands.scrubbed_outputs(self._call.seat, self._call.run.scrubber):
                return action()
        except plc.BrowserError as exc:
            raise plc.BrowserError(self._clean(_error_text(exc))) from None

    def _value(self, value):
        """`value` from the page, with each stored secret replaced."""
        if isinstance(value, str):
            return self._clean(value)
        if isinstance(value, list):
            return [self._value(item) for item in value]
        if isinstance(value, dict):
            return {self._clean(str(key)): self._value(item) for key, item in value.items()}
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            shown = self._clean(str(value))
            return value if shown == str(value) else shown
        return value

    def command(self, verb, *args):
        """Run one verb of the command language by name, as a script line would: `page.command("scroll",
        400)`. Returns its output. A step verb's lines run through here."""
        self._tick()
        words = [str(word) for word in args]
        if verb == "recipe":
            if len(words) < 2:
                raise UsageError("recipe <site> <verb> [arguments]")
            result = self.recipe(*words)
            return str(result.text) if result.text is not None else ""
        if verb == "go" and words:
            self._guard(words[0], "go")
        shown = " ".join([verb] + words)
        if self._call.run.sensitive:
            shown = " ".join([verb] + (words[:1] if verb in TARGETED else []))
        index = self._say(shown)
        output = self._drive(
            lambda: commands._step(
                self._call.seat, verb, words, self._call.run, self._call.allow_eval,
                script_epoch=self._call.epoch,
            )
        )
        text = self._clean(str(output or ""))
        self._amend(index, f"{shown} -> {text}")
        if verb in NAVIGATING:
            self._landed(verb)
        return text

    # -- the verbs

    def go(self, url):
        """Open `url`. It must be on the recipe's origins. Returns the URL the page ended on."""
        return self.command("go", url)

    def click(self, target):
        """Click a snapshot ref, a CSS selector or visible text. Returns the selector it clicked."""
        return self.command("click", target)

    def fill(self, target, text):
        """Put `text` in a field. The text is in the driver's command line: use `fill_secret` for a secret."""
        return self.command("fill", target, text)

    def fill_secret(self, target, name):
        """Type the stored secret `name` into a field, on a page whose origin the secret allows. The
        recipe names the secret and never holds its value."""
        if name not in self._recipe.secrets:
            raise plc.BrowserError(
                f"fill_secret: {name} is not among the secrets recipe {self._recipe.name} declares"
            )
        return self.command("fill-secret", target, name)

    def hide(self, name, value):
        """Treat `value`, which the recipe read from a page and holds only in memory (a one-time passcode),
        like a stored secret from now on, for the rest of the script: every later transcript line, error,
        return value, reply and snapshot shows `[secret:NAME]` where it would show the value, and the files
        playwright-cli has already written are scrubbed at once. A later step of the script that holds the
        same text, such as a number in a file it attaches, is scrubbed too. `name` is a secret name
        (capitals, digits and _). The value is at least 4 characters: a shorter one cannot be scrubbed.
        It is not a stored secret, so `fill_secret` can not type it; `fill_hidden` does. The scrub finds
        the value whole. A value typed into several boxes is in the page files one piece to a line, where
        only `no_page_files` keeps it off the disk."""
        try:
            self._call.run.hide(name, value)
        except secretstore.SecretStoreError as exc:
            raise plc.BrowserError(f"hide: {exc}") from None
        # The stop is read after the value is hidden: a stop that has landed must not leave it showing.
        self._tick()
        self._say(f"hide {name}")

    def fill_hidden(self, target, text):
        """Type `text` into a field without showing it. It is for a value the recipe holds only in memory, such
        as a one-time passcode it read from another page; `fill_secret` is for a stored secret. The transcript
        names the field and not the text, the text is in no command line, and a failure reports the first line
        of the driver's error and not its call log, which would print the text. The field must be in a page
        on the recipe's origins. Pass the value to `hide` too, so the page never shows it back. Returns the
        field and `[hidden]`."""
        self._tick()
        if not isinstance(text, str):
            raise plc.BrowserError("fill_hidden: the text is a str")
        index = self._say(f"fill_hidden {target}")
        seat = self._call.seat
        try:
            with commands.scrubbed_outputs(seat, self._call.run.scrubber):
                found = resolve.fill_target(seat, str(target))
                selector = f"aria-ref={found}" if resolve.REF_RE.match(found) else found
                plc.fill_secret(seat, selector, text, self._recipe.origin_parts, session.scratch_dir(seat))
        except plc.OriginRefused as exc:
            raise plc.BrowserError(self._clean(
                f"fill_hidden: the field is in a page at {exc.origin!r}, outside the origins of recipe "
                f"{self._recipe.name} ({', '.join(self._recipe.origins)})"
            )) from None
        except plc.BrowserError as exc:
            raise plc.BrowserError(self._clean(f"fill_hidden: {_headline(exc)}")) from None
        except OSError as exc:
            raise plc.BrowserError(f"fill_hidden: cannot write its private code file: {exc.strerror}") from None
        shown = f"{target} <- [hidden]"
        self._call.amend(index, f"fill_hidden {target} -> {shown}")
        return shown

    @contextlib.contextmanager
    def no_page_files(self):
        """Keep none of the page files playwright-cli writes while the block runs. It writes a snapshot of
        the page after each click, key press, check, hover and navigation, and a snapshot holds the value of
        every field. A passcode typed into six boxes is in them one digit to a line, where no scrub can find
        it, and the mail it was read from is in them too. The files that were there before the block are left
        alone (the scrub still reaches them). The new ones are deleted when the block ends, with an error or
        without. Nothing in the tool reads them. Use it around the reading and the typing of a value that
        must not stay on the disk."""
        self._tick()
        before = commands._cli_output_state(self._call.seat)
        try:
            yield
        finally:
            self._drop_page_files_since(before)

    def _drop_page_files_since(self, before):
        """Delete the playwright-cli files that were not there in `before`, a state of `commands._cli_output_state`."""
        for target in commands._cli_output_state(self._call.seat):
            if target not in before:
                try:
                    os.remove(target)
                except OSError:
                    pass

    @contextlib.contextmanager
    def sensitive(self, name, cleanup):
        """A step that holds a value which must not leave the browser, such as a one-time passcode read from a
        mail and typed into boxes: `with page.sensitive("passcode", cleanup): ...`. Open it before the first page
        that shows the value, and close it after the last. While it is open:

        - Nothing of the page is captured. A failure attaches no snapshot, and `snap`, `shot`, `console` and
          `video-start` are refused, here and, after a cleanup that did not confirm, in the rest of the script.
          A video that is recording when the step would open is a failure: it would keep the pages of the
          step, so the step does not open.
        - The playwright-cli files written meanwhile, the cleanup's included, are deleted when the step ends.

        When the block ends, with an error or without, the tool calls `cleanup()`. It is the recipe's own
        function. It makes the page hold nothing of the value (it closes the mail's tab, empties or leaves the
        boxes) and returns True only when it has looked and found that so. Anything else, `None` included, and
        any error, means the page is not confirmed clear, and the tool closes the seat's Chrome before the
        failure is reported.

        The error that leaves a failed step is `<recipe> <verb>: the <name> step failed`, with a note when
        Chrome was closed. It holds none of the text of the page or the driver, which may be the value. The
        exceptions are `NeedsPerson` and `UsageError`: the recipe wrote their text, and they pass through when
        the cleanup confirms the page clear. An exception that is not an `Exception`, such as Ctrl-C, passes
        through after the cleanup."""
        self._tick()
        if not isinstance(name, str) or not SECTION_NAME.fullmatch(name):
            raise ValueError("sensitive: the name is lower case letters, digits and -, such as passcode")
        if not callable(cleanup):
            raise TypeError("sensitive: the cleanup is a function that returns True when the page holds nothing of the step")
        run, seat = self._call.run, self._call.seat
        if session.video_state(seat):
            raise plc.BrowserError(
                f"sensitive: a video is recording, and it would keep the pages of the {name} step. "
                "Stop it with video-stop first"
            )
        before = commands._cli_output_state(seat)
        run.sensitive.append(name)
        kept = interrupted = None
        failed = False
        try:
            yield
        except (NeedsPerson, UsageError) as exc:
            kept = exc
        except Exception:
            failed = True
        except GeneratorExit:
            raise  # the block never ended: nobody can be told, and a finalizer drives no page
        except BaseException as exc:
            interrupted = exc
        confirmed, closed = self._clear(name, cleanup, before)
        if interrupted is not None:
            raise interrupted
        if failed or not confirmed:
            raise SensitiveFailure(name, seat, confirmed, closed) from None
        if kept is not None:
            raise kept

    def _clear(self, name, cleanup, before):
        """Run the cleanup of a sensitive step and close Chrome when it does not confirm. Returns
        `(confirmed, closed)`. Capture stays off while the step is open, and for the rest of the run after a
        cleanup that did not confirm: a Chrome that was closed has nothing left to capture, and nothing is tried."""
        run, seat = self._call.run, self._call.seat
        run.uncleared.append(name)
        confirmed, closed = False, None
        try:
            try:
                confirmed = cleanup() is True
            except Exception:
                confirmed = False
            if not confirmed:
                closed = commands.close_chrome(seat)
        finally:
            self._drop_page_files_since(before)
            run.sensitive.remove(name)
        if confirmed:
            run.uncleared.remove(name)
        return confirmed, closed

    def press(self, key):
        return self.command("press", key)

    def select(self, target, value):
        return self.command("select", target, value)

    def check(self, target):
        return self.command("check", target)

    def uncheck(self, target):
        return self.command("uncheck", target)

    def hover(self, target):
        return self.command("hover", target)

    def wait(self, seconds):
        """Wait. A stop ends the wait within a moment. A number over 300 is read as milliseconds."""
        seconds = commands._seconds(seconds)
        self._say(f"wait {seconds:g}s")
        end = time.monotonic() + seconds
        while True:
            self._tick()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(WAIT_SLICE_SECONDS, left))

    def wait_for(self, selector, seconds=10):
        """Wait for a visible match of the CSS selector."""
        return self.command("wait-for", selector, seconds)

    def wait_for_url(self, substring, seconds=10):
        """Wait until the page's URL holds `substring`. Returns the URL."""
        index = self._say(f"wait-for-url {substring}")
        deadline = time.monotonic() + commands._seconds(seconds)
        while True:
            self._tick()
            url = session.current_url(self._call.seat)
            if substring in url:
                self._amend(index, f"wait-for-url {substring} -> {self._clean(url)}")
                self._landed("wait-for-url")
                return self._clean(url)
            if time.monotonic() >= deadline:
                raise plc.BrowserError(f"wait-for-url: still at {self._clean(url)}")
            time.sleep(0.5)

    def text(self, selector):
        """The inner text of the first match of the CSS selector."""
        return self.command("text", selector)

    def find(self, text):
        """The snapshot nodes that hold `text`, with their context."""
        return self.command("find", text)

    def url(self):
        self._tick()
        return self._clean(session.current_url(self._call.seat))

    def snapshot_text(self):
        """The accessibility snapshot as text, scrubbed."""
        self._say("snapshot")
        return self._clean(self._drive(lambda: session.snapshot(self._call.seat)))

    def screenshot(self, name="screen", full_page=False):
        """Save a screenshot, attach it to the reply, and return its path. `name` gets .png unless
        it ends in .png, .jpg or .jpeg. `full_page` takes the whole scrollable page, not the part in view."""
        if not self._call.run.capturing:
            raise plc.BrowserError("screenshot: nothing of the page is captured while a sensitive step is open")
        name = _artifact_name(name)
        if not name.lower().endswith((".png", ".jpg", ".jpeg")):
            name += ".png"
        plc.refuse_protected(self._call.seat, name)  # the path is the tool's own, but the name in it is the recipe's
        path = commands.artifact(self._call.seat, name)
        self._say(f"screenshot {name}" + (" (full page)" if full_page else ""))
        self._drive(lambda: plc.screenshot(self._call.seat, path, full_page=full_page))
        self._call.run.attach(path)
        return path

    def download(self, target, seconds=30):
        """Click `target`, wait for the file it downloads, attach it, and return its path."""
        return self.command("download", target, seconds)

    def upload(self, target, path):
        """Click `target`, which opens the page's file chooser, and hand the chooser the file at `path`
        (an absolute path on this machine, outside the browser state directory)."""
        self.click(target)
        return self.command("upload", path)

    def drop(self, target, *paths):
        """Drop files onto an element, as a person dragging them in would."""
        return self.command("drop", target, *paths)

    def back(self):
        return self.command("back")

    def forward(self):
        return self.command("forward")

    def reload(self):
        return self.command("reload")

    # -- the recipe's own code in the page

    def evaluate(self, js, *values):
        """Call a JavaScript function in the page. `js` is its source, such as `(a, b) => a + b`; the
        values are its arguments, passed as JSON and never pasted into the code. Returns what it
        returns, which must be JSON data."""
        self._say(f"evaluate {js}")
        return self._code(evaluate_code(js, values))

    def run_code(self, body, *values):
        """Run Playwright statements with `page` and `values` in scope. `return` hands back JSON data."""
        self._say(f"run_code {body}")
        return self._code(run_code_text(body, values))

    def _code(self, code):
        output = self._drive(
            lambda: plc.run_code_file(self._call.seat, code, session.scratch_dir(self._call.seat))
        )
        outcome = plc.result_json(output)
        if not isinstance(outcome, dict):
            return None
        return self._value(outcome.get("value"))

    def fetch(self, url, method="GET", json=None, body=None, headers=None, timeout=60):
        """One `fetch` in the page, with its cookies. `url` may be relative to the page. `json` is sent
        as the JSON body; `body` is text. A redirect is not followed: it fails the call, because the
        browser would request the new address before the recipe's origins could be checked."""
        self._tick()
        if not isinstance(url, str):
            raise plc.BrowserError("fetch: the URL is text")
        try:
            relative = not urllib.parse.urlsplit(url).scheme
        except ValueError:
            relative = False
        if relative:
            url = urllib.parse.urljoin(session.current_url(self._call.seat), url)
        self._guard(url, "fetch")
        data, sent = _payload(json, body, headers)
        init = {"method": method.upper(), "headers": sent, "credentials": "include"}
        if data is not None:
            init["body"] = data
        index = self._say(f"fetch {init['method']} {url}")
        output = self._drive(
            lambda: plc.run_code_file(
                self._call.seat, fetch_code(url, init, int(timeout * 1000)),
                session.scratch_dir(self._call.seat), timeout=timeout + 15,
            )
        )
        outcome = plc.result_json(output)
        if not isinstance(outcome, dict) or not any(key in outcome for key in ("status", "failed", "redirect")):
            raise plc.BrowserError("fetch: the page returned no response")
        if "failed" in outcome:
            raise plc.BrowserError(self._clean(f"fetch: {outcome['failed']}"))
        if outcome.get("redirect"):
            raise plc.BrowserError(self._clean(
                f"fetch: {url} answered with a redirect. A recipe's fetch follows none, because the browser "
                "would request the new address before the recipe's origins could be checked. "
                "Fetch the address it points to, or open the page with go"
            ))
        self._guard(outcome["url"], "fetch (the response)")
        reply = Response(
            outcome["status"], self._clean(outcome["url"]),
            {name.lower(): self._clean(value) for name, value in outcome["headers"].items()},
            self._clean(outcome["text"]),
        )
        self._amend(index, f"fetch {init['method']} {url} -> {reply.status}")
        return reply

    def http(self, url, method="GET", json=None, body=None, headers=None, timeout=30):
        """Plain HTTP from this machine, with no cookies of the page, for public endpoints. The URL, and
        every redirect, must be on the recipe's origins."""
        self._tick()
        self._guard(url, "http")
        data, sent = _payload(json, body, headers)
        method = method.upper()
        index = self._say(f"http {method} {url}")
        request = urllib.request.Request(
            url, data=data.encode("utf-8") if isinstance(data, str) else data, method=method, headers=sent,
        )
        opener = urllib.request.build_opener(_Redirects(lambda target: self._guard(target, "http (a redirect)")))
        try:
            try:
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.status if hasattr(response, "status") else response.code
                raw = response.read(MAX_HTTP_BYTES + 1)
                charset = response.headers.get_content_charset() or "utf-8"
                headers_in = {name.lower(): value for name, value in response.headers.items()}
                final = response.geturl() if hasattr(response, "geturl") else url
        except plc.BrowserError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise plc.BrowserError(self._clean(f"http: {getattr(exc, 'reason', exc)}")) from None
        if len(raw) > MAX_HTTP_BYTES:
            raise plc.BrowserError(f"http: the response is over {MAX_HTTP_BYTES} bytes")
        reply = Response(
            status, self._clean(final), {name: self._clean(value) for name, value in headers_in.items()},
            self._clean(raw.decode(charset, errors="replace")),
        )
        self._amend(index, f"http {method} {url} -> {reply.status}")
        return reply

    # -- tabs, storage, window

    def tabs(self):
        """The addresses of the open tabs, in order. The first tab is `tabs()[0]`. The titles are not returned. A
        list the tool cannot read is an error, not an empty list: a recipe that looks for a tab to close must not
        take "I could not tell" for "there is none"."""
        self._tick()
        index = self._say("tabs")
        listing = self._drive(lambda: session.tabs(self._call.seat))
        urls = [tab["url"] for tab in listing]
        self._amend(index, f"tabs -> {len(urls)}")
        return [self._clean(url) for url in urls]

    def new_tab(self, url):
        """Open `url` in a new tab and select it. It must be on the recipe's origins."""
        self._tick()
        self._guard(url, "new_tab")
        self._say(f"new_tab {url}")
        self._drive(lambda: plc.run(self._call.seat, "tab-new", url, timeout=60))
        return self._landed("new_tab")

    def select_tab(self, index):
        """Select tab `index` (0 is the first). Returns its URL."""
        self._say(f"select_tab {index}")
        self._drive(lambda: plc.run(self._call.seat, "tab-select", plc.Own(str(int(index)))))
        return self._landed("select_tab")

    def close_tab(self, index=None):
        """Close tab `index`, or the current tab. Returns the URL of the tab that is current then."""
        self._say("close_tab" if index is None else f"close_tab {index}")
        extra = [] if index is None else [plc.Own(str(int(index)))]
        self._drive(lambda: plc.run(self._call.seat, "tab-close", *extra))
        return self._landed("close_tab")

    def local_storage_set(self, key, value):
        """Set a localStorage item of the page."""
        self._say(f"local_storage_set {key}")
        self._code(evaluate_code("(key, value) => localStorage.setItem(key, value)", (str(key), str(value))))

    def resize(self, width, height):
        self._say(f"resize {width}x{height}")
        self._drive(lambda: plc.run(self._call.seat, "resize", plc.Own(str(int(width))), plc.Own(str(int(height)))))

    # -- the reply, the transcript, the node's settings, other recipes

    def artifact(self, name, data_or_path):
        """Save `data_or_path` as a file of the reply and return its path. A str is text and bytes are
        bytes, both scrubbed when they are UTF-8 text; bytes that are not (a NUL byte is none), and hold a value in
        any encoding the tool reads them as, are refused. A `pathlib.Path` names a file to copy."""
        self._tick()
        name = _artifact_name(name)
        if plc.holds_protected(self._call.seat, name):
            raise plc.BrowserError("artifact: the name holds a value that must not be shown, and a file name is shown")
        if isinstance(data_or_path, PurePath):
            source = os.fspath(data_or_path)
            if session.inside_state_root(source):
                raise plc.BrowserError("artifact: refusing a file inside the browser state directory")
            with open(source, "rb") as handle:
                data = handle.read(MAX_ARTIFACT_BYTES + 1)
        elif isinstance(data_or_path, str):
            data = data_or_path.encode("utf-8")
        elif isinstance(data_or_path, (bytes, bytearray, memoryview)):
            data = bytes(data_or_path)
        else:
            raise plc.BrowserError("artifact: give text, bytes or a pathlib.Path")
        if len(data) > MAX_ARTIFACT_BYTES:
            raise plc.BrowserError(f"artifact: over {MAX_ARTIFACT_BYTES} bytes")
        text = data_or_path if isinstance(data_or_path, str) else _utf8_text(data)
        if text is not None:
            data = self._clean(text).encode("utf-8")
        elif any(self._call.run.holds(reading) for reading in _readings(data)):
            # Not UTF-8 text: a file in another encoding, or binary. It is written as it is, unless a value is in it (in the
            # ASCII text of a Latin-1 file, say): bytes are not text that a scrub can change and keep whole.
            raise plc.BrowserError("artifact: the data is not UTF-8 text and holds a value that must not be shown")
        path = commands.artifact(self._call.seat, name)
        aside = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with os.fdopen(os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(data)
        os.replace(aside, path)
        self._call.run.attach(path)
        self._say(f"artifact {name}")
        return path

    def log(self, text):
        """A line in the transcript, under this step."""
        self._tick()
        self._say(f"log: {text}")

    def env(self, name):
        """The value of a node setting the recipe declares in `env`."""
        self._tick()
        if name not in self._recipe.env:
            raise UsageError(f"{name} is not an env name recipe {self._recipe.name} declares")
        value = os.environ.get(name)
        if not value:
            raise UsageError(
                f"{name} is not set. Set it on the node with `a8s vars <node> set env.{name} <value>`"
            )
        self._say(f"env {name}")
        return value

    def recipe(self, site, verb, *args):
        """Run another recipe's verb and return its Result. One level deep: that verb may not do the same."""
        self._tick()
        words = [str(word) for word in args]
        self._say(" ".join(["recipe", str(site), str(verb)] + words))
        return self._call.nested(str(site), str(verb), words)
