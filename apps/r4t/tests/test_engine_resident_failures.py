from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from engines import resident


@pytest.mark.parametrize("frame,expected", [
    ({"type": "result", "subtype": "error_during_execution", "is_error": True,
      "errors": ["Not authenticated", "Please log in"]}, "Not authenticated\nPlease log in"),
    ({"type": "result", "subtype": "error_max_turns", "is_error": False,
      "errors": []}, "error_max_turns"),
    ({"type": "result", "subtype": "error_max_budget_usd", "errors": []},
     "error_max_budget_usd"),
    ({"type": "result", "subtype": "error_max_structured_output_retries", "errors": []},
     "error_max_structured_output_retries"),
    ({"type": "result", "subtype": "success", "is_error": True,
      "result": "Not authenticated"}, "Not authenticated"),
    ({"type": "result", "is_error": True}, "Claude result reported an error"),
])
def test_claude_result_failure_is_explicit_and_fatal(frame, expected):
    parsed = resident._parse_claude_frame(frame)
    assert parsed.error_text == expected
    assert parsed.fatal
    assert parsed.turn_completed
    assert parsed.result_text == ""
    assert parsed.assistant_text == ""


@pytest.mark.parametrize("frame", [
    {"type": "result", "subtype": "success", "is_error": False,
     "errors": [], "result": "  completed  "},
    {"type": "result", "result": "completed"},
    {"type": "result", "subtype": "success", "is_error": False,
     "errors": ["non-fatal diagnostic"], "result": "completed"},
])
def test_claude_success_remains_a_clean_completion(frame):
    parsed = resident._parse_claude_frame(frame)
    assert parsed.result_text == "completed"
    assert parsed.turn_completed
    assert not parsed.fatal
    assert not parsed.error_text


def test_claude_tool_failure_is_not_a_session_result():
    parsed = resident._parse_claude_frame({
        "type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "tool failed"},
        ]},
    })
    assert not parsed.fatal
    assert not parsed.turn_completed


@pytest.fixture
def launch_spy(monkeypatch):
    calls = []

    def refuse(argv, **kwargs):
        calls.append((argv, kwargs))
        raise OSError("test launch boundary")

    monkeypatch.setattr(resident.subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    return calls


def _spec(tmp_path: Path, engine: str) -> dict:
    return {
        "engine": engine, "dir_path": str(tmp_path), "agent": "test-seat",
        "template": ([engine, "exec", "{prompt}"] if engine in ("codex", "muse")
                     else ["devin", "acp"] if engine == "devin"
                     else [engine, "-p", "{prompt}"]), "timeout": 0.1,
        "scaffold": False, "memory": "off",
    }


@pytest.mark.parametrize("windows", [True, False])
def test_bridge_launch_has_no_incompatible_detached_flag(monkeypatch, tmp_path, launch_spy, windows):
    monkeypatch.setattr(resident, "IS_WINDOWS", windows)
    assert resident.enqueue_or_start(
        "claude", "hello", dir_path=tmp_path,
        template=["claude", "-p", "{prompt}"], model=None, effort=None,
        agent="test-seat", timeout=30, scaffold=False,
    ) == 1
    assert len(launch_spy) == 1
    _, kwargs = launch_spy[0]
    if windows:
        assert kwargs["creationflags"] == 0x08000200
        assert not kwargs.get("start_new_session", False)
    else:
        assert kwargs["start_new_session"] is True
        assert kwargs.get("creationflags", 0) == 0


@pytest.mark.parametrize("engine", sorted(resident.RESIDENT_ENGINES))
@pytest.mark.parametrize("windows", [True, False])
def test_every_resident_vendor_launch_suppresses_windows_console(
    monkeypatch, tmp_path, launch_spy, engine, windows,
):
    from engines import run

    monkeypatch.setattr(resident, "IS_WINDOWS", windows)
    monkeypatch.setattr(run, "resolve_argv0", lambda argv: argv)
    home = tmp_path / "resident"
    (home / "inbox").mkdir(parents=True)
    assert resident._run_session(home, _spec(tmp_path, engine), threading.Event(), {}) == 1
    assert len(launch_spy) == 1
    _, kwargs = launch_spy[0]
    if windows:
        assert kwargs["creationflags"] == 0x08000000
        assert not kwargs.get("start_new_session", False)
    else:
        assert kwargs.get("creationflags", 0) == 0
        assert kwargs.get("start_new_session", False) == (engine in ("cursor", "devin", "muse"))


@pytest.mark.parametrize("probe", [resident.codex_app_server_probe, resident.cursor_acp_probe, resident.devin_acp_probe])
def test_resident_handshake_probe_suppresses_windows_console(monkeypatch, tmp_path, launch_spy, probe):
    monkeypatch.setattr(resident, "IS_WINDOWS", True)
    result = probe(["fake-vendor"], tmp_path)
    assert "test launch boundary" in result["error"]
    assert len(launch_spy) == 1
    assert launch_spy[0][1]["creationflags"] == 0x08000000


@pytest.mark.parametrize("exit_after_result", [False, True])
def test_real_claude_error_result_cannot_capture_successful_memory(
    monkeypatch, tmp_path, capsys, exit_after_result,
):
    from engines import run

    fake = tmp_path / "fake.py"
    fake.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    print(json.dumps({'type':'result', 'subtype':'error_during_execution', "
        "'is_error':True, 'errors':['Not authenticated']}), flush=True)\n"
        f"    if {exit_after_result!r}: sys.exit(0)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run, "resolve_argv0", lambda argv: [sys.executable, str(fake)])
    monkeypatch.setattr(resident, "POLL_SECONDS", 0.01)
    home = tmp_path / "resident"
    (home / "inbox").mkdir(parents=True)
    resident._enqueue_message(home, "hello")
    memory = tmp_path / "memory"
    spec = _spec(tmp_path, "claude")
    spec.update(memory="on", memory_home=str(memory))
    stop = threading.Event()
    watchdog_fired = threading.Event()

    def abort_broken_session():
        watchdog_fired.set()
        stop.set()

    watchdog = threading.Timer(5, abort_broken_session)
    watchdog.daemon = True
    watchdog.start()
    try:
        code = resident._run_session(home, spec, stop, {})
    finally:
        watchdog.cancel()
    assert not watchdog_fired.is_set(), "error result left the session running"
    assert code != 0
    output = capsys.readouterr().out
    assert "error: Not authenticated" in output
    assert "resident engine cannot run this session; ending it" in output
    turns = list(memory.rglob("turns/*.json"))
    assert len(turns) == 1
    assert json.loads(turns[0].read_text())["exit"] == 1
    assert list(memory.rglob("queue/*.json")) == []


def test_muse_probe_suppresses_windows_console(monkeypatch, tmp_path, launch_spy):
    from ar3.proc import launch_options
    from engines import muse_resident
    from engines.muse_resident import probe_muse
    monkeypatch.setattr(muse_resident, "launch_options",
                        lambda **kw: launch_options(windows=True, **kw))
    result = probe_muse(["fake-vendor", "serve"], tmp_path, {})
    assert "test launch boundary" in result["error"]
    assert len(launch_spy) == 1
    flags = launch_spy[0][1]["creationflags"]
    assert flags & 0x08000000
    assert not flags & 0x00000008
