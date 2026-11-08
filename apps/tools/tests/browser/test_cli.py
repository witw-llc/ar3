import contextlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest

from ar3.locking import file_lock
from browser import cli
from browser import commands
from browser import seatlock
from browser import secretstore
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
    monkeypatch.setattr(session, "_attach", lambda s: events.append("attach"))
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
    monkeypatch.setattr(session, "_attach", lambda s: None)
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


def _chrome_up(monkeypatch, up=True):
    monkeypatch.setattr(session, "_profile_pids", lambda s: ["7"] if up else [])


def _stamp(seat, age):
    """The stamp a script that ended `age` seconds ago leaves."""
    home = session.seat_home(seat)
    os.makedirs(home, exist_ok=True)
    aside = os.path.join(home, f".last-active.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(aside, "w") as handle:
        handle.write(str(time.time() - age))
    os.replace(aside, os.path.join(home, "last-active"))


def _idle(monkeypatch, *extra, timeout="60"):
    return _run(monkeypatch, "idle", "--seat", "s", "--timeout", timeout, *extra)


def _closes(monkeypatch):
    """Record the closes. An idle close is not a stop, so it must not bump the epoch."""
    closed = []
    monkeypatch.setattr(session, "close_browser", closed.append)
    monkeypatch.setattr(
        session, "bump_stop_epoch", lambda s: pytest.fail("an idle close bumped the stop epoch")
    )
    return closed


def test_idle_does_nothing_for_a_seat_with_no_chrome(monkeypatch, capsys):
    closed = _closes(monkeypatch)
    _chrome_up(monkeypatch, up=False)
    monkeypatch.setattr(seatlock, "hold", lambda *a, **k: pytest.fail("took the seat"))
    assert _idle(monkeypatch) == 0
    out, err = capsys.readouterr()
    assert (out, err) == ("", "")
    assert closed == []
    assert not os.path.exists(session.seat_home("s"))


def test_idle_leaves_a_held_seat_alone_and_does_not_wait(monkeypatch, capsys):
    closed = _closes(monkeypatch)
    _chrome_up(monkeypatch)
    _stamp("s", 500)
    started = time.monotonic()
    with _held("s"):
        assert _idle(monkeypatch) == 0
    assert time.monotonic() - started < 1.5
    assert capsys.readouterr().out == "s: busy, not idle\n"
    assert closed == []


def test_idle_leaves_a_seat_used_more_recently_than_the_timeout(monkeypatch, capsys):
    closed = _closes(monkeypatch)
    _chrome_up(monkeypatch)
    _stamp("s", 5)
    stamp = session.last_activity("s")
    assert _idle(monkeypatch) == 0
    assert re.fullmatch(r"s: used [56]s ago, not idle\n", capsys.readouterr().out)
    assert closed == []
    # Looking is not using: the check must not restart the clock it reads.
    assert session.last_activity("s") == stamp


def test_idle_closes_a_seat_unused_for_the_timeout_and_leaves_the_stop_epoch(monkeypatch, capsys):
    epoch = session.bump_stop_epoch("s")
    closed = _closes(monkeypatch)
    _chrome_up(monkeypatch)
    _stamp("s", 500)
    stamp = session.last_activity("s")
    assert _idle(monkeypatch) == 0
    assert re.fullmatch(r"s: closed after (50\d)s idle\n", capsys.readouterr().out)
    assert closed == ["s"]
    assert session.stop_epoch("s") == epoch
    assert session.last_activity("s") == stamp


@pytest.mark.parametrize("text", [None, "", "soon", "nan"])
def test_idle_counts_a_missing_or_unreadable_stamp_as_idle(monkeypatch, capsys, text):
    closed = _closes(monkeypatch)
    _chrome_up(monkeypatch)
    if text is not None:
        os.makedirs(session.seat_home("s"))
        with open(os.path.join(session.seat_home("s"), "last-active"), "w") as handle:
            handle.write(text)
    assert _idle(monkeypatch) == 0
    assert capsys.readouterr().out == "s: closed, idle with no activity record\n"
    assert closed == ["s"]


def test_idle_does_not_close_while_a_launch_is_in_progress(monkeypatch, capsys):
    closed = _closes(monkeypatch)
    _chrome_up(monkeypatch)
    _stamp("s", 500)
    monkeypatch.setattr(cli, "LAUNCH_LOCK_WAIT_SECONDS", 0.3)
    started = time.monotonic()
    with session.launch_lock("s"):
        assert _idle(monkeypatch) == 0
    assert 0.3 <= time.monotonic() - started < 3
    assert capsys.readouterr().out == "s: a launch is in progress, not idle\n"
    assert closed == []


def test_idle_reports_a_close_that_failed(monkeypatch, capsys):
    def refuse(seat):
        raise cli.plc.BrowserError("Chrome for seat 's' did not come up under the seat's driver")

    monkeypatch.setattr(session, "close_browser", refuse)
    _chrome_up(monkeypatch)
    _stamp("s", 500)
    assert _idle(monkeypatch) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "s: Chrome for seat 's' did not come up under the seat's driver\n"


def test_idle_hands_the_chrome_flag_to_the_close(monkeypatch):
    monkeypatch.setenv("AR3_BROWSER_CHROME", "")
    seen = []
    monkeypatch.setattr(session, "close_browser", lambda s: seen.append(os.environ["AR3_BROWSER_CHROME"]))
    _chrome_up(monkeypatch)
    _stamp("s", 500)
    assert _idle(monkeypatch, "--chrome=/c/chrome") == 0
    assert seen == ["/c/chrome"]


def test_idle_closes_from_the_scratch_dir_of_the_seat(monkeypatch, tmp_path):
    # playwright-cli drops its per-command files in the working directory.
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(
        session, "close_browser", lambda s: seen.append(os.path.realpath(os.getcwd()))
    )
    _chrome_up(monkeypatch)
    _stamp("s", 500)
    assert _idle(monkeypatch) == 0
    assert seen == [os.path.realpath(session.scratch_dir("s"))]


def test_idle_never_goes_through_the_script_paths(monkeypatch):
    _closes(monkeypatch)
    _chrome_up(monkeypatch)
    _stamp("s", 500)
    monkeypatch.setattr(cli, "_hold", lambda *a: pytest.fail("idle took a script hold"))
    monkeypatch.setattr(cli, "_drive", lambda *a: pytest.fail("idle drove the browser"))
    monkeypatch.setattr(session, "ensure_running", lambda *a, **k: pytest.fail("idle opened Chrome"))
    monkeypatch.setattr(session, "open_if_down", lambda *a: pytest.fail("idle opened Chrome"))
    assert _idle(monkeypatch) == 0


@pytest.mark.parametrize("timeout", ["0", "-5", "abc", "", "nan", "inf", "1e999"])
def test_idle_wants_a_number_of_seconds_above_zero(monkeypatch, capsys, timeout):
    _closes(monkeypatch)
    with pytest.raises(SystemExit) as stop:
        _idle(monkeypatch, timeout=timeout)
    assert stop.value.code == 2
    assert "--timeout" in capsys.readouterr().err


def test_idle_needs_a_timeout(monkeypatch, capsys):
    with pytest.raises(SystemExit) as stop:
        _run(monkeypatch, "idle", "--seat", "s")
    assert stop.value.code == 2
    assert "--timeout" in capsys.readouterr().err


def test_idle_accepts_a_fraction_of_a_second(monkeypatch):
    _chrome_up(monkeypatch, up=False)
    assert _idle(monkeypatch, timeout="0.5") == 0


def test_a_script_that_read_the_epoch_before_an_idle_close_can_still_open_chrome(monkeypatch):
    session.bump_stop_epoch("s")
    epoch = session.stop_epoch("s")
    monkeypatch.setattr(session.plc, "is_open", lambda s: False)
    monkeypatch.setattr(session, "_start_chrome", lambda s: None)
    monkeypatch.setattr(session, "close_browser", lambda s: None)
    _chrome_up(monkeypatch)
    _stamp("s", 500)
    assert _idle(monkeypatch) == 0
    session.open_if_down("s", epoch)
    # The control: a stop moves the epoch, and the same script is refused.
    assert _run(monkeypatch, "close", "--seat", "s") == 0
    with pytest.raises(cli.plc.BrowserError, match="seat closed by a8s stop"):
        session.open_if_down("s", epoch)


def test_the_stop_hook_is_not_use_of_the_seat(monkeypatch):
    monkeypatch.setattr(session, "close_browser", lambda s: None)
    assert _run(monkeypatch, "close", "--seat", "s") == 0
    with _held("s"):
        assert _run(monkeypatch, "close", "--seat", "s") == 0
    assert session.last_activity("s") is None


def test_a_cli_verb_is_use_of_the_seat(monkeypatch):
    monkeypatch.setattr(session, "ensure_running", lambda seat, script_epoch=None: None)
    monkeypatch.setattr(session, "snapshot", lambda seat: "snapshot")
    assert session.last_activity("s") is None
    assert _run(monkeypatch, "snap", "--seat", "s") == 0
    assert session.last_activity("s") is not None


# --- the secret verbs -----------------------------------------------------------

VALUE = "hunter2-value"
ORIGIN = "https://app.example.com"


def _entry(value, origins=(ORIGIN,)):
    return {"value": value, "origins": list(origins)}


class _Stdin(io.StringIO):
    def __init__(self, text, tty=False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty

    @property
    def buffer(self):
        outer = self

        class Bytes:
            def read(self, limit=-1):
                return outer.read().encode()

        return Bytes()


def _stdin(monkeypatch, text, tty=False):
    monkeypatch.setattr(sys, "stdin", _Stdin(text, tty))


def _secret(monkeypatch, *argv):
    return _run(monkeypatch, "secret", *argv)


def _silent(capsys):
    """Everything the verb printed, joined: no path may show a secret value."""
    out, err = capsys.readouterr()
    return out + err


def test_secret_set_stores_the_value_from_stdin_and_prints_no_value(monkeypatch, capsys):
    _stdin(monkeypatch, VALUE + "\n")
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 0
    out, err = capsys.readouterr()
    assert out == "s: set PASSWORD\n"
    assert err == ""
    assert secretstore.read("s") == {"PASSWORD": _entry(VALUE)}


@pytest.mark.parametrize(
    "typed, stored",
    [
        (VALUE, VALUE),
        (VALUE + "\n", VALUE),
        (VALUE + "\n\n", VALUE + "\n"),
        (VALUE + "\r\n", VALUE + "\r"),
        ("  " + VALUE + "  \n", "  " + VALUE + "  "),
        ("line one\nline two\n", "line one\nline two"),
    ],
)
def test_secret_set_strips_one_trailing_newline_only(monkeypatch, typed, stored):
    _stdin(monkeypatch, typed)
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 0
    assert secretstore.read("s") == {"PASSWORD": _entry(stored)}


def test_secret_set_asks_at_a_terminal_without_echo(monkeypatch, capsys):
    _stdin(monkeypatch, "", tty=True)
    asked = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": asked.append(prompt) or VALUE)
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 0
    assert len(asked) == 1 and "PASSWORD" in asked[0] and VALUE not in asked[0]
    assert secretstore.read("s") == {"PASSWORD": _entry(VALUE)}
    assert VALUE not in _silent(capsys)


def test_secret_set_does_not_read_stdin_at_a_terminal(monkeypatch):
    class Boom(_Stdin):
        def read(self, *a):
            pytest.fail("stdin was read at a terminal")

    monkeypatch.setattr(sys, "stdin", Boom("", True))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": VALUE)
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 0


def test_secret_set_replaces_a_value_and_keeps_the_others(monkeypatch):
    secretstore.set_secret("s", "API_TOKEN", "token-value-1", [ORIGIN])
    _stdin(monkeypatch, "first-value")
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 0
    _stdin(monkeypatch, "second-value")
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 0
    assert secretstore.read("s") == {"API_TOKEN": _entry("token-value-1"), "PASSWORD": _entry("second-value")}


def test_secret_list_prints_each_name_with_its_origins_and_no_value(monkeypatch, capsys):
    secretstore.set_secret("s", "PASSWORD", VALUE, [ORIGIN, "https://*.example.org"])
    secretstore.set_secret("s", "API_TOKEN", "token-value-1", ["http://127.0.0.1:8080"])
    assert _secret(monkeypatch, "list", "--seat", "s") == 0
    out, err = capsys.readouterr()
    assert out == "API_TOKEN http://127.0.0.1:8080\nPASSWORD https://app.example.com https://*.example.org\n"
    assert err == ""
    assert VALUE not in out and "token-value-1" not in out


def test_secret_list_of_an_empty_store_prints_nothing(monkeypatch, capsys):
    assert _secret(monkeypatch, "list", "--seat", "s") == 0
    assert capsys.readouterr() == ("", "")


def test_secret_rm_removes_one_and_a_second_rm_says_it_is_not_set(monkeypatch, capsys):
    secretstore.set_secret("s", "PASSWORD", VALUE, [ORIGIN])
    secretstore.set_secret("s", "API_TOKEN", "token-value-1", [ORIGIN])
    assert _secret(monkeypatch, "rm", "PASSWORD", "--seat", "s") == 0
    assert capsys.readouterr() == ("s: removed PASSWORD\n", "")
    assert secretstore.read("s") == {"API_TOKEN": _entry("token-value-1")}
    assert _secret(monkeypatch, "rm", "PASSWORD", "--seat", "s") == 1
    assert capsys.readouterr() == ("", "s: secret PASSWORD is not set\n")
    assert secretstore.read("s") == {"API_TOKEN": _entry("token-value-1")}


def test_a_bad_name_is_refused_before_any_value_is_read(monkeypatch, capsys):
    class Boom(_Stdin):
        def read(self, *a):
            pytest.fail("stdin was read for a bad name")

    monkeypatch.setattr(sys, "stdin", Boom(VALUE))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": pytest.fail("prompted for a bad name"))
    assert _secret(monkeypatch, "set", "lower", "--origin", ORIGIN, "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "secret name" in err
    assert not os.path.exists(secretstore.path("s"))


def test_a_value_that_is_too_short_is_refused_and_not_printed(monkeypatch, capsys):
    _stdin(monkeypatch, "abc\n")
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "at least 4 characters" in err and "abc" not in err
    assert not os.path.exists(secretstore.path("s"))


def test_an_empty_stdin_is_a_value_that_is_too_short(monkeypatch, capsys):
    _stdin(monkeypatch, "")
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 1
    assert "at least 4 characters" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_a_store_open_to_others_stops_every_secret_verb_and_shows_no_value(monkeypatch, capsys):
    secretstore.set_secret("s", "PASSWORD", VALUE, [ORIGIN])
    target = secretstore.path("s")
    os.chmod(target, 0o644)
    for argv, text in ((("list",), ""), (("rm", "PASSWORD"), ""), (("set", "OTHER", "--origin", ORIGIN), "other-value\n")):
        _stdin(monkeypatch, text)
        assert _secret(monkeypatch, *argv, "--seat", "s") == 1
        out, err = capsys.readouterr()
        assert out == ""
        assert f"chmod 600 {target}" in err
        assert VALUE not in err


@pytest.mark.parametrize("content", [b"not json " + VALUE.encode(), b'{"version": 1, "secrets": "' + VALUE.encode() + b'"}'])
def test_a_corrupt_store_is_an_error_that_names_the_file_and_not_its_content(monkeypatch, capsys, content):
    target = secretstore.path("s")
    os.makedirs(os.path.dirname(target))
    with open(target, "wb") as handle:
        handle.write(content)
    os.chmod(target, 0o600)
    assert _secret(monkeypatch, "list", "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == "" and target in err and VALUE not in err


def test_no_secret_verb_prints_a_value_on_any_path(monkeypatch, capsys):
    shown = []
    _stdin(monkeypatch, VALUE)
    _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s")
    shown.append(_silent(capsys))
    _stdin(monkeypatch, VALUE)
    _secret(monkeypatch, "set", "lower", "--origin", ORIGIN, "--seat", "s")
    shown.append(_silent(capsys))
    _secret(monkeypatch, "list", "--seat", "s")
    shown.append(_silent(capsys))
    _secret(monkeypatch, "rm", "OTHER", "--seat", "s")
    shown.append(_silent(capsys))
    _secret(monkeypatch, "rm", "PASSWORD", "--seat", "s")
    shown.append(_silent(capsys))
    _stdin(monkeypatch, "abc")
    _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s")
    shown.append(_silent(capsys))
    assert VALUE not in "".join(shown)


def test_a_store_that_cannot_be_written_is_an_error_line_not_a_traceback(monkeypatch, capsys):
    def refuse(a, b):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(secretstore.os, "replace", refuse)
    _stdin(monkeypatch, VALUE)
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"s: cannot write the secret store {secretstore.path('s')}: Permission denied\n"
    assert VALUE not in err


def test_the_secret_verbs_take_the_seat_before_or_after_the_verb_or_from_the_environment(monkeypatch):
    _stdin(monkeypatch, VALUE)
    assert _run(monkeypatch, "--seat", "before", "secret", "set", "PASSWORD", "--origin", ORIGIN) == 0
    _stdin(monkeypatch, VALUE)
    assert _run(monkeypatch, "secret", "set", "PASSWORD", "--origin", ORIGIN, "--seat", "after") == 0
    monkeypatch.setenv("AR3_BROWSER_SEAT", "fromenv")
    _stdin(monkeypatch, VALUE)
    assert _run(monkeypatch, "secret", "set", "PASSWORD", "--origin", ORIGIN) == 0
    monkeypatch.delenv("AR3_BROWSER_SEAT")
    _stdin(monkeypatch, VALUE)
    assert _run(monkeypatch, "secret", "set", "PASSWORD", "--origin", ORIGIN) == 0
    for seat in ("before", "after", "fromenv", "browser"):
        assert secretstore.read(seat) == {"PASSWORD": _entry(VALUE)}


def test_the_secret_verbs_start_no_chrome_take_no_lock_and_leave_the_directory(monkeypatch):
    boom = lambda *a, **k: pytest.fail("a secret verb drove the browser")
    monkeypatch.setattr(cli, "_hold", boom)
    monkeypatch.setattr(cli, "_drive", boom)
    monkeypatch.setattr(seatlock, "hold", boom)
    monkeypatch.setattr(session, "ensure_running", boom)
    monkeypatch.setattr(session, "open_if_down", boom)
    monkeypatch.setattr(session, "scratch_dir", boom)
    monkeypatch.setattr(cli.os, "chdir", boom)
    _stdin(monkeypatch, VALUE)
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 0
    assert _secret(monkeypatch, "list", "--seat", "s") == 0
    assert _secret(monkeypatch, "rm", "PASSWORD", "--seat", "s") == 0


def test_a_secret_verb_is_not_use_of_the_seat(monkeypatch):
    _stdin(monkeypatch, VALUE)
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--seat", "s") == 0
    assert session.last_activity("s") is None


@pytest.mark.parametrize("argv", [["secret"], ["secret", "set"], ["secret", "rm"], ["secret", "get", "PASSWORD"]])
def test_an_incomplete_or_unknown_secret_verb_is_a_usage_error(monkeypatch, capsys, argv):
    with pytest.raises(SystemExit) as stop:
        _run(monkeypatch, *argv)
    assert stop.value.code == 2
    assert capsys.readouterr().out == ""


def test_the_secret_verb_has_help_that_names_no_value(monkeypatch, capsys):
    with pytest.raises(SystemExit) as stop:
        _run(monkeypatch, "secret", "--help")
    assert stop.value.code == 0
    help_text = capsys.readouterr().out
    for verb in ("set", "list", "rm"):
        assert verb in help_text


def test_secret_set_needs_an_origin_and_reads_no_value_without_one(monkeypatch, capsys):
    class Boom(_Stdin):
        def read(self, *a):
            pytest.fail("stdin was read before the origin was checked")

    monkeypatch.setattr(sys, "stdin", Boom(VALUE))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": pytest.fail("prompted without an origin"))
    assert _secret(monkeypatch, "set", "PASSWORD", "--seat", "s") == 1
    assert "--origin https://app.example.com" in capsys.readouterr().err
    assert not os.path.exists(secretstore.path("s"))


def test_secret_set_keeps_every_origin_given_normalized_and_once(monkeypatch, capsys):
    _stdin(monkeypatch, VALUE)
    argv = [
        "set", "PASSWORD", "--seat", "s",
        "--origin", "https://App.Example.com", "--origin", "https://*.example.org",
        "--origin", "https://app.example.com:443", "--origin", "http://127.0.0.1:18931",
    ]
    assert _secret(monkeypatch, *argv) == 0
    assert capsys.readouterr() == ("s: set PASSWORD\n", "")
    assert secretstore.read("s") == {
        "PASSWORD": _entry(VALUE, ["https://app.example.com", "https://*.example.org", "http://127.0.0.1:18931"])
    }


def test_secret_set_replaces_the_origins_with_the_value(monkeypatch):
    secretstore.set_secret("s", "PASSWORD", "first-value", ["https://a.example.com", "https://b.example.com"])
    _stdin(monkeypatch, "second-value")
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", "https://c.example.com", "--seat", "s") == 0
    assert secretstore.read("s") == {"PASSWORD": _entry("second-value", ["https://c.example.com"])}


@pytest.mark.parametrize(
    "origin",
    ["app.example.com", "https://app.example.com/login", "ftp://app.example.com", "https://*example.org",
     "https://app.example.com:99999", "https://user@app.example.com", "", "https://*.com", "http://*.org:8080"],
)
def test_a_bad_origin_is_refused_before_any_value_is_read(monkeypatch, capsys, origin):
    class Boom(_Stdin):
        def read(self, *a):
            pytest.fail("stdin was read for a bad origin")

    monkeypatch.setattr(sys, "stdin", Boom(VALUE))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": pytest.fail("prompted for a bad origin"))
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", ORIGIN, "--origin", origin, "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("s: an origin ") or err.startswith("s: a wildcard")
    assert not os.path.exists(secretstore.path("s"))


def test_a_wildcard_on_one_label_is_refused_with_the_reason_and_localhost_passes(monkeypatch, capsys):
    _stdin(monkeypatch, VALUE)
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", "https://*.com", "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == "" and "at least two labels" in err and "would match every site under it" in err
    assert not os.path.exists(secretstore.path("s"))
    for origin in ("https://*.example.com", "https://*.co.uk", "http://*.localhost:3000"):
        _stdin(monkeypatch, VALUE)
        assert _secret(monkeypatch, "set", "PASSWORD", "--origin", origin, "--seat", "s") == 0, origin
    capsys.readouterr()
    assert secretstore.read("s")["PASSWORD"]["origins"] == ["http://*.localhost:3000"]


def test_a_bad_origin_is_not_repeated_in_the_message(monkeypatch, capsys):
    _stdin(monkeypatch, VALUE)
    assert _secret(monkeypatch, "set", "PASSWORD", "--origin", "LEAK-4410/path", "--seat", "s") == 1
    assert "LEAK-4410" not in _silent(capsys)


# --- the verbs that print a page's text -----------------------------------------


def _store(*pairs):
    for name, value in pairs:
        secretstore.set_secret("s", name, value, [ORIGIN])


def test_snap_prints_the_page_through_the_scrub(monkeypatch, capsys):
    _store(("PASSWORD", VALUE))
    monkeypatch.setattr(session, "ensure_running", lambda seat, script_epoch=None: None)
    monkeypatch.setattr(session, "snapshot", lambda seat: f'- textbox "Password": {VALUE}')
    assert _run(monkeypatch, "snap", "--seat", "s") == 0
    assert capsys.readouterr().out == '- textbox "Password": [secret:PASSWORD]\n'


def test_console_prints_the_log_through_the_scrub(monkeypatch, capsys):
    _store(("PASSWORD", VALUE))
    monkeypatch.setattr(session, "ensure_running", lambda seat, script_epoch=None: None)
    monkeypatch.setattr(cli.plc, "run", lambda *a, **k: f"### Result\n[LOG] typed {VALUE}\n")
    assert _run(monkeypatch, "console", "--seat", "s") == 0
    assert capsys.readouterr().out == "[LOG] typed [secret:PASSWORD]\n"


def test_go_and_status_print_the_url_through_the_scrub(monkeypatch, capsys):
    _store(("PASSWORD", VALUE))
    monkeypatch.setattr(session, "ensure_running", lambda seat, script_epoch=None: None)
    monkeypatch.setattr(cli.plc, "run", lambda *a, **k: "### Result\nok\n")
    monkeypatch.setattr(session, "current_url", lambda seat: f"https://example.com/?pw={VALUE}")
    assert _run(monkeypatch, "go", "https://example.com/", "--seat", "s") == 0
    assert capsys.readouterr().out == "https://example.com/?pw=[secret:PASSWORD]\n"
    monkeypatch.setattr(session, "status", lambda seat: f"{seat}: attached at https://example.com/?pw={VALUE}")
    assert _run(monkeypatch, "status", "--seat", "s") == 0
    assert capsys.readouterr().out == "s: attached at https://example.com/?pw=[secret:PASSWORD]\n"


def test_a_driver_error_is_printed_through_the_scrub(monkeypatch, capsys):
    _store(("PASSWORD", VALUE))
    monkeypatch.setattr(session, "ensure_running", lambda seat, script_epoch=None: None)

    def fail(seat):
        raise cli.plc.BrowserError(f'TimeoutError: fill("{VALUE}")')

    monkeypatch.setattr(session, "snapshot", fail)
    assert _run(monkeypatch, "snap", "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == "" and err == 's: TimeoutError: fill("[secret:PASSWORD]")\n'


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
@pytest.mark.parametrize("verb", [["snap"], ["console"], ["go", "https://example.com/"], ["status"]])
def test_a_page_verb_prints_nothing_when_the_store_cannot_be_read(monkeypatch, capsys, verb):
    _store(("PASSWORD", VALUE))
    target = secretstore.path("s")
    os.chmod(target, 0o644)
    for name in ("ensure_running", "snapshot", "current_url", "status"):
        monkeypatch.setattr(session, name, lambda *a, **k: pytest.fail("the page was read"))
    monkeypatch.setattr(cli.plc, "run", lambda *a, **k: pytest.fail("the page was read"))
    assert _run(monkeypatch, *verb, "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == "" and f"chmod 600 {target}" in err


# --- site recipes: the directories a run loads from -----------------------------------------------------------
#
# The recipes of a seat's own come from `recipes/` in the root of the registered tool-browser node of its name,
# found in the a8s registry, and from nowhere else: not from the working directory, and not from a variable
# that every agent's wake carries. A marker recipe writes a file when it is imported, so a test can see that a
# recipe the listing does not show was not imported either.

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "browser")
TOOL_BROWSER = "/repo/apps/a8s/definitions/tool-browser.json"
COMMAND_SHELL = "/repo/apps/a8s/definitions/tool-command-shell.json"


def _register(tmp_path, monkeypatch, **nodes):
    """An a8s registry in a home of its own. `nodes` maps a name to (root, definition)."""
    home = tmp_path / "a8s-registry"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("A8S_HOME", str(home))
    (home / "a8s.json").write_text(json.dumps({
        "agents": {name: {"root": str(root), "definition": definition, "vars": {}} for name, (root, definition) in nodes.items()},
        "aliases": {}, "namespaces": {},
    }))


def _marker_recipe(root, marker, name="mine"):
    """`root/recipes/<name>.py`, a recipe that writes `marker` when it is imported."""
    (root / "recipes").mkdir(parents=True, exist_ok=True)
    (root / "recipes" / f"{name}.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('imported')\n"
        "from browser.recipe import Recipe\n"
        f"recipe = Recipe({name!r}, 'A recipe of the node', origins=['https://mine.example.com'])\n"
        "recipe.steps('go', help='Go.', steps='url')\n"
    )
    return root


def _wake(monkeypatch, tmp_path, seat="garmin"):
    """One `browser handle` of the message `recipe`, in print mode: the reply is printed."""
    monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path / "replies"))
    assert _run(monkeypatch, "--seat", seat, "handle", "--from", "iris", "--allow", "iris", "--", "recipe") == 0


def _listed(capsys):
    return capsys.readouterr().out


def test_a_wake_loads_the_recipes_of_the_registered_tool_browser_node_of_its_seat_name(monkeypatch, tmp_path, capsys):
    node, marker = tmp_path / "node", tmp_path / "imported"
    _marker_recipe(node, marker)
    _register(tmp_path, monkeypatch, garmin=(node, TOOL_BROWSER))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("A8S_TURN_RECIPIENT", raising=False)
    _wake(monkeypatch, tmp_path)
    assert "mine  A recipe of the node  [node]" in _listed(capsys)
    assert marker.exists(), "the marker proves that a recipe the tool lists has been imported"


def test_a_wake_loads_no_recipe_from_the_working_directory_whatever_the_environment_holds(monkeypatch, tmp_path, capsys):
    """The review's repro: every agent wake carries A8S_TURN_RECIPIENT, and a recipes/ directory is code that acts
    with the seat's signed-in identity."""
    here, marker = tmp_path / "somewhere", tmp_path / "imported"
    _marker_recipe(here, marker)
    _register(tmp_path, monkeypatch)
    monkeypatch.chdir(here)
    for value in ("garmin", "some-agent", ""):
        monkeypatch.setenv("A8S_TURN_RECIPIENT", value)
        _wake(monkeypatch, tmp_path)
        listing = _listed(capsys)
        assert "givebacks" in listing and "mine" not in listing and "[node]" not in listing
        assert not marker.exists(), f"the recipe of the working directory was imported (A8S_TURN_RECIPIENT={value!r})"


def test_a_registered_node_loads_its_root_and_not_the_working_directory(monkeypatch, tmp_path, capsys):
    node, here = tmp_path / "node", tmp_path / "here"
    _marker_recipe(node, tmp_path / "node-imported", name="ofnode")
    _marker_recipe(here, tmp_path / "here-imported", name="ofhere")
    _register(tmp_path, monkeypatch, garmin=(node, TOOL_BROWSER))
    monkeypatch.chdir(here)
    monkeypatch.setenv("A8S_TURN_RECIPIENT", "garmin")
    _wake(monkeypatch, tmp_path)
    listing = _listed(capsys)
    assert "ofnode" in listing and "ofhere" not in listing
    assert (tmp_path / "node-imported").exists() and not (tmp_path / "here-imported").exists()


def test_a_node_of_another_kind_with_a_recipes_directory_in_its_root_loads_nothing(monkeypatch, tmp_path, capsys):
    node, marker = tmp_path / "node", tmp_path / "imported"
    _marker_recipe(node, marker)
    _register(tmp_path, monkeypatch, garmin=(node, COMMAND_SHELL))
    monkeypatch.chdir(node)
    _wake(monkeypatch, tmp_path)
    listing = _listed(capsys)
    assert "givebacks" in listing and "mine" not in listing and not marker.exists()


def test_a_seat_that_is_no_registered_node_loads_nothing_though_another_node_has_recipes(monkeypatch, tmp_path, capsys):
    node, marker = tmp_path / "node", tmp_path / "imported"
    _marker_recipe(node, marker)
    _register(tmp_path, monkeypatch, other=(node, TOOL_BROWSER))
    _wake(monkeypatch, tmp_path, seat="garmin")
    listing = _listed(capsys)
    assert "mine" not in listing and not marker.exists()
    _wake(monkeypatch, tmp_path, seat="other")
    assert "mine  A recipe of the node  [node]" in _listed(capsys) and marker.exists()


def test_a_missing_or_damaged_registry_loads_nothing(monkeypatch, tmp_path, capsys):
    here, marker = tmp_path / "here", tmp_path / "imported"
    _marker_recipe(here, marker)
    monkeypatch.chdir(here)
    monkeypatch.setenv("A8S_HOME", str(tmp_path / "no-such-home"))
    _wake(monkeypatch, tmp_path)
    assert "mine" not in _listed(capsys)
    _register(tmp_path, monkeypatch)
    (tmp_path / "a8s-registry" / "a8s.json").write_text("{ not json")
    _wake(monkeypatch, tmp_path)
    assert "mine" not in _listed(capsys) and not marker.exists()


def test_a_node_whose_registered_root_is_not_an_absolute_path_loads_nothing(monkeypatch, tmp_path, capsys):
    here, marker = tmp_path / "here", tmp_path / "imported"
    _marker_recipe(here, marker)
    _register(tmp_path, monkeypatch, garmin=(".", TOOL_BROWSER))
    monkeypatch.chdir(here)
    _wake(monkeypatch, tmp_path)
    assert "mine" not in _listed(capsys) and not marker.exists()


def test_the_program_loads_no_recipe_from_the_directory_it_runs_in_when_a_wake_environment_is_inherited(
    monkeypatch, tmp_path
):
    here, marker = tmp_path / "here", tmp_path / "imported"
    _marker_recipe(here, marker)
    _register(tmp_path, monkeypatch)
    for words in (
        ["--seat", "garmin", "handle", "--from", "iris", "--allow", "iris", "--", "recipe"],
        ["--seat", "garmin", "do", "-"],
    ):
        done = subprocess.run(
            [sys.executable, TOOL, *words], cwd=here, input="recipe\n" if words[2] == "do" else None,
            env={**os.environ, "A8S_TURN_RECIPIENT": "garmin", "AR3_TOOL_REPLY_DIR": str(tmp_path / "replies")},
            capture_output=True, text=True, timeout=120,
        )
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert "givebacks" in done.stdout and "mine" not in done.stdout, done.stdout
        assert not marker.exists(), f"{words[2]} imported the recipe of its working directory"


def test_the_program_loads_the_recipes_of_a_registered_node_found_by_its_seat_name(monkeypatch, tmp_path):
    node, marker = tmp_path / "node", tmp_path / "imported"
    _marker_recipe(node, marker)
    _register(tmp_path, monkeypatch, garmin=(node, TOOL_BROWSER))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    done = subprocess.run(
        [sys.executable, TOOL, "--seat", "garmin", "handle", "--from", "iris", "--allow", "iris", "--", "recipe"],
        cwd=elsewhere, env={**os.environ, "AR3_TOOL_REPLY_DIR": str(tmp_path / "replies")},
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert "mine  A recipe of the node  [node]" in done.stdout and marker.exists()


def test_a_plain_do_loads_the_bundled_recipes_only(monkeypatch, tmp_path, capsys):
    node, marker = tmp_path / "node", tmp_path / "imported"
    _marker_recipe(node, marker)
    _register(tmp_path, monkeypatch, s=(node, TOOL_BROWSER))
    monkeypatch.chdir(node)
    monkeypatch.setenv("A8S_TURN_RECIPIENT", "s")
    script = tmp_path / "s.txt"
    script.write_text("recipe\n")
    assert _run(monkeypatch, "--seat", "s", "do", str(script)) == 0
    out = capsys.readouterr().out
    assert "givebacks" in out and "mine" not in out and not marker.exists()


def test_do_loads_the_recipes_directories_it_is_given_as_absolute_paths(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        commands, "run_script",
        lambda seat, body, **k: seen.append(k["recipes"]) or commands.Run(seat),
    )
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "s.txt"
    script.write_text("recipe\n")
    assert _run(monkeypatch, "--seat", "s", "do", str(script), "--recipes", "first", "--recipes", str(second)) == 0
    assert [str(directory) for directory in seen[0].dirs] == [os.path.abspath(first), str(second)]


def test_a_recipes_directory_that_is_not_one_stops_the_run_before_anything_starts(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: pytest.fail("the script ran"))
    monkeypatch.setattr(session, "scratch_dir", lambda seat: pytest.fail("the seat was touched"))
    script = tmp_path / "s.txt"
    script.write_text("recipe\n")
    assert _run(monkeypatch, "--seat", "s", "do", str(script), "--recipes", str(tmp_path / "typo")) == 2
    assert capsys.readouterr().err == f"s: --recipes {tmp_path / 'typo'} is not a directory\n"


def test_do_loads_a_recipes_directory_for_real(monkeypatch, tmp_path, capsys):
    node, marker = tmp_path / "mine", tmp_path / "imported"
    _marker_recipe(node, marker)
    script = tmp_path / "s.txt"
    script.write_text("recipe\n")
    assert _run(monkeypatch, "--seat", "s", "do", str(script), "--recipes", str(node / "recipes")) == 0
    assert "mine  A recipe of the node  [node]" in capsys.readouterr().out
    assert marker.exists()


# --- the hand verbs open no local file either --------------------------------------------------------------------


def _stub_hand_verbs(monkeypatch, url):
    """A seat whose page is at `url`. Returns the playwright-cli calls."""
    calls = []
    page = {"url": url}
    monkeypatch.setattr(session, "ensure_running", lambda seat, script_epoch=None: None)
    monkeypatch.setattr(session, "current_url", lambda seat: page["url"])
    monkeypatch.setattr(session, "snapshot", lambda seat: f"- generic: page at {page['url']}")

    def run(seat, *args, **kwargs):
        calls.append(args)
        if args[0] == "goto":
            page["url"] = args[1]
        return "### Result\nok\n"

    monkeypatch.setattr(cli.plc, "run", run)
    return calls, page


@pytest.mark.parametrize("url", ["file:///etc/hostname", "FILE://localhost/x", "view-source:file:///x"])
def test_go_by_hand_refuses_a_file_url_before_it_asks_the_driver(monkeypatch, capsys, url):
    calls, _page = _stub_hand_verbs(monkeypatch, "about:blank")
    assert _run(monkeypatch, "go", url, "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == "" and "local files are not opened" in err
    assert calls == []


def test_go_by_hand_that_ends_on_a_local_page_sends_it_away(monkeypatch, capsys):
    """A driver that follows a redirect, or opens a page its config allows, ends on a file: page."""
    calls, page = _stub_hand_verbs(monkeypatch, "about:blank")
    real = cli.plc.run

    def landing(seat, *args, **kwargs):
        out = real(seat, *args, **kwargs)
        if args[0] == "goto" and args[1] == "https://example.com/":
            page["url"] = "file:///etc/hostname"
        return out

    monkeypatch.setattr(cli.plc, "run", landing)
    assert _run(monkeypatch, "go", "https://example.com/", "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == "" and "the page is a local file" in err and page["url"] == "about:blank"


@pytest.mark.parametrize("verb", ["snap", "console"])
def test_snap_and_console_by_hand_print_nothing_of_a_local_page(monkeypatch, capsys, verb):
    calls, page = _stub_hand_verbs(monkeypatch, "file:///etc/hostname")
    assert _run(monkeypatch, verb, "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == "" and "the page is a local file" in err
    assert page["url"] == "about:blank"


def test_the_hand_verbs_still_work_on_an_ordinary_page(monkeypatch, capsys):
    calls, page = _stub_hand_verbs(monkeypatch, "https://example.com/")
    assert _run(monkeypatch, "snap", "--seat", "s") == 0
    assert capsys.readouterr().out == "- generic: page at https://example.com/\n"
    assert _run(monkeypatch, "go", "https://example.com/next", "--seat", "s") == 0
    assert capsys.readouterr().out == "https://example.com/next\n"


def test_secret_set_takes_a_dash_for_stdin_and_keeps_the_origins_of_a_stored_name(monkeypatch, capsys):
    other = "https://b.example.org"
    secretstore.set_secret("s", "PASSWORD", "old-value-1", [ORIGIN, other])
    _stdin(monkeypatch, VALUE + "\n")
    assert _secret(monkeypatch, "set", "PASSWORD", "-", "--seat", "s") == 0
    assert secretstore.read("s") == {"PASSWORD": _entry(VALUE, (ORIGIN, other))}
    assert VALUE not in _silent(capsys)


def test_secret_set_with_an_origin_replaces_the_stored_ones(monkeypatch):
    secretstore.set_secret("s", "PASSWORD", "old-value-1", [ORIGIN])
    _stdin(monkeypatch, VALUE)
    assert _secret(monkeypatch, "set", "PASSWORD", "-", "--origin", "https://b.example.org", "--seat", "s") == 0
    assert secretstore.read("s") == {"PASSWORD": _entry(VALUE, ("https://b.example.org",))}


def test_secret_set_refuses_a_value_given_as_an_argument_without_quoting_it(monkeypatch, capsys):
    for argv in (
        ("set", "PASSWORD", VALUE, "--origin", ORIGIN, "--seat", "s"),
        ("set", "PASSWORD", "-", VALUE, "--origin", ORIGIN, "--seat", "s"),
    ):
        try:
            code = _secret(monkeypatch, *argv)
        except SystemExit as stop:
            code = stop.code
        assert code in (1, 2)
        assert VALUE not in _silent(capsys)
    assert not os.path.exists(secretstore.path("s"))


def test_secret_set_refuses_input_that_is_not_utf8_without_quoting_it(monkeypatch, capsys):
    class Raw(_Stdin):
        @property
        def buffer(self):
            class Bytes:
                def read(self, limit=-1):
                    return b"abcd\xff" + VALUE.encode()

            return Bytes()

    monkeypatch.setattr(sys, "stdin", Raw(""))
    assert _secret(monkeypatch, "set", "PASSWORD", "-", "--origin", ORIGIN, "--seat", "s") == 1
    err = _silent(capsys)
    assert "UTF-8" in err and VALUE not in err


def test_cdp_and_profile_flags_export_env_and_resolve_relative_paths_before_chdir(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AR3_BROWSER_CDP", "")
    monkeypatch.setenv("AR3_BROWSER_PROFILE", "")
    rel_profile = tmp_path / "rel-prof"
    rel_profile.mkdir()
    seen = []
    monkeypatch.setattr(
        session, "status",
        lambda seat: seen.append((session.cdp_target(), session.custom_profile_dir())) or f"{seat}: ok",
    )
    monkeypatch.chdir(tmp_path)
    assert _run(monkeypatch, "--cdp", "chrome", "status", "--seat", "s") == 0
    monkeypatch.setenv("AR3_BROWSER_CDP", "")
    monkeypatch.chdir(tmp_path)
    assert _run(monkeypatch, "status", "--seat", "s", "--profile", "rel-prof") == 0
    assert seen == [("chrome", None), (None, str(rel_profile))]
    capsys.readouterr()


def test_cli_status_reports_binding_mismatch_on_stderr_and_returns_one(monkeypatch, capsys):
    def fail_status(seat):
        raise cli.plc.BrowserError(f"seat {seat!r} is already open in CDP mode (chrome) — close it first")

    monkeypatch.setattr(session, "status", fail_status)
    assert _run(monkeypatch, "status", "--seat", "s") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "already open in CDP mode" in err
