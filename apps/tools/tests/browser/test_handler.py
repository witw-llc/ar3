import json
import os
from pathlib import Path

import pytest

from browser import commands
from browser import handler
from browser import plc
from browser import seatlock
from browser import secretstore
from browser import session

from fake_browser import FakeBrowser


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


def test_a_wake_that_runs_a_script_is_use_of_the_seat(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: commands.Run("seat"))
    assert session.last_activity("seat") is None
    assert handler.handle("seat", "iris", "url", allow="iris") == 0
    assert session.last_activity("seat") is not None
    assert len(sent) == 1


@pytest.mark.parametrize("sender, message", [("iris", "help"), ("stranger", "url")])
def test_a_wake_that_runs_no_script_is_not_use_of_the_seat(monkeypatch, sender, message):
    _capture(monkeypatch)
    _no_run(monkeypatch)
    assert handler.handle("seat", sender, message, allow="iris") == 0
    assert session.last_activity("seat") is None


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


VALUE = "hunter2-value"


def _seat_with_secret(monkeypatch, seat="garmin"):
    secretstore.set_secret(seat, "PASSWORD", VALUE, ["https://app.example.com"])
    monkeypatch.setattr(commands.session, "ensure_running", lambda seat, **k: None)
    monkeypatch.setattr(commands.session, "current_url", lambda seat: f"https://example.com/?pw={VALUE}")
    monkeypatch.setattr(commands.session, "snapshot", lambda seat: f'- textbox "Password": {VALUE}')


def test_a_reply_holds_a_stored_value_nowhere_in_its_body_or_attachments(monkeypatch):
    _seat_with_secret(monkeypatch)
    sent = _capture(monkeypatch)
    monkeypatch.setattr(plc, "run", lambda *a, **k: f"### Result\nok {VALUE}\n")
    assert handler.handle("garmin", "iris", f"url\nsnap\ntype {VALUE}\nbogus", allow="iris") == 0
    (recipient, body, files), = sent
    assert body.startswith("garmin: failed: unknown command: bogus")
    assert VALUE not in body
    assert "https://example.com/?pw=[secret:PASSWORD]" in body
    assert files
    for path in files:
        with open(path, "rb") as handle:
            assert VALUE.encode() not in handle.read()


def test_the_reply_body_is_scrubbed_a_second_time_after_it_is_built(monkeypatch):
    run = commands.Run("garmin", secretstore.Scrubber({"PASSWORD": {"value": VALUE, "origins": ["https://app.example.com"]}}))
    run.steps.append({"command": "x", "ok": True, "output": f"typed {VALUE}"})
    run.error = f"failed with {VALUE}"
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: run)
    sent = _capture(monkeypatch)
    handler.handle("garmin", "iris", "url", allow="iris")
    body = sent[0][1]
    assert VALUE not in body
    assert body.startswith("garmin: failed: failed with [secret:PASSWORD]")
    assert "typed [secret:PASSWORD]" in body


def test_the_body_is_cut_after_the_scrub_not_before(monkeypatch):
    """The cut lands five characters into the value. Cut first, the reply would end in `hunte`."""
    header = "garmin: ok\n\n  text p\n      "
    run = commands.Run("garmin", secretstore.Scrubber({"PASSWORD": {"value": VALUE, "origins": ["https://app.example.com"]}}))
    run.note("text p", "x" * (handler.MAX_BODY - len(header) - 5) + VALUE + "y" * 100)
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: run)
    sent = _capture(monkeypatch)
    handler.handle("garmin", "iris", "text p", allow="iris")
    body = sent[0][1]
    assert len(body) == handler.MAX_BODY
    assert "hunte" not in body
    assert body.endswith("[secr")


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_a_store_that_cannot_be_read_gets_a_failed_reply_and_no_script_runs(monkeypatch):
    _seat_with_secret(monkeypatch)
    target = secretstore.path("garmin")
    os.chmod(target, 0o644)
    monkeypatch.setattr(commands.session, "ensure_running", lambda *a, **k: pytest.fail("Chrome opened"))
    sent = _capture(monkeypatch)
    assert handler.handle("garmin", "iris", "url", allow="iris") == 0
    (recipient, body, files), = sent
    assert body.startswith("garmin: failed: secret store ")
    assert f"chmod 600 {target}" in body
    assert VALUE not in body
    assert files == []


@pytest.mark.parametrize("line", ["secret set PASSWORD other-value", "secret list", "secret rm PASSWORD"])
def test_a_tell_cannot_set_list_or_remove_a_secret(monkeypatch, line):
    _seat_with_secret(monkeypatch)
    target = secretstore.path("garmin")
    before = open(target, "rb").read()
    sent = _capture(monkeypatch)
    assert handler.handle("garmin", "iris", line, allow="iris") == 0
    assert sent[0][1].startswith("garmin: failed: unknown command: secret")
    assert open(target, "rb").read() == before


# --- the exit code: a wake always 0, `ar3 tool` 1 when the script failed ---------------------------------


def _print_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path / "replies"))


def _failed_run(seat="seat"):
    run = commands.Run(seat)
    run.fail("click Submit", "nothing visible matching 'Submit'")
    return run


def test_a_wake_exits_zero_when_the_script_failed(monkeypatch):
    monkeypatch.delenv("AR3_TOOL_REPLY_DIR", raising=False)
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: _failed_run())
    sent = _capture(monkeypatch)
    assert handler.handle("seat", "iris", "click Submit", allow="iris") == 0
    assert sent[0][1].startswith("seat: failed: nothing visible")


def test_a_wake_exits_zero_when_the_seat_was_busy(monkeypatch):
    monkeypatch.delenv("AR3_TOOL_REPLY_DIR", raising=False)
    monkeypatch.setenv("AR3_BROWSER_SEAT_WAIT", "0.3")
    sent = _capture(monkeypatch)
    _no_run(monkeypatch)
    with _hold_elsewhere("seat"):
        assert handler.handle("seat", "iris", "url", allow="iris") == 0
    assert sent[0][1].endswith("Nothing ran.")


def test_ar3_tool_exits_one_when_the_script_failed_and_still_prints_the_reply(monkeypatch, tmp_path, capsys):
    _print_mode(monkeypatch, tmp_path)
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: _failed_run())
    assert handler.handle("seat", "operator", "click Submit", allow="operator") == 1
    out = capsys.readouterr().out
    assert out.startswith("seat: failed: nothing visible matching 'Submit'")
    assert "! click Submit" in out


def test_ar3_tool_exits_zero_when_the_script_ran_to_the_end(monkeypatch, tmp_path, capsys):
    _print_mode(monkeypatch, tmp_path)
    run = commands.Run("seat")
    run.note("url", "https://example.com/")
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: run)
    assert handler.handle("seat", "operator", "url", allow="operator") == 0
    assert capsys.readouterr().out.startswith("seat: ok")


def test_ar3_tool_exits_one_when_the_seat_was_busy_and_nothing_ran(monkeypatch, tmp_path, capsys):
    _print_mode(monkeypatch, tmp_path)
    monkeypatch.setenv("AR3_BROWSER_SEAT_WAIT", "0.3")
    _no_run(monkeypatch)
    with _hold_elsewhere("seat"):
        assert handler.handle("seat", "operator", "url", allow="operator") == 1
    assert capsys.readouterr().out.rstrip().endswith("Nothing ran.")


def test_ar3_tool_help_exits_zero(monkeypatch, tmp_path, capsys):
    _print_mode(monkeypatch, tmp_path)
    _no_run(monkeypatch)
    assert handler.handle("seat", "operator", "help", allow="operator") == 0


def test_a_reply_that_could_not_be_sent_keeps_its_own_code_in_print_mode_too(monkeypatch, tmp_path):
    _print_mode(monkeypatch, tmp_path)
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: _failed_run())
    monkeypatch.setattr(handler.rt, "send_reply", lambda *a, **k: 7)
    assert handler.handle("seat", "operator", "x", allow="operator") == 7


def test_a_failed_recipe_step_makes_ar3_tool_exit_one(monkeypatch, tmp_path, capsys):
    FakeBrowser(monkeypatch)
    _print_mode(monkeypatch, tmp_path)
    assert handler.handle("seat", "operator", "recipe nosuchsite", allow="operator") == 1
    assert "failed: unknown recipe: nosuchsite" in capsys.readouterr().out
    assert handler.handle("seat", "operator", "recipe", allow="operator") == 0


# --- the node's own recipes ---------------------------------------------------------------------------------
#
# They come from `recipes/` in the root of the registered tool-browser node whose name is the seat. The
# registry says which directory that is: the handler asks nothing of the working directory or the environment.


NODE_RECIPE = (
    "from browser.recipe import Recipe\n"
    "recipe = Recipe('mine', 'The node recipe', origins=['https://mine.example.com'])\n"
    "recipe.steps('go', help='Go.', steps='go https://mine.example.com/')\n"
)
TOOL_BROWSER = "/repo/apps/a8s/definitions/tool-browser.json"


def _node(tmp_path, monkeypatch, seat="seat", definition=TOOL_BROWSER):
    """A node directory with one recipe, registered under `seat` in an a8s home of its own."""
    node = tmp_path / "node"
    (node / "recipes").mkdir(parents=True)
    (node / "recipes" / "mine.py").write_text(NODE_RECIPE)
    home = tmp_path / "a8s-registry"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("A8S_HOME", str(home))
    (home / "a8s.json").write_text(json.dumps({
        "agents": {seat: {"root": str(node), "definition": definition, "vars": {}}},
        "aliases": {}, "namespaces": {},
    }))
    return str(node)


def test_a_wake_for_a_registered_node_loads_the_recipes_directory_of_that_node(monkeypatch, tmp_path):
    from browser import recipe

    node = _node(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(
        commands, "run_script",
        lambda seat, script, **k: seen.append(k["recipes"]) or commands.Run(seat),
    )
    _capture(monkeypatch)
    handler.handle("seat", "iris", "recipe", allow="iris")
    handler.handle("other", "iris", "recipe", allow="iris")
    assert [library.dirs for library in seen] == [[Path(node) / "recipes"], []]
    assert all(isinstance(library, recipe.Library) for library in seen)


def test_the_listing_in_a_reply_names_the_node_recipe_and_a_seat_that_is_no_node_has_none(monkeypatch, tmp_path):
    from browser import recipe

    monkeypatch.setattr(recipe, "BUNDLED_DIR", tmp_path / "no-recipes-here")
    _node(tmp_path, monkeypatch)
    sent = _capture(monkeypatch)
    handler.handle("seat", "iris", "recipe", allow="iris")
    handler.handle("other", "iris", "recipe", allow="iris")
    assert "mine  The node recipe  [node]" in sent[0][1]
    assert "no recipes" in sent[1][1] and "mine" not in sent[1][1]


def test_a_node_of_another_kind_loads_no_recipes_directory(monkeypatch, tmp_path):
    from browser import recipe

    monkeypatch.setattr(recipe, "BUNDLED_DIR", tmp_path / "no-recipes-here")
    _node(tmp_path, monkeypatch, definition="/repo/apps/a8s/definitions/tool-command-shell.json")
    sent = _capture(monkeypatch)
    handler.handle("seat", "iris", "recipe", allow="iris")
    assert "no recipes" in sent[0][1] and "mine" not in sent[0][1]


def test_the_node_directory_is_not_taken_from_the_working_directory_or_the_environment(monkeypatch, tmp_path):
    from browser import recipe

    monkeypatch.setattr(recipe, "BUNDLED_DIR", tmp_path / "no-recipes-here")
    node = _node(tmp_path, monkeypatch, seat="elsewhere")
    monkeypatch.chdir(node)
    monkeypatch.setenv("A8S_TURN_RECIPIENT", "seat")
    sent = _capture(monkeypatch)
    handler.handle("seat", "iris", "recipe", allow="iris")
    assert "no recipes" in sent[0][1] and "mine" not in sent[0][1]


def test_a_message_cannot_install_or_change_a_recipe(monkeypatch, tmp_path):
    node = _node(tmp_path, monkeypatch)
    attached = tmp_path / "evil.py"
    attached.write_text(NODE_RECIPE.replace("mine", "evil").replace("The node recipe", "Evil"))
    before = sorted((tmp_path / "node" / "recipes").iterdir())
    FakeBrowser(monkeypatch)
    sent = _capture(monkeypatch)
    for message in (
        f"recipe\n\nATTACHED FILE: {attached}",
        "recipe evil",
        "recipe mine go --recipes /tmp",
        "recipe ../../evil",
    ):
        handler.handle("seat", "iris", message, allow="iris")
    assert sorted((tmp_path / "node" / "recipes").iterdir()) == before
    assert "evil" not in sent[0][1].replace("unknown recipe", "")
    assert all(body[1].startswith("seat: ") for body in sent)
