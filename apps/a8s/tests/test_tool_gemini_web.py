"""The bundled `tool-gemini-web` definition, driven through real routing.

`a8s add <name> <dir> tool-gemini-web --ALLOW=... --BROWSER_SEAT=...` yields a
working node: the definition invokes the a8s-sibling package at
`$A8S_DIR/../tools/gemini_web`. A `sitecustomize` on the wake's PYTHONPATH
stands in for the browser: it answers the one `subprocess.run` that would start
the browser tool with the tools suite's fake Gemini seat, so no Chrome and no
network are involved and the reply still comes back through the outbox path
`tell` uses.

The exit-code tests settle what a8s does with a non-zero wake exit: it dead-
letters the envelope at once and never retries it, which is why this tool exits
0 on every path.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from commands import cmd_add
from core import Participant, agent_log_path, dead_letters_dir, inbox_dir, outbox_dir
from daemon import attached_loop
from definitions import default_definition_path, resolve_definition_arg
from mailbox import _write_outbox, ensure_mailboxes
from registry import load_registry, save_registry

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the gemini-web tool keeps its state with fcntl locks")

APPS = Path(__file__).resolve().parents[2]
TOOL = APPS / "tools" / "gemini_web"
FAKE_SEAT_DIR = APPS / "tools" / "tests" / "gemini_web"

SITECUSTOMIZE = '''
import json, os, subprocess, sys, tempfile

_real_run = subprocess.run
_seat = None


def _run(argv, *args, **kwargs):
    if isinstance(argv, list) and len(argv) > 3 and argv[1].endswith(os.path.join("tools", "browser")):
        if os.environ.get("STUB_BROWSER") == "down":
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="Chrome would not start")
        global _seat
        if _seat is None:
            sys.path[:0] = [{fake_dir!r}, {tools_dir!r}]
            from gemini_web import gemini
            from gw_support import FakeClock, FakeGeminiSeat

            clock = FakeClock()
            gemini._now, gemini._sleep = clock.now, clock.sleep
            _seat = FakeGeminiSeat(tempfile.mkdtemp(), reply=lambda prompt: "pong")
        run = _seat.run(kwargs["input"])
        payload = {{"ok": run.ok, "steps": run.steps, "files": run.files}}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload) + "\\n", stderr="")
    return _real_run(argv, *args, **kwargs)


subprocess.run = _run
'''


def _read_envelopes(directory: Path) -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(directory.glob("*.json"))]


def _setup(tmp_path, monkeypatch, allow="a", browser_seat="ar3-test-gw-seat", definition=None):
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        SITECUSTOMIZE.format(fake_dir=str(FAKE_SEAT_DIR), tools_dir=str(APPS / "tools"))
    )
    monkeypatch.setenv("PYTHONPATH", str(site))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    a_root = tmp_path / "a"
    b_root = tmp_path / "b"
    a_root.mkdir()
    b_root.mkdir()
    vars_map = {"ALLOW": allow}
    if browser_seat:
        vars_map["BROWSER_SEAT"] = browser_seat
    save_registry({
        "a": {"root": str(a_root), "definition": str(default_definition_path("filedrop"))},
        "gem": {
            "root": str(b_root),
            "definition": definition or str(default_definition_path("tool-gemini-web")),
            "vars": vars_map,
        },
    })
    ensure_mailboxes(Participant("a", a_root))
    ensure_mailboxes(Participant("gem", b_root))
    return a_root, b_root


def _wake_once(a_root):
    _write_outbox("a", a_root, "gem", "Reply with the single word pong.", [])
    assert attached_loop(["a", "gem"], 0.1, single_pass=True) == 0


class TestAddBuiltin:
    def test_bare_name_resolves_bundled(self, fake_home, tmp_path):
        root = tmp_path / "probe"
        root.mkdir()
        assert cmd_add(["probe", str(root), "tool-gemini-web", "--ALLOW=a", "--BROWSER_SEAT=g"]) == 0
        assert load_registry()["probe"]["definition"] == str(default_definition_path("tool-gemini-web"))

    def test_allow_and_browser_seat_are_required_and_the_rest_optional(self):
        definition = json.loads(resolve_definition_arg("tool-gemini-web").read_text())
        argv = definition["invoke"]
        for required in ("$ALLOW", "$BROWSER_SEAT"):
            assert required in argv
            assert required + "?" not in argv
        assert "--model=$MODEL?" in argv
        assert argv[:2] == ["$PYTHON", "$A8S_DIR/../tools/gemini_web"]
        assert definition["max_wake_seconds"] == 900
        assert definition["idle"] == {"timeout": 0}
        assert definition["stop"]["invoke"] == [
            "$PYTHON", "$A8S_DIR/../tools/browser", "close", "--seat", "$BROWSER_SEAT",
        ]

    def test_the_tool_package_exists_where_the_definition_points(self):
        assert (TOOL / "__main__.py").is_file()


class TestWake:
    def test_a_prompt_gets_gemini_s_reply(self, fake_home, tmp_path, monkeypatch):
        a_root, b_root = _setup(tmp_path, monkeypatch)
        _wake_once(a_root)
        replies = _read_envelopes(outbox_dir(b_root))
        assert len(replies) == 1
        assert replies[0]["to"] == "a"
        assert "pong" in replies[0]["content"]
        assert not list(dead_letters_dir("gem").glob("*")) if dead_letters_dir("gem").exists() else True
        assert (tmp_path / "xdg" / "ar3" / "gemini-web" / "gem" / "gem.json").is_file()

    def test_a_refused_sender_gets_nothing_and_is_acked(self, fake_home, tmp_path, monkeypatch):
        a_root, b_root = _setup(tmp_path, monkeypatch, allow="someone-else")
        _wake_once(a_root)
        assert _read_envelopes(outbox_dir(b_root)) == []
        assert not (dead_letters_dir("gem").exists() and list(dead_letters_dir("gem").glob("*")))
        assert not list(inbox_dir("gem").glob("*.json"))

    def test_a_missing_browser_seat_var_aborts_the_wake_with_no_reply(self, fake_home, tmp_path, monkeypatch):
        a_root, b_root = _setup(tmp_path, monkeypatch, browser_seat="")
        _wake_once(a_root)
        assert _read_envelopes(outbox_dir(b_root)) == []
        assert "wake aborted" in agent_log_path("gem").read_text()


class TestExitCodes:
    def test_a_nonzero_wake_exit_is_dead_lettered_and_never_retried(self, fake_home, tmp_path, monkeypatch):
        failing = tmp_path / "fail.json"
        failing.write_text(json.dumps({"invoke": [sys.executable, "-c", "import sys; sys.exit(1)"]}))
        a_root, b_root = _setup(tmp_path, monkeypatch, definition=str(failing))
        _wake_once(a_root)
        log = agent_log_path("gem").read_text()
        assert "wake failed (exit 1)" in log
        assert "1 dead letter(s) left in trash" in log
        assert not list(inbox_dir("gem").glob("*.json"))
        # A second pass finds nothing to retry.
        assert attached_loop(["a", "gem"], 0.1, single_pass=True) == 0
        assert agent_log_path("gem").read_text().count("wake failed") == 1

    def test_a_browser_that_cannot_run_still_exits_0_and_asks_for_the_message_again(
        self, fake_home, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("STUB_BROWSER", "down")
        a_root, b_root = _setup(tmp_path, monkeypatch)
        _wake_once(a_root)
        replies = _read_envelopes(outbox_dir(b_root))
        assert len(replies) == 1
        assert "Send it again" in replies[0]["content"]
        assert "Chrome would not start" in replies[0]["content"]
        assert "wake failed" not in agent_log_path("gem").read_text()
        assert not list(inbox_dir("gem").glob("*.json"))
