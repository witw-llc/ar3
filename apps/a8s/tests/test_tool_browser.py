"""The bundled `tool-browser` definition, driven through real routing.

`a8s add <name> <dir> tool-browser --ALLOW=...` yields a working node: the
definition invokes the a8s-sibling package at `$A8S_DIR/../tools/browser`. A
stub `playwright-cli` on PATH stands in for the browser, so a script reply
comes back through the same outbox path `tell` uses. The ACL is the required
`$ALLOW` var; a refused sender gets no reply.

One live test drives a real Chrome and is skipped unless
`AR3_TOOL_BROWSER_LIVE=1`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from commands import cmd_add
from core import Participant, agent_log_path, outbox_dir, read_last_wake, touch_last_active
from daemon import attached_loop, maybe_run_idle
from definitions import default_definition_path, resolve_definition_arg
from mailbox import _write_outbox, ensure_mailboxes
from registry import load_registry, save_registry

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stub playwright-cli is a POSIX script")

TOOL = Path(__file__).resolve().parents[2] / "tools" / "browser"

# The driver session of seat `stub` (browser/plc.py `session_name`): a slug and a digest of the seat.
STUB_SESSION = "stub-" + hashlib.sha256(b"stub").hexdigest()[:12]
STUB = """#!/bin/sh
# Stand-in for playwright-cli: one open session, one page.
case "$1" in
  list) printf -- '- """ + STUB_SESSION + """:\\n  - status: open\\n' ;;
  *)
    shift
    case "$1" in
      run-code) printf '### Result\\n"{\\\\"url\\\\":\\\\"about:blank\\\\",\\\\"visible\\\\":\\\\"visible\\\\",\\\\"window\\\\":7,\\\\"state\\\\":\\\\"normal\\\\"}"\\n' ;;
      eval) printf '### Result\\nhttps://stub.example/\\n' ;;
      *) printf '### Result\\nok\\n' ;;
    esac ;;
esac
"""


def _read_envelopes(directory: Path) -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(directory.glob("*.json"))]


def _setup(tmp_path, monkeypatch, allow: str):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "playwright-cli"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    a_root = tmp_path / "a"
    b_root = tmp_path / "b"
    a_root.mkdir()
    b_root.mkdir()
    save_registry({
        "a": {"root": str(a_root), "definition": str(default_definition_path("filedrop"))},
        "stub": {
            "root": str(b_root),
            "definition": str(default_definition_path("tool-browser")),
            "vars": {"ALLOW": allow},
        },
    })
    ensure_mailboxes(Participant("a", a_root))
    ensure_mailboxes(Participant("stub", b_root))
    return a_root, b_root


class TestAddBuiltin:
    def test_bare_name_resolves_bundled(self, fake_home, tmp_path):
        root = tmp_path / "probe"
        root.mkdir()
        assert cmd_add(["probe", str(root), "tool-browser", "--ALLOW=a"]) == 0
        assert load_registry()["probe"]["definition"] == str(default_definition_path("tool-browser"))

    def test_definition_requires_allow_var(self):
        path = resolve_definition_arg("tool-browser")
        definition = json.loads(path.read_text())
        argv = definition["invoke"]
        assert "$ALLOW" in argv
        assert "$ALLOW?" not in argv
        for flag in ("--allow-eval=$ALLOW_EVAL?", "--chrome=$CHROME?", "--cdp=$CDP?", "--profile=$PROFILE?"):
            assert flag in argv
        assert not any("PORT" in arg for arg in argv)
        assert definition["max_wake_seconds"] == 600
        assert definition["stop"]["invoke"][2] == "close"
        assert "--cdp=$CDP?" in definition["stop"]["invoke"]
        assert "--profile=$PROFILE?" in definition["stop"]["invoke"]
        assert definition["idle"] == {
            "timeout": "$TIMEOUT?",
            "invoke": [
                "$PYTHON", "$A8S_DIR/../tools/browser", "idle", "--seat", "$RECIPIENT",
                "--timeout=$TIMEOUT?", "--chrome=$CHROME?", "--cdp=$CDP?", "--profile=$PROFILE?",
            ],
        }


class TestWake:
    def test_script_reply_comes_back_with_the_transcript(self, fake_home, tmp_path, monkeypatch):
        a_root, b_root = _setup(tmp_path, monkeypatch, "a")
        _write_outbox("a", a_root, "stub", "url", [])

        assert attached_loop(["a", "stub"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(b_root))
        assert len(replies) == 1
        assert replies[0]["to"] == "a"
        assert replies[0]["content"].startswith("stub: ok")
        assert "https://stub.example/" in replies[0]["content"]

    def test_refused_sender_gets_no_reply(self, fake_home, tmp_path, monkeypatch):
        a_root, b_root = _setup(tmp_path, monkeypatch, "someone-else")
        _write_outbox("a", a_root, "stub", "url", [])

        assert attached_loop(["a", "stub"], 0.1, single_pass=True) == 0
        assert _read_envelopes(outbox_dir(b_root)) == []


RECIPE = (
    "from browser.recipe import Recipe\n"
    "recipe = Recipe('mine', 'The own recipe of the node', origins=['https://mine.example.com'])\n"
    "recipe.steps('go', help='Go.', steps='go https://mine.example.com/')\n"
)


class TestSiteRecipes:
    """The wake passes the node's name as the seat. The browser tool finds the registered node of that
    name in the a8s registry, and loads the recipes the operator keeps in the node's root. A message only
    names a recipe."""

    def test_a_wake_loads_the_recipes_directory_of_the_node_it_runs_for(self, fake_home, tmp_path, monkeypatch):
        a_root, b_root = _setup(tmp_path, monkeypatch, "a")
        monkeypatch.delenv("AR3_TOOL_REPLY_DIR", raising=False)
        (b_root / "recipes").mkdir()
        (b_root / "recipes" / "mine.py").write_text(RECIPE)
        _write_outbox("a", a_root, "stub", "recipe", [])

        assert attached_loop(["a", "stub"], 0.1, single_pass=True) == 0
        (reply,) = _read_envelopes(outbox_dir(b_root))
        assert reply["content"].startswith("stub: ok")
        assert "mine  The own recipe of the node  [node]" in reply["content"]
        assert sorted(path.name for path in (b_root / "recipes").iterdir()) == ["mine.py"]

    def test_a_node_without_recipes_lists_none(self, fake_home, tmp_path, monkeypatch):
        a_root, b_root = _setup(tmp_path, monkeypatch, "a")
        monkeypatch.delenv("AR3_TOOL_REPLY_DIR", raising=False)
        _write_outbox("a", a_root, "stub", "recipe", [])

        assert attached_loop(["a", "stub"], 0.1, single_pass=True) == 0
        (reply,) = _read_envelopes(outbox_dir(b_root))
        assert reply["content"].startswith("stub: ok") and "[node]" not in reply["content"]

    def test_a_script_that_failed_still_ends_the_wake_with_exit_zero(self, fake_home, tmp_path, monkeypatch):
        a_root, b_root = _setup(tmp_path, monkeypatch, "a")
        monkeypatch.delenv("AR3_TOOL_REPLY_DIR", raising=False)
        _write_outbox("a", a_root, "stub", "recipe nosuchsite", [])

        assert attached_loop(["a", "stub"], 0.1, single_pass=True) == 0
        (reply,) = _read_envelopes(outbox_dir(b_root))
        assert reply["content"].startswith("stub: failed: unknown recipe: nosuchsite")
        # a8s logs a wake that exited with another code, and dead-letters its message.
        assert "(exit" not in agent_log_path("stub").read_text(encoding="utf-8")


class TestSeatLock:
    def test_a_wake_meets_a_held_seat_and_replies_busy(self, fake_home, tmp_path, monkeypatch):
        import fcntl

        from ar3.proc import process_start_token

        a_root, b_root = _setup(tmp_path, monkeypatch, "a")
        monkeypatch.setenv("AR3_BROWSER_SEAT_WAIT", "0.5")
        lock = tmp_path / "xdg" / "ar3" / "browser" / "stub" / "lock"
        lock.parent.mkdir(parents=True)
        pid = os.getpid()
        lock.write_text(json.dumps({
            "pid": pid, "start": process_start_token(pid), "kind": "ar3 tool",
            "sender": "operator", "since": "2026-10-23 09:00:00 PDT",
        }))
        _write_outbox("a", a_root, "stub", "url", [])
        with open(lock, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            assert attached_loop(["a", "stub"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(b_root))
        assert len(replies) == 1
        assert replies[0]["content"].startswith("stub: busy: seat held by ar3 tool from operator (pid ")
        assert replies[0]["content"].endswith("Nothing ran.")


@pytest.mark.skipif(
    os.environ.get("AR3_TOOL_BROWSER_LIVE") != "1",
    reason="drives a real Chrome and playwright-cli; set AR3_TOOL_BROWSER_LIVE=1 "
    "(with DISPLAY for a headed seat, and node/playwright-cli on PATH)",
)
def test_live_open_go_snap_close(tmp_path):
    env = dict(os.environ, XDG_DATA_HOME=str(tmp_path / "xdg"), AR3_TOOL_REPLY_DIR=str(tmp_path / "out"))
    script = "open\ngo https://example.com\nsnap\nclose\n"
    try:
        result = subprocess.run(
            [sys.executable, str(TOOL), "handle", "--seat", "ar3livetest",
             "--from", "a", "--allow", "a", "--", script],
            env=env, capture_output=True, text=True, timeout=180,
        )
    finally:
        subprocess.run([sys.executable, str(TOOL), "close", "--seat", "ar3livetest"],
                       env=env, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "ar3livetest: ok" in result.stdout
    snapshots = list((tmp_path / "out").glob("*-snapshot.txt"))
    assert snapshots and "Example Domain" in snapshots[0].read_text()
    assert shutil.which("pgrep") is None or subprocess.run(
        ["pgrep", "-f", "ar3/browser/ar3livetest"], capture_output=True
    ).returncode == 1


def _seat_home(tmp_path):
    return tmp_path / "xdg" / "ar3" / "browser" / "stub"


def _stamp_seat(tmp_path, age_seconds):
    """The seat's last-active stamp, as a script that ended `age_seconds` ago
    leaves it. Written aside and renamed, so the idle verb never reads half."""
    home = _seat_home(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    aside = home / f".last-active.{os.getpid()}.{threading.get_ident()}.tmp"
    aside.write_text(str(time.time() - age_seconds), encoding="utf-8")
    os.replace(aside, home / "last-active")


def _stand_in_chrome(tmp_path):
    """A process the seat reads as its Chrome: it carries the seat's profile
    flag and no `--type=`. No browser starts."""
    profile = _seat_home(tmp_path) / "chrome"
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", f"--user-data-dir={profile}"],
        start_new_session=True,
    )


def _idle_wake(tmp_path, monkeypatch, timeout="100"):
    """A registered tool-browser node named `stub` whose idle check is due: its
    last wake ended ten minutes ago. `fire()` runs that one check, as the
    daemon's idle pass does, and returns the log. It does not run the whole
    loop: the loop's last act is the stop hook, which closes Chrome too."""
    _, b_root = _setup(tmp_path, monkeypatch, "a")
    registry = load_registry()
    if timeout is not None:
        registry["stub"]["vars"]["TIMEOUT"] = timeout
    save_registry(registry)

    def fire():
        touch_last_active("stub", datetime.now(timezone.utc) - timedelta(seconds=600))
        maybe_run_idle(Participant("stub", b_root))
        log = agent_log_path("stub")
        return log.read_text(encoding="utf-8") if log.is_file() else ""

    return fire


@pytest.mark.skipif(shutil.which("pgrep") is None, reason="the seat finds its Chrome with pgrep")
class TestIdle:
    """a8s fires the browser tool's `idle` verb once the node is quiet, and the
    verb closes the seat's Chrome only when no script used the seat for
    TIMEOUT seconds. The Chrome here is a stand-in process; the verb, the
    argv a8s builds for it and the close path are the real ones."""

    def test_timeout_is_the_idle_timeout_and_an_unset_one_is_none(self):
        from definitions import build_idle_command, idle_timeout_seconds

        definition = json.loads(resolve_definition_arg("tool-browser").read_text())
        assert idle_timeout_seconds(definition, {"ALLOW": "a"}) is None
        node_vars = {"ALLOW": "a", "TIMEOUT": "300", "CHROME": "/c/chrome"}
        assert idle_timeout_seconds(definition, node_vars) == 300.0
        argv = build_idle_command(definition, "garmin", vars=node_vars)
        assert argv[2:] == ["idle", "--seat", "garmin", "--timeout=300", "--chrome=/c/chrome"]

    def test_a_seat_used_recently_keeps_its_chrome(self, fake_home, tmp_path, monkeypatch):
        fire = _idle_wake(tmp_path, monkeypatch, timeout="100")
        chrome = _stand_in_chrome(tmp_path)
        try:
            _stamp_seat(tmp_path, age_seconds=5)
            log = fire()
            assert "idle exec:" in log and "idle wake failed" not in log
            assert "not idle" in log
            assert chrome.poll() is None
        finally:
            chrome.kill()
            chrome.wait()

    def test_a_seat_idle_for_the_timeout_loses_its_chrome(self, fake_home, tmp_path, monkeypatch):
        fire = _idle_wake(tmp_path, monkeypatch, timeout="100")
        chrome = _stand_in_chrome(tmp_path)
        try:
            _stamp_seat(tmp_path, age_seconds=500)
            log = fire()
            assert "idle wake failed" not in log
            assert "closed after 5" in log
            assert chrome.wait(timeout=20) is not None
            assert read_last_wake("stub") is True
        finally:
            if chrome.poll() is None:
                chrome.kill()
            chrome.wait()

    def test_a_seat_with_no_activity_record_counts_as_idle(self, fake_home, tmp_path, monkeypatch):
        fire = _idle_wake(tmp_path, monkeypatch, timeout="100")
        chrome = _stand_in_chrome(tmp_path)
        try:
            log = fire()
            assert "closed, idle with no activity record" in log
            assert chrome.wait(timeout=20) is not None
        finally:
            if chrome.poll() is None:
                chrome.kill()
            chrome.wait()

    def test_a_node_without_timeout_never_fires_idle(self, fake_home, tmp_path, monkeypatch):
        fire = _idle_wake(tmp_path, monkeypatch, timeout=None)
        chrome = _stand_in_chrome(tmp_path)
        try:
            log = fire()
            assert "idle exec:" not in log
            assert chrome.poll() is None
        finally:
            chrome.kill()
            chrome.wait()

    def test_a_seat_with_no_chrome_idles_in_silence(self, fake_home, tmp_path, monkeypatch):
        fire = _idle_wake(tmp_path, monkeypatch, timeout="100")
        log = fire()
        assert "idle exec:" in log and "idle wake failed" not in log
        assert "closed" not in log and "not idle" not in log
        assert read_last_wake("stub") is True
        assert not _seat_home(tmp_path).exists()


class TestHelpWake:
    @pytest.mark.parametrize("spelling", ["help", "/help", "--help", "/?"])
    def test_help_answers_the_usage(self, fake_home, tmp_path, monkeypatch, spelling):
        a_root, b_root = _setup(tmp_path, monkeypatch, "a")
        _write_outbox("a", a_root, "stub", spelling, [])

        assert attached_loop(["a", "stub"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(b_root))
        assert len(replies) == 1
        assert replies[0]["to"] == "a"
        assert replies[0]["content"].startswith("stub: tool-browser \u2014 usage")
        assert "README: apps/tools/browser/README.md" in replies[0]["content"]

    def test_a_refused_sender_gets_no_help(self, fake_home, tmp_path, monkeypatch):
        a_root, b_root = _setup(tmp_path, monkeypatch, "someone-else")
        _write_outbox("a", a_root, "stub", "help", [])

        assert attached_loop(["a", "stub"], 0.1, single_pass=True) == 0
        assert _read_envelopes(outbox_dir(b_root)) == []


@pytest.mark.skipif(
    os.environ.get("AR3_TOOL_BROWSER_LIVE") != "1",
    reason="drives a real Chrome and playwright-cli; set AR3_TOOL_BROWSER_LIVE=1",
)
def test_live_ar3_tool_session_on_a_registered_node(tmp_path):
    """`ar3 tool browser --node` drives a headless seat through a piped
    session: four replies, artifacts, and no Chrome left on the profile."""
    name = f"ar3scratch{os.getpid()}"
    home = tmp_path / "a8s"
    home.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (home / "a8s.json").write_text(json.dumps({"agents": {name: {
        "root": str(root),
        "definition": str(Path(TOOL).parent.parent / "a8s" / "definitions" / "tool-browser.json"),
        "vars": {"ALLOW": "nobody"},
    }}, "aliases": {}, "namespaces": {}}))
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env.update(A8S_HOME=str(home), XDG_DATA_HOME=str(tmp_path / "xdg"))
    ar3_cli = Path(TOOL).parent.parent / "ar3" / "cli.py"
    try:
        result = subprocess.run(
            [sys.executable, str(ar3_cli), "tool", "browser", "--node", name],
            input="open\ngo https://example.com\nsnap\nclose\n",
            env=env, capture_output=True, text=True, timeout=240,
        )
    finally:
        subprocess.run([sys.executable, str(TOOL), "close", "--seat", name],
                       env=env, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count(f"{name}: ok") == 4
    assert "artifact: " in result.stdout
    artifacts = [Path(line[len("artifact: "):]) for line in result.stdout.splitlines()
                 if line.startswith("artifact: ")]
    assert any("Example Domain" in p.read_text() for p in artifacts if p.name.endswith("snapshot.txt"))
    assert shutil.which("pgrep") is None or subprocess.run(
        ["pgrep", "-f", f"ar3/browser/{name}"], capture_output=True
    ).returncode == 1
