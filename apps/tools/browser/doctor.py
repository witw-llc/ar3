"""What a seat needs before it can be registered, and whether this box has it."""
import os
import shutil
import subprocess

from ar3.playwright_cli import installed_version
from ar3.proc import launch_options

from . import plc
from . import session


def _playwright_version(playwright):
    try:
        done = subprocess.run([playwright, "--version"], capture_output=True, text=True, timeout=10, **launch_options())
    except (OSError, subprocess.SubprocessError):
        return None
    return installed_version(done.stdout) if done.returncode == 0 else None


def _checks():
    target = session.cdp_target()
    if target:
        try:
            endpoint = session._resolve_cdp_endpoint(target)
            yield True, f"cdp: {target} ({endpoint})"
        except plc.BrowserError as exc:
            yield False, f"cdp: {exc}"
    else:
        chrome = session.chrome_path()
        yield (
            bool(chrome) and os.path.exists(chrome),
            f"chrome: {chrome}"
            if chrome
            else "chrome: not found — install it, or set the CHROME var or AR3_BROWSER_CHROME",
        )
    playwright = shutil.which("playwright-cli")
    if playwright:
        version = _playwright_version(playwright)
        shown = f"{playwright} ({version})" if version else playwright
        yield True, f"playwright-cli: {shown}"
    else:
        yield (
            False,
            "playwright-cli: not found — the first browser command offers to "
            f"run `{' '.join(plc.INSTALL_CMD)}`",
        )


def report():
    print(f"state: {session.state_root()}")
    failures = 0
    for ok, line in _checks():
        print(f"  {'ok  ' if ok else 'MISSING'} {line}")
        failures += 0 if ok else 1
    return 1 if failures else 0
