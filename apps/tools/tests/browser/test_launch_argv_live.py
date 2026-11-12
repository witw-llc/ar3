"""The Chrome a seat really starts carries no flag outside the set the tool asked for. Live: needs Chrome and
playwright-cli, and runs only when AR3_TOOL_BROWSER_LIVE=1.

playwright-cli adds Chrome flags of its own (`--disable-blink-features=AutomationControlled`, which makes Chrome
show a warning bar unless `--test-type` is also set). A mock of playwright-cli cannot show what it adds, so this
reads the browser process's argv from the process table after a real launch.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from browser import session

pytestmark = pytest.mark.skipif(
    os.environ.get("AR3_TOOL_BROWSER_LIVE") != "1",
    reason="live: set AR3_TOOL_BROWSER_LIVE=1",
)

TOOL = Path(__file__).resolve().parents[2] / "browser"


def _need(condition, reason):
    if not condition:
        pytest.skip(reason)


def _browser_argv(profile):
    """The argument list of the seat's browser process (the one with the profile flag and no `--type=`), as `ps`
    prints it. None when no such process runs."""
    flag = "-lf" if sys.platform == "darwin" else "-af"
    out = subprocess.run(["pgrep", flag, "--", f"--user-data-dir={profile}"], capture_output=True, text=True)
    for line in out.stdout.splitlines():
        _pid, _, command = line.partition(" ")
        if "--user-data-dir=" in command and " --type=" not in f" {command}":
            return command.split()
    return None


def test_the_chrome_a_seat_starts_has_no_flag_the_seat_did_not_ask_for(tmp_path, monkeypatch):
    _need(shutil.which("playwright-cli"), "playwright-cli is not installed")
    _need(session.chrome_path() and os.path.exists(session.chrome_path()), "Chrome is not installed")
    _need(shutil.which("pgrep"), "pgrep is not available, so the browser process cannot be read")
    _need(sys.platform != "win32", "no process of a seat can be read on Windows")
    seat = f"ar3scratch{os.getpid()}argv"
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env.update(XDG_DATA_HOME=str(tmp_path / "data"), A8S_HOME=str(tmp_path / "a8s"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("DISPLAY", raising=False)
    profile = session.profile_dir(seat)

    def tool(*args):
        return subprocess.run([sys.executable, str(TOOL), *args, "--seat", seat],
                              env=env, capture_output=True, text=True, timeout=90)

    try:
        opened = tool("open")
        assert opened.returncode == 0, (opened.stdout, opened.stderr)
        argv = _browser_argv(profile)
        assert argv, "the seat's browser process was not found"
        flags = [a for a in argv[1:] if a.startswith("--")]
        # Positive controls: this is the process we meant, and the two flags that matter are in it.
        assert "--test-type=webdriver" in flags, argv
        assert "--disable-blink-features=AutomationControlled" in flags, argv
        launch = session._launch_args(seat)
        asked = {a.split("=", 1)[0] for a in launch if a.startswith("--")}
        allowed_exact = set(session.PLAYWRIGHT_ADDED_FLAGS)
        if "--headless=new" in launch:
            allowed_exact.update(session.CHROME_HEADLESS_ADDED_FLAGS)
        stray = [f for f in flags if f.split("=", 1)[0] not in asked and f not in allowed_exact]
        assert stray == [], argv
        assert "did not ask for" not in opened.stderr, opened.stderr
    finally:
        tool("close")
