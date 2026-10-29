import json
import os
import re
import stat
import subprocess
import time

import pytest

from browser import commands
from browser import plc
from browser import secretstore


def _stub_browser(monkeypatch, tmp_path, **overrides):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(commands.session, "ensure_running", lambda seat, **k: None)
    monkeypatch.setattr(commands.session, "current_url", lambda seat: "https://example.com/")
    monkeypatch.setattr(commands.session, "snapshot", lambda seat: "- button 'Sign in'")
    monkeypatch.setattr(plc, "run", lambda *a, **k: "### Result\nok\n")
    # No Chrome stands behind the stub, so its page list cannot be read (`session.pages`).
    monkeypatch.setattr(commands.session, "pages", lambda seat: None)
    for name, value in overrides.items():
        monkeypatch.setattr(commands.session, name, value)


def test_comments_blanks_and_attachment_lines_are_not_commands():
    body = "# a comment\n\nurl\nATTACHED FILE: notes.zip\ngo https://example.com\n"
    assert commands.script_commands(body) == [
        ("url", None),
        ("go https://example.com", None),
    ]


def test_a_run_stops_at_the_first_failure(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("seat", "url\nbogus thing\nurl\n")
    assert not run.ok
    assert [step["command"] for step in run.steps] == ["url", "bogus thing"]
    assert "unknown command: bogus" in run.error


def test_a_failure_attaches_the_page_it_died_on(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("seat", "bogus\n")
    assert len(run.files) == 1
    assert open(run.files[0]).read() == "- button 'Sign in'"


def test_click_accepts_a_ref_from_the_last_snap(monkeypatch, tmp_path):
    """#10, end to end: `click e5` must reach playwright-cli with the ref
    itself as the target, not fall through to resolve's text/CSS matcher."""
    _stub_browser(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(plc, "run", lambda *a, **k: calls.append(a) or "### Result\nok\n")
    run = commands.run_script("seat", "click e5\n")
    assert run.ok, run.error
    assert ("seat", "eval", "() => true", "e5") in calls
    assert ("seat", "click", "e5") in calls


def test_click_reports_a_stale_ref_instead_of_nothing_visible(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)

    def fake_run(seat, *args, **kwargs):
        if args[0] == "eval":
            raise plc.BrowserError(
                "Error: Ref e5 not found in the current page snapshot. "
                "Try capturing new snapshot."
            )
        raise AssertionError("click must not run once the ref fails to resolve")

    monkeypatch.setattr(plc, "run", fake_run)
    run = commands.run_script("seat", "click e5\n")
    assert not run.ok
    assert run.error == "ref e5 is not on the page any more — snap again"


def test_eval_is_refused_unless_the_seat_opted_in(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("seat", "eval 1 + 1")
    assert not run.ok
    assert "AR3_BROWSER_ALLOW_EVAL" in run.error


def test_eval_runs_once_the_seat_opts_in(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(plc, "evaluate", lambda seat, expression, timeout=30: "2")
    run = commands.run_script("seat", "eval 1 + 1", allow_eval=True)
    assert run.ok
    assert run.steps[0]["output"] == "2"


def test_run_code_is_refused_unless_the_seat_opted_in(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        plc, "run_code",
        lambda *a, **k: pytest_fail("run-code reached playwright without the opt-in"),
    )
    run = commands.run_script("seat", "run-code <<END\nreturn 1;\nEND\n")
    assert not run.ok
    assert "AR3_BROWSER_ALLOW_EVAL" in run.error


def test_run_code_reaches_playwright_once_the_seat_opts_in(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    bodies = _capture_code(monkeypatch, "### Result\nclicked\n")
    run = commands.run_script("seat", "run-code <<END\nreturn 'clicked';\nEND\n", allow_eval=True)
    assert run.ok
    assert bodies == ["return 'clicked';"]
    assert run.steps[0]["output"] == "clicked"


def test_run_code_without_a_block_takes_the_rest_of_the_line(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    bodies = _capture_code(monkeypatch)
    run = commands.run_script("seat", "run-code return page.url(); // it's here\n", allow_eval=True)
    assert run.ok
    assert bodies == ["return page.url(); // it's here"]


def test_a_block_is_taken_verbatim(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    bodies = _capture_code(monkeypatch)
    script = (
        "run-code <<END\n"
        "# this is code, not a comment\n"
        "\n"
        "  const menu = page.locator('button').first();\n"
        "go https://example.com/not-a-command\n"
        "  await menu.click();\n"
        "END\n"
        "url\n"
    )
    run = commands.run_script("seat", script, allow_eval=True)
    assert run.ok
    assert bodies == [
        "# this is code, not a comment\n"
        "\n"
        "  const menu = page.locator('button').first();\n"
        "go https://example.com/not-a-command\n"
        "  await menu.click();"
    ]
    assert [step["command"] for step in run.steps] == ["run-code", "url"]


def test_only_a_whole_line_marker_closes_a_block(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    bodies = _capture_code(monkeypatch)
    script = (
        "run-code <<END\n"
        "ENDING;\n"
        "// the setup ends here: END\n"
        "  END\n"
        "url\n"
    )
    run = commands.run_script("seat", script, allow_eval=True)
    assert run.ok
    assert bodies == ["ENDING;\n// the setup ends here: END"]
    assert [step["command"] for step in run.steps] == ["run-code", "url"]


def test_a_marker_inside_a_longer_line_does_not_close_a_block(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    bodies = _capture_code(monkeypatch)
    script = (
        "run-code <<END\n"
        "await page.getByText('END').click();\n"
        "return 'after the word END';\n"
        "END\n"
    )
    run = commands.run_script("seat", script, allow_eval=True)
    assert run.ok
    assert bodies == ["await page.getByText('END').click();\nreturn 'after the word END';"]


def test_two_blocks_in_one_script_keep_their_own_markers(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    bodies = _capture_code(monkeypatch)
    script = (
        "run-code <<ONE\n"
        "return 'first';\n"
        "ONE\n"
        "run-code <<TWO\n"
        "ONE\n"
        "return 'second';\n"
        "TWO\n"
    )
    run = commands.run_script("seat", script, allow_eval=True)
    assert run.ok
    assert bodies == ["return 'first';", "ONE\nreturn 'second';"]


def test_an_unterminated_block_names_the_marker_and_runs_nothing(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        plc, "run_code", lambda *a, **k: pytest_fail("a truncated body reached playwright")
    )
    run = commands.run_script("seat", "url\nrun-code <<END\nreturn 1;\n", allow_eval=True)
    assert not run.ok
    assert "unterminated <<END" in run.error
    assert [step["command"] for step in run.steps] == ["run-code <<END"]


def test_a_block_feeds_any_verb_that_takes_the_rest_of_the_line(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(plc, "run", lambda *a, **k: calls.append(a) or "### Result\nok\n")
    run = commands.run_script("seat", "type <<TEXT\nline one\n  line two\nTEXT\n")
    assert run.ok
    assert calls[0] == ("seat", "type", "line one\n  line two")


def test_an_opener_that_also_carries_an_argument_is_refused(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        plc, "run", lambda *a, **k: pytest_fail("a truncated argument reached playwright")
    )
    run = commands.run_script("seat", "type Dear team <<TEXT\nand the rest\nTEXT\n")
    assert not run.ok
    assert run.error == (
        "a line that opens a block cannot also carry an inline argument: "
        "type Dear team <<TEXT"
    )
    assert [step["command"] for step in run.steps] == ["type Dear team <<TEXT"]


def test_run_code_keeps_no_exception_to_the_opener_rule(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        plc, "run_code", lambda *a, **k: pytest_fail("a truncated body reached playwright")
    )
    run = commands.run_script(
        "seat", "run-code some.js() <<END\nreturn 1;\nEND\n", allow_eval=True
    )
    assert not run.ok
    assert "cannot also carry an inline argument" in run.error
    assert [step["command"] for step in run.steps] == ["run-code some.js() <<END"]


def test_go_refuses_anything_that_is_not_a_url(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("seat", "go example.com")
    assert not run.ok
    assert "full http(s) URL" in run.error


def test_missing_arguments_report_usage(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("seat", "fill username")
    assert "usage: fill" in run.error


def test_an_unbalanced_quote_is_a_parse_error_not_a_crash(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("seat", "fill 'username")
    assert "unparseable" in run.error


def test_shot_attaches_the_screenshot(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(plc, "screenshot", lambda seat, path, timeout=60: _touch(path))
    run = commands.run_script("seat", "shot")
    assert run.ok
    assert run.files and run.files[0].endswith("-screen.png")


def test_long_waits_are_capped(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    slept = []
    monkeypatch.setattr(commands.time, "sleep", slept.append)
    commands.run_script("seat", "wait 9000")
    assert slept == [9.0]


def test_console_attaches_the_log_and_echoes_it(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        plc, "run",
        lambda *a, **k: "### Result\nTotal messages: 1\n\n[ERROR] boom @ :0\n",
    )
    run = commands.run_script("seat", "console")
    assert run.ok
    assert run.files and run.files[0].endswith("-console.log")
    assert "[ERROR] boom" in open(run.files[0]).read()
    assert "[ERROR] boom" in run.steps[0]["output"]


def test_console_passes_a_minimum_level_through(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(plc, "run", lambda *a, **k: calls.append(a) or "### Result\nok\n")
    run = commands.run_script("seat", "console warning")
    assert run.ok
    assert calls[0] == ("seat", "console", "warning")


def test_video_records_a_round_trip(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    calls = []

    def fake_run(seat, *args, **kwargs):
        calls.append(args)
        if args[0] == "video-stop":
            with open(commands.session.video_state("seat")["path"], "w") as handle:
                handle.write("webm")
        return "### Result\nok\n"

    monkeypatch.setattr(plc, "run", fake_run)
    run = commands.run_script("seat", "video-start trip\nvideo-chapter leg one\nvideo-stop\n")
    assert run.ok
    assert calls[0][0] == "video-start" and calls[0][1].endswith("-trip.webm")
    assert calls[1] == ("video-chapter", "leg one")
    assert calls[2] == ("video-stop",)
    assert run.files and run.files[0].endswith("-trip.webm")
    assert commands.session.video_state("seat") is None


def test_video_start_refuses_a_second_recording(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    commands.session.save_video_state(
        "seat", {"path": "/tmp/active.webm", "started_at": 0, "chapters": 0}
    )
    run = commands.run_script("seat", "video-start")
    assert not run.ok
    assert "already recording" in run.error


def test_video_stop_without_a_recording_is_an_error(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("seat", "video-stop")
    assert not run.ok
    assert "no recording in progress" in run.error


def test_video_stop_rejects_an_empty_recording(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    empty = str(tmp_path / "empty.webm")
    _touch(empty)
    commands.session.save_video_state(
        "seat", {"path": empty, "started_at": 0, "chapters": 0}
    )
    monkeypatch.setattr(plc, "run", lambda *a, **k: "### Result\nok\n")
    run = commands.run_script("seat", "video-stop")
    assert not run.ok
    assert "is empty" in run.error
    assert commands.session.video_state("seat") is None


def test_video_stop_does_not_open_the_browser(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        commands.session, "ensure_running",
        lambda seat, **k: pytest_fail("video-stop opened the browser"),
    )
    run = commands.run_script("seat", "video-stop")
    assert not run.ok
    assert "no recording in progress" in run.error


def pytest_fail(message):
    raise AssertionError(message)


def _capture_code(monkeypatch, output="### Result\nok\n"):
    """Record every body handed to plc.run_code, and answer with `output`."""
    bodies = []

    def fake_run_code(seat, body, timeout=60):
        bodies.append(body)
        return output

    monkeypatch.setattr(plc, "run_code", fake_run_code)
    return bodies


def _touch(path):
    open(path, "w").close()
    return path


def _stub_targets(monkeypatch):
    monkeypatch.setattr(commands.resolve, "click_target", lambda seat, arg: arg)


def _stub_real_argv(monkeypatch, answer=None):
    """A playwright-cli stub that enforces its REAL argv contract.

    A stub that accepts anything only proves this repo builds the argv it meant
    to build. The installed CLI takes exactly one positional for `upload` — its
    help says "one or multiple files" and names the argument "the absolute
    paths", and the parser still rejects two before it opens a browser. Believing
    the help shipped a broken verb, so the contract lives here now.
    """
    seen = []

    def run(seat, *args, **kwargs):
        seen.append(args)
        verb, rest = args[0], list(args[1:])
        if verb == "upload":
            positional = [arg for arg in rest if not arg.startswith("-")]
            if len(positional) != 1:
                raise plc.BrowserError(
                    f"too many arguments: expected 1, received {len(positional)}"
                )
        if verb == "drop":
            if not rest or rest[0].startswith("-"):
                raise plc.BrowserError("drop needs a target")
            flags = rest[1:]
            if any(flags[index] != "--path" for index in range(0, len(flags), 2)):
                raise plc.BrowserError("drop takes files as repeated --path")
        return answer(args) if answer else ""

    monkeypatch.setattr(plc, "run", run)
    return seen


def test_upload_refuses_a_relative_path(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("seat", "upload notes.txt\n")
    assert not run.ok
    assert "not an absolute path" in run.error


def test_upload_refuses_a_file_that_is_not_there(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("seat", f"upload {tmp_path / 'gone.txt'}\n")
    assert not run.ok
    assert "no such file" in run.error


def test_upload_hands_its_one_path_to_playwright(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    seen = _stub_real_argv(monkeypatch)
    one = tmp_path / "a.txt"
    one.write_text("a")
    run = commands.run_script("seat", f"upload {one}\n")
    assert run.ok, run.error
    assert seen == [("upload", str(one))]
    assert run.steps[0]["output"] == "a.txt"


def test_upload_refuses_several_files_and_names_drop(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _stub_real_argv(monkeypatch)
    one, two = tmp_path / "a.txt", tmp_path / "b.txt"
    one.write_text("a")
    two.write_text("b")
    run = commands.run_script("seat", f"upload {one} {two}\n")
    # The chooser is answered once and closes, so a second upload goes nowhere.
    # The refusal has to point at the verb that does take several.
    assert not run.ok
    assert "takes one file" in run.error
    assert "drop" in run.error


def test_upload_never_sends_an_argv_the_real_cli_refuses(monkeypatch, tmp_path):
    """The stub enforces playwright-cli's own argv rules, not this repo's."""
    _stub_browser(monkeypatch, tmp_path)
    _stub_real_argv(monkeypatch)
    one = tmp_path / "a.txt"
    one.write_text("a")
    run = commands.run_script("seat", f"upload {one}\n")
    assert run.ok, run.error
    assert "expected 1" not in (run.error or "")


def test_drop_sends_one_path_flag_per_file(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    seen = _stub_real_argv(monkeypatch)
    one, two = tmp_path / "a.txt", tmp_path / "b.txt"
    one.write_text("a")
    two.write_text("b")
    run = commands.run_script("seat", f"drop div.box {one} {two}\n")
    assert run.ok, run.error
    assert seen == [("drop", "div.box", "--path", str(one), "--path", str(two))]


def test_drop_needs_a_target_and_a_file(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    run = commands.run_script("seat", "drop div.box\n")
    assert not run.ok
    assert "drop <target> <path>" in run.error


class FakeDownloads:
    """Chrome as the download verb meets it: each download is written into the
    directory Chrome was last pointed at, and fixed there once it starts."""

    def __init__(self, monkeypatch, seat="seat"):
        self.routed = commands.session.downloads_dir(seat)
        self.started = []
        self.on_click = []
        monkeypatch.setattr(plc, "run_code", self.run_code)
        monkeypatch.setattr(plc, "run", self.click)

    def run_code(self, seat, body, timeout=60):
        match = re.search(r"downloadPath: '([^']*)'", body)
        if match:
            self.routed = match.group(1)
        return ""

    def click(self, seat, *args, **kwargs):
        self.on_click.pop(0)(self)
        return ""

    def start(self, name, data):
        partial = os.path.join(self.routed, name + commands.PARTIAL_DOWNLOAD)
        with open(partial, "wb") as handle:
            handle.write(data)
        self.started.append(partial)
        return partial

    @staticmethod
    def finish(partial):
        os.replace(partial, partial[: -len(commands.PARTIAL_DOWNLOAD)])


def test_a_late_file_from_a_timed_out_download_is_never_returned_for_a_later_click(
    monkeypatch, tmp_path
):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    chrome = FakeDownloads(monkeypatch)

    def second(c):
        c.start("current.png", b"CURRENT IMAGE")
        c.finish(c.started[0])  # the first call's download lands during the second

    chrome.on_click = [lambda c: c.start("prior.png", b"PRIOR IMAGE"), second]
    first = commands.run_script("seat", "download #first 0\n")
    assert not first.ok and "prior.png.crdownload is still arriving" in first.error

    later = commands.run_script("seat", "download #second 0\n")
    assert not later.ok, "the prior download was handed back for a later click"
    assert "current.png.crdownload is still arriving" in later.error
    assert not any(path.endswith("prior.png") for path in later.files)


def test_a_download_returns_its_own_bytes_while_an_earlier_one_finishes(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    chrome = FakeDownloads(monkeypatch)

    def second(c):
        c.finish(c.started[0])
        c.finish(c.start("current.png", b"CURRENT IMAGE"))

    chrome.on_click = [lambda c: c.start("prior.png", b"PRIOR IMAGE"), second]
    commands.run_script("seat", "download #first 0\n")
    later = commands.run_script("seat", "download #second 0\n")
    assert later.ok, later.error
    assert open(later.files[0], "rb").read() == b"CURRENT IMAGE"


def test_two_same_name_downloads_in_one_second_keep_their_own_bytes(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    monkeypatch.setattr(commands.time, "strftime", lambda _: "20260924T000000")
    chrome = FakeDownloads(monkeypatch)
    chrome.on_click = [
        lambda c: c.finish(c.start("same.png", b"FIRST")),
        lambda c: c.finish(c.start("same.png", b"SECOND")),
    ]
    one = commands.run_script("seat", "download #one 0\n")
    two = commands.run_script("seat", "download #two 0\n")
    assert one.ok and two.ok, (one.error, two.error)
    assert one.files[0] != two.files[0]
    assert open(one.files[0], "rb").read() == b"FIRST"
    assert open(two.files[0], "rb").read() == b"SECOND"


def test_an_artifact_path_is_never_one_already_taken(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(commands.time, "strftime", lambda _: "20260924T000000")
    first = commands.artifact("seat", "screen.png")
    open(first, "w").close()
    assert commands.artifact("seat", "screen.png") != first


def test_download_attaches_the_file_the_click_saved_into_the_seat(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    chrome = FakeDownloads(monkeypatch)
    chrome.on_click = [lambda c: c.finish(c.start("chart.png", b"\x89PNG bytes"))]

    run = commands.run_script("seat", "download button.save\n")
    assert run.ok, run.error
    assert len(run.files) == 1
    assert run.files[0].startswith(commands.session.artifacts_dir("seat"))
    assert run.files[0].endswith("chart.png")
    assert open(run.files[0], "rb").read() == b"\x89PNG bytes"
    # Moved, not copied, and the call's own directory goes with it.
    assert os.listdir(commands.session.downloads_dir("seat")) == []


def test_a_download_a_page_named_after_a_stored_value_is_kept_under_a_name_that_holds_none(monkeypatch, tmp_path):
    """The name a page gives a file goes into the path of the attachment, and the reply is sent with `a8s tell --attach=<path>`:
    a name with the stored PIN in it would be in a command line. The extension stays when it is clean."""
    _with_secret(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "PIN", "482916", [ORIGIN])
    _stub_targets(monkeypatch)
    chrome = FakeDownloads(monkeypatch)
    chrome.on_click = [lambda c: c.finish(c.start("invoice-482916.csv", b"a,b\n"))]
    run = commands.run_script("seat", "download button.save\n")
    assert run.ok, run.error
    assert os.path.basename(run.files[0]).endswith("-download.csv") and "482916" not in run.files[0]
    assert open(run.files[0], "rb").read() == b"a,b\n"
    chrome.on_click = [lambda c: c.finish(c.start("report.482916", b"x"))]
    run = commands.run_script("seat", "download button.save\n")
    assert os.path.basename(run.files[0]).endswith("-download") and "482916" not in run.files[0]
    chrome.on_click = [lambda c: c.finish(c.start("report.csv", b"x"))]
    assert commands.run_script("seat", "download button.save\n").files[0].endswith("report.csv"), "a clean name is kept"


def test_a_run_holds_what_its_scrubber_holds_and_nothing_without_one():
    assert commands.Run("seat", None).holds("anything at all") is False
    run = commands.Run("seat", secretstore.Scrubber({"PIN": {"value": "482916", "origins": [ORIGIN]}}))
    assert run.holds("pin 482916 here") is True and run.holds("pin 482917 here") is False


def test_the_end_of_a_value_that_has_a_dot_is_no_extension_of_a_download(monkeypatch, tmp_path):
    """`os.path.splitext` takes what follows the last dot, and for a value like `Quartz.Ledger5819` that is the second half of
    it. An extension is kept only when it is a dot and a few letters or digits, and the scrub of the name leaves it alone."""
    _with_secret(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "NAME", "Quartz.Ledger5819", [ORIGIN])
    secretstore.set_secret("seat", "DATE", "Summer.2026!x", [ORIGIN])
    secretstore.set_secret("seat", "WORD", "xlsx", [ORIGIN])
    _stub_targets(monkeypatch)
    chrome = FakeDownloads(monkeypatch)
    for page_name, saved_as in (
        ("Quartz.Ledger5819", "download"),
        ("Quartz.Ledger5819.pdf", "download.pdf"),
        ("report.Quartz.Ledger5819", "download"),
        ("Summer.2026!x", "download"),
        ("Summer.2026!x.csv", "download.csv"),
        ("xlsx.xlsx", "download"),
        ("sheet.xlsx", "download"),  # the value is the extension: the scrub takes it, so it is not kept
        ("xlsx-sheet.csv", "download.csv"),
        ("Quartz.Ledger5819.my file", "download"),  # not a dot and a few letters or digits: not an extension
        ("xlsx-sheet.averyveryverylongextension", "download"),
        ("xlsx-sheet.<b>", "download"),
        ("notes.txt", "notes.txt"),
    ):
        chrome.on_click = [lambda c, n=page_name: c.finish(c.start(n, b"x"))]
        run = commands.run_script("seat", "download button.save\n")
        assert run.ok, run.error
        name = os.path.basename(run.files[0])
        assert re.fullmatch(r"\d{8}T\d{6}(-\d+)?-" + re.escape(saved_as), name), (page_name, name)  # a time stamp, the name


def test_download_points_chrome_at_a_directory_of_its_own_before_clicking(
    monkeypatch, tmp_path
):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    chrome = FakeDownloads(monkeypatch)
    seen = []
    chrome.on_click = [lambda c: seen.append(c.routed) or c.finish(c.start("a.txt", b"a"))]
    commands.run_script("seat", "download button.save\n")
    base = commands.session.downloads_dir("seat")
    assert seen[0] != base
    assert os.path.dirname(seen[0]) == base


def test_download_waits_for_a_partial_file_to_finish(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    chrome = FakeDownloads(monkeypatch)
    chrome.on_click = [lambda c: c.start("late.pdf", b"p")]
    sleeps = []

    def finish_later(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            chrome.finish(chrome.started[0])

    monkeypatch.setattr(commands.time, "sleep", finish_later)
    run = commands.run_script("seat", "download button.save 5\n")
    assert run.ok, run.error
    assert len(sleeps) == 3
    assert run.files[0].endswith("late.pdf")


def test_download_ignores_files_that_were_already_there(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    with open(os.path.join(commands.session.downloads_dir("seat"), "old.txt"), "wb") as handle:
        handle.write(b"earlier")
    chrome = FakeDownloads(monkeypatch)
    chrome.on_click = [lambda c: c.finish(c.start("new.txt", b"now"))]
    run = commands.run_script("seat", "download button.save\n")
    assert run.ok, run.error
    assert run.files[0].endswith("new.txt")


def test_download_fails_when_nothing_downloads(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    chrome = FakeDownloads(monkeypatch)
    chrome.on_click = [lambda c: None]
    run = commands.run_script("seat", "download button.save 0\n")
    assert not run.ok
    assert "saved no finished file" in run.error
    # An empty call directory is not left behind.
    assert os.listdir(commands.session.downloads_dir("seat")) == []


def test_download_that_never_finishes_names_the_partial_file(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _stub_targets(monkeypatch)
    chrome = FakeDownloads(monkeypatch)
    chrome.on_click = [lambda c: c.start("big.iso", b"x")]
    run = commands.run_script("seat", "download button.save 0\n")
    assert not run.ok
    assert "big.iso.crdownload is still arriving" in run.error
    assert os.path.dirname(chrome.started[0]) in run.error


def test_text_arrives_as_the_page_wrote_it(monkeypatch, tmp_path):
    """The private code file returns the text in a JSON object, and the verb decodes it once."""
    import json
    page_text = 'line one\nsaid "hi" \\ back\\slash, a literal \\n, caf\u00e9 \u2014 \U0001F600'
    for ensure_ascii in (False, True):
        _stub_browser(monkeypatch, tmp_path)
        printed = json.dumps({"value": page_text}, ensure_ascii=ensure_ascii)
        monkeypatch.setattr(plc, "run", lambda *a, printed=printed, **k: f"{printed}\n")
        run = commands.run_script("seat", "text #reply\n")
        assert run.ok, run.error
        assert run.steps[0]["output"] == page_text


def test_a_stop_between_steps_ends_the_script_with_the_stop_text(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    ran = []

    def step(seat, verb, args, run, allow_eval, script_epoch=None):
        ran.append(verb)
        if verb == "url":
            commands.session.bump_stop_epoch(seat)
        return "ok"

    monkeypatch.setattr(commands, "_step", step)
    run = commands.run_script("seat", "url\nopen\nurl\n")
    assert ran == ["url"]
    assert not run.ok
    assert [s["command"] for s in run.steps] == ["url", "open"]
    assert run.error == "seat closed by a8s stop"
    assert run.files == []


# --- secrets --------------------------------------------------------------------

VALUE = "hunter2-value"
ORIGIN = "https://app.example.com"
TYPED = '{"typed":"https://app.example.com"}\n'
_REAL_PLC_RUN = plc.run


def _with_secret(monkeypatch, tmp_path, **overrides):
    """A stubbed seat whose store holds PASSWORD, allowed on one origin."""
    _stub_browser(monkeypatch, tmp_path, **overrides)
    secretstore.set_secret("seat", "PASSWORD", VALUE, [ORIGIN])


def _everything(run):
    """All the text a run can hand back, as one string."""
    attached = []
    for path in run.files:
        with open(path, "rb") as handle:
            attached.append(handle.read().decode("utf-8", "replace"))
    return "\n".join([run.transcript(), json.dumps(run.steps), str(run.error), *attached])


class _Completed:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeCli:
    """playwright-cli as `plc.run` meets it, one level below `plc.run`: a
    subprocess that takes an argv and answers on stdout. It records every argv and
    reads a `--filename` code file the way the daemon does, so a test can check what
    the file held, what mode it had, and that it is gone afterwards."""

    def __init__(self, monkeypatch, selector="#pw"):
        self.calls = []
        self.code = []
        self.classified = []
        self.selector = selector
        self.verdict = None  # what the page-side pass answers; None means the one match `selector`
        self.verdicts = {}  # the answer per kind of pass, "click" or "fill", over `verdict`
        self.fill = _Completed(0, TYPED)
        monkeypatch.setattr(plc, "run", _REAL_PLC_RUN)
        monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
        monkeypatch.setattr(plc.subprocess, "run", self.run)

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        args = cmd[2:]
        filenames = [arg[len("--filename="):] for arg in args if arg.startswith("--filename=")]
        if "run-code" in args and filenames:
            path = filenames[0]
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            if "Target.getTargets" in text or "Target.closeTarget" in text:
                # `session.pages` asking Chrome for its targets: this fake has no Chrome behind it.
                return _Completed(1, "", "The browser 'seat' is not open, please run open first")
            if "structuralPath" in text:
                # The page-side pass that turns a target into a selector. Its function and its argument are in
                # this file, so neither is in the command line.
                self.classified.append(text)
                kind = "fill" if "FILLABLE" in text else "click"
                verdict = self.verdicts.get(kind) or self.verdict or {
                    "selectorValid": True, "selectorCount": 1, "selectorVisibleCount": 1,
                    "selectorTarget": self.selector, "selectorPlain": "body > input",
                    "exact": [], "substring": [], "candidates": [],
                }
                return _Completed(0, json.dumps({"value": verdict}) + "\n")
            self.code.append({
                "text": text,
                "file_mode": stat.S_IMODE(os.stat(path).st_mode),
                "dir_mode": stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode),
                "path": path,
            })
            if isinstance(self.fill, BaseException):
                raise self.fill
            return self.fill
        if args[0] == "screenshot":
            for arg in args:
                if arg.startswith("--filename="):
                    with open(arg[len("--filename="):], "wb") as handle:
                        handle.write(b"\x89PNG fake")
        return _Completed(0, "### Result\nok\n")

    def argv_text(self):
        return "\0".join(part for cmd in self.calls for part in cmd)


def _cli(monkeypatch, tmp_path, snapshot=None, **kwargs):
    _with_secret(monkeypatch, tmp_path)
    if snapshot is not None:
        monkeypatch.setattr(commands.session, "snapshot", lambda seat: snapshot)
    return FakeCli(monkeypatch, **kwargs)


def test_fill_secret_types_the_stored_value_through_a_private_file(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    run = commands.run_script("seat", "fill-secret #pw PASSWORD")
    assert run.ok, run.error
    assert run.steps == [
        {"command": "fill-secret #pw PASSWORD", "ok": True, "output": "#pw <- [secret:PASSWORD]"}
    ]
    (code,) = cli.code
    assert f".fill({json.dumps(VALUE)}," in code["text"]
    assert 'page.locator("#pw")' in code["text"]
    assert not os.path.exists(code["path"])
    assert not os.path.exists(os.path.dirname(code["path"]))


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_the_code_file_is_0600_in_a_0700_directory_under_the_seat(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    commands.run_script("seat", "fill-secret #pw PASSWORD")
    (code,) = cli.code
    assert code["file_mode"] == 0o600
    assert code["dir_mode"] == 0o700
    assert code["path"].startswith(commands.session.scratch_dir("seat") + os.sep)
    assert os.listdir(commands.session.scratch_dir("seat")) == []


def test_the_value_is_in_no_argv_of_any_subprocess(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path, snapshot=f"- textbox: {VALUE}")
    run = commands.run_script("seat", "fill-secret #pw PASSWORD\nsnap\nurl\ntext #pw")
    assert run.ok, run.error
    assert cli.calls and VALUE not in cli.argv_text()
    (fill,) = [cmd for cmd in cli.calls if f"--filename={cli.code[0]['path']}" in cmd]
    assert fill[1] == f"-s={plc.session_name('seat')}"
    assert "--raw" in fill
    assert [a for a in fill if a.startswith("--filename=")] == [f"--filename={cli.code[0]['path']}"]
    # The target of the fill and of the text is in a private file too, so it is in no command line either.
    assert cli.classified and not any("#pw" in part for cmd in cli.calls for part in cmd)


def test_the_transcript_and_the_attachments_hold_no_value(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path, snapshot=f'- textbox "Password": {VALUE}')
    cli.fill = _Completed(0, f"### Ran Playwright code\n```js\n.fill({json.dumps(VALUE)})\n```\n{TYPED}")
    run = commands.run_script("seat", "fill-secret #pw PASSWORD\nsnap")
    assert run.ok, run.error
    everything = _everything(run)
    assert VALUE not in everything
    assert '- textbox "Password": [secret:PASSWORD]' in everything


def test_fill_secret_takes_a_ref_as_an_aria_ref_locator(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    for ref in ("e5", "f2e4"):
        run = commands.run_script("seat", f"fill-secret {ref} PASSWORD")
        assert run.ok, run.error
        assert run.steps[0]["output"] == f"{ref} <- [secret:PASSWORD]"
        assert f'page.locator("aria-ref={ref}")' in cli.code[-1]["text"]
    assert ["eval", "() => true", "e5"] in [cmd[2:] for cmd in cli.calls]


def test_fill_secret_takes_a_label_as_the_selector_the_page_resolves(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path, selector="input.password")
    run = commands.run_script("seat", 'fill-secret "Your password" PASSWORD')
    assert run.ok, run.error
    assert run.steps[0]["output"] == "input.password <- [secret:PASSWORD]"
    assert 'page.locator("input.password")' in cli.code[0]["text"]


def test_the_selector_and_value_are_json_string_literals(monkeypatch, tmp_path):
    odd = 'pa"ss\\word\n</script> \u2028 caf\u00e9'
    selector = "input[name='a\"b']"
    _with_secret(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "ODD", odd, [ORIGIN])
    cli = FakeCli(monkeypatch, selector=selector)
    run = commands.run_script("seat", "fill-secret #pw ODD")
    assert run.ok, run.error
    text = cli.code[0]["text"]
    assert text == plc.fill_secret_code(selector, odd, [secretstore.origin_parts(ORIGIN)])
    assert json.dumps(selector) in text and json.dumps(odd) in text


def test_an_unknown_secret_fails_before_any_page_work(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    run = commands.run_script("seat", "fill-secret #pw NOPE\nurl")
    assert not run.ok
    assert run.error == (
        "secret NOPE is not set for seat seat; "
        "the operator sets it with `ar3 tool browser --node seat secret set NOPE - --origin ORIGIN`"
    )
    assert [step["command"] for step in run.steps] == ["fill-secret #pw NOPE"]
    assert cli.calls == []


def test_a_name_that_cannot_be_a_secret_is_not_set(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    run = commands.run_script("seat", "fill-secret #pw password")
    assert not run.ok
    assert run.error.startswith("secret password is not set for seat seat; ")
    assert cli.code == []


def test_an_unknown_secret_on_a_seat_with_no_store_names_the_seat_and_the_fix(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("garmin", "fill-secret #pw PASSWORD")
    assert run.error == (
        "secret PASSWORD is not set for seat garmin; "
        "the operator sets it with `ar3 tool browser --node garmin secret set PASSWORD - --origin ORIGIN`"
    )


@pytest.mark.parametrize("line", ["fill-secret", "fill-secret #pw", "fill-secret #pw PASSWORD extra", "fill-secret #pw PASSWORD 'two words'"])
def test_fill_secret_wants_a_target_and_one_name(monkeypatch, tmp_path, line):
    cli = _cli(monkeypatch, tmp_path)
    run = commands.run_script("seat", line)
    assert run.error == "usage: fill-secret <label or selector> <NAME>"
    assert cli.code == []


def test_a_failed_fill_shows_only_the_kind_of_its_error_and_not_the_call_log_that_prints_the_value(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path, snapshot=f"- textbox: {VALUE}")
    cli.fill = _Completed(
        1,
        "### Error\nTimeoutError: locator.fill: Timeout 5000ms exceeded.\nCall log:\n"
        f'\x1b[2m  - locator resolved to <input id="pw" readonly/>\x1b[22m\n\x1b[2m  - fill("{VALUE}")\x1b[22m\n',
    )
    run = commands.run_script("seat", "fill-secret #pw PASSWORD")
    assert not run.ok
    assert run.error == "### Error\nTimeoutError: " + plc.DRIVER_MESSAGE_WITHHELD
    assert VALUE not in _everything(run) and "fill(" not in _everything(run)
    (code,) = cli.code
    assert not os.path.exists(os.path.dirname(code["path"]))


def test_a_failure_in_the_fill_leaves_no_chained_error_with_the_value(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    cli.fill = _Completed(1, f'### Error\nfill("{VALUE}")')
    run = commands.Run("seat", secretstore.Scrubber({"PASSWORD": {"value": VALUE, "origins": [ORIGIN]}}))
    with pytest.raises(plc.BrowserError) as raised:
        commands._fill_secret("seat", ["#pw", "PASSWORD"], run)
    assert VALUE not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_a_fill_that_times_out_removes_its_file(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    cli.fill = subprocess.TimeoutExpired(["playwright-cli"], 30)
    run = commands.run_script("seat", "fill-secret #pw PASSWORD")
    assert run.error == "playwright-cli timed out after 30s"
    (code,) = cli.code
    assert not os.path.exists(os.path.dirname(code["path"]))


def test_a_code_file_that_cannot_be_written_fails_the_step_without_a_traceback(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    make = plc.tempfile.mkdtemp

    def refuse(*a, **k):
        if k.get("prefix") != plc.SECRET_DIR_PREFIX:
            return make(*a, **k)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(plc.tempfile, "mkdtemp", refuse)
    run = commands.run_script("seat", "fill-secret #pw PASSWORD")
    assert run.error == "fill-secret: cannot write its private code file: Permission denied"
    assert cli.code == []


@pytest.mark.parametrize("line", ["click #pw", "fill #pw text", "text #pw", "assert-text x", "wait-for #pw 1", "hover Sign in"])
def test_a_page_call_whose_code_file_cannot_be_written_fails_the_step_without_a_traceback(monkeypatch, tmp_path, line):
    """The target of a click, the text of an assertion and the selector of a wait travel in a private file too."""
    _cli(monkeypatch, tmp_path)

    def refuse(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(plc.tempfile, "mkdtemp", refuse)
    run = commands.run_script("seat", line)
    assert not run.ok
    assert run.error == "cannot write its private code file: Permission denied"


def test_a_secret_is_a_script_verb_and_a_script_has_no_verb_that_sets_or_reads_one(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    target = secretstore.path("seat")
    before = open(target, "rb").read()
    for line in ("secret set OTHER other-value", "secret list", "secret rm PASSWORD", "secret get PASSWORD"):
        run = commands.run_script("seat", line)
        assert not run.ok
        assert run.error == "unknown command: secret"
        assert run.steps[0]["command"].startswith("secret")
        assert open(target, "rb").read() == before


def test_a_secret_line_in_a_script_writes_nothing(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    run = commands.run_script("seat", "secret set PASSWORD hunter2-value\nurl")
    assert run.error == "unknown command: secret"
    assert [step["command"] for step in run.steps] == ["secret set PASSWORD hunter2-value"]
    assert not os.path.exists(secretstore.path("seat"))


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_a_store_that_cannot_be_read_stops_the_script_before_anything_runs(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    target = secretstore.path("seat")
    os.chmod(target, 0o644)
    monkeypatch.setattr(commands.session, "ensure_running", lambda *a, **k: pytest.fail("Chrome opened"))
    monkeypatch.setattr(commands.session, "snapshot", lambda seat: pytest.fail("a snapshot was taken"))
    monkeypatch.setattr(plc, "run", lambda *a, **k: pytest.fail("the driver ran"))
    run = commands.run_script("seat", "url\nfill-secret #pw PASSWORD")
    assert not run.ok
    assert f"chmod 600 {target}" in run.error
    assert [step["command"] for step in run.steps] == ["secret store"]
    assert run.files == []
    assert VALUE not in _everything(run)


# --- the scrub, one output kind at a time ---------------------------------------


def test_url_output_is_scrubbed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path, current_url=lambda seat: f"https://example.com/?pw={VALUE}")
    run = commands.run_script("seat", "url")
    assert run.steps[0]["output"] == "https://example.com/?pw=[secret:PASSWORD]"


def test_text_output_is_scrubbed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    monkeypatch.setattr(plc, "run", lambda *a, **k: json.dumps({"value": f"your password is {VALUE}"}) + "\n")
    run = commands.run_script("seat", "text #reveal")
    assert run.steps[0]["output"] == "your password is [secret:PASSWORD]"


def test_find_output_is_scrubbed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    monkeypatch.setattr(plc, "run", lambda *a, **k: f"### Result\n- textbox [ref=e3]: {VALUE}\n")
    run = commands.run_script("seat", "find textbox")
    assert run.steps[0]["output"] == "- textbox [ref=e3]: [secret:PASSWORD]"


def test_requests_output_is_scrubbed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    monkeypatch.setattr(plc, "run", lambda *a, **k: f"### Result\n1. [GET] https://example.com/?pw={VALUE} => [200]\n")
    run = commands.run_script("seat", "requests")
    assert run.steps[0]["output"] == "1. [GET] https://example.com/?pw=[secret:PASSWORD] => [200]"


def test_console_output_and_its_attachment_are_scrubbed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    monkeypatch.setattr(plc, "run", lambda *a, **k: f"### Result\nTotal messages: 1\n\n[LOG] typed {VALUE} @ :0\n")
    run = commands.run_script("seat", "console")
    assert run.ok
    assert "typed [secret:PASSWORD]" in run.steps[0]["output"]
    (path,) = run.files
    assert path.endswith("-console.log")
    with open(path) as handle:
        body = handle.read()
    assert "typed [secret:PASSWORD]" in body and VALUE not in body


def test_a_run_scrubs_the_output_of_any_step_even_one_the_verbs_no_longer_produce():
    """`eval` and `run-code` cannot run on a seat that holds a secret (see the guard below), but
    the run cleans whatever any step hands it."""
    run = commands.Run("seat", secretstore.Scrubber({"PASSWORD": {"value": VALUE, "origins": [ORIGIN]}}))
    run.note("eval document.getElementById('pw').value", VALUE)
    run.note("run-code", f"### Result\n{VALUE}")
    assert [step["output"] for step in run.steps] == ["[secret:PASSWORD]", "### Result\n[secret:PASSWORD]"]


def test_a_snapshot_is_scrubbed_before_it_is_attached(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path, snapshot=lambda seat: f'- textbox "Password" [ref=e4]: {VALUE}')
    run = commands.run_script("seat", "snap")
    (path,) = run.files
    with open(path) as handle:
        body = handle.read()
    assert body == '- textbox "Password" [ref=e4]: [secret:PASSWORD]'
    assert VALUE not in _everything(run)


def test_the_failure_snapshot_is_scrubbed_before_it_is_attached(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path, snapshot=lambda seat: f'- textbox "Password" [ref=e4]: {VALUE}')
    run = commands.run_script("seat", "bogus")
    (path,) = run.files
    assert path.endswith("-failure.txt")
    with open(path) as handle:
        assert handle.read() == '- textbox "Password" [ref=e4]: [secret:PASSWORD]'


def test_an_error_message_is_scrubbed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)

    def fail(seat, *args, **kwargs):
        raise plc.BrowserError(f'TimeoutError: fill("{VALUE}") failed')

    monkeypatch.setattr(plc, "run", fail)
    run = commands.run_script("seat", "press Enter")
    assert run.error == 'TimeoutError: fill("[secret:PASSWORD]") failed'
    assert run.steps[0]["error"] == run.error
    assert VALUE not in _everything(run)


def test_a_command_line_that_holds_a_value_is_scrubbed_in_the_transcript(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    run = commands.run_script("seat", f"type {VALUE}")
    assert run.steps[0]["command"] == "type [secret:PASSWORD]"
    assert VALUE not in _everything(run)


def test_a_parse_error_that_quotes_a_value_is_scrubbed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    run = commands.run_script("seat", f"type {VALUE} <<END\nbody\nEND")
    assert not run.ok
    assert VALUE not in _everything(run)
    assert "[secret:PASSWORD]" in run.error


def test_an_assertion_that_quotes_a_value_is_scrubbed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    monkeypatch.setattr(plc, "call_in_page", lambda seat, function, values, directory, timeout=30: False)
    run = commands.run_script("seat", f"assert-text {VALUE}")
    assert run.error == "assert-text: '[secret:PASSWORD]' is not on the page"


def test_a_run_with_no_secret_changes_nothing_and_reads_no_scratch(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(commands, "_cli_output_state", lambda seat: pytest.fail("scratch was read"))
    run = commands.run_script("seat", f"type {VALUE}\nurl")
    assert run.ok
    assert run.steps[0]["command"] == f"type {VALUE}"


# --- the files playwright-cli writes ---------------------------------------------


def _cli_dir(seat="seat"):
    path = commands.session.cli_output_dir(seat)
    os.makedirs(path, exist_ok=True)
    return path


def test_a_snapshot_file_playwright_cli_writes_during_a_step_is_scrubbed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    written = []

    def click(seat, *args, **kwargs):
        target = os.path.join(_cli_dir(), "page-2026-10-24T21-31-13-548Z.yml")
        with open(target, "w") as handle:
            handle.write(f'- textbox "Password" [ref=e4]: {VALUE}\n')
        written.append(target)
        return "### Result\nok\n"

    monkeypatch.setattr(plc, "run", click)
    run = commands.run_script("seat", "press Enter")
    assert run.ok, run.error
    with open(written[0]) as handle:
        assert handle.read() == '- textbox "Password" [ref=e4]: [secret:PASSWORD]\n'


def test_a_console_log_playwright_cli_appends_to_is_scrubbed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    log = os.path.join(_cli_dir(), "console-2026-10-24T21-08-06-169Z.log")
    with open(log, "w") as handle:
        handle.write("[       1ms] [LOG] hello\n")

    def page_logs(seat, *args, **kwargs):
        with open(log, "a") as handle:
            handle.write(f"[       2ms] [LOG] typed {VALUE}\n")
        return "### Result\nok\n"

    monkeypatch.setattr(plc, "run", page_logs)
    commands.run_script("seat", "press Enter")
    with open(log) as handle:
        assert handle.read() == "[       1ms] [LOG] hello\n[       2ms] [LOG] typed [secret:PASSWORD]\n"


def test_a_file_written_by_a_step_that_failed_is_scrubbed_too(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    target = os.path.join(_cli_dir(), "page-x.yml")

    def write_then_fail(seat, *args, **kwargs):
        with open(target, "w") as handle:
            handle.write(f"value {VALUE}")
        raise plc.BrowserError("boom")

    monkeypatch.setattr(plc, "run", write_then_fail)
    run = commands.run_script("seat", "press Enter")
    assert not run.ok
    with open(target) as handle:
        assert handle.read() == "value [secret:PASSWORD]"


def test_only_the_files_written_since_the_last_sweep_are_rewritten(monkeypatch, tmp_path):
    """A file that an earlier sweep has been over, and that nothing has touched since, is not read again."""
    _with_secret(monkeypatch, tmp_path)
    old = os.path.join(_cli_dir(), "page-old.yml")
    with open(old, "w") as handle:
        handle.write(f"older {VALUE}")
    long_ago = time.time() - 3600
    os.utime(old, (long_ago, long_ago))
    commands.session.mark_swept("seat", time.time_ns() - 600 * 10**9)
    before = os.stat(old)
    commands.run_script("seat", "url")
    after = os.stat(old)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_screenshot_is_left_as_the_pixels_it_is(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    pixels = b"\x89PNG\r\n\x1a\n" + VALUE.encode() + b"\xff\x00"

    def shoot(seat, path, timeout=60):
        with open(path, "wb") as handle:
            handle.write(pixels)
        return path

    monkeypatch.setattr(plc, "screenshot", shoot)
    run = commands.run_script("seat", "shot")
    (path,) = run.files
    with open(path, "rb") as handle:
        assert handle.read() == pixels


# --- origins -----------------------------------------------------------------------


def test_the_origins_of_the_secret_travel_in_the_code_file_as_parts(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "PASSWORD", VALUE, [ORIGIN, "https://*.example.org:8443", "http://127.0.0.1:18931"])
    cli = FakeCli(monkeypatch)
    run = commands.run_script("seat", "fill-secret #pw PASSWORD")
    assert run.ok, run.error
    parts = [
        {"scheme": "https", "host": "app.example.com", "port": "", "wildcard": False},
        {"scheme": "https", "host": "example.org", "port": "8443", "wildcard": True},
        {"scheme": "http", "host": "127.0.0.1", "port": "18931", "wildcard": False},
    ]
    assert cli.code[0]["text"] == plc.fill_secret_code("#pw", VALUE, parts)
    assert json.dumps(parts) in cli.code[0]["text"]


def test_a_secret_is_not_typed_on_an_origin_it_is_not_allowed_on(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path, snapshot=f"- textbox: {VALUE}")
    cli.fill = _Completed(0, '{"refused":"https://evil.example"}\n')
    run = commands.run_script("seat", "fill-secret #pw PASSWORD\nurl")
    assert not run.ok
    assert run.error == (
        "fill-secret: secret PASSWORD may not be typed on origin https://evil.example; "
        "allowed origins: https://app.example.com"
    )
    assert [step["command"] for step in run.steps] == ["fill-secret #pw PASSWORD"]
    assert VALUE not in _everything(run)
    (code,) = cli.code
    assert not os.path.exists(os.path.dirname(code["path"]))


def test_the_refusal_lists_every_origin_the_secret_is_allowed_on(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "PASSWORD", VALUE, [ORIGIN, "https://*.example.org", "http://127.0.0.1:18931"])
    cli = FakeCli(monkeypatch)
    cli.fill = _Completed(0, '{"refused":"null"}\n')
    run = commands.run_script("seat", "fill-secret #pw PASSWORD")
    assert run.error == (
        "fill-secret: secret PASSWORD may not be typed on origin null; "
        "allowed origins: https://app.example.com, https://*.example.org, http://127.0.0.1:18931"
    )


def test_a_refusal_leaves_no_chained_error(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    cli.fill = _Completed(0, '{"refused":"https://evil.example"}\n')
    run = commands.Run("seat", secretstore.Scrubber({"PASSWORD": {"value": VALUE, "origins": [ORIGIN]}}))
    with pytest.raises(plc.BrowserError) as raised:
        commands._fill_secret("seat", ["#pw", "PASSWORD"], run)
    assert not isinstance(raised.value, plc.OriginRefused)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_the_step_after_a_refusal_does_not_run(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    cli.fill = _Completed(0, '{"refused":"https://evil.example"}\n')
    run = commands.run_script("seat", "fill-secret #pw PASSWORD\nclick #go")
    assert not run.ok
    assert not [cmd for cmd in cli.calls if "click" in cmd]


# --- the state directory is out of reach of go, upload and drop ------------------------


def _calls(monkeypatch):
    seen = []
    monkeypatch.setattr(plc, "run", lambda *a, **k: seen.append(a) or "### Result\nok\n")
    return seen


def _state(monkeypatch, tmp_path):
    """A stubbed seat with a store, and the paths around it."""
    _with_secret(monkeypatch, tmp_path)
    store = secretstore.path("seat")
    root = commands.session.state_root()
    return store, root


def _go(url):
    return commands.run_script("seat", f"go {url}")


LOCAL = "local files are not opened"


def test_go_refuses_a_file_outside_the_state_directory_too(monkeypatch, tmp_path):
    """The tool opens no local file. The page of another seat's store is a file, and so is any page beside it."""
    _state(monkeypatch, tmp_path)
    page = tmp_path / "page.html"
    page.write_text("<p>hi</p>")
    seen = _calls(monkeypatch)
    run = _go(f"file://{page}")
    assert not run.ok
    assert run.error == f"go: refusing 'file://{page}' \u2014 {LOCAL}"
    assert seen == []


@pytest.mark.parametrize(
    "where",
    [
        "{store}",
        "{root}/seat",
        "{root}/seat/",
        "{root}",
        "{root}/",
        "{root}/seat/scratch",
        "{root}/seat/chrome/Default/Cookies",
        "{root}/other-seat/secrets.json",
        "{root}/seat/no-such-file",
        "{root}/seat/../seat/secrets.json",
        "{root}/seat/./secrets.json",
        "{root}//seat//secrets.json",
        "{root}/seat/secrets%2Ejson",
        "{root}/%73eat/secrets.json",
    ],
)
def test_go_refuses_a_file_url_inside_the_state_directory(monkeypatch, tmp_path, where):
    store, root = _state(monkeypatch, tmp_path)
    seen = _calls(monkeypatch)
    for prefix in ("file://", "file://localhost"):
        run = _go(prefix + where.format(store=store, root=root))
        assert not run.ok
        assert LOCAL in run.error
    assert seen == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/hostname",
        "file://localhost/etc/hostname",
        "file://host.example.com/share/x",
        "file:/etc/hostname",
        "file:etc/hostname",
        "file:",
        "file://",
        "file:///",
        "FILE:///etc/hostname",
        "File:///etc/hostname",
        "fIlE://localhost/etc/hostname",
    ],
)
def test_go_refuses_every_spelling_of_a_file_url(monkeypatch, tmp_path, url):
    _state(monkeypatch, tmp_path)
    seen = _calls(monkeypatch)
    run = _go(url)
    assert not run.ok and LOCAL in run.error
    assert seen == []


@pytest.mark.parametrize("url", [" file:///etc/hostname", "\tfile:///etc/hostname", "fi\tle:///etc/hostname", "\x01file:///x"])
def test_go_refuses_a_file_url_a_browser_would_read_through_blanks_and_control_characters(monkeypatch, tmp_path, url):
    """A browser drops blanks and control characters at the head of a URL and tabs and newlines inside it."""
    _state(monkeypatch, tmp_path)
    seen = _calls(monkeypatch)
    run = commands.run_script("seat", f"go '{url}'")
    assert not run.ok and LOCAL in run.error
    assert seen == []


def test_go_refuses_a_symlink_that_leads_into_the_state_directory(monkeypatch, tmp_path):
    store, root = _state(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file-link").symlink_to(store)
    (outside / "dir-link").symlink_to(root)
    (outside / "seat-link").symlink_to(os.path.join(root, "seat"))
    seen = _calls(monkeypatch)
    for url in (
        f"file://{outside}/file-link",
        f"file://{outside}/dir-link/seat/secrets.json",
        f"file://{outside}/dir-link",
        f"file://{outside}/seat-link/secrets.json",
        f"file://{outside}/seat-link/../seat/secrets.json",
    ):
        run = _go(url)
        assert not run.ok, url
        assert LOCAL in run.error, url
    assert seen == []


def test_go_refuses_a_url_that_cannot_be_a_path(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    seen = _calls(monkeypatch)
    run = _go("file:///tmp/a%00b")
    assert not run.ok and LOCAL in run.error
    assert seen == []


@pytest.mark.parametrize("make", ["{tmp}/dir", "{tmp}"])
def test_go_opens_no_directory_either(monkeypatch, tmp_path, make):
    _state(monkeypatch, tmp_path)
    (tmp_path / "dir").mkdir()
    seen = _calls(monkeypatch)
    for suffix in ("", "/"):
        run = _go("file://" + make.format(tmp=tmp_path) + suffix)
        assert not run.ok and LOCAL in run.error
    assert seen == []


def test_go_never_reaches_the_driver_with_a_file_url_whatever_the_environment_of_the_node_says(monkeypatch, tmp_path):
    """The reviewed chain starts with `go` to a local page of its own. The node's environment can ask playwright-cli
    to open local files (PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS); the tool still opens none."""
    _state(monkeypatch, tmp_path)
    monkeypatch.setenv(plc.FILE_ACCESS_ENV, "1")
    outside = tmp_path / "outside.html"
    outside.write_text('<a href="file:///some/other/seat/state-marker">protected</a>')
    seen = _calls(monkeypatch)
    run = commands.run_script("seat", f"go file://{outside}\nclick protected\ntext body\nsnap")
    assert not run.ok and len(run.steps) == 1 and LOCAL in run.error
    assert not [call for call in seen if call[1] in ("goto", "click", "eval")], "the driver was asked to open or click"
    assert "protected" not in _everything(run)


@pytest.mark.parametrize("url", ["https://example.com/", "http://127.0.0.1:18931/", "about:blank"])
def test_go_still_opens_what_is_not_a_local_file(monkeypatch, tmp_path, url):
    _state(monkeypatch, tmp_path)
    seen = _calls(monkeypatch)
    assert _go(url).ok
    assert ("seat", "goto", url) in seen


@pytest.mark.parametrize("verb", ["upload", "drop"])
@pytest.mark.parametrize(
    "where",
    ["{store}", "{root}/seat/state.json", "{root}/seat/chrome/Default/Cookies", "{root}/other/secrets.json", "{root}/seat/no-such-file", "{root}/seat/../seat/secrets.json", "{root}"],
)
def test_upload_and_drop_refuse_a_path_inside_the_state_directory(monkeypatch, tmp_path, verb, where):
    store, root = _state(monkeypatch, tmp_path)
    monkeypatch.setattr(commands.resolve, "click_target", lambda seat, arg: arg)
    seen = _calls(monkeypatch)
    path = where.format(store=store, root=root)
    line = f"upload {path}" if verb == "upload" else f"drop div.box {path}"
    run = commands.run_script("seat", line)
    assert not run.ok
    assert run.error == f"{verb}: refusing {path!r} \u2014 it is inside the browser state directory"
    assert seen == []


@pytest.mark.parametrize("verb", ["upload", "drop"])
def test_upload_and_drop_refuse_a_symlink_that_leads_into_the_state_directory(monkeypatch, tmp_path, verb):
    store, root = _state(monkeypatch, tmp_path)
    monkeypatch.setattr(commands.resolve, "click_target", lambda seat, arg: arg)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "innocent.txt").symlink_to(store)
    (outside / "dir-link").symlink_to(root)
    seen = _calls(monkeypatch)
    for path in (f"{outside}/innocent.txt", f"{outside}/dir-link/seat/secrets.json"):
        line = f"upload {path}" if verb == "upload" else f"drop div.box {path}"
        run = commands.run_script("seat", line)
        assert not run.ok, path
        assert "inside the browser state directory" in run.error, path
    assert seen == []


def test_drop_refuses_the_whole_drop_when_one_path_is_inside(monkeypatch, tmp_path):
    store, root = _state(monkeypatch, tmp_path)
    monkeypatch.setattr(commands.resolve, "click_target", lambda seat, arg: arg)
    fine = tmp_path / "fine.txt"
    fine.write_text("x")
    seen = _calls(monkeypatch)
    run = commands.run_script("seat", f"drop div.box {fine} {store}")
    assert not run.ok and "inside the browser state directory" in run.error
    assert seen == []


def test_drop_looks_at_its_paths_before_it_asks_the_page_for_the_target(monkeypatch, tmp_path):
    store, root = _state(monkeypatch, tmp_path)
    monkeypatch.setattr(commands.resolve, "click_target", lambda seat, arg: pytest.fail("the page was asked"))
    seen = _calls(monkeypatch)
    run = commands.run_script("seat", f"drop #no-such-target {store}")
    assert "inside the browser state directory" in run.error
    assert seen == []


def test_the_answer_for_a_path_inside_the_state_directory_does_not_say_whether_it_exists(monkeypatch, tmp_path):
    store, root = _state(monkeypatch, tmp_path)
    here = commands.run_script("seat", f"upload {store}").error
    gone = commands.run_script("seat", f"upload {root}/seat/never-was").error
    assert here.replace(store, "X") == gone.replace(f"{root}/seat/never-was", "X")


def test_upload_and_drop_still_take_a_file_outside_the_state_directory(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    monkeypatch.setattr(commands.resolve, "click_target", lambda seat, arg: arg)
    fine = tmp_path / "fine.txt"
    fine.write_text("x")
    seen = _calls(monkeypatch)
    assert commands.run_script("seat", f"upload {fine}").ok
    assert commands.run_script("seat", f"drop div.box {fine}").ok
    assert [call[1] for call in seen] == ["upload", "drop"]


# --- the seat directories ----------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_a_run_closes_the_seat_directories_it_finds_open(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    home = commands.session.seat_home("seat")
    scratch = os.path.join(home, "scratch")
    for path in (home, scratch):
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o775)
    run = commands.run_script("seat", "url")
    assert run.ok, run.error
    for path in (home, scratch):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700, path


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_a_run_makes_the_seat_directories_closed_whatever_the_umask(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    old = os.umask(0)
    try:
        run = commands.run_script("seat", "url")
    finally:
        os.umask(old)
    assert run.ok, run.error
    home = commands.session.seat_home("seat")
    for path in (home, os.path.join(home, "scratch")):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700, path


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_a_run_that_stops_on_an_unreadable_store_still_closes_the_seat_directories(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    os.chmod(secretstore.path("seat"), 0o644)
    home = commands.session.seat_home("seat")
    scratch = os.path.join(home, "scratch")
    os.makedirs(scratch, exist_ok=True)
    os.chmod(home, 0o775)
    os.chmod(scratch, 0o775)
    run = commands.run_script("seat", "url")
    assert not run.ok and "chmod 600" in run.error
    for path in (home, scratch):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700, path


# --- eval and run-code are off on a seat that holds a secret ------------------------------------

GUARD = (
    "eval and run-code are off on a seat that holds secrets; they could read a field a secret was "
    "typed into. Remove the secrets (ar3 tool browser --seat NAME secret rm SECRET) or use a seat without them."
)
CALLER_CODE = [
    "eval document.getElementById('pw').value",
    "run-code <<END\nreturn await page.locator('#pw').inputValue();\nEND",
]


def _no_driver(monkeypatch):
    for name in ("evaluate", "run_code"):
        monkeypatch.setattr(plc, name, lambda *a, **k: pytest.fail("the caller's code reached the driver"))


@pytest.mark.parametrize("allow_eval", [True, False])
@pytest.mark.parametrize("script", CALLER_CODE, ids=["eval", "run-code"])
def test_a_seat_that_holds_a_secret_refuses_eval_and_run_code_whatever_allow_eval_says(
    monkeypatch, tmp_path, script, allow_eval
):
    _with_secret(monkeypatch, tmp_path)
    _no_driver(monkeypatch)
    run = commands.run_script("seat", script, allow_eval=allow_eval)
    assert not run.ok
    assert run.error == GUARD
    assert run.steps[0]["error"] == GUARD
    assert VALUE not in _everything(run)


def test_the_refusal_names_the_reason_and_the_fix(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    run = commands.run_script("seat", "eval 1 + 1", allow_eval=True)
    assert "could read a field a secret was typed into" in run.error
    assert "ar3 tool browser --seat NAME secret rm SECRET" in run.error and "a seat without them" in run.error
    assert run.error == commands.EVAL_OFF_WITH_SECRETS


def test_a_script_stops_at_the_refused_eval_and_the_steps_before_it_ran(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    _no_driver(monkeypatch)
    run = commands.run_script("seat", "url\neval 1 + 1\nurl", allow_eval=True)
    assert [step["command"] for step in run.steps] == ["url", "eval 1 + 1"]
    assert run.steps[0]["ok"] and not run.steps[1]["ok"]


def test_one_secret_is_enough_and_two_are_the_same(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "OTHER", "other-value", [ORIGIN])
    _no_driver(monkeypatch)
    assert commands.run_script("seat", "eval 1", allow_eval=True).error == GUARD
    secretstore.remove("seat", "OTHER")
    assert commands.run_script("seat", "eval 1", allow_eval=True).error == GUARD


def test_removing_the_last_secret_gives_the_verbs_back(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    monkeypatch.setattr(plc, "evaluate", lambda seat, expression, timeout=30: "2")
    monkeypatch.setattr(plc, "run_code", lambda seat, body, timeout=60: "### Result\nok\n")
    assert commands.run_script("seat", "eval 1 + 1", allow_eval=True).error == GUARD
    assert secretstore.remove("seat", "PASSWORD") is True
    run = commands.run_script("seat", "eval 1 + 1\nrun-code return 1;", allow_eval=True)
    assert run.ok, run.error
    assert [step["output"] for step in run.steps] == ["2", "ok"]


def test_a_seat_with_no_secret_keeps_eval_and_run_code_behind_the_opt_in(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    off = commands.run_script("seat", "eval 1 + 1", allow_eval=False)
    assert "AR3_BROWSER_ALLOW_EVAL" in off.error and "secret" not in off.error
    off = commands.run_script("seat", "run-code return 1;", allow_eval=False)
    assert "AR3_BROWSER_ALLOW_EVAL" in off.error and "secret" not in off.error
    monkeypatch.setattr(plc, "evaluate", lambda seat, expression, timeout=30: "2")
    monkeypatch.setattr(plc, "run_code", lambda seat, body, timeout=60: "### Result\nok\n")
    on = commands.run_script("seat", "eval 1 + 1\nrun-code return 1;", allow_eval=True)
    assert on.ok, on.error
    assert [step["output"] for step in on.steps] == ["2", "ok"]


def test_a_store_with_no_secret_in_it_is_a_seat_with_no_secret(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    secretstore.save("seat", {})
    monkeypatch.setattr(plc, "evaluate", lambda seat, expression, timeout=30: "2")
    run = commands.run_script("seat", "eval 1 + 1", allow_eval=True)
    assert run.ok, run.error
    assert run.steps[0]["output"] == "2"


def test_fill_secret_still_works_on_a_seat_with_allow_eval_on(monkeypatch, tmp_path):
    cli = _cli(monkeypatch, tmp_path)
    run = commands.run_script("seat", "fill-secret #pw PASSWORD", allow_eval=True)
    assert run.ok, run.error
    assert run.steps[0]["output"] == "#pw <- [secret:PASSWORD]"
    assert len(cli.code) == 1 and json.dumps(VALUE) in cli.code[0]["text"]
    assert commands.run_script("seat", "eval 1", allow_eval=True).error == GUARD


def test_the_tools_own_run_code_calls_are_not_the_callers_and_still_run(monkeypatch, tmp_path):
    """`wait-for` and the session code reach the driver through the tool's own calls, not through the verb."""
    _with_secret(monkeypatch, tmp_path)
    files = []
    monkeypatch.setattr(plc, "run_code_file", lambda seat, code, directory, timeout=60: files.append(code) or "")
    run = commands.run_script("seat", "wait-for #go 3", allow_eval=True)
    assert run.ok, run.error
    assert len(files) == 1 and "waitForSelector" in files[0]


def test_the_python_api_is_not_guarded_only_the_two_verbs(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(plc.subprocess, "run", lambda cmd, **k: calls.append(cmd) or _Completed(0, "### Result\nok\n"))
    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    monkeypatch.setattr(plc, "run", _REAL_PLC_RUN)
    assert plc.result_or(plc.run_code("seat", "return 1;"))
    assert plc.evaluate("seat", "1") == "ok"
    assert len(calls) == 2


def test_a_tell_that_asks_for_eval_on_a_seat_that_holds_a_secret_gets_the_refusal(monkeypatch, tmp_path):
    from browser import handler

    _with_secret(monkeypatch, tmp_path)
    _no_driver(monkeypatch)
    sent = []
    monkeypatch.setattr(handler.rt, "send_reply", lambda recipient, body, attachments=(): sent.append(body) or 0)
    assert handler.handle("seat", "iris", "eval 1 + 1", allow="iris", allow_eval="1") == 0
    assert sent[0].startswith(f"seat: failed: {GUARD}")


# --- the transcript of a step that did something before it failed -----------------------------------------


def test_a_failed_step_prints_what_it_did_and_then_its_error():
    run = commands.Run("seat")
    run.fail("recipe shop get", "then it broke", "  go https://example.com/ -> https://example.com/")
    assert run.steps == [{
        "command": "recipe shop get", "ok": False, "error": "then it broke",
        "output": "  go https://example.com/ -> https://example.com/",
    }]
    assert run.transcript().splitlines() == [
        "! recipe shop get",
        "        go https://example.com/ -> https://example.com/",
        "      then it broke",
    ]


def test_a_failed_step_with_no_output_keeps_the_shape_it_always_had():
    run = commands.Run("seat")
    run.fail("click Buy", "nothing visible matching 'Buy'")
    assert run.steps == [{"command": "click Buy", "ok": False, "error": "nothing visible matching 'Buy'"}]
    assert run.transcript().splitlines() == ["! click Buy", "      nothing visible matching 'Buy'"]


def test_the_output_of_a_failed_step_is_scrubbed_like_its_error():
    scrubber = secretstore.Scrubber({"PASSWORD": {"value": "hunter2-value", "origins": ["https://example.com"]}})
    run = commands.Run("seat", scrubber)
    run.fail("recipe shop get", "boom hunter2-value", "  fill #pw hunter2-value")
    assert "hunter2-value" not in json.dumps(run.steps) + run.transcript()


def test_a_run_hides_a_value_it_read_and_scrubs_the_files_playwright_cli_already_wrote():
    run = commands.Run("seat")
    assert run.scrubber is None
    seen = os.path.join(_cli_dir(), "page-1.yml")
    other = os.path.join(_cli_dir(), "page-2.yml")
    with open(seen, "w") as handle:
        handle.write("- generic: Your code is 482916\n")
    with open(other, "w") as handle:
        handle.write("- generic: nothing here\n")
    run.hide("PASSCODE", "482916")
    with open(seen) as handle:
        assert handle.read() == "- generic: Your code is [secret:PASSCODE]\n"
    with open(other) as handle:
        assert handle.read() == "- generic: nothing here\n"
    assert run.clean("code 482916") == "code [secret:PASSCODE]"
    run.note("go https://example.com/?c=482916", "got 482916")
    run.fail("click x", "failed on 482916", "  fill 482916")
    assert "482916" not in json.dumps(run.steps) + run.transcript()


def test_a_run_that_holds_a_secret_hides_a_value_beside_it():
    scrubber = secretstore.Scrubber({"PASSWORD": {"value": "hunter2-value", "origins": ["https://example.com"]}})
    run = commands.Run("seat", scrubber)
    run.hide("PASSCODE", "482916")
    assert run.clean("hunter2-value 482916") == "[secret:PASSWORD] [secret:PASSCODE]"
    assert run.secret("PASSCODE") is None


def test_a_value_hidden_in_one_step_is_scrubbed_from_the_files_of_the_next(monkeypatch, tmp_path):
    """The script's scrubber is the run's: a step that hides a value changes what the steps after it keep."""
    _stub_browser(monkeypatch, tmp_path)
    target = os.path.join(_cli_dir(), "page-x.yml")
    scrubbers = []

    def write(seat, *args, **kwargs):
        with open(target, "w") as handle:
            handle.write("code 482916\n")
        return "### Result\nok\n"

    monkeypatch.setattr(plc, "run", write)
    real = commands.scrubbed_outputs

    def spy(seat, scrubber):
        scrubbers.append(scrubber)
        return real(seat, scrubber)

    monkeypatch.setattr(commands, "scrubbed_outputs", spy)
    original = commands._step

    def step(seat, verb, args, run, allow_eval, script_epoch=None):
        if verb == "url":
            with open(target) as handle:
                assert handle.read() == "code 482916\n"
            run.hide("PASSCODE", "482916")
            with open(target) as handle:
                assert handle.read() == "code [secret:PASSCODE]\n", "hiding did not scrub the file already there"
        return original(seat, verb, args, run, allow_eval, script_epoch=script_epoch)

    monkeypatch.setattr(commands, "_step", step)
    run = commands.run_script("seat", "press Enter\nurl\npress Enter")
    assert run.ok, run.error
    assert len({id(scrubber) for scrubber in scrubbers}) == 1
    with open(target) as handle:
        assert handle.read() == "code [secret:PASSCODE]\n"


def test_eval_is_refused_in_the_rest_of_a_script_in_which_a_value_was_hidden_and_the_reason_is_given(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    original = commands._step

    def step(seat, verb, args, run, allow_eval, script_epoch=None):
        if verb == "url":
            run.hide("PASSCODE", "482916")
        return original(seat, verb, args, run, allow_eval, script_epoch=script_epoch)

    monkeypatch.setattr(commands, "_step", step)
    before = commands.run_script("seat", "eval 1", allow_eval=True)
    assert before.ok, before.error
    after = commands.run_script("seat", "url\neval 1\nrun-code return 1;", allow_eval=True)
    assert not after.ok and after.error == commands.EVAL_OFF_AFTER_HIDE
    assert "secret rm" not in after.error, "the seat holds no secret, so the fix is not to remove one"
    assert [step_["command"] for step_ in after.steps] == ["url", "eval 1"]


def test_the_refusal_on_a_seat_that_holds_a_secret_still_names_the_secret_fix_after_a_hide(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    original = commands._step

    def step(seat, verb, args, run, allow_eval, script_epoch=None):
        if verb == "url":
            run.hide("PASSCODE", "482916")
        return original(seat, verb, args, run, allow_eval, script_epoch=script_epoch)

    monkeypatch.setattr(commands, "_step", step)
    result = commands.run_script("seat", "url\neval 1", allow_eval=True)
    assert result.error == commands.EVAL_OFF_WITH_SECRETS


def test_a_blank_line_in_a_steps_output_stays_blank_in_the_transcript():
    run = commands.Run("seat")
    run.note("recipe shop", "title\n\nverb")
    assert run.transcript().splitlines() == ["  recipe shop", "      title", "", "      verb"]


def test_a_file_is_attached_once_however_often_it_is_named():
    run = commands.Run("seat")
    for path in ("/tmp/a.png", "/tmp/b.png", "/tmp/a.png"):
        run.attach(path)
    assert run.files == ["/tmp/a.png", "/tmp/b.png"]


def test_a_script_hands_its_recipes_to_the_recipe_verb_through_the_run(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    seen = []
    from browser import recipe

    monkeypatch.setattr(recipe, "invoke", lambda seat, words, run, allow_eval, epoch, library: seen.append((words, library)) or "listed")
    library = object()
    result = commands.run_script("seat", "recipe shop list --limit 3", recipes=library)
    assert result.ok and result.steps[0]["output"] == "listed"
    assert seen == [(["shop", "list", "--limit", "3"], library)]


def test_the_recipe_verb_opens_no_browser_of_its_own_accord(monkeypatch, tmp_path):
    from browser import recipe

    _stub_browser(monkeypatch, tmp_path, ensure_running=lambda *a, **k: pytest.fail("Chrome opened"))
    monkeypatch.setattr(recipe, "BUNDLED_DIR", tmp_path / "no-recipes-here")
    result = commands.run_script("seat", "recipe\nrecipe help")
    assert result.ok, result.error
    assert [step["output"] for step in result.steps] == ["no recipes", "no recipes"]
