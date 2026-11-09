"""playwright-cli wrapper.

Everything that knows the CLI's wire format lives here: its markdown-ish
sections, its habit of reporting failure while exiting 0, and the argument
shapes that differ from what the verb names suggest.
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading

from ar3.playwright_cli import INSTALL_COMMAND
from ar3.proc import launch_options

INSTALL_CMD = list(INSTALL_COMMAND)
_asked = False

# `fill_secret` makes a private directory with this prefix in the seat's scratch dir, and
# `run_code_file` one with the other.
SECRET_DIR_PREFIX = "fill-secret-"
CODE_DIR_PREFIX = "run-code-"
# playwright-cli's own action timeout, which its `fill` uses.
FILL_TIMEOUT_MS = 5000
# playwright-cli opens a file: page only when its daemon starts with this variable set, and the daemon takes
# the environment of the call that starts it. No call of this tool passes it on.
FILE_ACCESS_ENV = "PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS"
# The variables that move what the driver writes, or make it write more: its output directory, a config file that
# can name another, its debug log (which holds the protocol, text typed into a field included) and the directories
# its tests use. The sweep looks at `scratch/.playwright-cli`, so the driver writes there and nowhere else.
DRIVER_OUTPUT_ENV = re.compile(
    r"(PLAYWRIGHT_MCP_(OUTPUT_DIR|CONFIG|SAVE_\w+)|PWTEST_(SOCKETS_DIR|DAEMON_SESSION_DIR)|DEBUG|DEBUG_FILE)",
    re.IGNORECASE,
)
# What the driver, and the daemon it starts, may do to a file it makes: nothing for another user.
DRIVER_UMASK = 0o077


class BrowserError(Exception):
    pass


# What a refused command line says. It names no value, no secret and no argument.
COMMAND_REFUSED = (
    "playwright-cli was not run: an argument of its command line held a value that must not be in one"
)
# A snapshot ref, as playwright-cli prints it. The driver makes it, and its number says nothing.
REF_SHAPE = re.compile(r"(f\d+)?e\d+")
# Where a verb takes a snapshot ref as the element it acts on: the places, counted from the first argument after the verb.
# A ref is exempt from the check of a command line only there. In any other place the same text is data (`type e482916`,
# `fill #user e482916`), and it is the caller's: a stored PIN of 482916 is in it.
TARGET_PLACES = {
    "click": (0,), "hover": (0,), "check": (0,), "uncheck": (0,), "fill": (0,), "select": (0,), "drop": (0,), "eval": (1,),
}


class Own(str):
    """Text the tool wrote itself: a path it made, a port, a window size, code with no text of the caller or of
    the page in it. The check of a command line (`check_command`) does not look at it. Text of the caller or of
    the page is never `Own`, and a call that mixes the two checks the second by hand (`refuse_protected`)."""


# The scrubbers of the scripts that run now, by seat. A scrubber knows the stored secrets and the values a
# script hid, and `Scrubber.holds` says whether a text has one in it. See `protecting`.
_protected = {}
_protected_lock = threading.Lock()


@contextlib.contextmanager
def protecting(seat, scrubber):
    """For the length of the block, no value that `scrubber` holds, in any form it knows, may be in an argument
    of a playwright-cli command of this seat. The scrubber is read at each call, so a value that the script
    hides meanwhile is protected from then on."""
    with _protected_lock:
        before = _protected.get(seat)
        _protected[seat] = scrubber
    try:
        yield
    finally:
        with _protected_lock:
            if before is None:
                _protected.pop(seat, None)
            else:
                _protected[seat] = before


def protect(seat, scrubber):
    """Protect the values of `scrubber` on this seat until `protecting` ends (for a scrubber made late)."""
    with _protected_lock:
        _protected[seat] = scrubber


def holds_protected(seat, text):
    """True when `text` holds a value that must not be in a command line, in any form the scrubber knows."""
    guard = _protected.get(seat)
    return guard is not None and guard.holds(text)


def scrub_protected(seat, text):
    """`text` with each protected value replaced by its marker."""
    guard = _protected.get(seat)
    return guard.scrub(text) if guard is not None else text


def refuse_protected(seat, text):
    """Raise the refusal of a command line when `text`, which will be in one, holds a protected value."""
    if holds_protected(seat, text):
        raise BrowserError(COMMAND_REFUSED)


# What stands in the place of the driver's message on a seat that holds a secret, after the kind of the error.
DRIVER_MESSAGE_WITHHELD = (
    "the driver's message is withheld on a seat that holds secrets; the failure snapshot is attached"
)
# What stands in the place of the text that a message about odd output would quote on such a seat.
EXCERPT_WITHHELD = "(its text is withheld on a seat that holds secrets)"


def holds_secrets(seat):
    """True while a script of `seat` runs that holds a stored secret, or a value it hid (see `protecting`). With no seat
    named, true while any seat does: a text that does not know whose it is stays closed."""
    if seat is None:
        return any(bool(guard) for guard in list(_protected.values()))
    guard = _protected.get(seat)
    return guard is not None and bool(guard)


def driver_error(seat, text):
    """The text of an error of the driver as the caller of `seat` may see it. On a seat that holds a secret no text of the
    driver stays. Playwright prints a preview of each element it names, with its text cut at 50 characters and its
    attributes at 500: in the call log, in the list of a strict-mode violation, and in the first line of some errors
    too (a selector that resolved to the wrong element names that element). A value that is cut like that and written
    under layers of escapes cannot be found by the scrub, so no line of the driver is passed on, not even a short one.
    What stays is the kind of the error, read from a fixed list and never copied: `TimeoutError` or `Error`. The
    caller's own line is in the transcript, and the failure snapshot, scrubbed whole, is attached. The header the driver
    puts first (`### Error`) stays. On a seat that holds no secret the error stays as it is."""
    if not holds_secrets(seat):
        return text
    lines = text.strip().splitlines()
    header = [lines.pop(0)] if lines and lines[0].strip() == "### Error" else []
    while lines and not lines[0].strip():
        lines.pop(0)
    kind = "TimeoutError" if lines and lines[0].lstrip().startswith("TimeoutError") else "Error"
    return "\n".join(header + [f"{kind}: {DRIVER_MESSAGE_WITHHELD}"])


def check_command(seat, args):
    """The last gate before playwright-cli starts. `ps` shows every local user the arguments of every process,
    so a value that is stored for the seat, or that a script hid, must be in none of them. It can come in from
    the caller's text, or from the page: a page can make an id, a class or an address from a value it was
    given. Raises BrowserError, naming nothing, and nothing has run.

    Not looked at: the verb (and a flag before it), text marked `Own`, and a snapshot ref that is not itself the value
    where the verb takes the element it acts on (`TARGET_PLACES`)."""
    guard = _protected.get(seat)
    if guard is None or not guard:
        return
    start = 2 if args and str(args[0]).startswith("--") else 1
    places = TARGET_PLACES.get(str(args[start - 1]) if len(args) >= start else "", ())
    for place, arg in enumerate(args[start:]):
        if isinstance(arg, Own):
            continue
        text = arg if isinstance(arg, str) else str(arg)
        if place in places and REF_SHAPE.fullmatch(text):
            # The number of a ref says nothing, unless the value is that very text.
            if guard.is_value(text):
                raise BrowserError(COMMAND_REFUSED)
            continue
        if guard.holds(text):
            raise BrowserError(COMMAND_REFUSED)


def _offer_install():
    """Offer to install playwright-cli the first time a seat needs it.

    A person at a terminal gets the prompt; a seat woken by a8s has no
    terminal and must fail with the command rather than block on a question
    nobody will answer.
    """
    global _asked
    if _asked:
        return False
    _asked = True

    command = " ".join(INSTALL_CMD)
    interactive = os.environ.get("AR3_BROWSER_INSTALL_PLAYWRIGHT", "").lower() not in ("1", "true", "yes")
    if interactive:
        try:
            terminal = open("/dev/tty", "r+")
        except OSError:
            return False
        with terminal:
            terminal.write(
                f"playwright-cli is not installed, and driving the browser needs it.\n"
                f"Install it now? ({command}) [Y/n] "
            )
            terminal.flush()
            if terminal.readline().strip().lower() not in ("", "y", "yes"):
                return False

    if shutil.which(INSTALL_CMD[0]) is None:
        return False
    print(f"$ {command}", file=sys.stderr)
    # A terminal-approved install inherits the console; opt-in background installs do not.
    subprocess.run(INSTALL_CMD, check=False, **launch_options(interactive=interactive))
    return shutil.which("playwright-cli") is not None


def require():
    """Path to playwright-cli, installing it first if this run may ask."""
    found = shutil.which("playwright-cli")
    if found:
        return found
    if _offer_install():
        return shutil.which("playwright-cli")
    raise BrowserError(
        f"playwright-cli is not on PATH — install it with `{' '.join(INSTALL_CMD)}`"
    )


def _section(output, header):
    """Body of a '### <header>' block, or None. Sections run to the next '### '."""
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"### {header}":
            continue
        body = []
        for follow in lines[index + 1:]:
            if follow.startswith("### "):
                break
            body.append(follow)
        return "\n".join(body).strip()
    return None


def js_string(value):
    """Escape a Python string for a single-quoted JS literal."""
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\0", "")
    )


# The variable that names the directory of the daemon's socket. Unset, the driver puts it under
# `<tmp>/pw-<hash of the user name>`, a name any local user can work out and make first.
IPC_ENV = "PWTEST_SOCKETS_DIR"


# The driver refuses a socket path longer than 103 bytes, and puts its sockets one level below the directory
# (`browser/<16 hex>.sock` at the shortest), so a directory longer than this stops every daemon from starting.
IPC_DIR_MAX_BYTES = 73


def ipc_dir():
    """Where the seat daemons' sockets live: a directory of this user's, 0700. A socket there is a daemon's whole
    command surface (the CLI's, and the browser server's), so no other OS user may reach it. Not caller-set:
    `cli_env` drops any inherited value and names this one.

    It is `ipc` in the browser state root. When that path is too long for a socket, it is a directory named by a
    digest of that path, in this user's `ar3-browser-<uid>` under `$XDG_RUNTIME_DIR`, or under the system temp
    directory when there is none or it is too long too. `private_ipc_dir` refuses either when it is not this user's."""
    home = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "ar3", "browser", "ipc")
    if len(os.fsencode(home)) <= IPC_DIR_MAX_BYTES:
        return home
    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    digest = hashlib.sha256(os.fsencode(home)).hexdigest()[:12]
    for base in (os.environ.get("XDG_RUNTIME_DIR"), tempfile.gettempdir(), "/tmp" if os.name == "posix" else None):
        if base:
            path = os.path.join(base, f"ar3-browser-{uid}", digest)
            if len(os.fsencode(path)) <= IPC_DIR_MAX_BYTES:
                return path
    return path


def _private_level(path):
    """Make `path` a 0700 directory of this user's, or raise BrowserError: a link, a file or another user's
    directory is not one the daemon's socket may land in."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and info.st_uid != os.geteuid()):
        raise BrowserError("the driver's socket directory is not a directory of this user's; nothing was run")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)


def private_ipc_dir():
    """`ipc_dir`, made 0700 when missing and tightened when looser. Raises BrowserError, having run nothing, when it,
    or the per-user directory a short one sits in, is a link, not a directory, or another user's."""
    path = ipc_dir()
    parent = os.path.dirname(path)
    if os.path.basename(parent).startswith("ar3-browser-"):
        _private_level(parent)
    else:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    _private_level(path)
    return path


def cli_env():
    """The environment of every playwright-cli call, and so of the daemon that a call starts: this
    process's, without the switch that lets the driver open local files, and without the variables that move or
    add to what the driver writes (`DRIVER_OUTPUT_ENV`). The tool opens no local file, and the files of the driver are
    where the sweep looks, so a node whose environment sets any of them does not change that for the seat's driver.
    The socket directory is always `ipc_dir`, whatever the caller's environment says (on Windows the driver uses a
    named pipe, and the variable only changes the pipe's name)."""
    env = {
        name: value for name, value in os.environ.items()
        if name.upper() != FILE_ACCESS_ENV and not DRIVER_OUTPUT_ENV.fullmatch(name)
    }
    env[IPC_ENV] = ipc_dir()
    return env


def _driver_options():
    """The options of every run of the driver: its environment, and a umask that keeps what it writes, and what the
    daemon it starts writes, to the node's OS user (mode 0600 for a file, 0700 for a directory)."""
    private_ipc_dir()
    options = {"env": cli_env()}
    if os.name == "posix":
        options["umask"] = DRIVER_UMASK
    return options


def session_name(seat):
    """The playwright-cli session of `seat`. The driver turns every character outside letters, digits and `-` in a
    session name into `-` when it names the daemon's socket, so `a_b`, `a.b` and `a-b` would share one socket, and
    on a case-insensitive file system so would `Work` and `work`. The name is a lowercase slug of the seat, to read
    in `playwright-cli list`, and a digest of the exact seat, to tell seats apart."""
    slug = re.sub(r"[^a-z0-9]+", "-", seat.lower()).strip("-")[:24].strip("-") or "seat"
    return f"{slug}-{hashlib.sha256(seat.encode('utf-8', 'surrogatepass')).hexdigest()[:12]}"


def run(seat, *args, timeout=30):
    """Run one playwright-cli command against this seat's session.

    playwright-cli can print an error block and still exit 0, so a zero status
    is not enough to call a step successful.
    """
    check_command(seat, args)
    cmd = [require(), f"-s={session_name(seat)}", *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **_driver_options(), **launch_options())
    except subprocess.TimeoutExpired:
        raise BrowserError(f"playwright-cli timed out after {timeout}s") from None
    if result.returncode != 0:
        raise BrowserError(driver_error(
            seat, result.stderr.strip() or result.stdout.strip() or "playwright-cli failed"
        ))
    failure = _section(result.stdout, "Error")
    if failure is not None:
        raise BrowserError(driver_error(seat, failure) or "playwright-cli reported an error")
    return result.stdout


def _excerpt(text, limit=200):
    """The first `limit` characters of `text` for a message, ended with an ellipsis when it was cut. A tool that
    cuts text must say so in the way the scrub looks for: a value cut in two is then found by its start."""
    return text[:limit] + ("\u2026" if len(text) > limit else "")


def _shown(seat, text):
    """`'the first 200 characters'` for a message about odd output of the driver or the page, or, on a seat that holds a
    secret, a note that the text is withheld. The scrub finds a value that was cut once and escaped once; a text that the
    page wrote under more layers, and that this cut in two, is not one it can know (see `driver_error`)."""
    return EXCERPT_WITHHELD if holds_secrets(seat) else repr(_excerpt(text))


def result_of(output, seat=None):
    """The '### Result' body; a string result arrives as the string itself.

    playwright-cli prints a string result as a JSON literal, so its newlines,
    quotes and backslashes are escapes until that literal is decoded whole. `seat` is the seat the
    output is of, for the message about output that is not a result.
    """
    body = _section(output, "Result")
    if body is None:
        raise BrowserError(f"unrecognized playwright-cli output: {_shown(seat, output)}")
    if len(body) >= 2 and body[0] == body[-1] == '"':
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass
    if len(body) >= 2 and body[0] == body[-1] and body[0] in ("'", '"'):
        body = body[1:-1]
    return body


def result_or(output):
    """The '### Result' body when there is one, else the output itself."""
    body = _section(output, "Result")
    return body if body is not None else output.strip()


def sessions():
    """Names of the live playwright-cli sessions.

    `list` prints top-level sessions as `- name:` and nests their details
    underneath — an indented `- status: open` is a field, not a session, so
    only column-zero entries count.
    """
    if shutil.which("playwright-cli") is None:
        return []
    try:
        result = subprocess.run(
            ["playwright-cli", "list"], capture_output=True, text=True, timeout=10, **_driver_options(), **launch_options()
        )
    except subprocess.TimeoutExpired:
        return []
    names = []
    for line in result.stdout.splitlines():
        if line.startswith("- ") and ":" in line:
            names.append(line[2:].split(":", 1)[0].strip())
    return names


def is_open(seat):
    return session_name(seat) in sessions()


def evaluate(seat, expression, timeout=30):
    """Evaluate `expression`, the source of a JavaScript expression, in the page. It is an argument of the
    command line, so it is for code of the tool's own (`Own`) and for the caller's `eval` verb. Text of the
    caller or of the page goes through `call_in_page`."""
    return result_of(run(seat, "eval", expression, timeout=timeout), seat)


def parse_json(payload, seat=None):
    """The text of a JSON.stringify result, parsed."""
    try:
        return json.loads(payload)
    except json.JSONDecodeError as err:
        raise BrowserError(f"expected JSON from the page, got {_shown(seat, payload)}") from err


def run_code(seat, body, timeout=60):
    """Execute a statement body with the Playwright `page` in scope.

    The body gets lines of its own: a multi-line body whose last line is a
    `//` comment would otherwise swallow the closing brace. The body is an argument of the
    command line: `Own` code stays `Own`, and any other body is looked at by `check_command`.

    The wrapper is a convenience, not a sandbox — a body can close the
    function and keep writing, and running arbitrary code is the point of the
    verb. The seat's opt-in is the boundary; nothing here contains anything.
    """
    text = f"async function f(page) {{\n{body}\n}}"
    return run(seat, "run-code", Own(text) if isinstance(body, Own) else text, timeout=timeout)


class OriginRefused(BrowserError):
    """The field sits in a document whose origin the secret may not be typed on."""

    def __init__(self, origin):
        super().__init__(f"origin {origin} is not allowed")
        self.origin = origin


def fill_secret_code(selector, value, origins):
    """The code `fill_secret` runs, as the text of one async function.

    It asks the field for the origin of its own document, compares it with
    `origins` (dicts of scheme, host, port and wildcard, as `secretstore` makes
    them), and only then fills. A wildcard host matches any subdomain of its
    domain and not the domain itself. The function returns `{refused: origin}` or
    `{typed: origin}`, and nothing else.
    """
    return (
        "async (page) => {\n"
        f"  const allowed = {json.dumps(origins)};\n"
        f"  const target = page.locator({json.dumps(selector)});\n"
        "  const seen = await target.evaluate("
        f"(el) => el.ownerDocument.location.origin, undefined, {{timeout: {FILL_TIMEOUT_MS}}});\n"
        "  let url = null;\n"
        "  try { url = new URL(seen); } catch (error) {}\n"
        "  const ok = url !== null && allowed.some((o) =>\n"
        "    url.protocol === o.scheme + ':' && url.port === o.port &&\n"
        "    (o.wildcard ? url.hostname.endsWith('.' + o.host) : url.hostname === o.host));\n"
        "  if (!ok) return {refused: seen};\n"
        f"  await target.fill({json.dumps(value)}, {{timeout: {FILL_TIMEOUT_MS}}});\n"
        "  return {typed: seen};\n"
        "}\n"
    )


def _fill_outcome(output):
    """What the fill code returned: {"typed": origin} or {"refused": origin}."""
    lines = [line for line in output.splitlines() if line.strip()]
    try:
        outcome = json.loads(lines[-1])
    except (IndexError, ValueError):
        outcome = None
    if not isinstance(outcome, dict) or not isinstance(outcome.get("typed", outcome.get("refused")), str):
        raise BrowserError("fill-secret: playwright-cli returned no outcome")
    return outcome


def fill_secret(seat, selector, value, origins, directory, timeout=30):
    """Type `value` into the field `selector` names, on a page whose origin is one
    of `origins`, with the value in no argv. Returns the origin it typed on.
    Raises OriginRefused, having typed nothing, for any other origin.

    `fill` takes its text as an argument, and `ps` shows every local user the
    arguments of every process. `run-code --filename` takes the code from a file
    instead: the CLI sends the daemon only the path, and the daemon reads the
    file. The code holds the value as a literal, because the scope the code runs
    in has no `require` and no `fs` to read a data file with.

    The file is made 0600 in a 0700 directory of its own under `directory`, and
    removed when the call returns. `--raw` drops the "Ran Playwright code"
    section of the reply, which would echo the code with the value in it. An
    error can still carry the value: Playwright prints it in the call log of a
    failed `fill`. The caller scrubs what it keeps.

    The origin check runs in the same file, right before the fill, on the origin
    of the field's own document, so a cross-origin iframe counts as its own
    origin and not as the page around it. Between the check and the fill there
    is a short window, two calls to the browser a few milliseconds apart. A page
    that moved the field's frame to another origin in exactly that window could
    have the value typed into the new document. That is acceptable. The check
    stops the case that matters, a sender or an instruction hidden in a page that
    opens another site and names a secret there, and it stops it every time. The
    page that could win the race already controls its tab, a ref from the old
    page no longer resolves in the new one, and closing the window would mean
    replacing Playwright's `fill` with a script of ours that runs inside the page.
    """
    code = fill_secret_code(selector, value, origins)
    outcome = _fill_outcome(_run_private_file(seat, code, directory, SECRET_DIR_PREFIX, "fill.js", timeout))
    if "refused" in outcome:
        raise OriginRefused(outcome["refused"])
    return outcome["typed"]


def _run_private_file(seat, code, directory, prefix, filename, timeout):
    """Run `code` (the text of one async function) with `playwright-cli --raw run-code
    --filename`, from a file made 0600 in a 0700 directory of its own under `directory`, and
    removed when the call returns. The text is in no argv, so `ps` shows nothing of it and its
    length is not bound by the size of an argument. Returns the call's output."""
    folder = tempfile.mkdtemp(prefix=prefix, dir=directory)
    try:
        target = os.path.join(folder, filename)
        aside = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
        descriptor = os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(code)
        os.replace(aside, target)
        return run(seat, "--raw", "run-code", Own(f"--filename={target}"), timeout=timeout)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def run_code_file(seat, code, directory, timeout=60):
    """Run `code`, the text of one async function that takes `page`, from a private file (see
    `_run_private_file`). Returns the call's output: the value the function returned, as one line
    of JSON, or nothing for `undefined`. `result_json` reads it."""
    return _run_private_file(seat, code, directory, CODE_DIR_PREFIX, "code.js", timeout)


def result_json(output):
    """The value a `--raw run-code` call returned: its last non-empty line, parsed as JSON. None
    when it returned nothing."""
    # Only a newline ends a line. JSON.stringify leaves U+2028 and U+2029 in a string, and a page can put them there.
    lines = [line for line in output.split("\n") if line.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except ValueError:
        raise BrowserError("playwright-cli returned a value that is not JSON") from None


def literal(value):
    """`value` as a JavaScript string literal that holds its JSON. The code parses it with
    JSON.parse, so a value is data, and it can not end the literal or become code."""
    return json.dumps(json.dumps(value))


def evaluate_code(function, values):
    """The file `call_in_page` and the facade's `evaluate` run. `function` is the source of a JavaScript
    function, the tool's or a recipe's own code; the page calls it with `values` as its arguments. The
    values travel as the argument of Playwright's `evaluate`, so the page receives them as data."""
    return (
        "async (page) => {\n"
        f"  const values = JSON.parse({literal(list(values))});\n"
        "  const result = await page.evaluate((values) => (\n"
        f"{function}\n"
        "  )(...values), values);\n"
        "  return {value: result};\n"
        "}\n"
    )


def call_in_page(seat, function, values, directory, timeout=30):
    """Call `function` in the page with `values` as its arguments, and return what it returns (JSON data).
    `function` is the source of a function that the tool wrote. `values` are the text of the caller or of the
    page: they, and the function, travel in a private file (see `_run_private_file`), so none of it is in a
    command line, whatever it holds."""
    outcome = result_json(_private_call(seat, evaluate_code(function, values), directory, timeout))
    return outcome.get("value") if isinstance(outcome, dict) else None


def _private_call(seat, code, directory, timeout):
    """`run_code_file`, with a file that cannot be written reported as a failure of the step."""
    try:
        return run_code_file(seat, code, directory, timeout=timeout)
    except OSError as exc:
        raise BrowserError(f"cannot write its private code file: {exc.strerror}") from None


def wait_code(selector, timeout_ms):
    """The file `wait_for_selector` runs: wait until the first match of the CSS selector is visible. The
    selector is data (`literal`), never code."""
    return (
        "async (page) => {\n"
        f"  const request = JSON.parse({literal([selector, timeout_ms])});\n"
        "  await page.waitForSelector(request[0], {state: 'visible', timeout: request[1]});\n"
        "  return {value: true};\n"
        "}\n"
    )


def wait_for_selector(seat, selector, seconds, directory):
    """Wait up to `seconds` for a visible match of the CSS selector, with the selector in a private file."""
    _private_call(seat, wait_code(selector, int(seconds * 1000)), directory, seconds + 10)


def screenshot(seat, path, timeout=60, full_page=False):
    """Save a screenshot. A bare positional is an element selector, not a path. `full_page` takes the
    whole scrollable page, not the part in view."""
    flags = [Own("--full-page")] if full_page else []
    output = run(seat, "screenshot", Own(f"--filename={path}"), *flags, timeout=timeout)
    if not os.path.exists(path):
        raise BrowserError(f"screenshot reported success but wrote no file: {_shown(seat, output)}")
    return path
