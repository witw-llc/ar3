"""playwright-cli wrapper.

Everything that knows the CLI's wire format lives here: its markdown-ish
sections, its habit of reporting failure while exiting 0, and the argument
shapes that differ from what the verb names suggest.
"""
import json
import os
import shutil
import subprocess
import sys

INSTALL_CMD = ["npm", "install", "-g", "@playwright/cli"]
_asked = False


class BrowserError(Exception):
    pass


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
    if os.environ.get("AR3_BROWSER_INSTALL_PLAYWRIGHT", "").lower() not in ("1", "true", "yes"):
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
    subprocess.run(INSTALL_CMD, check=False)
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


def run(seat, *args, timeout=30):
    """Run one playwright-cli command against this seat's session.

    playwright-cli can print an error block and still exit 0, so a zero status
    is not enough to call a step successful.
    """
    cmd = [require(), f"-s={seat}", *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BrowserError(f"playwright-cli timed out after {timeout}s") from None
    if result.returncode != 0:
        raise BrowserError(
            result.stderr.strip() or result.stdout.strip() or "playwright-cli failed"
        )
    failure = _section(result.stdout, "Error")
    if failure is not None:
        raise BrowserError(failure or "playwright-cli reported an error")
    return result.stdout


def result_of(output):
    """The '### Result' body; a string result arrives as the string itself.

    playwright-cli prints a string result as a JSON literal, so its newlines,
    quotes and backslashes are escapes until that literal is decoded whole.
    """
    body = _section(output, "Result")
    if body is None:
        raise BrowserError(f"unrecognized playwright-cli output: {output[:200]!r}")
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
            ["playwright-cli", "list"], capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        return []
    names = []
    for line in result.stdout.splitlines():
        if line.startswith("- ") and ":" in line:
            names.append(line[2:].split(":", 1)[0].strip())
    return names


def is_open(seat):
    return seat in sessions()


def evaluate(seat, expression, timeout=30):
    return result_of(run(seat, "eval", expression, timeout=timeout))


def parse_json(payload):
    """The text of a JSON.stringify result, parsed."""
    try:
        return json.loads(payload)
    except json.JSONDecodeError as err:
        raise BrowserError(f"expected JSON from the page, got {payload[:200]!r}") from err


def evaluate_json(seat, expression, timeout=30):
    return parse_json(evaluate(seat, f"JSON.stringify({expression})", timeout=timeout))


def run_code(seat, body, timeout=60):
    """Execute a statement body with the Playwright `page` in scope.

    The body gets lines of its own: a multi-line body whose last line is a
    `//` comment would otherwise swallow the closing brace.

    The wrapper is a convenience, not a sandbox — a body can close the
    function and keep writing, and running arbitrary code is the point of the
    verb. The seat's opt-in is the boundary; nothing here contains anything.
    """
    return run(seat, "run-code", f"async function f(page) {{\n{body}\n}}", timeout=timeout)


def screenshot(seat, path, timeout=60):
    """Save a screenshot. A bare positional is an element selector, not a path."""
    output = run(seat, "screenshot", f"--filename={path}", timeout=timeout)
    if not os.path.exists(path):
        raise BrowserError(f"screenshot reported success but wrote no file: {output[:200]!r}")
    return path
