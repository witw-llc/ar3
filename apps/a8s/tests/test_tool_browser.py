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

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from commands import cmd_add
from core import Participant, outbox_dir
from daemon import attached_loop
from definitions import default_definition_path, resolve_definition_arg
from mailbox import _write_outbox, ensure_mailboxes
from registry import load_registry, save_registry

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stub playwright-cli is a POSIX script")

TOOL = Path(__file__).resolve().parents[2] / "tools" / "browser"

STUB = """#!/bin/sh
# Stand-in for playwright-cli: one open session, one page.
case "$1" in
  list) printf -- '- stub:\\n  - status: open\\n' ;;
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
        for flag in ("--allow-eval=$ALLOW_EVAL?", "--chrome=$CHROME?", "--port=$PORT?"):
            assert flag in argv
        assert definition["max_wake_seconds"] == 600
        assert definition["stop"]["invoke"][2] == "close"


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
