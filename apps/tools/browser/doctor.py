"""What a seat needs before it can be registered, and whether this box has it."""
import os
import shutil
import subprocess

from ar3.playwright_cli import version_note
from ar3.proc import launch_options

from . import plc
from . import session


def _checks():
    chrome = session.chrome_path()
    yield (
        bool(chrome) and os.path.exists(chrome),
        f"chrome: {chrome}"
        if chrome
        else "chrome: not found — install it, or set the CHROME var or AR3_BROWSER_CHROME",
    )
    playwright = shutil.which("playwright-cli")
    yield (
        bool(playwright),
        f"playwright-cli: {playwright}" if playwright
        else "playwright-cli: not found — the first browser command offers to "
        f"run `{' '.join(plc.INSTALL_CMD)}`",
    )


def _version_warning():
    """A sentence when the installed playwright-cli is not the release the tool is proven against. It is a note,
    never a failure: the tool still runs."""
    playwright = shutil.which("playwright-cli")
    if not playwright:
        return None
    try:
        done = subprocess.run([playwright, "--version"], capture_output=True, text=True, timeout=10, **launch_options())
    except (OSError, subprocess.SubprocessError):
        return None
    return version_note(done.stdout) if done.returncode == 0 else None


def report():
    print(f"state: {session.state_root()}")
    failures = 0
    for ok, line in _checks():
        print(f"  {'ok  ' if ok else 'MISSING'} {line}")
        failures += 0 if ok else 1
    warning = _version_warning()
    if warning:
        print(f"  note {warning}; install it with `{' '.join(plc.INSTALL_CMD)}`")
    return 1 if failures else 0
