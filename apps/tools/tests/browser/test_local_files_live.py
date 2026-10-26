"""Local files, in a real headless Chrome with a real playwright-cli. Live: needs Chrome, Node and
playwright-cli, and runs only when AR3_TOOL_BROWSER_LIVE=1. State lives in temp XDG directories, and the
"other seat" whose store the chain tries to read is a synthetic marker file in the temp state root, never a
real store.

The reviewed chain: `go` to a local HTML page of the outside, click a link on it that leads to the state marker
of another seat, then `text body` and `snap`. It needs a driver that opens local files, which playwright-cli
does when its daemon starts with PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS=1.

- The first test sets the switch in the environment of the caller, as a node can. The tool takes it out of the
  environment of every driver call, so the driver it starts has none, `go` refuses the page, and an http page
  still works.
- The second test starts the driver some other way, with the switch, and leaves the seat on the outside page.
  The control shows that the driver would show the marker. Then the chain runs through the tool: the step
  that ends on a local file fails, the page is sent to about:blank, and no content leaves.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from site_fixture import Site

pytestmark = pytest.mark.skipif(
    os.environ.get("AR3_TOOL_BROWSER_LIVE") != "1",
    reason="live: set AR3_TOOL_BROWSER_LIVE=1",
)

TOOL = Path(__file__).resolve().parents[2] / "browser"
SWITCH = "PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS"
MARKER = "SYNTHETIC-PROTECTED-MARKER-live-6b27d0"
LOCAL = "local files are not opened"


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if k not in (
        "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "A8S_TURN_RECIPIENT", "AR3_TOOL_REPLY_DIR", "TELL_OUTBOX_DIR",
        "AR3_BROWSER_ALLOW_EVAL", SWITCH,
    )}
    env.update(
        XDG_DATA_HOME=str(tmp_path / "data"), XDG_CACHE_HOME=str(tmp_path / "cache"),
        A8S_HOME=str(tmp_path / "a8s"), NO_UPDATE_NOTIFIER="1",
    )
    env.update(extra)
    return env


def _do(env, seat, script, timeout=180):
    done = subprocess.run(
        [sys.executable, str(TOOL), "--seat", seat, "do", "-", "--json"],
        env=env, input=script, capture_output=True, text=True, timeout=timeout,
    )
    return done, json.loads(done.stdout.strip().splitlines()[-1])


def _tool(env, seat, *args, timeout=120):
    return subprocess.run(
        [sys.executable, str(TOOL), *args, "--seat", seat], env=env, capture_output=True, text=True, timeout=timeout,
    )


def _cli(env, seat, *args, timeout=60):
    """playwright-cli by hand, the way an operator, or a tool of an older version, starts and drives a driver.
    It runs in a directory of its own: the driver writes its page files into the directory it runs in."""
    here = Path(env["XDG_DATA_HOME"]).parent / "by-hand"
    here.mkdir(exist_ok=True)
    return subprocess.run(
        ["playwright-cli", f"-s={seat}", *args], env=env, capture_output=True, text=True, timeout=timeout, cwd=here,
    )


def _daemon_pids(seat):
    done = subprocess.run(["pgrep", "-f", f"cliDaemon[.]js {seat}( |$)"], capture_output=True, text=True)
    return done.stdout.split()


def _daemon_has_the_switch(seat):
    """True or False for the daemon of the seat, from its own environment; None where /proc has none."""
    pids = _daemon_pids(seat)
    assert pids, f"no playwright daemon runs for seat {seat}"
    environment = Path(f"/proc/{pids[0]}/environ")
    if not environment.exists():
        return None
    return any(entry.startswith(SWITCH.encode() + b"=") for entry in environment.read_bytes().split(b"\0"))


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


def _gone(profile, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        listing = subprocess.run(
            ["pgrep", "-af", "--", f"--user-data-dir={profile}"], capture_output=True, text=True
        ).stdout.strip()
        if not listing:
            return True
        time.sleep(0.25)
    return False


def _stage(tmp_path):
    """The state marker of another seat, and a local page of the outside that links to it."""
    other = tmp_path / "data/ar3/browser/other-seat"
    other.mkdir(parents=True)
    protected = other / "state-marker.txt"
    protected.write_text(MARKER + "\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    page = outside / "page.html"
    page.write_text(f'<html><body><h1>Outside page</h1><a id="l" href="file://{protected}">protected</a></body></html>')
    return protected, page


def test_with_the_switch_in_the_callers_environment_the_chain_is_refused_and_an_http_page_still_works(tmp_path):
    seat = f"ar3scratch{os.getpid()}files"
    protected, page = _stage(tmp_path)
    env = _env(tmp_path, **{SWITCH: "1"})
    home = tmp_path / "data/ar3/browser" / seat
    site = Site()
    seen = []
    try:
        # The chain, from its first step. `go` refuses the page of the outside, and nothing after it runs.
        done, payload = _do(env, seat, f"go file://{page}\nclick protected\ntext body\nsnap\n")
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 1
        assert len(payload["steps"]) == 1 and payload["steps"][0]["command"] == f"go file://{page}"
        assert LOCAL in payload["steps"][0]["error"]

        # An http page still works: the same seat, the same Chrome.
        done, payload = _do(env, seat, f"go {site.base}/\ntext h1\nassert-text Another site\nsnap\nshot\n")
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert [step["output"] for step in payload["steps"][:2]][1] == "Another site"
        assert [path for path in payload["files"] if path.endswith("snapshot.txt")]
        assert [path for path in payload["files"] if path.endswith("screen.png")]

        # The switch was in the caller's environment, and the driver the tool started has none.
        has = _daemon_has_the_switch(seat)
        if has is not None:
            assert has is False, "the playwright daemon of the seat started with the switch"

        # A script that goes to the page by any route comes back with nothing.
        done, payload = _do(env, seat, f"go FILE://{page}\ngo file:{page}\n")
        assert LOCAL in payload["steps"][0]["error"]

        assert _tool(env, seat, "close").returncode == 0
        assert _gone(home / "chrome")
        everything = "\n".join(seen)
        assert MARKER not in everything
        assert _files_holding([home / "artifacts", home / "scratch", tmp_path / "cache"], MARKER) == []
        assert protected.read_text().strip() == MARKER, "the marker is untouched"
    finally:
        site.stop()
        _tool(env, seat, "close")
        _gone(home / "chrome")


def test_a_driver_started_with_the_switch_some_other_way_shows_no_local_page_through_the_tool(tmp_path):
    seat = f"ar3scratch{os.getpid()}drivers"
    protected, page = _stage(tmp_path)
    env = _env(tmp_path)
    switched = _env(tmp_path, **{SWITCH: "1"})
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    seen = []
    try:
        # Open the seat through the tool, then replace its driver by one started by hand with the switch. The
        # Chrome stays up when the driver goes: it is attached, not owned.
        done, payload = _do(env, seat, "open\n")
        assert done.returncode == 0, payload
        assert _daemon_has_the_switch(seat) in (False, None)
        assert _cli(env, seat, "close").returncode == 0
        port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
        attached = _cli(switched, seat, "attach", f"--cdp=http://127.0.0.1:{port}")
        assert attached.returncode == 0, (attached.stdout, attached.stderr)
        assert _daemon_has_the_switch(seat) in (True, None), "the driver started by hand has the switch"

        # The control: this driver opens the outside page and the marker itself. Without the layers of the
        # tool, the chain reads the marker, so the chain below proves something.
        assert _cli(switched, seat, "goto", f"file://{page}").returncode == 0
        control = _cli(switched, seat, "goto", f"file://{protected}")
        assert control.returncode == 0, (control.stdout, control.stderr)
        reading = _cli(switched, seat, "eval", "document.body.innerText")
        assert MARKER in reading.stdout, "the control: this driver shows the marker"

        # The chain through the tool, from the outside page.
        assert _cli(switched, seat, "goto", f"file://{page}").returncode == 0
        done, payload = _do(env, seat, "click protected\ntext body\nsnap\nshot\n")
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 1
        assert len(payload["steps"]) == 1, "the script stops at the first failure"
        assert payload["steps"][0]["error"] == (
            f"click: the page is a local file, and {LOCAL}. The page was sent to about:blank."
        )
        done, payload = _do(env, seat, "url\n")
        seen.append(done.stdout + done.stderr)
        assert payload["steps"][0]["output"] == "about:blank"

        # A seat left on the marker itself: the steps that read it fail, and none returns or attaches a byte.
        assert _cli(switched, seat, "goto", f"file://{protected}").returncode == 0
        for line in ("text body", "snap", "shot", "find MARKER", "url", "console"):
            assert _cli(switched, seat, "goto", f"file://{protected}").returncode == 0
            done, payload = _do(env, seat, line + "\n")
            seen.append(done.stdout + done.stderr)
            assert done.returncode == 1, line
            assert LOCAL in payload["steps"][0]["error"], line
            assert not [path for path in payload["files"] if path.endswith(("snapshot.txt", "screen.png", "console.log"))], line
            assert "output" not in payload["steps"][0], line

        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
        everything = "\n".join(seen)
        assert MARKER not in everything
        assert _files_holding([home / "artifacts", home / "scratch", tmp_path / "cache"], MARKER) == []
    finally:
        _tool(env, seat, "close")
        _gone(profile)


def _devtools(port, path, method="GET"):
    """One request to Chrome's DevTools endpoint on the loopback, with no proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method), timeout=10) as answer:
        return json.loads(answer.read().decode("utf-8")) if path.startswith("/json/new") or path == "/json" else None


def test_a_local_page_in_another_tab_fails_the_step_and_the_tab_is_closed(tmp_path):
    """A tab opened by hand, or left from before, beside the page the driver drives: the endpoint lists it, and the
    step that finds it fails, sends the driven page away and closes the tab."""
    seat = f"ar3scratch{os.getpid()}tabs"
    protected, _page = _stage(tmp_path)
    env = _env(tmp_path)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    site = Site()
    seen = []
    try:
        done, payload = _do(env, seat, f"go {site.base}/\nurl\n")
        assert done.returncode == 0, payload
        port = (profile / "DevToolsActivePort").read_text().splitlines()[0]

        opened = _devtools(port, f"/json/new?file://{protected}", method="PUT")
        assert opened["type"] == "page" and opened["url"].startswith("file://")
        pages = [target["url"] for target in _devtools(port, "/json") if target["type"] == "page"]
        assert any(url.startswith("file://") for url in pages) and any(url.startswith("http://") for url in pages)

        done, payload = _do(env, seat, "url\n")
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 1
        assert payload["steps"][0]["error"] == (
            f"url: the page is a local file, and {LOCAL}. The page was sent to about:blank. 1 other local page was closed."
        )
        pages = [target["url"] for target in _devtools(port, "/json") if target["type"] == "page"]
        assert pages and not any(url.startswith("file:") for url in pages), pages
        for path in payload["files"]:
            assert MARKER.encode() not in Path(path).read_bytes()

        # The seat is whole again: the next script runs, on a blank page.
        done, payload = _do(env, seat, "url\n")
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0 and payload["steps"][0]["output"] == "about:blank"

        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
        assert MARKER not in "\n".join(seen)
        assert _files_holding([home / "artifacts", home / "scratch", tmp_path / "cache"], MARKER) == []
    finally:
        site.stop()
        _tool(env, seat, "close")
        _gone(profile)
