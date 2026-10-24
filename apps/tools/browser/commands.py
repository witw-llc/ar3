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
import os
import re
import shlex
import tempfile
import time

from . import plc
from . import resolve
from . import session

MAX_WAIT = 300.0

# Verbs that must not auto-open the browser: `close` is the whole point, and
# `video-stop` needs to report a stale recording rather than launch Chrome to
# discover one.
NO_ENSURE = {"close", "video-stop"}

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
    """The transcript of one script: what each step did, and what it produced."""

    def __init__(self, seat):
        self.seat = seat
        self.steps = []
        self.files = []
        self.error = None

    def note(self, line, output=""):
        self.steps.append({"command": line, "ok": True, "output": output})

    def fail(self, line, message):
        self.steps.append({"command": line, "ok": False, "error": message})
        self.error = message

    def attach(self, path):
        self.files.append(path)

    @property
    def ok(self):
        return self.error is None

    def transcript(self):
        lines = []
        for step in self.steps:
            mark = "  " if step["ok"] else "! "
            lines.append(f"{mark}{step['command']}")
            detail = step.get("output") or step.get("error") or ""
            for line in detail.splitlines():
                lines.append(f"      {line}")
        return "\n".join(lines)


def artifact(seat, suffix):
    """A path in the seat's artifacts that no earlier artifact holds.

    The stamp has one-second resolution, so two artifacts of one name in the
    same second get a counter rather than the same path.
    """
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


def _write_snapshot(seat, run, label="snapshot"):
    path = artifact(seat, f"{label}.txt")
    with open(path, "w") as handle:
        handle.write(session.snapshot(seat))
    run.attach(path)
    return path


def _seconds(value):
    """Anything over 300 is milliseconds, not a long nap."""
    number = float(value)
    if number > MAX_WAIT:
        number = number / 1000.0
    return min(number, MAX_WAIT)


def _need(args, count, usage):
    if len(args) < count:
        raise plc.BrowserError(f"usage: {usage}")


def _go(seat, args):
    _need(args, 1, "go <url>")
    url = args[0]
    if not url.startswith(("http://", "https://", "file://", "about:")):
        raise plc.BrowserError(f"go: refusing {url!r} — give a full http(s) URL")
    plc.run(seat, "goto", url, timeout=60)
    return session.current_url(seat)


def _click(seat, args):
    _need(args, 1, "click <text or selector>")
    target = resolve.click_target(seat, " ".join(args))
    plc.run(seat, "click", target)
    return target


def _fill(seat, args):
    _need(args, 2, "fill <label or selector> <text>")
    target = resolve.fill_target(seat, args[0])
    plc.run(seat, "fill", target, " ".join(args[1:]))
    return target


def _text(seat, args):
    _need(args, 1, "text <selector>")
    selector = plc.js_string(args[0])
    return plc.evaluate(
        seat, f"(document.querySelector('{selector}')?.innerText || '').trim()"
    )


def _wait_for(seat, args):
    _need(args, 1, "wait-for <selector> [seconds]")
    timeout = _seconds(args[1]) if len(args) > 1 else 10.0
    selector = plc.js_string(args[0])
    plc.run_code(
        seat,
        f"await page.waitForSelector('{selector}', "
        f"{{state: 'visible', timeout: {int(timeout * 1000)}}});",
        timeout=timeout + 10,
    )
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
    found = plc.evaluate_json(
        seat, f"(document.body?.innerText || '').includes('{plc.js_string(wanted)}')"
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
    argv = ["console", *args[:1]]
    body = plc.result_or(plc.run(seat, *argv))
    path = artifact(seat, "console.log")
    with open(path, "w") as handle:
        handle.write(body + "\n")
    run.attach(path)
    return body or "(console is empty)"


def _video_start(seat, args):
    active = session.video_state(seat)
    if active:
        raise plc.BrowserError(f"video-start: already recording {active['path']}")
    slug = "-".join(args) if args else "recording"
    path = artifact(seat, f"{slug}.webm")
    plc.run(seat, "video-start", path, "--size", "1280x1024")
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
    """
    if not os.path.isabs(path):
        raise plc.BrowserError(
            f"{verb}: {path!r} is not an absolute path — name the file by its full "
            "path on the machine holding the browser"
        )
    if not os.path.isfile(path):
        raise plc.BrowserError(f"{verb}: no such file: {path}")
    return path


def _upload(seat, args):
    """Hand one file to a file chooser the page has already opened.

    playwright-cli's own `upload` is the only thing that answers a chooser: the
    modal belongs to the driver, not the DOM, so no amount of page JS reaches it.

    One file, because that is what the command takes. Its help says "one or
    multiple files" and calls the argument "the absolute paths", but the parser
    accepts a single positional and rejects two before it touches the browser.
    A second `upload` is not a workaround either: the first one answers the
    chooser and closes it, so the rest go nowhere. Multi-file belongs to `drop`,
    whose `--path` really does repeat.
    """
    _need(args, 1, "upload <path>")
    if len(args) > 1:
        raise plc.BrowserError(
            f"upload takes one file, not {len(args)} — the file chooser is answered "
            "once and closes. Use `drop <target> <path> <path> ...` for several at a time"
        )
    path = _local_file(args[0], "upload")
    plc.run(seat, "upload", path, timeout=120)
    return os.path.basename(path)


def _drop(seat, args):
    """Drop files onto an element, as a person dragging them in would.

    Preferred over `upload` where a page accepts it, because it opens no menu
    and no chooser — one command, no intermediate state to get wedged in.
    """
    _need(args, 2, "drop <target> <path> [<path> ...]")
    target = resolve.click_target(seat, args[0])
    paths = [_local_file(arg, "drop") for arg in args[1:]]
    argv = ["drop", target]
    for path in paths:
        argv += ["--path", path]
    plc.run(seat, *argv, timeout=120)
    return f"{target} <- " + ", ".join(os.path.basename(path) for path in paths)


PARTIAL_DOWNLOAD = ".crdownload"


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

    path = _claim_artifact(seat, os.path.join(directory, finished[0]), finished[0])
    if not os.listdir(directory):
        os.rmdir(directory)
    run.attach(path)
    return path


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
    if verb == "open":
        session.ensure_running(seat, script_epoch=script_epoch)
        return session.current_url(seat)
    if verb == "close":
        session.close_browser(seat)
        return "closed"
    if verb == "save":
        return session.save_state(seat)
    if verb == "url":
        return session.current_url(seat)
    if verb == "go":
        return _go(seat, args)
    if verb == "click":
        return _click(seat, args)
    if verb == "fill":
        return _fill(seat, args)
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
    if verb == "find":
        _need(args, 1, "find <text>")
        return plc.result_or(plc.run(seat, "find", args[0]))
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
        return plc.result_or(plc.run(seat, "requests", timeout=60))
    if verb == "console":
        return _console(seat, args, run)
    if verb == "video-start":
        return _video_start(seat, args)
    if verb == "video-stop":
        return _video_stop(seat, args, run)
    if verb == "video-chapter":
        return _video_chapter(seat, args)
    if verb == "snap":
        return _write_snapshot(seat, run)
    if verb == "shot":
        path = plc.screenshot(seat, artifact(seat, "screen.png"))
        run.attach(path)
        return path
    if verb == "upload":
        return _upload(seat, args)
    if verb == "drop":
        return _drop(seat, args)
    if verb == "download":
        return _download(seat, args, run)
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


def run_script(seat, body, allow_eval=False, epoch=None):
    """`epoch` is the stop epoch the caller read at its entry, before any wait
    for the seat. None reads it now."""
    run = Run(seat)
    session.prune_scratch(seat)
    try:
        # Parse the whole script before running any of it, so a block nobody
        # closed cannot run as a truncated body.
        steps = script_commands(body)
    except ScriptError as exc:
        steps = []
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
            if verb not in NO_ENSURE:
                session.ensure_running(seat, script_epoch=epoch0)
            output = _step(seat, verb, args, run, allow_eval, script_epoch=epoch0)
            run.note(line, str(output or ""))
        except plc.BrowserError as exc:
            run.fail(line, str(exc))
            stopped = stopped or str(exc) == session.STOP_MESSAGE
            break

    if not run.ok and not stopped:
        try:
            _write_snapshot(seat, run, "failure")
        except plc.BrowserError:
            pass
    return run
