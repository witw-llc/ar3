"""The command vocabulary a tell is allowed to use.

A script is one command per line, run in order, stopping at the first failure —
and a failure attaches a snapshot of the page it died on, because the next
message is going to be a correction and the sender needs to see what the
browser sees.

The vocabulary is fixed on purpose. A seat holds live logins, so a message
naming a verb is a different thing from a message carrying code: `eval` and
`run-code` are the doors to arbitrary execution and they stay shut behind one
opt-in unless the seat turns it on.
"""
import contextlib
import os
import re
import shlex
import tempfile
import time

from . import plc
from . import resolve
from . import secretstore
from . import session

MAX_WAIT = 300.0

# Verbs that must not auto-open the browser: `close` is the whole point,
# `video-stop` needs to report a stale recording rather than launch Chrome to
# discover one, and `recipe` opens it only for the verb it runs: listing the
# recipes or reading their help needs no browser.
NO_ENSURE = {"close", "video-stop", "recipe"}

# The verbs that capture the page into a file of the reply. None runs while a sensitive step is open.
CAPTURE_VERBS = ("snap", "shot", "console", "video-start")

# The tool opens no file of this machine. A seat holds live logins, and the page it shows may be the store of
# another seat, which the scrub of this seat does not know and a screenshot is never scrubbed of.
LOCAL_FILE = "local files are not opened"

# Verbs whose argument is the rest of the line verbatim, not shlex words:
# `eval console.log('x')` must keep its quotes, and `type don't` must not die
# on an unbalanced apostrophe.
RAW_ARGS = {
    "eval", "run-code", "type", "find", "assert-text", "video-chapter", "dialog-accept",
}

# `<<MARKER` at the end of a command line opens a block; the marker is the last
# word on the line.
HEREDOC = re.compile(r"^(?P<command>.*?)\s*<<\s*(?P<marker>\S+)$")


class ScriptError(Exception):
    """A script that cannot be parsed: the line it died on, and why."""

    def __init__(self, line, message):
        super().__init__(message)
        self.line = line


class Run:
    """The transcript of one script: what each step did, and what it produced.

    A run holds the seat's scrubber. A step's command, its output and its error
    pass through it before they are kept, so no secret value reaches the
    transcript, the JSON or the reply. It also holds the `recipe.Library` that
    the `recipe` verb reads; None means the bundled recipes.
    """

    def __init__(self, seat, scrubber=None, recipes=None):
        self.seat = seat
        self.scrubber = scrubber
        self.recipes = recipes
        self.steps = []
        self.files = []
        self.error = None
        # The sensitive steps open now, innermost last, and those whose cleanup did not confirm the page clear.
        self.sensitive = []
        self.uncleared = []
        # What the running step started from: the time, and how many files the run had attached. A step that
        # ends on a local file is undone back to it. The time is all it takes, so a run reads no directory
        # until it must.
        self.step_mark = None
        self.page_used = False

    @property
    def capturing(self):
        """False while a sensitive step is open, and for the rest of the run after one whose cleanup did not
        confirm that the page is clear. Nothing of the page is captured then: no failure snapshot, no
        screenshot, no console log, no video."""
        return not self.sensitive and not self.uncleared

    def begin_step(self):
        self.step_mark = (time.time_ns(), len(self.files))
        self.page_used = False

    def clean(self, text):
        return self.scrubber.scrub(text) if self.scrubber else text

    def holds(self, text):
        """True when `text` holds a value that the scrub of this run knows."""
        return bool(self.scrubber) and self.scrubber.holds(text)

    def secret(self, name):
        """The stored {"value", "origins"} of `name`, or None."""
        return self.scrubber.secrets.get(name) if self.scrubber else None

    def hide(self, name, value):
        """Scrub `value`, which a recipe read at run time, from now on as `[secret:NAME]` (see
        `Scrubber.hide`). The files playwright-cli has already written may hold it, so they are
        scrubbed now, and the ones a page call or a later step writes are scrubbed around it. A
        console log that the page appends to between two calls is not reached."""
        if self.scrubber is None:
            self.scrubber = secretstore.Scrubber({})
            plc.protect(self.seat, self.scrubber)
        self.scrubber.hide(name, value)
        for target in _cli_output_state(self.seat):
            self.scrubber.scrub_file(target, remove_on_failure=True)

    def note(self, line, output=""):
        self.steps.append({"command": self.clean(line), "ok": True, "output": self.clean(output)})

    def fail(self, line, message, output=""):
        """`output` is what the step did before it failed: a recipe's page calls."""
        message = self.clean(message)
        step = {"command": self.clean(line), "ok": False, "error": message}
        if output:
            step["output"] = self.clean(output)
        self.steps.append(step)
        self.error = message

    def attach(self, path):
        if path not in self.files:
            self.files.append(path)

    @property
    def ok(self):
        return self.error is None

    def transcript(self):
        lines = []
        for step in self.steps:
            mark = "  " if step["ok"] else "! "
            lines.append(f"{mark}{step['command']}")
            detail = "\n".join(part for part in (step.get("output"), step.get("error")) if part)
            for line in detail.splitlines():
                lines.append(f"      {line}" if line else "")
        return "\n".join(lines)


def artifact(seat, suffix):
    """A path in the seat's artifacts that no earlier artifact holds.

    The stamp has one-second resolution, so two artifacts of one name in the
    same second get a counter rather than the same path.
    """
    session.private_dir(session.seat_home(seat))
    directory = session.artifacts_dir(seat)
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    path = os.path.join(directory, f"{stamp}-{suffix}")
    count = 1
    while os.path.lexists(path):
        count += 1
        path = os.path.join(directory, f"{stamp}-{count}-{suffix}")
    return path


def _claim_artifact(seat, source, name):
    """Move `source` into artifacts without replacing anything already there.

    os.link refuses an existing target, so a path another run took between
    choosing and moving is skipped rather than overwritten.
    """
    while True:
        path = artifact(seat, name)
        try:
            os.link(source, path)
        except FileExistsError:
            continue
        os.remove(source)
        return path


def _write_snapshot(seat, run, label="snapshot", target=None, depth=None, boxes=False):
    path = artifact(seat, f"{label}.txt")
    text = (
        session.snapshot(seat)
        if target is None and depth is None and not boxes
        else session.snapshot(seat, target=target, depth=depth, boxes=boxes)
    )
    with open(path, "w") as handle:
        handle.write(run.clean(text))
    run.attach(path)
    return path


def _snap(seat, args, run):
    if not args:
        return _write_snapshot(seat, run)
    targets = []
    depth = None
    boxes = False
    index = 0
    usage = "usage: snap [--depth <N>] [--boxes] [<ref or selector>]"
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--boxes":
            boxes = True
        elif arg == "--depth":
            if index >= len(args):
                plc.refuse_protected(seat, arg)
                raise plc.BrowserError(usage)
            raw_depth = args[index]
            index += 1
            plc.refuse_protected(seat, raw_depth)
            if not re.fullmatch(r"[0-9]{1,3}", raw_depth):
                raise plc.BrowserError(f"{usage} — N is 0 to 999")
            depth = raw_depth
        elif arg.startswith("--depth="):
            raw_depth = arg.split("=", 1)[1]
            plc.refuse_protected(seat, raw_depth)
            if not re.fullmatch(r"[0-9]{1,3}", raw_depth):
                raise plc.BrowserError(f"{usage} — N is 0 to 999")
            depth = raw_depth
        elif arg.startswith("-"):
            plc.refuse_protected(seat, arg)
            raise plc.BrowserError(usage)
        else:
            targets.append(arg)
    if len(targets) > 1:
        plc.check_command(seat, ["snapshot", *targets])
        raise plc.BrowserError(usage)
    resolved = resolve.element_target(seat, targets[0], "snap") if targets else None
    return _write_snapshot(seat, run, target=resolved, depth=depth, boxes=boxes)


def _seconds(value):
    """Anything over 300 is milliseconds, not a long nap."""
    number = float(value)
    if number > MAX_WAIT:
        number = number / 1000.0
    return min(number, MAX_WAIT)


def _need(args, count, usage):
    if len(args) < count:
        raise plc.BrowserError(f"usage: {usage}")


def _tab_index(args, verb):
    usage = "shot [N]" if verb == "shot" else "tab N"
    if len(args) != 1 or not re.fullmatch(r"[0-9]+", args[0]) or len(args[0]) > 9:
        raise plc.BrowserError(f"usage: {usage} — N is a zero-based tab index")
    return int(args[0])


def _select_tab(seat, index, run, verb):
    listing = session.tabs(seat)
    if index >= len(listing):
        raise plc.BrowserError(f"{verb}: tab {index} is out of range (0..{len(listing) - 1}); run tabs")
    if any(is_local_file(tab["url"]) for tab in listing):
        raise leave_local_file(seat, run, verb)
    target = listing[index]
    plc.run(seat, "tab-select", plc.Own(str(index)))
    after = session.tabs(seat)
    if any(is_local_file(tab["url"]) for tab in after):
        raise leave_local_file(seat, run, verb)
    selected = next(tab for tab in after if tab["selected"])
    if selected["id"] != target["id"]:
        raise plc.BrowserError(f"{verb}: tabs changed during selection; run tabs again")
    return selected


def _resize(seat, args):
    if len(args) != 2 or not all(re.fullmatch(r"[1-9][0-9]{0,3}", arg) for arg in args):
        raise plc.BrowserError("usage: resize <width> <height> — whole pixels, 1 to 9999")
    plc.run(seat, "resize", args[0], args[1])
    return f"{args[0]}x{args[1]}"


# What a URL parser drops from the head of a URL: the C0 controls and the space.
URL_BLANKS = "".join(chr(code) for code in range(0x21))


def is_local_file(url):
    """True for a URL that names a file of this machine, also behind `view-source:` or `filesystem:`. A
    browser reads the scheme without case, drops the blanks and control characters at the head of a URL,
    and drops tabs and newlines wherever they stand in it."""
    if not isinstance(url, str):
        return False
    head = url
    while True:
        head = re.sub(r"[\t\n\r]", "", head.lstrip(URL_BLANKS)[:128]).lower()
        if not head.startswith(("view-source:", "filesystem:")):
            return head.startswith("file:")
        head = head.split(":", 1)[1]


def refuse_local_url(url, verb):
    """Raise the refusal of `verb` for a `file:` URL."""
    if is_local_file(url):
        raise plc.BrowserError(f"{verb}: refusing {url!r} — {LOCAL_FILE}")


def _go(seat, args):
    _need(args, 1, "go <url>")
    url = args[0]
    refuse_local_url(url, "go")
    if not url.startswith(("http://", "https://", "about:")):
        raise plc.BrowserError(f"go: refusing {url!r} — give a full http(s) URL")
    plc.run(seat, "goto", url, timeout=60)
    return session.current_url(seat)


def _click(seat, args):
    _need(args, 1, "click <text or selector>")
    target = resolve.click_target(seat, " ".join(args))
    plc.run(seat, "click", target)
    return target


def _dblclick(seat, args):
    _need(args, 1, "dblclick <text or selector>")
    target = resolve.click_target(seat, " ".join(args))
    plc.run(seat, "dblclick", target)
    return target


def _fill(seat, args):
    _need(args, 2, "fill <label or selector> <text>")
    target = resolve.fill_target(seat, args[0])
    plc.run(seat, "fill", target, " ".join(args[1:]))
    return target


def _fill_secret(seat, args, run):
    """`fill`, with the text taken from the seat's secret store, not from the script.

    The sender names the secret and never holds the value. It is typed without
    entering any argv (see `plc.fill_secret`), only on a page whose origin the
    operator allowed for this secret, and the step reports the name only. A
    refusal comes before any work on the page.
    """
    if len(args) != 2:
        raise plc.BrowserError("usage: fill-secret <label or selector> <NAME>")
    name = args[1]
    entry = run.secret(name)
    if entry is None:
        raise plc.BrowserError(
            f"secret {name} is not set for seat {seat}; "
            f"the operator sets it with `ar3 tool browser --node {seat} secret set {name} - --origin ORIGIN`"
        )
    target = resolve.fill_target(seat, args[0])
    selector = f"aria-ref={target}" if resolve.REF_RE.match(target) else target
    origins = [secretstore.origin_parts(origin) for origin in entry["origins"]]
    try:
        plc.fill_secret(seat, selector, entry["value"], origins, session.scratch_dir(seat))
    except plc.OriginRefused as exc:
        raise plc.BrowserError(
            f"fill-secret: secret {name} may not be typed on origin {exc.origin}; "
            f"allowed origins: {', '.join(entry['origins'])}"
        ) from None
    except plc.BrowserError as exc:
        # Playwright prints the value in the call log of a failed fill.
        raise plc.BrowserError(run.clean(str(exc))) from None
    except OSError as exc:
        raise plc.BrowserError(f"fill-secret: cannot write its private code file: {exc.strerror}") from None
    return f"{target} <- [secret:{name}]"


def _text(seat, args):
    _need(args, 1, "text <selector>")
    found = plc.call_in_page(
        seat, "(selector) => (document.querySelector(selector)?.innerText || '').trim()",
        [args[0]], session.scratch_dir(seat),
    )
    return found if isinstance(found, str) else ""


def _wait_for(seat, args):
    _need(args, 1, "wait-for <selector> [seconds]")
    timeout = _seconds(args[1]) if len(args) > 1 else 10.0
    plc.wait_for_selector(seat, args[0], timeout, session.scratch_dir(seat))
    return f"visible: {args[0]}"


def _wait_for_url(seat, args):
    _need(args, 1, "wait-for-url <substring> [seconds]")
    deadline = time.monotonic() + (_seconds(args[1]) if len(args) > 1 else 10.0)
    while time.monotonic() < deadline:
        url = session.current_url(seat)
        if args[0] in url:
            return url
        time.sleep(0.5)
    raise plc.BrowserError(f"wait-for-url: still at {session.current_url(seat)}")


def _assert_text(seat, args):
    _need(args, 1, "assert-text <text>")
    wanted = args[0]
    found = plc.call_in_page(
        seat, "(wanted) => (document.body?.innerText || '').includes(wanted)", [wanted], session.scratch_dir(seat),
    )
    if not found:
        raise plc.BrowserError(f"assert-text: {wanted!r} is not on the page")
    return "found"


def _assert_url(seat, args):
    _need(args, 1, "assert-url <substring>")
    url = session.current_url(seat)
    if args[0] not in url:
        raise plc.BrowserError(f"assert-url: at {url}")
    return url


def _console(seat, args, run):
    """Attach the page's console log; the transcript keeps a copy inline."""
    for arg in args:
        plc.refuse_protected(seat, arg)
    argv = ["console"]
    levels = []
    cleared = False
    for arg in args:
        if arg == "--all":
            argv.append(plc.Own("--all"))
        elif arg == "--clear":
            argv.append(plc.Own("--clear"))
            cleared = True
        elif not arg.startswith("-") and not levels:
            levels.append(arg)
        else:
            raise plc.BrowserError("usage: console [error|warning|info|debug] [--all] [--clear]")
    if levels:
        argv.append(levels[0])
    body = run.clean(plc.result_or(plc.run(seat, *argv)))
    if cleared and not body:
        return "cleared"
    path = artifact(seat, "console.log")
    with open(path, "w") as handle:
        handle.write(body + "\n")
    run.attach(path)
    return body or "(console is empty)"


def _find(seat, args):
    _need(args, 1, "find [--regex] [--max-results <N>] <text>")
    rest = args[0]
    plc.refuse_protected(seat, rest)
    regex = False
    max_results = None
    usage = "usage: find [--regex] [--max-results <N>] <text>"
    while True:
        if rest == "--regex" or rest.startswith("--regex "):
            regex = True
            rest = rest[len("--regex"):].lstrip()
            continue
        match = re.match(r"^--max-results(?:=(\S+)|\s+(\S+))(?:\s+(.*))?$", rest, re.S)
        if match:
            raw_limit = match.group(1) or match.group(2)
            if not re.fullmatch(r"[1-9][0-9]{0,5}", raw_limit):
                raise plc.BrowserError(f"{usage} — N is a positive integer")
            max_results = raw_limit
            rest = (match.group(3) or "").lstrip()
            continue
        if rest == "--max-results":
            raise plc.BrowserError(usage)
        break
    if not rest:
        raise plc.BrowserError(usage)
    argv = ["find"]
    if max_results is not None:
        argv += [plc.Own("--max-results"), max_results]
    if regex:
        if rest.startswith("-"):
            argv.append(f"--regex={rest}")
        else:
            argv += [plc.Own("--regex"), rest]
    elif rest.startswith("-"):
        argv += [plc.Own("--"), rest]
    else:
        argv.append(rest)
    return plc.result_or(plc.run(seat, *argv))


REQUEST_PART_VERBS = (
    "request",
    "request-headers",
    "request-body",
    "response-headers",
    "response-body",
)

REQUEST_PART_OFF_WITH_SECRETS = (
    "request details and bodies are off on a seat that holds secrets; a request or response can carry "
    "a secret in an encoded form the scrub cannot find. Use `requests` to list URLs and status codes, "
    "remove the secrets (ar3 tool browser --seat NAME secret rm SECRET), or use a seat without them."
)
REQUEST_PART_OFF_AFTER_HIDE = (
    "request details and bodies are off in a script after a recipe has hidden a value in it; a request "
    "or response can carry that value in an encoded form. Run them in a script of their own."
)
_HEADER_LINE_RE = re.compile(r"^([!#$%&'*+\-.^_`|~0-9A-Za-z]+):(?: (.*))?$")
_INDENTED_HEADER_RE = re.compile(r"^    ([!#$%&'*+\-.^_`|~0-9A-Za-z]+):(?: (.*))?$")
_REDACTED_HEADERS = frozenset({"cookie", "set-cookie", "authorization", "proxy-authorization"})


def _redact_headers(body, indented=False):
    """Replace credential header values (`Cookie`, `Set-Cookie`, `Authorization`, `Proxy-Authorization`),
    including multi-line `Set-Cookie` continuations joined with `\\n`."""
    lines = []
    in_section = not indented
    redacting = False
    for line in body.splitlines():
        if indented:
            if line in ("  Request headers", "  Response headers"):
                in_section = True
                redacting = False
                lines.append(line)
                continue
            if not line or (line.startswith("  ") and not line.startswith("    ")):
                in_section = False
                redacting = False
                lines.append(line)
                continue
            if not in_section:
                lines.append(line)
                continue
        match = (_INDENTED_HEADER_RE if indented else _HEADER_LINE_RE).match(line)
        if match:
            name = match.group(1)
            redacting = name.lower() in _REDACTED_HEADERS
            prefix = "    " if indented else ""
            lines.append(f"{prefix}{name}: [redacted]" if redacting else line)
        elif not redacting:
            lines.append(line)
    return "\n".join(lines)


def _requests(seat, args):
    for arg in args:
        plc.refuse_protected(seat, arg)
    argv = ["requests"]
    cleared = False
    index = 0
    usage = "usage: requests [--static] [--all] [--clear] [--filter <regex>]"
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--static":
            argv.append(plc.Own("--static"))
        elif arg == "--all":
            argv.append(plc.Own("--all"))
        elif arg == "--clear":
            argv.append(plc.Own("--clear"))
            cleared = True
        elif arg == "--filter" or arg.startswith("--filter="):
            if arg == "--filter":
                if index >= len(args) or not args[index]:
                    raise plc.BrowserError(usage)
                pattern = args[index]
                index += 1
            else:
                pattern = arg.split("=", 1)[1]
                if not pattern:
                    raise plc.BrowserError(usage)
            if pattern.startswith("-"):
                argv.append(f"--filter={pattern}")
            else:
                argv += [plc.Own("--filter"), pattern]
        else:
            raise plc.BrowserError(usage)
    body = plc.result_or(plc.run(seat, *argv, timeout=60))
    if cleared and not body:
        return "cleared"
    return body


def _request_part(seat, verb, args, run):
    for arg in args:
        plc.refuse_protected(seat, arg)
    if not run.capturing:
        raise plc.BrowserError(f"{verb}: nothing of the page is captured while a sensitive step is open")
    if run.scrubber:
        raise plc.BrowserError(REQUEST_PART_OFF_WITH_SECRETS if run.scrubber.secrets else REQUEST_PART_OFF_AFTER_HIDE)
    if len(args) != 1 or not re.fullmatch(r"[1-9][0-9]{0,8}", args[0]):
        raise plc.BrowserError(f"usage: {verb} <N> — N is a 1-based request number from `requests`")
    if verb == "response-body":
        before = set(_cli_output_state(seat))
        try:
            body = plc.result_or(plc.run(seat, verb, args[0], timeout=60))
        finally:
            added = set(_cli_output_state(seat)) - before
            for path in added:
                _remove(path)
        if added and body.strip().replace("\\", "/").startswith(".playwright-cli/"):
            return "(binary response body)"
        return body
    body = plc.result_or(plc.run(seat, verb, args[0], timeout=60))
    if verb in ("request", "request-headers", "response-headers"):
        return _redact_headers(body, indented=(verb == "request"))
    return body


def _video_start(seat, args):
    active = session.video_state(seat)
    if active:
        raise plc.BrowserError(f"video-start: already recording {active['path']}")
    slug = "-".join(args) if args else "recording"
    plc.refuse_protected(seat, slug)  # the path is the tool's own, but the slug in it is the caller's
    path = artifact(seat, f"{slug}.webm")
    plc.run(seat, "video-start", plc.Own(path), plc.Own("--size"), plc.Own("1280x1024"))
    session.save_video_state(
        seat, {"path": path, "started_at": time.time(), "chapters": 0}
    )
    return path


def _video_stop(seat, args, run):
    state = session.video_state(seat)
    if not state:
        raise plc.BrowserError("video-stop: no recording in progress")
    try:
        output = plc.run(seat, "video-stop", timeout=60)
    finally:
        # Stopped or not, the recording is gone once the call returns —
        # a lost session must not leave the seat looking busy forever.
        session.clear_video_state(seat)
    if "no videos were recorded" in output.lower():
        raise plc.BrowserError(f"video-stop: playwright-cli recorded nothing: {output.strip()}")
    path = state["path"]
    if not os.path.exists(path):
        raise plc.BrowserError(f"video-stop: no file written at {path}")
    if os.path.getsize(path) == 0:
        raise plc.BrowserError(f"video-stop: {path} is empty")
    run.attach(path)
    return f"{path} ({state.get('chapters') or 0} chapters)"


def _video_chapter(seat, args):
    _need(args, 1, "video-chapter <title>")
    state = session.video_state(seat)
    if not state:
        raise plc.BrowserError("video-chapter: no recording in progress")
    title = args[0]
    plc.run(seat, "video-chapter", title)
    state["chapters"] = int(state.get("chapters") or 0) + 1
    session.save_video_state(seat, state)
    return f"chapter {state['chapters']}: {title}"


DOWNLOAD_POLL_SECONDS = 30.0


def _local_file(path, verb):
    """A path the seat can actually open, or a refusal that says why.

    Relative paths are refused rather than resolved: the seat's cwd is its own
    scratch dir, so a relative path from a sender means a file on the *sender's*
    machine, and quietly resolving it here would find something else or nothing.

    A path inside the browser state directory is refused before it is looked at:
    that directory holds every seat's secret store, and the answer must not say
    whether a file is there.
    """
    if not os.path.isabs(path):
        raise plc.BrowserError(
            f"{verb}: {path!r} is not an absolute path — name the file by its full "
            "path on the machine holding the browser"
        )
    if session.inside_state_root(path):
        raise plc.BrowserError(f"{verb}: refusing {path!r} — it is inside the browser state directory")
    if not os.path.isfile(path):
        raise plc.BrowserError(f"{verb}: no such file: {path}")
    return path


def _upload(seat, args):
    """Hand one or more files to a file chooser the page has already opened.

    playwright-cli's own `upload` is the only thing that answers a chooser: the
    modal belongs to the driver, not the DOM, so no amount of page JS reaches it.
    """
    _need(args, 1, "upload <path> [<path> ...]")
    paths = [_local_file(arg, "upload") for arg in args]
    plc.run(seat, "upload", *paths, timeout=120)
    return ", ".join(os.path.basename(path) for path in paths)


def _drop(seat, args):
    """Drop files onto an element, as a person dragging them in would.

    Preferred over `upload` where a page accepts it, because it opens no menu
    and no chooser — one command, no intermediate state to get wedged in.
    """
    _need(args, 2, "drop <target> <path> [<path> ...]")
    paths = [_local_file(arg, "drop") for arg in args[1:]]
    target = resolve.click_target(seat, args[0])
    argv = ["drop", target]
    for path in paths:
        argv += [plc.Own("--path"), path]
    plc.run(seat, *argv, timeout=120)
    return f"{target} <- " + ", ".join(os.path.basename(path) for path in paths)


PARTIAL_DOWNLOAD = ".crdownload"


# What a file name ends with that is an extension: a dot and a few letters or digits.
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}")


def _download_name(seat, name):
    """The name an artifact is given for a file that a page named: the page's, unless it holds a value that must not be shown.
    The reply is sent with `a8s tell --attach=<path>`, so the name is in a command line. The extension is kept when it is
    one, and the scrub of the name leaves it as it was: the end of a value (`Summer.2026`) is no extension."""
    if not plc.holds_protected(seat, name):
        return name
    suffix = os.path.splitext(name)[1]
    kept = _EXTENSION.fullmatch(suffix) and plc.scrub_protected(seat, name).endswith(suffix)
    return "download" + (suffix if kept else "")


def _download(seat, args, run):
    """Click something that downloads, and attach what came back.

    Each call points Chrome at a directory of its own before it clicks. Chrome
    fixes a download's path when the download starts, so a download an earlier
    call gave up on finishes in that call's directory and can never be taken
    for this one. Chrome writes `<name>.crdownload` while bytes arrive and
    renames it when they are all there, so the wait is for a finished file.
    It is moved into artifacts; a directory left holding a partial file is
    where that file lands if it ever finishes.
    """
    _need(args, 1, "download <target> [seconds]")
    timeout = _seconds(args[1]) if len(args) > 1 else DOWNLOAD_POLL_SECONDS
    target = resolve.click_target(seat, args[0])
    directory = tempfile.mkdtemp(prefix="call-", dir=session.downloads_dir(seat))
    plc.run_code(seat, session._route_downloads_js(seat, directory), timeout=15)

    plc.run(seat, "click", target, timeout=60)
    deadline = time.monotonic() + timeout
    while True:
        names = sorted(os.listdir(directory))
        finished = [name for name in names if not name.endswith(PARTIAL_DOWNLOAD)]
        if finished:
            break
        if time.monotonic() >= deadline:
            detail = f"; {names[0]} is still arriving" if names else ""
            if not names:
                os.rmdir(directory)
            raise plc.BrowserError(
                f"download: {target} saved no finished file within {timeout:.0f}s "
                f"(Chrome was saving into {directory}){detail}"
            )
        time.sleep(0.25)

    path = _claim_artifact(seat, os.path.join(directory, finished[0]), _download_name(seat, finished[0]))
    if not os.listdir(directory):
        os.rmdir(directory)
    run.attach(path)
    return path


EVAL_OFF_WITH_SECRETS = (
    "eval and run-code are off on a seat that holds secrets; they could read a field a secret "
    "was typed into. Remove the secrets (ar3 tool browser --seat NAME secret rm SECRET) or use a seat without them."
)
EVAL_OFF_AFTER_HIDE = (
    "eval and run-code are off in a script after a recipe has hidden a value in it; they could read the "
    "field it was typed into. Run them in a script of their own."
)


def _refuse_on_a_seat_with_secrets(run):
    """The caller's `eval` and `run-code` run code of the caller's choosing in the page and the
    driver, which can read a field a secret was typed into and change what it read before the
    scrub sees it, or open the state directory by a URL. The opt-in does not weigh against that,
    so a seat whose store holds a secret refuses both, whatever `ALLOW_EVAL` says. So does the rest
    of a script in which a recipe has hidden a value it typed (`Run.hide`), for the same reason.

    The guard is on these two verbs. The tool's own code reaches the driver through `plc` and is
    not the caller's: `fill-secret`'s file, the session and `wait-for` and `download` internals,
    and any reviewed recipe that uses the Python API instead of the verbs.
    """
    if run.scrubber:
        raise plc.BrowserError(EVAL_OFF_WITH_SECRETS if run.scrubber.secrets else EVAL_OFF_AFTER_HIDE)


def _run_code(seat, args, allow_eval):
    """Playwright statements with `page` in scope — the driver, not the DOM.

    Behind the same opt-in as `eval` on purpose: this is the same class of
    capability and strictly more of it, so it must not be the easier door.
    """
    if not allow_eval:
        raise plc.BrowserError(
            "run-code refused: this seat has not opted in (AR3_BROWSER_ALLOW_EVAL)"
        )
    _need(args, 1, "run-code <<END ... END")
    # A body is free to return nothing, so report a result when there is one
    # rather than insisting the driver produced one.
    return plc.result_or(plc.run_code(seat, args[0], timeout=60))


def _step(seat, verb, args, run, allow_eval, script_epoch=None):
    if verb in CAPTURE_VERBS and not run.capturing:
        raise plc.BrowserError(f"{verb}: nothing of the page is captured while a sensitive step is open")
    if verb == "recipe":
        from . import recipe

        return recipe.invoke(seat, args, run, allow_eval, script_epoch, run.recipes)
    if verb == "open":
        session.ensure_running(seat, script_epoch=script_epoch)
        return session.current_url(seat)
    if verb == "close":
        session.close_browser(seat)
        return "closed"
    if verb == "save":
        session.save_state(seat)
        return "saved"
    if verb == "url":
        return session.current_url(seat)
    if verb == "tabs":
        if args:
            raise plc.BrowserError("usage: tabs")
        listing = session.tabs(seat)
        return "\n".join(
            f"{index}{' *' if tab['selected'] else ''}: {tab['url']}"
            for index, tab in enumerate(listing)
        )
    if verb == "tab":
        return _select_tab(seat, _tab_index(args, verb), run, verb)["url"]
    if verb == "go":
        return _go(seat, args)
    if verb == "click":
        return _click(seat, args)
    if verb == "dblclick":
        return _dblclick(seat, args)
    if verb == "fill":
        return _fill(seat, args)
    if verb == "fill-secret":
        return _fill_secret(seat, args, run)
    if verb == "press":
        _need(args, 1, "press <key>")
        plc.run(seat, "press", args[0])
        return args[0]
    if verb == "type":
        _need(args, 1, "type <text>")
        plc.run(seat, "type", args[0])
        return "typed"
    if verb == "text":
        return _text(seat, args)
    if verb == "wait":
        _need(args, 1, "wait <seconds>")
        time.sleep(_seconds(args[0]))
        return f"{_seconds(args[0])}s"
    if verb == "wait-for":
        return _wait_for(seat, args)
    if verb == "wait-for-url":
        return _wait_for_url(seat, args)
    if verb == "assert-text":
        return _assert_text(seat, args)
    if verb == "assert-url":
        return _assert_url(seat, args)
    if verb == "back":
        plc.run(seat, "go-back", timeout=60)
        return session.current_url(seat)
    if verb == "forward":
        plc.run(seat, "go-forward", timeout=60)
        return session.current_url(seat)
    if verb == "reload":
        plc.run(seat, "reload", timeout=60)
        return session.current_url(seat)
    if verb == "scroll":
        _need(args, 1, "scroll <dy>")
        plc.run(seat, "mousewheel", "0", args[0])
        return args[0]
    if verb == "resize":
        return _resize(seat, args)
    if verb == "find":
        return _find(seat, args)
    if verb == "select":
        _need(args, 2, "select <label or selector> <value>")
        target = resolve.fill_target(seat, args[0])
        plc.run(seat, "select", target, " ".join(args[1:]))
        return target
    if verb in ("check", "uncheck", "hover"):
        _need(args, 1, f"{verb} <text or selector>")
        target = resolve.click_target(seat, " ".join(args))
        plc.run(seat, verb, target)
        return target
    if verb == "dialog-accept":
        plc.run(seat, "dialog-accept", *args[:1])
        return "accepted"
    if verb == "dialog-dismiss":
        plc.run(seat, "dialog-dismiss")
        return "dismissed"
    if verb == "requests":
        return _requests(seat, args)
    if verb in REQUEST_PART_VERBS:
        return _request_part(seat, verb, args, run)
    if verb == "console":
        return _console(seat, args, run)
    if verb == "video-start":
        return _video_start(seat, args)
    if verb == "video-stop":
        return _video_stop(seat, args, run)
    if verb == "video-chapter":
        return _video_chapter(seat, args)
    if verb == "snap":
        return _snap(seat, args, run)
    if verb == "shot":
        if args:
            selected = _select_tab(seat, _tab_index(args, verb), run, verb)
        else:
            listing = session.tabs(seat)
            if any(is_local_file(tab["url"]) for tab in listing):
                raise leave_local_file(seat, run, verb)
            selected = next(tab for tab in listing if tab["selected"])
        path = session.indexed_screenshot(seat, selected["id"], artifact(seat, "screen.png"))
        run.attach(path)
        return path
    if verb == "upload":
        return _upload(seat, args)
    if verb == "drop":
        return _drop(seat, args)
    if verb == "download":
        return _download(seat, args, run)
    if verb in ("eval", "run-code"):
        _refuse_on_a_seat_with_secrets(run)
    if verb == "eval":
        if not allow_eval:
            raise plc.BrowserError(
                "eval refused: this seat has not opted in (AR3_BROWSER_ALLOW_EVAL)"
            )
        _need(args, 1, "eval <expression>")
        return plc.evaluate(seat, args[0], timeout=60)
    if verb == "run-code":
        return _run_code(seat, args, allow_eval)
    raise plc.BrowserError(f"unknown command: {verb}")


def _cli_output_state(seat):
    """{path: (mtime_ns, size)} of the files playwright-cli has written for this seat."""
    state = {}
    try:
        with os.scandir(session.cli_output_dir(seat)) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        info = entry.stat(follow_symlinks=False)
                        state[entry.path] = (info.st_mtime_ns, info.st_size)
                except OSError:
                    pass
    except OSError:
        pass
    return state


@contextlib.contextmanager
def scrubbed_outputs(seat, scrubber):
    """Scrub the files playwright-cli writes while the body runs. The snapshot a
    click links to holds the value of every field on the page, and the console
    log holds whatever the page logged. With no secret stored, nothing is read."""
    if not scrubber:
        yield
        return
    before = _cli_output_state(seat)
    try:
        yield
    finally:
        for target, stamp in _cli_output_state(seat).items():
            if before.get(target) != stamp:
                scrubber.scrub_file(target, remove_on_failure=True)


# playwright-cli writes these as pictures or recordings. They are not text, and a sweep skips them unread.
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".webm", ".pdf", ".zip")


def sweep_outputs(seat, scrubber):
    """Scrub the files that playwright-cli wrote for this seat since the last sweep, before anything of the
    script can read them. The daemon appends to its console log after a call has returned, and
    `scrubbed_outputs` looks only at the files a call changed, so a page that logs a value seconds after it
    was typed leaves it in a file that no later call would scrub. The sweep starts every script and every
    command of the CLI. It looks at each file changed since the last sweep began (a margin before it, for a
    file system that stamps coarsely), or at every file when there was none, and makes each mode 0600. A file
    that holds a value and cannot be rewritten is removed. With no value to hide it reads nothing and does
    not move the mark: a secret stored later still finds the files that came before."""
    if not scrubber:
        return
    began = time.time_ns()
    since = session.sweep_mark(seat)
    if since is not None and since > began + CLOCK_MARGIN_NS:
        since = None  # the clock was set back since the last sweep: the stamps of the files cannot be compared with it
    for target, (changed, _size) in _cli_output_state(seat).items():
        if since is not None and changed < since - CLOCK_MARGIN_NS:
            continue
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        if not target.lower().endswith(BINARY_SUFFIXES):
            scrubber.scrub_file(target, remove_on_failure=True)
    session.mark_swept(seat, began)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# A file system may stamp a file to the second, or two. The files a moment before a step are undone with it.
CLOCK_MARGIN_NS = 2_000_000_000
# How long a Chrome that was closed may still be listed before it counts as one that would not close.
CLOSE_WAIT_SECONDS = 3.0


def close_chrome(seat):
    """Close the seat's Chrome. True when no Chrome of the seat is left, which is looked at again for a
    moment, because a process that was killed may still be listed. False when the close failed or Chrome
    stayed."""
    try:
        session.close_browser(seat)
        deadline = time.monotonic() + CLOSE_WAIT_SECONDS
        while session.chrome_running(seat):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        return True
    except Exception:
        return False


def _undo_step(seat, run):
    """Remove what the running step wrote: the files playwright-cli made or changed since it began, and the
    files it attached to the reply."""
    if run is None or run.step_mark is None:
        return
    started, attached = run.step_mark
    for path, (changed, _size) in _cli_output_state(seat).items():
        if changed >= started - CLOCK_MARGIN_NS:
            _remove(path)
    for path in run.files[attached:]:
        _remove(path)
    del run.files[attached:]


def _discard_recording(seat):
    """A recording keeps the frames of the page it ran on, and nothing scrubs pixels. Stop it, remove its
    file, and forget it. True when there was one. Only a file inside the seat's artifacts is removed."""
    state = session.video_state(seat)
    if not state:
        return False
    try:
        plc.run(seat, "video-stop", timeout=60)
    except plc.BrowserError:
        pass
    session.clear_video_state(seat)
    path = state.get("path") if isinstance(state, dict) else None
    if isinstance(path, str):
        artifacts = os.path.realpath(session.artifacts_dir(seat))
        if os.path.realpath(path).startswith(artifacts + os.sep):
            _remove(path)
    return True


def _close_local_pages(seat):
    """Close each page of Chrome, other than the one the driver drives, that is a local file, through its
    own target list (`session.pages`). Returns how many were closed, or None when one stayed open. A Chrome whose list
    cannot be read has nothing to close here: the page the driver drives is the one that was looked at."""
    found = session.pages(seat)
    if found is None:
        return 0
    local = [page_id for page_id, url in found if is_local_file(url)]
    for page_id in local:
        session.close_page(seat, page_id)
    if local:
        after = session.pages(seat)
        if after is None or any(is_local_file(url) for _page_id, url in after):
            return None
    return len(local)


def leave_local_file(seat, run, verb):
    """A page is a local file. Undo the step, send the page the driver drives to about:blank, close any other page
    that is a local file, and return the error that fails the step. A page that cannot be sent or closed takes
    Chrome with it: a local file on screen must not wait for the next step. A recording that was running is
    discarded. The error holds no text of the page."""
    _undo_step(seat, run)
    discarded = _discard_recording(seat)
    note = "The page was sent to about:blank."
    try:
        plc.run(seat, "goto", plc.Own("about:blank"), timeout=30)
    except plc.BrowserError:
        if close_chrome(seat):
            note = "The page could not be sent to about:blank, so Chrome was closed."
        else:
            note = "The page could not be sent to about:blank, and Chrome could not be closed."
    else:
        closed = _close_local_pages(seat)
        if closed is None:
            if close_chrome(seat):
                note = "A page in another tab could not be closed, so Chrome was closed."
            else:
                note = "A page in another tab could not be closed, and Chrome could not be closed."
        elif closed:
            note += f" {closed} other local {'page was' if closed == 1 else 'pages were'} closed."
    if discarded:
        note += " The recording was stopped and discarded."
    return plc.BrowserError(f"{verb}: the page is a local file, and {LOCAL_FILE}. {note}")


def refuse_a_local_page(seat, run, verb):
    """Fail the step when a page of Chrome is a local file. `go` opens none, and the driver is started without the
    switch that lets it, so this holds the case that a driver started some other way, or a page left from before,
    brings one in. The addresses of all the pages come from Chrome's own target list, which costs one driver call;
    when it cannot be read, the driver is asked for the address of the page it drives, which costs
    a call. A page whose address cannot be read is left to the step."""
    found = session.pages(seat)
    if found is not None:
        if any(is_local_file(url) for _page_id, url in found):
            raise leave_local_file(seat, run, verb)
        return
    try:
        url = session.current_url(seat)
    except plc.BrowserError:
        return
    if is_local_file(url):
        raise leave_local_file(seat, run, verb)


def script_commands(body):
    """Commands of a message, each with the block it opened or None.

    Comments, blanks and a8s attachment lines are not commands. A line ending
    in `<<MARKER` opens a block: the lines after it, up to one whose whole
    stripped content is MARKER, become that command's last argument, taken
    verbatim — no comments stripped, no blanks dropped, no quoting, indentation
    kept. A `#` inside a block is body text, and so is a line that reads like
    a verb.

    A line that opens a block carries nothing but the verb: the block is the
    whole argument. One rule for every verb, so no verb can quietly drop half
    of what the sender wrote and report success.

    The form belongs to the parser, not to one verb: it is how any verb in
    RAW_ARGS takes an argument too long for a line. `run-code` is what it
    exists for, because a useful Playwright body is never one line.
    """
    parsed = []
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith(("ATTACHED FILE:", "ATTACHMENT UNAVAILABLE:")):
            continue
        opener = HEREDOC.match(line)
        command = opener.group("command").strip() if opener else ""
        if not command:
            parsed.append((line, None))
            continue
        if len(command.split()) > 1:
            raise ScriptError(
                line,
                "a line that opens a block cannot also carry an inline argument: "
                f"{line}",
            )
        marker = opener.group("marker")
        block = []
        for raw in lines[index:]:
            index += 1
            if raw.strip() == marker:
                break
            block.append(raw)
        else:
            raise ScriptError(line, f"unterminated <<{marker}: no line reads {marker}")
        parsed.append((command, "\n".join(block)))
    return parsed


def run_script(seat, body, allow_eval=False, epoch=None, recipes=None):
    """`epoch` is the stop epoch the caller read at its entry, before any wait
    for the seat. None reads it now. `recipes` is the `recipe.Library` the
    `recipe` verb reads; None means the bundled recipes."""
    session.secure_seat(seat)
    try:
        # Read once. A store that cannot be read cannot be scrubbed with, so no
        # script runs: a page may hold a value the reply would then show.
        scrubber = secretstore.Scrubber(secretstore.read(seat))
    except secretstore.SecretStoreError as exc:
        run = Run(seat)
        run.fail("secret store", str(exc))
        return run
    run = Run(seat, scrubber, recipes)
    session.prune_scratch(seat)
    # The values to keep out of a command line are the ones this script holds, those it hides included.
    with plc.protecting(seat, scrubber):
        sweep_outputs(seat, scrubber)
        _run_steps(seat, body, run, scrubber, allow_eval, epoch)
    return run


def _run_steps(seat, body, run, scrubber, allow_eval, epoch):
    try:
        # Parse the whole script before running any of it, so a block nobody
        # closed cannot run as a truncated body.
        steps = script_commands(body)
    except ScriptError as exc:
        steps = []
        try:
            session._check_session_binding(seat)
        except plc.BrowserError as bind_exc:
            run.fail(exc.line, str(bind_exc))
            return
        run.fail(exc.line, str(exc))
    epoch0 = session.stop_epoch(seat) if epoch is None else epoch
    stopped = False
    for line, block in steps:
        if session.stop_epoch(seat) != epoch0:
            # `a8s stop` closed the seat under this script: end it here.
            run.fail(line, session.STOP_MESSAGE)
            stopped = True
            break
        verb = line.split(None, 1)[0]
        try:
            if verb != "close":
                session._check_session_binding(seat)
        except plc.BrowserError as exc:
            run.fail(line, str(exc))
            stopped = True
            break
        if verb in RAW_ARGS:
            args = line.split(None, 1)[1:]
        else:
            try:
                words = shlex.split(line)
            except ValueError as exc:
                run.fail(line, f"unparseable: {exc}")
                break
            if not words:
                continue
            verb, args = words[0], words[1:]
        if block is not None:
            # The opener carried no other argument — the parser refuses one —
            # so the block is the command's whole argument, for every verb.
            args = [block]
        try:
            with scrubbed_outputs(seat, scrubber):
                run.begin_step()
                if verb not in NO_ENSURE:
                    session.ensure_running(seat, script_epoch=epoch0)
                    run.page_used = True
                try:
                    output = _step(seat, verb, args, run, allow_eval, script_epoch=epoch0)
                except plc.BrowserError:
                    # A step that fails on a local page may hold its text in what it did before it failed.
                    if run.page_used:
                        refuse_a_local_page(seat, run, verb)
                    raise
                if run.page_used:
                    refuse_a_local_page(seat, run, verb)
            run.note(line, str(output or ""))
        except plc.BrowserError as exc:
            run.fail(line, str(exc), getattr(exc, "output", ""))
            stopped = stopped or str(exc) == session.STOP_MESSAGE or getattr(exc, "binding_mismatch", False)
            break

    if not run.ok and not stopped and run.capturing:
        try:
            session._check_session_binding(seat)
            with scrubbed_outputs(seat, scrubber):
                refuse_a_local_page(seat, run, "failure")
                _write_snapshot(seat, run, "failure")
        except plc.BrowserError:
            pass
