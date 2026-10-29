"""The files that the driver and the browser write on their own.

playwright-cli keeps a file for each page it shows and a log of the console and one of the network, and the
daemon appends to the logs after a call has returned. Chrome keeps the storage of every site, and `state.json` is a
copy of it. A site keeps what it likes in its storage, a value typed into one of its fields among it.

The rule for them is the one for the profile. They stay in the seat's directory, which is mode 0700, they are
mode 0600, and no verb attaches, returns or uploads one. The files of the driver are also scrubbed before any
script can read them: `commands.sweep_outputs` runs first in every script and every command of the CLI, and a
call scrubs the files it changed as it ends. What a page wrote to its console seconds after a call returned is
therefore in a file only until the next call begins.

The live test with a real Chrome and a page that does all of it is test_output_paths_live.py.
"""
import os
import stat
import threading
import time

import pytest

from browser import cli
from browser import commands
from browser import plc
from browser import secretstore
from browser import session

from test_cli import ORIGIN, VALUE, _run, _store
from test_commands import _cli_dir, _stub_browser, _with_secret

POSIX_MODES = pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")


def _log(name="console-2026-10-25T05-10-09-626Z.log", text=None, seat="seat"):
    """A console log of the driver, with a line the page wrote after the last call returned."""
    path = os.path.join(_cli_dir(seat), name)
    with open(path, "w") as handle:
        handle.write(text if text is not None else f"[    4001ms] [LOG] remembered user {VALUE}\n")
    return path


def _read(path):
    with open(path) as handle:
        return handle.read()


def _read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


# --- the sweep ------------------------------------------------------------------------------------------------


def test_a_console_log_written_after_the_last_call_is_scrubbed_before_the_first_step(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    log = _log()
    seen = []
    monkeypatch.setattr(commands, "_step", lambda seat, verb, args, run, allow_eval, script_epoch=None: seen.append(_read(log)) or "ok")
    run = commands.run_script("seat", "url")
    assert run.ok, run.error
    assert seen == ["[    4001ms] [LOG] remembered user [secret:PASSWORD]\n"], "it was clean when the first step began"


def test_the_sweep_runs_even_when_the_script_cannot_be_parsed(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    log = _log()
    run = commands.run_script("seat", "run-code <<END\nno end")
    assert not run.ok
    assert VALUE not in _read(log)


def test_the_probe_of_the_review_a_later_call_leaves_no_file_holding_the_value(monkeypatch, tmp_path):
    """The page logs the value seconds after the fill. The first script is over; the log gains the line; the
    next script, of any kind, begins by scrubbing it."""
    _with_secret(monkeypatch, tmp_path)
    log = _log(text="[       1ms] [LOG] hello\n")
    assert commands.run_script("seat", "url").ok
    with open(log, "a") as handle:
        handle.write(f"[    4001ms] [LOG] remembered user {VALUE}\n")
    assert VALUE in _read(log), "between two scripts the file holds it, inside the seat directory"
    assert commands.run_script("seat", "url").ok
    assert _read(log) == "[       1ms] [LOG] hello\n[    4001ms] [LOG] remembered user [secret:PASSWORD]\n"


def test_a_file_the_sweep_has_been_over_and_nothing_has_touched_is_not_read_again(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    old = _log("page-old.yml", "- generic: an older page\n")
    long_ago = time.time() - 7200
    os.utime(old, (long_ago, long_ago))
    session.mark_swept("seat", time.time_ns() - 600 * 10**9)
    fresh = _log("console-fresh.log")
    read = []
    real = secretstore.Scrubber.scrub_file
    monkeypatch.setattr(secretstore.Scrubber, "scrub_file", lambda self, target, **k: read.append(os.path.basename(target)) or real(self, target, **k))
    commands.sweep_outputs("seat", secretstore.Scrubber(secretstore.read("seat")))
    assert read == ["console-fresh.log"]
    assert VALUE not in _read(fresh)


def test_a_file_changed_since_the_mark_is_read_even_when_the_mark_is_a_moment_before_it(monkeypatch, tmp_path):
    """A file system stamps coarsely, so a file written just after the sweep began can carry a stamp just before it."""
    _with_secret(monkeypatch, tmp_path)
    log = _log()
    session.mark_swept("seat", time.time_ns() + 1_000_000_000)  # the stamp of the file is a second before the mark
    commands.sweep_outputs("seat", secretstore.Scrubber(secretstore.read("seat")))
    assert VALUE not in _read(log)


def test_every_file_is_read_when_the_mark_is_in_the_future_for_the_clock_was_set_back(monkeypatch, tmp_path):
    """The stamps of the files and the mark cannot be compared when the clock went back since the sweep."""
    _with_secret(monkeypatch, tmp_path)
    old = _log("page-old.yml", f"older {VALUE}")
    long_ago = time.time() - 7200
    os.utime(old, (long_ago, long_ago))
    session.mark_swept("seat", time.time_ns() + 3600 * 10**9)
    commands.sweep_outputs("seat", secretstore.Scrubber(secretstore.read("seat")))
    assert VALUE not in _read(old)
    assert session.sweep_mark("seat") < time.time_ns() + 10**9, "the mark is the time of this sweep now"


def test_a_file_with_bytes_that_are_not_utf8_and_a_value_in_it_is_removed_by_the_sweep(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    log = os.path.join(_cli_dir(), "console-x.log")
    with open(log, "wb") as handle:
        handle.write(b"[LOG] typed " + VALUE.encode() + b" \xff\xfe\n")
    clean = os.path.join(_cli_dir(), "console-y.log")
    with open(clean, "wb") as handle:
        handle.write(b"[LOG] nothing \xff\xfe\n")
    commands.sweep_outputs("seat", secretstore.Scrubber(secretstore.read("seat")))
    assert not os.path.exists(log) and os.path.exists(clean)


def test_every_file_is_read_when_there_was_no_sweep(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    old = _log("page-old.yml", f"older {VALUE}")
    long_ago = time.time() - 86400
    os.utime(old, (long_ago, long_ago))
    assert session.sweep_mark("seat") is None
    commands.sweep_outputs("seat", secretstore.Scrubber(secretstore.read("seat")))
    assert VALUE not in _read(old)


def test_the_sweep_moves_the_mark_to_the_time_it_began(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    before = time.time_ns()
    commands.sweep_outputs("seat", secretstore.Scrubber(secretstore.read("seat")))
    mark = session.sweep_mark("seat")
    assert before <= mark <= time.time_ns()


def test_with_no_secret_to_hide_nothing_is_read_and_the_mark_stays_where_it_was(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(commands, "_cli_output_state", lambda seat: pytest.fail("the files were listed"))
    commands.sweep_outputs("seat", secretstore.Scrubber({}))
    assert session.sweep_mark("seat") is None


def test_a_secret_stored_later_still_finds_the_files_that_came_before(monkeypatch, tmp_path):
    """No sweep ran while the seat held no secret, so no mark says the old files are clean."""
    _stub_browser(monkeypatch, tmp_path)
    log = _log()
    assert commands.run_script("seat", "url").ok
    secretstore.set_secret("seat", "PASSWORD", VALUE, [ORIGIN])
    assert commands.run_script("seat", "url").ok
    assert VALUE not in _read(log)


@POSIX_MODES
def test_the_files_of_the_driver_are_made_mode_0600(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    text = _log("page-a.yml", "- generic: nothing\n")
    pixels = os.path.join(_cli_dir(), "page-b.png")
    with open(pixels, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + VALUE.encode() + b"\xff")
    os.chmod(text, 0o664)
    os.chmod(pixels, 0o664)
    commands.sweep_outputs("seat", secretstore.Scrubber(secretstore.read("seat")))
    assert stat.S_IMODE(os.stat(text).st_mode) == 0o600 and stat.S_IMODE(os.stat(pixels).st_mode) == 0o600


def test_a_picture_and_a_recording_are_not_read(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    for name in ("page-1.png", "page-2.JPEG", "clip.webm", "doc.pdf"):
        with open(os.path.join(_cli_dir(), name), "wb") as handle:
            handle.write(VALUE.encode())
    read = []
    real = secretstore.Scrubber.scrub_file
    monkeypatch.setattr(secretstore.Scrubber, "scrub_file", lambda self, target, **k: read.append(target) or real(self, target, **k))
    commands.sweep_outputs("seat", secretstore.Scrubber(secretstore.read("seat")))
    assert read == [], "none of the four was read"
    for name in ("page-1.png", "page-2.JPEG", "clip.webm", "doc.pdf"):
        assert _read_bytes(os.path.join(_cli_dir(), name)) == VALUE.encode()


def test_a_file_the_sweep_cannot_read_does_not_stop_the_script(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    log = _log()
    monkeypatch.setattr(secretstore.Scrubber, "scrub_file", lambda self, target, **k: False)
    run = commands.run_script("seat", "url")
    assert run.ok, run.error
    assert VALUE in _read(log), "it could not be scrubbed, and the run still went on (the step scrub does its part)"


def test_a_file_that_holds_the_value_and_cannot_be_rewritten_is_removed(monkeypatch, tmp_path):
    """A log of the driver is not worth keeping with a value in it."""
    _with_secret(monkeypatch, tmp_path)
    log = _log()
    other = _log("page-1.yml", "- generic: nothing\n")
    real = os.replace

    def refuse(source, target):
        if os.path.basename(target) == os.path.basename(log):
            raise OSError("no space left on device")
        return real(source, target)

    monkeypatch.setattr(secretstore.os, "replace", refuse)
    commands.sweep_outputs("seat", secretstore.Scrubber(secretstore.read("seat")))
    assert not os.path.exists(log)
    assert os.path.exists(other), "a file with no value in it is left alone"
    assert not [name for name in os.listdir(_cli_dir()) if name.endswith(".tmp")]


def test_a_file_that_can_be_neither_rewritten_nor_removed_is_emptied(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    log = _log()
    real_unlink = os.unlink

    def refuse(target, *args, **kwargs):
        if os.path.basename(target) == os.path.basename(log):
            raise OSError("operation not permitted")
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(secretstore.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("read only")))
    monkeypatch.setattr(secretstore.os, "unlink", refuse)
    commands.sweep_outputs("seat", secretstore.Scrubber(secretstore.read("seat")))
    assert _read(log) == ""


def test_a_file_that_can_be_neither_rewritten_removed_nor_emptied_is_reported_unchanged(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    log = _log()
    monkeypatch.setattr(secretstore.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("read only")))
    monkeypatch.setattr(secretstore.os, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    monkeypatch.setattr(secretstore.os, "truncate", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    scrubber = secretstore.Scrubber(secretstore.read("seat"))
    assert scrubber.scrub_file(log, remove_on_failure=True) is False
    assert VALUE in _read(log)


def test_the_files_a_step_changed_are_removed_when_they_cannot_be_scrubbed_too(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    target = os.path.join(_cli_dir(), "page-x.yml")
    real = os.replace

    def write(seat, *args, **kwargs):
        with open(target, "w") as handle:
            handle.write(f"value {VALUE}")
        return "### Result\nok\n"

    monkeypatch.setattr(plc, "run", write)
    monkeypatch.setattr(secretstore.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("read only")))
    commands.run_script("seat", "press Enter")
    assert not os.path.exists(target)


@POSIX_MODES
def test_the_mark_is_a_private_file_made_beside_its_target_and_renamed_over_it(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    renamed = []
    real = os.replace
    monkeypatch.setattr(session.os, "replace", lambda a, b: (renamed.append((a, b)), real(a, b))[1])
    session.mark_swept("seat", 12345)
    (source, destination), = renamed
    assert os.path.basename(source) == f"swept-at.{os.getpid()}.{threading.get_ident()}.tmp"
    assert os.path.dirname(source) == os.path.dirname(destination) == session.seat_home("seat")
    assert stat.S_IMODE(os.stat(destination).st_mode) == 0o600
    assert session.sweep_mark("seat") == 12345
    assert os.listdir(session.seat_home("seat")).count("swept-at") == 1
    assert not [name for name in os.listdir(session.seat_home("seat")) if name.endswith(".tmp")]


def test_a_mark_that_cannot_be_written_is_ignored(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(session.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("read only")))
    session.mark_swept("seat", 1)
    assert session.sweep_mark("seat") is None
    assert not [name for name in os.listdir(session.seat_home("seat")) if name.endswith(".tmp")]


@pytest.mark.parametrize("text", ["", "abc", "1.5", "-", "12 34"])
def test_a_mark_that_is_not_a_number_means_there_was_no_sweep(monkeypatch, tmp_path, text):
    _stub_browser(monkeypatch, tmp_path)
    session.private_dir(session.seat_home("seat"))
    with open(os.path.join(session.seat_home("seat"), "swept-at"), "w") as handle:
        handle.write(text)
    assert session.sweep_mark("seat") is None


# --- the commands of the CLI begin the same way ---------------------------------------------------------------


@pytest.mark.parametrize("verb", [["open"], ["snap"], ["console"], ["go", "https://example.com/"]])
def test_a_command_of_the_cli_sweeps_the_files_of_the_driver_first(monkeypatch, tmp_path, verb):
    _store(("PASSWORD", VALUE))
    log = _log(seat="s")
    seen = []
    monkeypatch.setattr(session, "ensure_running", lambda seat, script_epoch=None: seen.append(_read(log)))
    monkeypatch.setattr(session, "open_if_down", lambda seat, epoch: seen.append(_read(log)))
    monkeypatch.setattr(session, "status", lambda seat: "s: attached")
    monkeypatch.setattr(session, "snapshot", lambda seat: "- page")
    monkeypatch.setattr(session, "current_url", lambda seat: "https://example.com/")
    monkeypatch.setattr(cli.plc, "run", lambda *a, **k: "### Result\nok\n")
    monkeypatch.setattr(commands, "refuse_a_local_page", lambda *a, **k: None)
    assert _run(monkeypatch, *verb, "--seat", "s") == 0
    assert seen and VALUE not in seen[0], "the file was clean when the command first used the page"
    assert VALUE not in _read(log)


# --- state.json -----------------------------------------------------------------------------------------------


def _state_path(seat="seat"):
    return os.path.join(session.seat_home(seat), "state.json")


def _daemon_writes(monkeypatch, content, mode=0o644):
    """playwright-cli as `state-save` meets it: it writes the file, in its own mode, the way Node does."""
    calls = []

    def run(seat, *args, **kwargs):
        calls.append(args)
        if args[0] == "state-save":
            target = args[1]
            aside = target + ".daemon"
            with open(os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as handle:
                handle.write(content)
            os.chmod(aside, mode)
            os.replace(aside, target)
        return "### Result\nok\n"

    monkeypatch.setattr(plc, "run", run)
    return calls


@POSIX_MODES
def test_state_json_is_mode_0600_whatever_mode_the_driver_writes_it_in(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    _daemon_writes(monkeypatch, '{"origins": [{"localStorage": [{"name": "remembered_user", "value": "x"}]}]}', 0o644)
    path = session.save_state("seat")
    assert path == _state_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "remembered_user" in _read(path), "the driver's content is kept: it is the profile's own copy"


@POSIX_MODES
def test_state_json_is_private_before_the_driver_writes_into_it(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    seen = []

    def run(seat, *args, **kwargs):
        if args[0] == "state-save":
            seen.append(stat.S_IMODE(os.stat(args[1]).st_mode))
        return "### Result\nok\n"

    monkeypatch.setattr(plc, "run", run)
    session.save_state("seat")
    assert seen == [0o600]


@POSIX_MODES
def test_an_older_state_json_with_a_wide_mode_is_closed(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    session.private_dir(session.seat_home("seat"))
    with open(_state_path(), "w") as handle:
        handle.write("{}")
    os.chmod(_state_path(), 0o666)
    monkeypatch.setattr(plc, "run", lambda *a, **k: "### Result\nok\n")
    session.save_state("seat")
    assert stat.S_IMODE(os.stat(_state_path()).st_mode) == 0o600
    assert _read(_state_path()) == "{}", "the file the driver did not write is not emptied"


def test_a_save_that_fails_leaves_no_empty_state_file_it_made(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)

    def fail(seat, *args, **kwargs):
        raise plc.BrowserError("the browser is not open")

    monkeypatch.setattr(plc, "run", fail)
    with pytest.raises(plc.BrowserError):
        session.save_state("seat")
    assert not os.path.exists(_state_path())


def test_a_save_that_fails_leaves_the_state_file_that_was_there(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    session.private_dir(session.seat_home("seat"))
    with open(_state_path(), "w") as handle:
        handle.write("{}")
    monkeypatch.setattr(plc, "run", lambda *a, **k: (_ for _ in ()).throw(plc.BrowserError("no")))
    with pytest.raises(plc.BrowserError):
        session.save_state("seat")
    assert _read(_state_path()) == "{}"


@POSIX_MODES
def test_the_seat_directory_stays_mode_0700_around_it(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(plc, "run", lambda *a, **k: "### Result\nok\n")
    os.makedirs(session.seat_home("seat"), mode=0o755, exist_ok=True)
    os.chmod(session.seat_home("seat"), 0o755)
    session.save_state("seat")
    assert stat.S_IMODE(os.stat(session.seat_home("seat")).st_mode) == 0o700


def test_save_reports_that_it_saved_and_nothing_of_the_file(monkeypatch, tmp_path):
    """The localStorage of the site holds the user name the tool typed. The step names no content and no path."""
    _with_secret(monkeypatch, tmp_path)
    _daemon_writes(monkeypatch, f'{{"localStorage": [{{"name": "remembered_user", "value": "{VALUE}"}}]}}')
    run = commands.run_script("seat", "save")
    assert run.ok, run.error
    assert run.steps == [{"command": "save", "ok": True, "output": "saved"}]
    assert run.files == []
    assert VALUE not in run.transcript()
    assert VALUE in _read(_state_path()), "the site's own copy is in the seat's state file, readable by the OS user only"


def test_the_state_file_is_never_attached_by_any_verb(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    _daemon_writes(monkeypatch, f"remembered {VALUE}")
    monkeypatch.setattr(commands.session, "snapshot", lambda seat: "- page")
    run = commands.run_script("seat", "save\nsnap\nurl\nclose")
    assert run.ok, run.error
    assert _state_path() not in run.files
    assert not [path for path in run.files if path.endswith("state.json")]


@pytest.mark.parametrize("line", ["upload {path}", "drop #target {path}"])
def test_the_state_file_cannot_be_handed_back_to_a_page(monkeypatch, tmp_path, line):
    _with_secret(monkeypatch, tmp_path)
    _daemon_writes(monkeypatch, f"remembered {VALUE}")
    run = commands.run_script("seat", "save\n" + line.format(path=_state_path()))
    assert not run.ok and "inside the browser state directory" in run.error
    assert VALUE not in run.transcript()


@POSIX_MODES
def test_closing_the_browser_saves_the_state_privately_too(monkeypatch, tmp_path):
    _stub_browser(monkeypatch, tmp_path)
    monkeypatch.setattr(plc, "is_open", lambda seat: True)
    monkeypatch.setattr(session, "_terminate_profile", lambda seat: None)
    _daemon_writes(monkeypatch, "{}", 0o644)
    session.close_browser("seat")
    assert stat.S_IMODE(os.stat(_state_path()).st_mode) == 0o600
