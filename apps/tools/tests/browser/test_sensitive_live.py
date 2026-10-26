"""A sensitive step in a real headless Chrome, through `browser do`: the fixture recipe's `guarded` verb types a
value that no scrub knows (the node's setting, written backwards, as a passcode is one digit to a box) into the
sign-in page of the fixture site, and ends the way `--how` says. Live: needs Chrome, Node and playwright-cli,
and runs only when AR3_TOOL_BROWSER_LIVE=1. State lives in temp XDG directories.

- fine: the cleanup confirms (it loads the page again and reads the field empty). Chrome stays up, and no
  file the tool keeps holds the value.
- fail: the step fails with a driver message that holds the value. The error that leaves is one line, no
  failure snapshot is attached, and no file holds the value. Chrome stays up: the cleanup confirmed.
- stuck: the cleanup cannot confirm. Chrome is closed before the failure is reported, the error says so, and
  no failure snapshot is taken.
- exposed: the control. The same failure with no sensitive step. The error and the failure snapshot hold the
  value, so the checks above see what they claim to see.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from site_fixture import NODE, Site

pytestmark = pytest.mark.skipif(
    os.environ.get("AR3_TOOL_BROWSER_LIVE") != "1",
    reason="live: set AR3_TOOL_BROWSER_LIVE=1",
)

TOOL = Path(__file__).resolve().parents[2] / "browser"
VALUE = "86753091"
TYPED = VALUE[::-1]
GENERIC = "fixture guarded: the passcode step failed"


def _env(tmp_path, base):
    env = {k: v for k, v in os.environ.items() if k not in (
        "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "A8S_TURN_RECIPIENT", "AR3_TOOL_REPLY_DIR", "TELL_OUTBOX_DIR",
        "AR3_BROWSER_ALLOW_EVAL", "FIXTURE_BASE", "FIXTURE_ENTRY_USER", "FIXTURE_ENTRY_PASS",
    )}
    env.update(
        XDG_DATA_HOME=str(tmp_path / "data"), XDG_CACHE_HOME=str(tmp_path / "cache"),
        A8S_HOME=str(tmp_path / "a8s"), NO_UPDATE_NOTIFIER="1", FIXTURE_BASE=base, FIXTURE_ENTRY_USER=VALUE,
    )
    return env


def _do(env, seat, script, timeout=180):
    done = subprocess.run(
        [sys.executable, str(TOOL), "--seat", seat, "do", "-", "--json", "--recipes", str(NODE / "recipes")],
        env=env, input=script, capture_output=True, text=True, timeout=timeout,
    )
    return done, json.loads(done.stdout.strip().splitlines()[-1])


def _close(env, seat):
    return subprocess.run(
        [sys.executable, str(TOOL), "close", "--seat", seat], env=env, capture_output=True, text=True, timeout=120,
    )


def _running(profile):
    listing = subprocess.run(
        ["pgrep", "-af", "--", f"--user-data-dir={profile}"], capture_output=True, text=True
    ).stdout.strip()
    return bool(listing)


def _gone(profile, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _running(profile):
            return True
        time.sleep(0.25)
    return False


def _files_holding(roots, value):
    found = []
    for root in roots:
        for directory, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(directory, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                try:
                    if value.encode() in Path(path).read_bytes():
                        found.append(path)
                except OSError:
                    continue
    return found


def test_a_sensitive_step_in_a_real_chrome(tmp_path):
    seat = f"ar3scratch{os.getpid()}guard"
    site = Site()
    env = _env(tmp_path, site.base)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    kept = [home / "artifacts", home / "scratch", tmp_path / "cache"]
    seen = []
    try:
        # fine: the cleanup confirms, Chrome stays, no file holds the value.
        done, payload = _do(env, seat, "recipe fixture guarded --how fine\n")
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert payload["steps"][0]["output"].splitlines()[-1] == "typed and cleared"
        assert "  fill_hidden #user -> #user <- [hidden]" in payload["steps"][0]["output"].splitlines()
        assert _running(profile), "a step that ended clear leaves Chrome alone"
        assert _files_holding(kept, TYPED) == [], "a page file of the step survived it"

        # fail: one line leaves, no snapshot is attached, Chrome stays, no file holds the value.
        done, payload = _do(env, seat, "recipe fixture guarded --how fail\n")
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 1
        assert payload["steps"][0]["error"] == GENERIC
        # The cleanup confirmed the page clear, so the snapshot is taken, of the page it left: the field is empty.
        (failure,) = [path for path in payload["files"] if path.endswith("failure.txt")]
        assert "textbox" in Path(failure).read_text() and TYPED not in Path(failure).read_text()
        assert _running(profile)
        assert _files_holding(kept, TYPED) == []

        # stuck: the cleanup cannot confirm, so Chrome is closed before the failure is reported.
        done, payload = _do(env, seat, "recipe fixture guarded --how stuck\n")
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 1
        assert payload["steps"][0]["error"] == GENERIC + ". The page could not be confirmed clear, so Chrome was closed."
        assert _gone(profile), "Chrome was not closed"
        assert [path for path in payload["files"] if path.endswith("failure.txt")] == []
        assert _files_holding(kept, TYPED) == []

        # The seat works on afterwards: the next script opens Chrome again, signed-in state and all.
        done, payload = _do(env, seat, "recipe fixture guarded --how fine\n")
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert _running(profile)

        # exposed, the control: the same failure with no sensitive step shows the value in the error and in
        # the failure snapshot, and in a page file. The checks above can see what they claim to see.
        done, payload = _do(env, seat, "recipe fixture guarded --how exposed\n")
        assert done.returncode == 1
        assert TYPED in payload["steps"][0]["error"], "the control: the driver's message holds the value"
        failure = [path for path in payload["files"] if path.endswith("failure.txt")]
        assert failure and TYPED in Path(failure[0]).read_text(), "the control: the failure snapshot shows the value"
        assert _files_holding(kept, TYPED), "the control: a page file holds the value"

        assert _close(env, seat).returncode == 0
        assert _gone(profile)
        everything = "\n".join(seen)
        assert TYPED not in everything and VALUE not in everything
    finally:
        site.stop()
        _close(env, seat)
        _gone(profile)
