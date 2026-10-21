"""What a seat needs before it can be registered, and whether this box has it."""
import os
import shutil

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


def report():
    print(f"state: {session.state_root()}")
    failures = 0
    for ok, line in _checks():
        print(f"  {'ok  ' if ok else 'MISSING'} {line}")
        failures += 0 if ok else 1
    return 1 if failures else 0
