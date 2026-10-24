import contextlib
import json
import sys

import pytest

from ar3.locking import file_lock
from browser import cli
from browser import commands
from browser import seatlock
from browser import session


def _held(seat):
    stack = contextlib.ExitStack()
    stack.enter_context(file_lock(seatlock.lock_path(seat), blocking=False))
    return stack


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["browser", *argv])
    return cli.main()


def test_close_forces_past_a_held_seat_after_the_stop_wait(monkeypatch, capsys):
    closed = []
    monkeypatch.setattr(session, "close_browser", closed.append)
    with _held("s"):
        assert _run(monkeypatch, "close", "--seat", "s") == 0
    assert closed == ["s"]
    err = capsys.readouterr().err
    assert "s: closing Chrome. The seat is held by " in err


def test_do_json_reports_a_busy_seat_in_the_transcript_shape(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("AR3_BROWSER_SEAT_WAIT", "0.3")
    monkeypatch.setattr(
        commands, "run_script",
        lambda *a, **k: pytest.fail("the script ran on a busy seat"),
    )
    script = tmp_path / "s.txt"
    script.write_text("url\n")
    with _held("s"):
        assert _run(monkeypatch, "--seat", "s", "do", str(script), "--json") == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["files"] == []
    assert out["steps"][0]["command"] == ""
    assert out["steps"][0]["error"].startswith("busy: seat held by ")


def _record_close_order(monkeypatch):
    order = []
    monkeypatch.setattr(session, "bump_stop_epoch", lambda s: order.append("bump"))
    monkeypatch.setattr(session, "close_browser", lambda s: order.append("close"))
    return order


def test_close_bumps_the_stop_epoch_before_it_closes(monkeypatch):
    order = _record_close_order(monkeypatch)
    assert _run(monkeypatch, "close", "--seat", "s") == 0
    assert order == ["bump", "close"]


def test_a_forced_close_bumps_the_stop_epoch_before_it_closes(monkeypatch):
    order = _record_close_order(monkeypatch)
    with _held("s"):
        assert _run(monkeypatch, "close", "--seat", "s") == 0
    assert order == ["bump", "close"]


def test_close_does_not_wait_for_a_held_seat(monkeypatch):
    import time
    order = _record_close_order(monkeypatch)
    started = time.monotonic()
    with _held("s"):
        assert _run(monkeypatch, "close", "--seat", "s") == 0
    assert time.monotonic() - started < 1.5
    assert order == ["bump", "close"]


def _hold_launch_lock_in_a_process(seat, seconds):
    import subprocess, textwrap
    code = textwrap.dedent(f"""
        import sys, time
        from browser import session
        with session.launch_lock({seat!r}):
            print("held", flush=True)
            time.sleep({seconds})
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            env={**__import__("os").environ, "PYTHONPATH": ":".join(sys.path)})
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_close_gives_up_on_a_stalled_launch_lock_and_closes_anyway(monkeypatch, capsys):
    import time
    order = _record_close_order(monkeypatch)
    proc = _hold_launch_lock_in_a_process("s", 15)
    try:
        started = time.monotonic()
        assert _run(monkeypatch, "close", "--seat", "s") == 0
        assert time.monotonic() - started < 4.5
    finally:
        proc.kill()
        proc.wait()
    assert order == ["bump", "close"]
    assert "s: launch lock held past 3 s; closing anyway." in capsys.readouterr().err


def _stop_lands_before_the_spawn(monkeypatch):
    """Chrome is down; a stop bumps the epoch after the entry read it and before the spawn."""
    events = []
    monkeypatch.setattr(session.plc, "is_open", lambda s: False)
    monkeypatch.setattr(session, "_start_chrome", lambda s: events.append("spawn") or "base")
    monkeypatch.setattr(session, "_attach", lambda s, b: events.append("attach"))
    monkeypatch.setattr(session, "_terminate_profile", lambda s: events.append("terminate"))
    monkeypatch.setattr(session, "_before_spawn", lambda: session.bump_stop_epoch("s"))
    return events


@pytest.mark.parametrize("verb", ["open", "snap", "console", "go"])
def test_a_cli_verb_that_began_before_a_stop_does_not_leave_chrome(monkeypatch, capsys, verb):
    events = _stop_lands_before_the_spawn(monkeypatch)
    argv = [verb, "--seat", "s"] + (["https://example.com"] if verb == "go" else [])
    assert _run(monkeypatch, *argv) == 1
    assert "seat closed by a8s stop" in capsys.readouterr().err
    assert events == ["spawn", "terminate"]


def test_a_cli_verb_that_begins_after_a_stop_opens_chrome(monkeypatch, capsys):
    events = []
    session.bump_stop_epoch("s")
    monkeypatch.setattr(session.plc, "is_open", lambda s: bool(events))
    monkeypatch.setattr(session, "_start_chrome", lambda s: events.append("spawn") or "base")
    monkeypatch.setattr(session, "_attach", lambda s, b: None)
    monkeypatch.setattr(session, "status", lambda s: "s: attached")
    assert _run(monkeypatch, "open", "--seat", "s") == 0
    assert events == ["spawn"]


def test_the_only_spawn_site_is_inside_the_guarded_opener():
    import ast
    import pathlib
    package = pathlib.Path(session.__file__).parent
    sites = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(func):
                    if (isinstance(node, ast.Call)
                            and getattr(node.func, "id", getattr(node.func, "attr", None)) == "_launch_chrome"):
                        sites.append((path.name, func.name))
    assert sites == [("session.py", "_start_chrome")]
    callers = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if isinstance(func, ast.FunctionDef):
                for node in ast.walk(func):
                    if (isinstance(node, ast.Call)
                            and getattr(node.func, "id", getattr(node.func, "attr", None)) == "_start_chrome"):
                        callers.append((path.name, func.name))
    assert callers == [("session.py", "open_if_down")]
