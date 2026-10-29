"""The idle verb closes a real Chrome only when the seat has gone unused. Live: needs Chrome and playwright-cli."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AR3_TOOL_BROWSER_LIVE") != "1",
    reason="live: set AR3_TOOL_BROWSER_LIVE=1",
)

TOOL = Path(__file__).resolve().parents[2] / "browser"


def _chrome_up(profile):
    """True when the browser process of `profile` runs: it carries the profile flag and no `--type=`. With the pipe
    transport Chrome writes no `DevToolsActivePort`, so the process is the sign it started."""
    out = subprocess.run(["pgrep", "-af", "--", f"--user-data-dir={profile}"], capture_output=True, text=True)
    return any(" --type=" not in f" {line}" for line in out.stdout.splitlines())


def _profile_processes(profile):
    out = subprocess.run(
        ["pgrep", "-af", "--", f"--user-data-dir={profile}"],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def _gone(profile, seconds=10):
    """True once no process holds the profile; the OS needs a moment after a quit."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _profile_processes(profile):
            return True
        time.sleep(0.25)
    return not _profile_processes(profile)


def _env(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env.update(XDG_DATA_HOME=str(tmp_path / "data"), A8S_HOME=str(tmp_path / "a8s"))
    return env


def _tool(env, seat, *args, timeout=90):
    return subprocess.run(
        [sys.executable, str(TOOL), *args, "--seat", seat],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def test_idle_leaves_a_used_seat_and_closes_an_unused_one(tmp_path):
    seat = f"ar3scratch{os.getpid()}idle"
    env = _env(tmp_path)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    try:
        opened = _tool(env, seat, "open")
        assert opened.returncode == 0, (opened.stdout, opened.stderr)
        assert _profile_processes(profile)
        assert (home / "last-active").is_file()

        # Negative control: a long timeout against a seat used a moment ago.
        young = _tool(env, seat, "idle", "--timeout", "3600")
        assert young.returncode == 0, (young.stdout, young.stderr)
        assert "not idle" in young.stdout, young.stdout
        assert _profile_processes(profile)
        assert _tool(env, seat, "status").stdout.startswith(f"{seat}: attached")

        # Positive control: the same seat, a short timeout, after the wait.
        time.sleep(2.5)
        old = _tool(env, seat, "idle", "--timeout", "2")
        assert old.returncode == 0, (old.stdout, old.stderr)
        assert f"{seat}: closed after " in old.stdout, old.stdout
        assert _gone(profile), _profile_processes(profile)
        assert not (home / "stop-epoch").exists()

        # A second check finds no Chrome and says nothing.
        quiet = _tool(env, seat, "idle", "--timeout", "2")
        assert (quiet.returncode, quiet.stdout, quiet.stderr) == (0, "", "")

        # An idle close is not a stop: the next script opens Chrome again.
        again = _tool(env, seat, "open")
        assert again.returncode == 0, (again.stdout, again.stderr)
        assert _profile_processes(profile)
    finally:
        _tool(env, seat, "close")
        _gone(profile)


def test_idle_never_cuts_off_a_running_script(tmp_path):
    seat = f"ar3scratch{os.getpid()}busy"
    env = _env(tmp_path)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    script = tmp_path / "script"
    script.write_text("open\nwait 8\nurl\n")
    out = open(tmp_path / "out", "w")
    err = open(tmp_path / "err", "w")
    runner = subprocess.Popen(
        [sys.executable, str(TOOL), "--seat", seat, "do", str(script), "--json"],
        env=env, stdout=out, stderr=err,
    )
    try:
        deadline = time.monotonic() + 40
        while not _chrome_up(profile) and time.monotonic() < deadline:
            assert runner.poll() is None, (tmp_path / "out").read_text()
            time.sleep(0.2)
        assert _chrome_up(profile)
        time.sleep(1.5)
        assert runner.poll() is None

        busy = _tool(env, seat, "idle", "--timeout", "1")
        assert busy.returncode == 0, (busy.stdout, busy.stderr)
        assert busy.stdout == f"{seat}: busy, not idle\n"
        assert _profile_processes(profile)

        runner.wait(timeout=60)
        payload = json.loads((tmp_path / "out").read_text().strip().splitlines()[-1])
        assert runner.returncode == 0 and payload["ok"] is True, payload
        assert [step["ok"] for step in payload["steps"]] == [True, True, True]
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
        out.close()
        err.close()
        _tool(env, seat, "close")
        _gone(profile)
