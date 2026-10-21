import json
import os
import subprocess
import sys

import pytest

from gemini_web import browser_seat as browser


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def payload(**kwargs):
    body = {"ok": True, "steps": [], "files": []}
    body.update(kwargs)
    return json.dumps(body)


def test_outputs_keep_script_order_and_empty_results(tmp_path):
    run = browser.Transcript(
        True,
        [
            {"command": "snap", "ok": True, "output": "/tmp/snap.txt"},
            {"command": "text main", "ok": True, "output": ""},
            {"command": "text article", "ok": True, "output": "second"},
        ],
        [],
    )
    assert run.outputs("text") == ["", "second"]
    assert run.outputs("snap") == ["/tmp/snap.txt"]
    assert run.outputs("click") == []


def test_a_failed_step_names_itself_and_drops_out_of_the_outputs():
    run = browser.Transcript(
        False,
        [
            {"command": "text main", "ok": True, "output": "kept"},
            {"command": "click Rename", "ok": False, "error": "nothing visible matching"},
        ],
        [],
    )
    assert run.outputs("text") == ["kept"]
    assert run.outputs("click") == []
    assert "nothing visible matching" in run.error


def test_a_run_goes_to_the_named_seat_with_the_script_on_stdin(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return FakeProc(stdout=payload(steps=[{"command": "url", "ok": True, "output": "x"}]))

    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    run = browser.BrowserSeat("profile").run("url")
    assert seen["argv"] == [sys.executable, browser.BROWSER_TOOL, "-s", "profile", "do", "-", "--json"]
    assert seen["input"] == "url"
    assert run.outputs("url") == ["x"]


def test_a_failed_script_comes_back_as_a_transcript_not_an_exception(monkeypatch):
    """`do` exits 1 for a script that failed a step, and that transcript is
    exactly what the caller has to read."""
    steps = [{"command": "click Rename", "ok": False, "error": "no match"}]
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda argv, **kw: FakeProc(stdout=payload(ok=False, steps=steps), returncode=1),
    )
    run = browser.BrowserSeat("profile").run("click Rename")
    assert not run.ok
    assert run.error == "no match"


def test_noise_around_the_payload_is_ignored(monkeypatch):
    stdout = "npm notice: update available\n" + payload(steps=[]) + "\n"
    monkeypatch.setattr(browser.subprocess, "run", lambda argv, **kw: FakeProc(stdout=stdout))
    assert browser.BrowserSeat("profile").run("url").ok


def test_an_answer_with_no_run_in_it_names_the_seat(monkeypatch):
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda argv, **kw: FakeProc(stderr="Chrome would not start", returncode=2),
    )
    with pytest.raises(browser.BrowserError) as exc:
        browser.BrowserSeat("profile").run("url")
    assert "profile" in str(exc.value)
    assert "Chrome would not start" in str(exc.value)


def test_the_browser_tool_is_the_package_inside_the_suite():
    assert browser.BROWSER_TOOL.endswith(os.path.join("tools", "browser"))
    assert os.path.isfile(os.path.join(browser.BROWSER_TOOL, "__main__.py"))


def test_a_browser_tool_that_cannot_start_is_a_browser_error(monkeypatch):
    def fake_run(argv, **kwargs):
        raise OSError("no such interpreter")

    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    with pytest.raises(browser.BrowserError) as exc:
        browser.BrowserSeat("profile").run("url")
    assert "no such interpreter" in str(exc.value)


def test_a_seat_that_never_answers_is_an_error_naming_the_wait(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 300)

    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    with pytest.raises(browser.BrowserError) as exc:
        browser.BrowserSeat("profile", timeout=300).run("url")
    assert "300s" in str(exc.value)
