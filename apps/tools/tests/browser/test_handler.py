import json

import pytest

from browser import commands
from browser import handler
from browser import seatlock


def _capture(monkeypatch):
    sent = []

    def send(recipient, body, attachments=()):
        sent.append((recipient, body, list(attachments)))
        return 0

    monkeypatch.setattr(handler.rt, "send_reply", send)
    return sent


def _no_browser(monkeypatch):
    monkeypatch.setattr(
        commands, "run_script",
        lambda *a, **k: pytest_fail("the browser ran for an unlisted sender"),
    )


def test_an_empty_allowlist_accepts_nobody(monkeypatch):
    sent = _capture(monkeypatch)
    _no_browser(monkeypatch)
    assert handler.handle("garmin", "iris", "url", allow="") == 0
    assert sent == []


def test_the_allowlist_is_case_insensitive(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: commands.Run("garmin"))
    assert handler.handle("garmin", "CODEX", "url", allow="Iris, codex") == 0
    assert len(sent) == 1


def test_a_refused_sender_gets_no_reply_and_the_wake_exits_zero(monkeypatch, capsys):
    sent = _capture(monkeypatch)
    _no_browser(monkeypatch)
    assert handler.handle("garmin", "stranger", "go https://example.com", allow="iris") == 0
    assert sent == []
    assert "refused" in capsys.readouterr().err


def test_a_reply_carries_the_transcript_and_the_artifacts(monkeypatch):
    run = commands.Run("garmin")
    run.note("url", "https://example.com/")
    run.attach("/tmp/shot.png")
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: run)
    sent = _capture(monkeypatch)

    assert handler.handle("garmin", "iris", "url", allow="iris") == 0
    recipient, body, files = sent[0]
    assert recipient == "iris"
    assert body.startswith("garmin: ok")
    assert "https://example.com/" in body
    assert files == ["/tmp/shot.png"]


def test_a_failing_verb_still_gets_one_reply_and_exits_zero(monkeypatch):
    run = commands.Run("garmin")
    run.fail("click Submit", "nothing visible matching 'Submit'")
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: run)
    sent = _capture(monkeypatch)
    assert handler.handle("garmin", "iris", "click Submit", allow="iris") == 0
    assert len(sent) == 1
    assert "failed" in sent[0][1]


def test_a_reply_that_cannot_be_sent_is_the_one_thing_that_exits_nonzero(monkeypatch):
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: commands.Run("garmin"))
    monkeypatch.setattr(handler.rt, "send_reply", lambda *a, **k: 1)
    assert handler.handle("garmin", "iris", "url", allow="iris") == 1


def test_attachment_lines_are_not_part_of_the_script(monkeypatch):
    seen = []
    monkeypatch.setattr(
        commands, "run_script",
        lambda seat, body, allow_eval=False, **k: seen.append(body) or commands.Run(seat),
    )
    _capture(monkeypatch)
    handler.handle("garmin", "iris", "url\n\nATTACHED FILE: /tmp/x", allow="iris")
    assert seen == ["url"]


def test_allow_eval_arg_overrides_the_environment(monkeypatch):
    monkeypatch.delenv("AR3_BROWSER_ALLOW_EVAL", raising=False)
    seen = []
    monkeypatch.setattr(
        commands, "run_script",
        lambda seat, body, allow_eval=False, **k: seen.append(allow_eval) or commands.Run(seat),
    )
    _capture(monkeypatch)
    handler.handle("garmin", "iris", "url", allow="iris", allow_eval="1")
    handler.handle("garmin", "iris", "url", allow="iris", allow_eval="0")
    assert seen == [True, False]


def pytest_fail(message):
    raise AssertionError(message)


HELP_SPELLINGS = ["help", "/help", "--help", "/?", "HELP", "  /Help  "]


def _no_run(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the browser ran for a help message")

    monkeypatch.setattr(commands, "run_script", boom)


def test_help_answers_the_usage_and_runs_nothing(monkeypatch):
    usage = handler.rt.usage_from_readme(handler.README)
    assert usage

    sent = _capture(monkeypatch)
    _no_run(monkeypatch)
    for spelling in HELP_SPELLINGS:
        assert handler.handle("garmin", "iris", spelling, allow="iris") == 0
    assert len(sent) == len(HELP_SPELLINGS)
    for recipient, body, files in sent:
        assert recipient == "iris"
        assert body.startswith("garmin: tool-browser — usage")
        assert usage in body
        assert body.endswith("README: apps/tools/browser/README.md")
        assert files == []


def test_help_with_an_attachment_line_is_still_help(monkeypatch):
    sent = _capture(monkeypatch)
    _no_run(monkeypatch)
    handler.handle("garmin", "iris", "help\n\nATTACHED FILE: /tmp/x", allow="iris")
    assert "usage" in sent[0][1]


def test_help_me_open_a_page_is_a_script_not_help(monkeypatch):
    seen = []
    monkeypatch.setattr(
        commands, "run_script",
        lambda seat, script, **k: seen.append(script) or commands.Run(seat),
    )
    _capture(monkeypatch)
    handler.handle("garmin", "iris", "help me open a page", allow="iris")
    handler.handle("garmin", "iris", "url\nhelp", allow="iris")
    assert seen == ["help me open a page", "url\nhelp"]


def test_a_refused_sender_gets_no_help(monkeypatch, capsys):
    sent = _capture(monkeypatch)
    _no_run(monkeypatch)
    assert handler.handle("garmin", "stranger", "help", allow="iris") == 0
    assert sent == []
    assert "refused" in capsys.readouterr().err


def test_the_readme_has_a_usage_section():
    assert handler.rt.usage_from_readme(handler.README)


def test_help_without_a_usage_section_says_so(monkeypatch, tmp_path):
    bare = tmp_path / "README.md"
    bare.write_text("# tool-browser\n\n## State\n\nnone\n")
    monkeypatch.setattr(handler, "README", bare)
    sent = _capture(monkeypatch)
    handler.handle("garmin", "iris", "help", allow="iris")
    assert 'no "## Usage" section in apps/tools/browser/README.md' in sent[0][1]


def _hold_elsewhere(seat):
    """Take the seat lock through a second descriptor, as another process would."""
    import contextlib
    from ar3.locking import file_lock

    stack = contextlib.ExitStack()
    stack.enter_context(file_lock(seatlock.lock_path(seat), blocking=False))
    return stack


def test_a_held_seat_gets_a_busy_reply_and_nothing_runs(monkeypatch):
    monkeypatch.setenv("AR3_BROWSER_SEAT_WAIT", "0.3")
    sent = _capture(monkeypatch)
    _no_run(monkeypatch)
    with _hold_elsewhere("seat"):
        assert handler.handle("seat", "iris", "url", allow="iris") == 0
    assert sent[0][0] == "iris"
    assert sent[0][1].startswith("seat: busy: seat held by ")
    assert sent[0][1].endswith(". Nothing ran.")


def test_help_does_not_wait_for_the_seat(monkeypatch):
    monkeypatch.setenv("AR3_BROWSER_SEAT_WAIT", "30")
    sent = _capture(monkeypatch)
    with _hold_elsewhere("seat"):
        assert handler.handle("seat", "iris", "help", allow="iris") == 0
    assert "usage" in sent[0][1]


def test_a_refused_sender_does_not_learn_the_seat_is_busy(monkeypatch, capsys):
    monkeypatch.setenv("AR3_BROWSER_SEAT_WAIT", "30")
    sent = _capture(monkeypatch)
    with _hold_elsewhere("seat"):
        assert handler.handle("seat", "stranger", "url", allow="iris") == 0
    assert sent == []
    assert "busy" not in capsys.readouterr().err


@pytest.mark.parametrize("reply_dir, kind", [(None, "a8s wake"), ("/tmp/replies", "ar3 tool")])
def test_the_hold_kind_follows_the_mode(monkeypatch, reply_dir, kind):
    if reply_dir:
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", reply_dir)
    else:
        monkeypatch.delenv("AR3_TOOL_REPLY_DIR", raising=False)
    seen = []

    def run(seat, script, **k):
        seen.append(json.loads(seatlock.lock_path(seat).read_text())["kind"])
        return commands.Run(seat)

    monkeypatch.setattr(commands, "run_script", run)
    _capture(monkeypatch)
    handler.handle("seat", "iris", "url", allow="iris")
    assert seen == [kind]


def test_print_mode_does_not_cut_the_body(monkeypatch):
    monkeypatch.setenv("AR3_TOOL_REPLY_DIR", "/tmp/replies")
    run = commands.Run("seat")
    run.note("text", "x" * (handler.MAX_BODY + 500))
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: run)
    sent = _capture(monkeypatch)
    handler.handle("seat", "iris", "text p", allow="iris")
    assert len(sent[0][1]) > handler.MAX_BODY


def _hold_seat_in_a_process(seat):
    import os, subprocess, sys, textwrap
    code = textwrap.dedent(f"""
        import time
        from browser import seatlock
        with seatlock.hold({seat!r}, "test holder"):
            print("held", flush=True)
            time.sleep(60)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert proc.stdout.readline().strip() == "held"
    return proc


def _waiter_with_stub_chrome(monkeypatch):
    from browser import session
    events = []
    monkeypatch.setattr(session.plc, "is_open", lambda s: False)
    monkeypatch.setattr(session, "_start_chrome", lambda s: events.append("spawn") or "base")
    monkeypatch.setattr(session, "_attach", lambda s, b: None)
    monkeypatch.setattr(session, "_window_of_page", lambda s: {"window": 1, "visible": "visible"})
    monkeypatch.setenv("AR3_BROWSER_SEAT_WAIT", "30")
    return events


def _stop_while_waiting(start, proc):
    import threading, time
    from browser import session
    thread = threading.Thread(target=start)
    thread.start()
    time.sleep(1)
    assert thread.is_alive()
    session.bump_stop_epoch("s")
    proc.kill()
    proc.wait()
    thread.join(30)
    assert not thread.is_alive()


def test_a_wake_waiting_for_the_seat_when_the_stop_lands_launches_nothing(monkeypatch):
    events = _waiter_with_stub_chrome(monkeypatch)
    sent = _capture(monkeypatch)
    proc = _hold_seat_in_a_process("s")
    _stop_while_waiting(lambda: handler.handle("s", "iris", "open", allow="iris"), proc)
    assert events == []
    assert "seat closed by a8s stop" in sent[0][1]


def test_a_do_waiting_for_the_seat_when_the_stop_lands_launches_nothing(monkeypatch, capsys, tmp_path):
    import sys
    from browser import cli
    events = _waiter_with_stub_chrome(monkeypatch)
    script = tmp_path / "s.txt"
    script.write_text("open\n")
    monkeypatch.setattr(sys, "argv", ["browser", "--seat", "s", "do", str(script)])
    proc = _hold_seat_in_a_process("s")
    result = []
    _stop_while_waiting(lambda: result.append(cli.main()), proc)
    assert result == [1]
    assert events == []
    assert "seat closed by a8s stop" in capsys.readouterr().out


def test_a_do_that_begins_after_the_stop_opens_chrome(monkeypatch, capsys, tmp_path):
    import sys
    from browser import cli, session
    events = _waiter_with_stub_chrome(monkeypatch)
    monkeypatch.setattr(session.plc, "is_open", lambda s: bool(events))
    monkeypatch.setattr(session, "current_url", lambda s: "about:blank")
    session.bump_stop_epoch("s")
    script = tmp_path / "s.txt"
    script.write_text("open\n")
    monkeypatch.setattr(sys, "argv", ["browser", "--seat", "s", "do", str(script)])
    assert cli.main() == 0
    assert events == ["spawn"]
