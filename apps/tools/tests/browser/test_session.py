import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time

import pytest

from browser import session


def test_each_seat_gets_its_own_profile_and_artifacts(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert session.profile_dir("garmin") != session.profile_dir("work")
    assert session.profile_dir("garmin").startswith(str(tmp_path))
    assert session.artifacts_dir("garmin").startswith(session.seat_home("garmin"))


def test_state_root_is_outside_the_install(monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "/data")
    assert session.state_root() == "/data/ar3/browser"


def _launched_args(launched):
    """A stand-in for `plc.run` that records the Chrome command line of each launch config it is handed."""
    def run(seat, *args, **kwargs):
        if args and args[0] == "open":
            config = args[1].split("=", 1)[1]
            with open(config, encoding="utf-8") as handle:
                launched.append(json.load(handle)["browser"]["launchOptions"]["args"])
        return ""
    return run


def _sibling_process(seat, helper=False):
    """A harmless process whose argv carries the seat's profile flag; with
    `helper`, it also carries `--type=` before it, like a Chrome helper."""
    flag = f"--user-data-dir={session.profile_dir(seat)}"
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    if helper:
        argv.append("--type=renderer")
    argv.append(flag)
    proc = subprocess.Popen(argv)
    time.sleep(0.3)
    return proc


@pytest.mark.skipif(shutil.which("pgrep") is None, reason="needs pgrep")
def test_main_pids_is_the_profile_holder_without_a_type_flag(monkeypatch, tmp_path):
    """The binary name is never matched: a `google-chrome` wrapper execs
    `/opt/google/chrome/chrome`, so only the profile flag and the absence of
    `--type=` identify the main process."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/usr/bin/google-chrome")
    main = _sibling_process("review-main")
    helper = _sibling_process("review-main", helper=True)
    try:
        assert session._main_pids("review-main") == [str(main.pid)]
        assert sorted(session._profile_pids("review-main")) == sorted(
            [str(main.pid), str(helper.pid)]
        )
        assert session._main_pids("review-mai") == []
    finally:
        for proc in (main, helper):
            proc.kill()
            proc.wait()


@pytest.mark.skipif(shutil.which("pgrep") is None, reason="needs pgrep")
def test_profile_pids_match_the_exact_seat_only(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    procs = {name: _sibling_process(name) for name in ("review-axb", "review-ab")}
    try:
        for other in ("review-a.b", "review-a"):
            assert session._profile_pids(other) == []
        assert session._profile_pids("review-axb") == [str(procs["review-axb"].pid)]
        assert session._profile_pids("review-ab") == [str(procs["review-ab"].pid)]
    finally:
        for proc in procs.values():
            proc.kill()
            proc.wait()


def test_close_escalates_to_sigkill_for_a_wedged_browser(monkeypatch, tmp_path):
    """A browser that shrugs off SIGTERM (the stuck-dock-icon case) still dies."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(session.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(session.plc, "is_open", lambda seat: False)
    monkeypatch.setattr(session, "_main_pids", lambda seat: ["11"])
    monkeypatch.setattr(session, "_profile_pids", lambda seat: ["11", "12"])
    monkeypatch.setattr(session, "_daemon_pids", lambda seat: ["99"])
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    killed = []
    monkeypatch.setattr(session, "_signal", lambda pid, sig: killed.append((pid, sig)))
    session.close_browser("seat")
    import signal
    assert ( "11", signal.SIGTERM) in killed
    assert ("11", signal.SIGKILL) in killed
    assert ("12", signal.SIGKILL) in killed
    assert ("99", signal.SIGTERM) in killed


def _fake_close(monkeypatch, per_step_seconds, budget):
    """Fake the playwright layer; each step spends `per_step_seconds` of real time."""
    order = []
    monkeypatch.setattr(session, "GRACEFUL_CLOSE_SECONDS", budget)
    monkeypatch.setattr(session.plc, "is_open", lambda seat: True)
    monkeypatch.setattr(session, "video_state", lambda seat: {"path": "x"})
    monkeypatch.setattr(session, "clear_video_state", lambda seat: None)

    def step(name):
        def run(*args, timeout=30, **kw):
            order.append((name, timeout))
            time.sleep(per_step_seconds)
            return ""
        return run

    monkeypatch.setattr(session.plc, "run", lambda seat, *a, timeout=30: step(a[0])(timeout=timeout))
    monkeypatch.setattr(session.plc, "run_code", lambda seat, body, timeout=60: step("browser-close")(timeout=timeout))
    monkeypatch.setattr(session, "_terminate_profile", lambda seat: order.append(("terminate", 0)))
    return order


def test_a_slow_graceful_step_cannot_push_close_past_the_stop_deadline(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    order = _fake_close(monkeypatch, per_step_seconds=0.6, budget=1.0)
    begun = time.monotonic()
    session.close_browser("seat")
    assert time.monotonic() - begun < 1.0 + 1.0
    assert order[-1] == ("terminate", 0)
    # the budget ran out, so later graceful steps were skipped
    assert [n for n, _ in order].count("close") == 0
    assert all(t <= 1.0 for _, t in order[:-1])


def test_a_fast_close_runs_every_graceful_step_in_order(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    order = _fake_close(monkeypatch, per_step_seconds=0, budget=15)
    session.close_browser("seat")
    assert [n for n, _ in order] == [
        "dialog-dismiss", "state-save", "video-stop", "browser-close", "close", "terminate",
    ]


def _pipe_launch(monkeypatch, tmp_path, owned=True, profile=()):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    monkeypatch.setattr(session.plc, "is_open", lambda seat: False)
    monkeypatch.setattr(session, "_profile_pids", lambda seat: list(profile))
    monkeypatch.setattr(session, "owned_by_driver", lambda seat: owned)
    steps = []
    monkeypatch.setattr(session, "_terminate_profile", lambda seat: steps.append("terminate"))
    monkeypatch.setattr(session, "check_launch_argv", lambda seat: [])
    return steps


def test_open_cleans_up_chrome_when_the_launch_fails(monkeypatch, tmp_path):
    """A failed launch must not leave a browser nobody can drive running."""
    steps = _pipe_launch(monkeypatch, tmp_path)
    def failing_run(*a, **k):
        raise session.plc.BrowserError("open refused")
    monkeypatch.setattr(session.plc, "run", failing_run)
    with pytest.raises(session.plc.BrowserError):
        session.open_if_down("seat", None)
    assert steps == ["terminate"]


def test_open_cleans_up_chrome_when_routing_downloads_fails(monkeypatch, tmp_path):
    steps = _pipe_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(session.plc, "run", lambda *a, **k: "")
    def failing_code(*a, **k):
        raise session.plc.BrowserError("refused")
    monkeypatch.setattr(session.plc, "run_code", failing_code)
    with pytest.raises(session.plc.BrowserError):
        session.open_if_down("seat", None)
    assert steps == ["terminate"]


def test_a_launch_that_is_not_the_seats_own_chrome_under_its_driver_is_ended(monkeypatch, tmp_path):
    steps = _pipe_launch(monkeypatch, tmp_path, owned=False)
    monkeypatch.setattr(session.plc, "run", lambda *a, **k: "")
    monkeypatch.setattr(session.plc, "run_code", lambda *a, **k: steps.append("route"))
    with pytest.raises(session.plc.BrowserError, match="did not come up under the seat's driver"):
        session.open_if_down("seat", None)
    assert steps == ["terminate"]


def test_a_chrome_that_holds_the_profile_with_no_driver_is_ended_before_the_launch(monkeypatch, tmp_path):
    steps = _pipe_launch(monkeypatch, tmp_path, profile=["7"])
    monkeypatch.setattr(session.plc, "run", lambda seat, *a, **k: steps.append(a[0]))
    monkeypatch.setattr(session.plc, "run_code", lambda *a, **k: steps.append("route"))
    session.open_if_down("seat", None)
    assert steps == ["terminate", "open", "route"]


def test_the_launch_config_is_the_seats_own_flags_over_the_pipe_and_private(monkeypatch, tmp_path):
    steps = _pipe_launch(monkeypatch, tmp_path)
    configs = []
    def run(seat, *args, **kwargs):
        assert isinstance(args[1], session.plc.Own)
        configs.append(args[1].split("=", 1)[1])
        return ""
    monkeypatch.setattr(session.plc, "run", run)
    monkeypatch.setattr(session.plc, "run_code", lambda *a, **k: "")
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    session.open_if_down("seat", None)
    assert configs == [session.launch_config_path("seat")]
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(configs[0]).st_mode) == 0o600
    with open(configs[0], encoding="utf-8") as handle:
        browser = json.load(handle)["browser"]
    options = browser["launchOptions"]
    assert browser["userDataDir"] == session.profile_dir("seat")
    assert browser["contextOptions"] == {"viewport": None}
    assert options["executablePath"] == "/bin/chrome"
    assert options["ignoreDefaultArgs"] is True
    assert options["chromiumSandbox"] is True
    assert options["args"] == [
        f"--user-data-dir={session.profile_dir('seat')}",
        "--remote-debugging-pipe",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-backgrounding-occluded-windows",
        "--test-type=webdriver",
        "--headless=new",
        "about:blank",
    ]
    assert not any(arg.startswith("--remote-debugging-port") for arg in options["args"])


def test_the_bar_off_flag_is_in_the_seats_args_and_why():
    """playwright-cli adds `--disable-blink-features=AutomationControlled` to every Chromium launch whose args hold
    no argument with `--disable-blink-features` in it, whatever `ignoreDefaultArgs` says. Chrome shows its "unsupported
    command-line flag" bar for that flag with ANY value (the empty value too). The flag has to stay, because without it `navigator.webdriver` is true over the
    pipe. `--test-type=webdriver` is the switch that keeps Chrome from showing the bar. If this fails, the bar is back
    on every seat."""
    args = session._launch_args("seat")
    assert session.BAR_OFF_FLAG == "--test-type=webdriver"
    assert session.BAR_OFF_FLAG in args
    assert session.PLAYWRIGHT_ADDED_FLAGS == ("--disable-blink-features=AutomationControlled",)
    assert not any("--disable-blink-features" in arg for arg in args)
    assert args[-1] == "about:blank"


def test_the_argv_a_real_launch_produces_has_no_unasked_flag(monkeypatch):
    """The command line Chrome gets is ours, then playwright-cli's one addition. Nothing else may be in it."""
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    argv = session._launch_args("seat") + list(session.PLAYWRIGHT_ADDED_FLAGS)
    assert session.unasked_flags("seat", argv) == []


def test_flags_nobody_asked_for_are_named(monkeypatch):
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    argv = session._launch_args("seat") + list(session.PLAYWRIGHT_ADDED_FLAGS)
    extra = ["--enable-automation", "--no-sandbox", "--password-store=basic", "--disable-blink-features=Other"]
    assert session.unasked_flags("seat", argv + extra) == extra
    lost_guard = [a for a in argv if a != session.BAR_OFF_FLAG]
    assert session.unasked_flags("seat", lost_guard) == []
    assert session.unasked_flags("seat", ["--user-data-dir=/elsewhere"]) == []


def _checked(monkeypatch, pids, argv):
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    monkeypatch.setattr(session, "_main_pids", lambda seat: pids)
    monkeypatch.setattr(session, "_process_argv", lambda pid: argv)


def test_a_launch_with_an_unasked_flag_writes_one_line_and_goes_on(monkeypatch, capsys):
    chrome = ["/usr/bin/chrome"] + session._launch_args("seat") + ["--disable-blink-features=AutomationControlled"]
    _checked(monkeypatch, ["7"], chrome + ["--enable-automation", "--no-sandbox"])
    assert session.check_launch_argv("seat") == ["--enable-automation", "--no-sandbox"]
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "seat: Chrome started with a flag the seat did not ask for: --enable-automation --no-sandbox" in err


def test_a_clean_launch_says_nothing(monkeypatch, capsys):
    chrome = ["/usr/bin/chrome"] + session._launch_args("seat") + ["--disable-blink-features=AutomationControlled"]
    _checked(monkeypatch, ["7"], chrome)
    assert session.check_launch_argv("seat") == []
    assert capsys.readouterr().err == ""


def test_the_launch_check_never_raises_and_skips_what_it_cannot_read(monkeypatch, capsys):
    _checked(monkeypatch, [], None)
    assert session.check_launch_argv("seat") == []
    _checked(monkeypatch, ["7", "8"], ["chrome", "--enable-automation"])
    assert session.check_launch_argv("seat") == []
    _checked(monkeypatch, ["7"], None)
    assert session.check_launch_argv("seat") == []
    def broken(pid):
        raise RuntimeError("proc table gone")
    monkeypatch.setattr(session, "_process_argv", broken)
    assert session.check_launch_argv("seat") == []
    monkeypatch.setattr(session.platform, "system", lambda: "Windows")
    assert session.check_launch_argv("seat") == []
    assert capsys.readouterr().err == ""


def test_an_unasked_flag_does_not_stop_the_launch(monkeypatch, tmp_path, capsys):
    real = session.check_launch_argv
    steps = _pipe_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(session, "check_launch_argv", real)
    _checked(monkeypatch, ["7"], ["chrome", "--enable-automation"])
    monkeypatch.setattr(session.plc, "run", lambda *a, **k: "")
    monkeypatch.setattr(session.plc, "run_code", lambda *a, **k: steps.append("route"))
    session.open_if_down("seat", None)
    assert steps == ["route"]
    assert "--enable-automation" in capsys.readouterr().err


def test_the_driver_owns_the_seat_only_with_one_browser_process_under_its_daemon(monkeypatch):
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    monkeypatch.setattr(session, "_daemon_pids", lambda seat: ["40"])
    parents = {"7": "40", "8": "1"}
    monkeypatch.setattr(session, "_parent_pid", lambda pid: parents[pid])
    monkeypatch.setattr(session, "_main_pids", lambda seat: ["7"])
    assert session.owned_by_driver("seat") is True
    monkeypatch.setattr(session, "_main_pids", lambda seat: ["8"])
    assert session.owned_by_driver("seat") is False
    monkeypatch.setattr(session, "_main_pids", lambda seat: ["7", "8"])
    assert session.owned_by_driver("seat") is False
    monkeypatch.setattr(session, "_main_pids", lambda seat: [])
    assert session.owned_by_driver("seat") is False


class FakeSeat:
    """A seat's Chrome as the plc layer sees it: one driven page, maybe in a window."""

    def __init__(self, monkeypatch, url, window_state="normal", visibility="visible"):
        self.url = url
        self.window_state = window_state
        self.visibility = visibility
        self.calls = []
        monkeypatch.setattr(session.plc, "is_open", lambda seat: True)
        monkeypatch.setattr(session.plc, "evaluate", self.evaluate)
        monkeypatch.setattr(session.plc, "run_code", self.run_code)
        monkeypatch.setattr(session.plc, "run", self.run)
        monkeypatch.setattr(session, "close_browser", lambda seat: self.restart("close"))
        monkeypatch.setattr(session, "open_if_down", lambda seat, epoch=None: self.restart("open"))

    def restart(self, step):
        self.calls.append(step)
        if step == "open":
            self.url, self.window_state, self.visibility = "about:blank", "normal", "visible"

    def evaluate(self, seat, expression, timeout=30):
        return self.visibility if "visibilityState" in expression else self.url

    def run_code(self, seat, body, timeout=60):
        if "setWindowBounds" in body:
            self.calls.append("unminimise")
            self.window_state, self.visibility = "normal", "visible"
            return "### Result\nundefined"
        if "bringToFront" in body:
            self.calls.append("front")
            self.visibility = "visible"
            return "### Result\nundefined"
        window = "null" if self.window_state is None else "7"
        state = "null" if self.window_state is None else f'\\"{self.window_state}\\"'
        return (
            f'### Result\n"{{\\"url\\":\\"{self.url}\\",\\"visible\\":\\"{self.visibility}\\",'
            f'\\"window\\":{window},\\"state\\":{state}}}"'
        )

    def run(self, seat, *args, timeout=30):
        self.calls.append(" ".join(args))
        if args[0] == "goto":
            self.url = args[1]
        return ""


def test_a_window_behind_other_windows_keeps_its_chrome_and_its_page(monkeypatch):
    """A covered window reads visible under the launch flag, and is left alone."""
    seat = FakeSeat(monkeypatch, "https://example.com/")
    session.ensure_running("seat")
    assert seat.calls == []
    assert seat.url == "https://example.com/"


def test_a_hidden_page_in_a_normal_window_is_brought_forward_not_restarted(monkeypatch):
    """A hidden app's page reads hidden in a normal window; it is not a dead browser."""
    seat = FakeSeat(monkeypatch, "https://example.com/", visibility="hidden")
    session.ensure_running("seat")
    assert seat.calls == ["front"]
    assert seat.url == "https://example.com/"


def test_a_minimised_window_is_restored_not_restarted(monkeypatch):
    seat = FakeSeat(
        monkeypatch, "https://example.com/", window_state="minimized", visibility="hidden"
    )
    session.ensure_running("seat")
    assert seat.calls == ["unminimise"]
    assert seat.url == "https://example.com/"


def test_a_page_no_window_holds_is_restarted_back_onto_its_url(monkeypatch):
    seat = FakeSeat(monkeypatch, "https://example.com/", window_state=None, visibility="hidden")
    session.ensure_running("seat")
    assert seat.calls == ["close", "open", "goto https://example.com/"]
    assert seat.url == "https://example.com/"


def test_a_restart_from_a_blank_page_navigates_nowhere(monkeypatch):
    seat = FakeSeat(monkeypatch, "about:blank", window_state=None, visibility="hidden")
    session.ensure_running("seat")
    assert seat.calls == ["close", "open"]


def test_a_restart_that_cannot_return_names_the_page_it_lost(monkeypatch):
    seat = FakeSeat(monkeypatch, "https://example.com/", window_state=None, visibility="hidden")
    def refuse(seat_name, *args, timeout=30):
        raise session.plc.BrowserError("net::ERR_NAME_NOT_RESOLVED")
    monkeypatch.setattr(session.plc, "run", refuse)
    try:
        session.ensure_running("seat")
    except session.plc.BrowserError as exc:
        assert "https://example.com/" in str(exc)
    else:
        raise AssertionError("a lost page must not pass silently")
    assert seat.calls == ["close", "open"]


def test_a_probe_the_driver_refuses_leaves_the_browser_alone(monkeypatch):
    """An open dialog blocks the driver; that seat is busy, not dead."""
    seat = FakeSeat(monkeypatch, "https://example.com/", visibility="hidden")
    def busy(*a, **k):
        raise session.plc.BrowserError("does not handle the modal state")
    monkeypatch.setattr(session.plc, "run_code", busy)
    monkeypatch.setattr(session.plc, "evaluate", busy)
    session.ensure_running("seat")
    assert seat.calls == []


def test_chrome_launches_with_occluded_windows_kept_visible(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    launched = []
    monkeypatch.setattr(session.plc, "run", _launched_args(launched))
    session._launch_chrome("seat")
    assert "--disable-backgrounding-occluded-windows" in launched[0]


def test_every_probe_routes_downloads_into_the_seat(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    bodies = []
    monkeypatch.setattr(session.plc, "is_open", lambda seat: True)
    page = {"url": "about:blank", "visible": "visible", "window": 7, "state": "normal"}
    def probe(seat, body, timeout=60):
        bodies.append(body)
        return "### Result\n" + json.dumps(json.dumps(page))
    monkeypatch.setattr(session.plc, "run_code", probe)
    session.ensure_running("seat")
    assert "Browser.setDownloadBehavior" in bodies[0]
    assert f"downloadPath: '{session.downloads_dir('seat')}'" in bodies[0]
    assert session.downloads_dir("seat").startswith(str(tmp_path))


def test_open_routes_downloads_right_after_the_launch(monkeypatch, tmp_path):
    _pipe_launch(monkeypatch, tmp_path)
    steps = []
    monkeypatch.setattr(session.plc, "run", lambda seat, *a, **k: steps.append(a[0]))
    monkeypatch.setattr(
        session.plc, "run_code",
        lambda seat, body, timeout=60: steps.append(
            "route" if "setDownloadBehavior" in body else "code"
        ),
    )
    session.open_if_down("seat", None)
    assert steps == ["open", "route"]


def test_chrome_launches_headless_on_linux_without_a_display(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    launched = []
    monkeypatch.setattr(session.plc, "run", _launched_args(launched))
    session._launch_chrome("seat")
    assert "--headless=new" in launched[0]


def test_chrome_launches_headed_when_a_display_is_set_or_off_linux(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    launched = []
    monkeypatch.setattr(session.plc, "run", _launched_args(launched))
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    monkeypatch.setenv("DISPLAY", ":0")
    session._launch_chrome("seat")
    monkeypatch.setattr(session.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("DISPLAY")
    session._launch_chrome("seat")
    assert not any("--headless=new" in cmd for cmd in launched)


def test_ensure_running_with_a_running_chrome_ignores_the_epoch(monkeypatch):
    seat = FakeSeat(monkeypatch, "https://example.com/")
    monkeypatch.setattr(session, "stop_epoch", lambda s: pytest.fail("epoch read"))
    session.ensure_running("seat", script_epoch="old")


def _closed_chrome(monkeypatch):
    launched = []
    monkeypatch.setattr(session.plc, "is_open", lambda s: False)
    monkeypatch.setattr(session, "_start_chrome", lambda s: launched.append(s))
    monkeypatch.setattr(session, "_attach", lambda s: None)
    monkeypatch.setattr(session, "_window_of_page", lambda s: {"window": 1, "visible": "visible"})
    return launched


def test_ensure_running_will_not_relaunch_after_a_stop(monkeypatch):
    launched = _closed_chrome(monkeypatch)
    old = session.stop_epoch("seat")
    session.bump_stop_epoch("seat")
    with pytest.raises(session.plc.BrowserError, match="seat closed by a8s stop"):
        session.ensure_running("seat", script_epoch=old)
    assert launched == []


def test_ensure_running_launches_when_the_epoch_is_unchanged_or_absent(monkeypatch):
    launched = _closed_chrome(monkeypatch)
    session.bump_stop_epoch("seat")
    session.ensure_running("seat", script_epoch=session.stop_epoch("seat"))
    session.ensure_running("seat")
    assert launched == ["seat", "seat"]


def test_the_stop_epoch_is_empty_until_bumped_and_changes_each_bump():
    assert session.stop_epoch("fresh") == ""
    first = session.bump_stop_epoch("fresh")
    second = session.bump_stop_epoch("fresh")
    assert first and second != first
    assert session.stop_epoch("fresh") == second


def test_the_activity_stamp_is_missing_until_touched_and_then_reads_as_now():
    assert session.last_activity("fresh") is None
    before = time.time()
    session.touch_activity("fresh")
    stamp = session.last_activity("fresh")
    assert before <= stamp <= time.time()
    assert os.listdir(session.seat_home("fresh")) == ["last-active"]


@pytest.mark.parametrize("text", ["", "  \n", "soon", "nan", "inf", "-inf", "12s"])
def test_an_activity_stamp_that_is_not_a_number_reads_as_missing(text):
    os.makedirs(session.seat_home("seat"))
    with open(os.path.join(session.seat_home("seat"), "last-active"), "w") as handle:
        handle.write(text)
    assert session.last_activity("seat") is None


def test_an_unreadable_activity_stamp_reads_as_missing():
    os.makedirs(os.path.join(session.seat_home("seat"), "last-active"))
    assert session.last_activity("seat") is None


def test_the_activity_stamp_is_written_aside_and_renamed(monkeypatch):
    # A reader polling the stamp must never meet a half-written file, so the
    # write goes to a name only this process and thread use, then replaces it.
    replaced = []
    real = os.replace
    monkeypatch.setattr(session.os, "replace", lambda a, b: (replaced.append((a, b)), real(a, b)))
    session.touch_activity("seat")
    (source, target), = replaced
    assert os.path.basename(source) == f".last-active.{os.getpid()}.{threading.get_ident()}.tmp"
    assert target == os.path.join(session.seat_home("seat"), "last-active")
    assert session.last_activity("seat") is not None


def test_a_reader_never_sees_a_partial_activity_stamp():
    session.touch_activity("seat")
    done = threading.Event()
    bad = []

    def read():
        while not done.is_set():
            if session.last_activity("seat") is None:
                bad.append("unreadable")

    def write():
        for _ in range(300):
            session.touch_activity("seat")

    reader = threading.Thread(target=read)
    writers = [threading.Thread(target=write) for _ in range(4)]
    reader.start()
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()
    done.set()
    reader.join()
    assert bad == []
    assert sorted(os.listdir(session.seat_home("seat"))) == ["last-active"]


def test_a_stamp_that_cannot_be_written_is_ignored(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_DATA_HOME", str(blocker))
    session.touch_activity("seat")
    assert session.last_activity("seat") is None


def test_chrome_running_is_the_profile_pgrep_and_starts_nothing(monkeypatch):
    monkeypatch.setattr(session, "_profile_pids", lambda s: [])
    assert session.chrome_running("seat") is False
    monkeypatch.setattr(session, "_profile_pids", lambda s: ["7", "8"])
    assert session.chrome_running("seat") is True
    assert not os.path.exists(session.seat_home("seat"))


def test_a_launch_waits_on_the_launch_lock_then_refuses_after_the_stop(monkeypatch):
    import textwrap
    launched = _closed_chrome(monkeypatch)
    old = session.stop_epoch("seat")
    code = textwrap.dedent("""
        import time
        from browser import session
        with session.launch_lock("seat"):
            print("held", flush=True)
            time.sleep(1.5)
            session.bump_stop_epoch("seat")
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": ":".join(sys.path)})
    try:
        assert proc.stdout.readline().strip() == "held"
        started = time.monotonic()
        with pytest.raises(session.plc.BrowserError, match="seat closed by a8s stop"):
            session.ensure_running("seat", script_epoch=old)
        assert time.monotonic() - started > 1.0
    finally:
        proc.wait()
    assert launched == []


def test_a_stop_that_gave_up_on_the_launch_lock_still_ends_the_launch(monkeypatch, capsys):
    """The exact barrier: the launch passes its check, the stop times out and finds
    no Chrome, then the launch spawns, re-reads the epoch and ends its own Chrome."""
    import threading
    from browser import cli
    events = []
    monkeypatch.setattr(session.plc, "is_open", lambda s: False)
    monkeypatch.setattr(session, "_start_chrome", lambda s: events.append("spawn") or "base")
    monkeypatch.setattr(session, "_attach", lambda s: events.append("attach"))
    monkeypatch.setattr(session, "_terminate_profile", lambda s: events.append("terminate"))
    monkeypatch.setattr(session, "close_browser", lambda s: events.append("close"))
    monkeypatch.setattr(cli, "LAUNCH_LOCK_WAIT_SECONDS", 0.5)
    at_hook, release = threading.Event(), threading.Event()

    def hook():
        at_hook.set()
        assert release.wait(20)

    monkeypatch.setattr(session, "_before_spawn", hook)
    old = session.stop_epoch("seat")
    result = {}

    def launch():
        try:
            session.ensure_running("seat", script_epoch=old)
        except session.plc.BrowserError as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=launch)
    thread.start()
    assert at_hook.wait(10)
    assert cli._close("seat") == 0
    assert events == ["close"]
    assert "launch lock held past 3 s; closing anyway." in capsys.readouterr().err
    release.set()
    thread.join(20)
    assert events == ["close", "spawn", "terminate"]
    assert result["error"] == "seat closed by a8s stop"


def test_the_restart_repair_does_not_reopen_chrome_after_a_stop(monkeypatch):
    events = []
    monkeypatch.setattr(session.plc, "is_open", lambda s: False)
    monkeypatch.setattr(session, "close_browser", lambda s: events.append("close"))
    monkeypatch.setattr(session, "_start_chrome", lambda s: events.append("spawn") or "base")
    monkeypatch.setattr(session, "_before_spawn", None)
    old = session.stop_epoch("seat")
    session.bump_stop_epoch("seat")
    with pytest.raises(session.plc.BrowserError, match="seat closed by a8s stop"):
        session._restart_browser("seat", "https://example.com/", old)
    assert events == ["close"]


def test_the_restart_repair_cannot_leave_chrome_when_the_stop_lands_before_the_spawn(monkeypatch):
    events = []
    monkeypatch.setattr(session.plc, "is_open", lambda s: False)
    monkeypatch.setattr(session, "close_browser", lambda s: events.append("close"))
    monkeypatch.setattr(session, "_start_chrome", lambda s: events.append("spawn") or "base")
    monkeypatch.setattr(session, "_terminate_profile", lambda s: events.append("terminate"))
    monkeypatch.setattr(session, "_before_spawn", lambda: session.bump_stop_epoch("seat"))
    with pytest.raises(session.plc.BrowserError, match="seat closed by a8s stop"):
        session._restart_browser("seat", "", session.stop_epoch("seat"))
    assert events == ["close", "spawn", "terminate"]


# --- Chrome's password offer and breach check -------------------------------------

BOTH_OFF = {"credentials_enable_service": False, "profile": {"password_manager_leak_detection": False}}


def _launch(monkeypatch, tmp_path, seat="seat"):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    launched = []
    monkeypatch.setattr(session.plc, "run", _launched_args(launched))
    session._launch_chrome(seat)
    return launched


def _preferences(seat="seat"):
    with open(os.path.join(session.profile_dir(seat), "Default", "Preferences"), encoding="utf-8") as handle:
        return json.load(handle)


def _write_preferences(tmp_path, prefs, seat="seat", **dump):
    directory = os.path.join(session.profile_dir(seat), "Default")
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, "Preferences")
    with open(target, "w") as handle:
        json.dump(prefs, handle, **dump)
    return target


def test_a_new_profile_starts_with_the_password_offer_and_the_breach_check_off(monkeypatch, tmp_path):
    launched = _launch(monkeypatch, tmp_path)
    assert launched
    assert _preferences() == BOTH_OFF


def test_the_two_preferences_are_the_ones_chrome_reads(monkeypatch, tmp_path):
    """Chrome 155 names them in Password Manager Internals: "Saving disabled" for the first,
    "Leak detection disabled in settings" for the second."""
    assert session.PASSWORD_PREFERENCES == (
        (("credentials_enable_service",), False),
        (("profile", "password_manager_leak_detection"), False),
    )


def test_an_existing_profile_keeps_its_preferences_and_loses_the_offer_and_the_check(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    kept = {
        "credentials_enable_service": True,
        "profile": {"name": "Person 1", "password_manager_leak_detection": True, "avatar_index": 26},
        "browser": {"window_placement": {"top": 3}},
        "intl": {"selected_languages": "en-US,en"},
    }
    _write_preferences(tmp_path, kept)
    _launch(monkeypatch, tmp_path)
    assert _preferences() == {
        **kept,
        "credentials_enable_service": False,
        "profile": {"name": "Person 1", "password_manager_leak_detection": False, "avatar_index": 26},
    }


@pytest.mark.parametrize(
    "present",
    [
        {"credentials_enable_service": False},
        {"profile": {"password_manager_leak_detection": False}},
        {"profile": {"name": "x"}},
        {"credentials_enable_service": True},
        {"profile": {"password_manager_leak_detection": True}},
        {},
    ],
)
def test_whichever_setting_is_missing_or_on_is_turned_off(monkeypatch, tmp_path, present):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _write_preferences(tmp_path, dict(present))
    _launch(monkeypatch, tmp_path)
    prefs = _preferences()
    assert prefs["credentials_enable_service"] is False
    assert prefs["profile"]["password_manager_leak_detection"] is False
    assert prefs["profile"].get("name") == present.get("profile", {}).get("name")


def test_a_profile_with_both_already_off_is_not_rewritten(monkeypatch, tmp_path):
    _launch(monkeypatch, tmp_path)
    target = _write_preferences(tmp_path, {**BOTH_OFF, "intl": {"a": 1}}, indent=4)
    before = (os.stat(target).st_ino, open(target, "rb").read())
    _launch(monkeypatch, tmp_path)
    assert (os.stat(target).st_ino, open(target, "rb").read()) == before


def test_a_profile_with_only_one_off_is_rewritten_for_the_other(monkeypatch, tmp_path):
    _launch(monkeypatch, tmp_path)
    target = _write_preferences(tmp_path, {"credentials_enable_service": False, "intl": {"a": 1}})
    before = os.stat(target).st_ino
    _launch(monkeypatch, tmp_path)
    assert os.stat(target).st_ino != before
    assert _preferences() == {**BOTH_OFF, "intl": {"a": 1}}


def test_the_preferences_are_written_aside_and_renamed(monkeypatch, tmp_path):
    replaced = []
    real = os.replace
    monkeypatch.setattr(session.os, "replace", lambda a, b: (replaced.append((a, b)), real(a, b))[1])
    _launch(monkeypatch, tmp_path)
    (source, target), = [pair for pair in replaced if pair[1].endswith("Preferences")]
    assert os.path.basename(source) == f".Preferences.{os.getpid()}.{threading.get_ident()}.tmp"
    assert os.path.dirname(source) == os.path.dirname(target)
    assert os.listdir(os.path.dirname(target)) == ["Preferences"]


@pytest.mark.parametrize("damage", [b"{not json", b"", b"\xff\xfe\x00", b"[1, 2]", b"null", b'{"profile": "x"}', b'{"profile": [1]}'])
def test_a_damaged_preferences_file_is_left_for_chrome_to_replace(monkeypatch, tmp_path, damage):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    directory = os.path.join(session.profile_dir("seat"), "Default")
    os.makedirs(directory)
    target = os.path.join(directory, "Preferences")
    with open(target, "wb") as handle:
        handle.write(damage)
    launched = _launch(monkeypatch, tmp_path)
    assert launched
    assert open(target, "rb").read() == damage
    assert os.listdir(directory) == ["Preferences"]


def test_the_preferences_are_set_before_chrome_is_spawned(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    seen = []

    def run(seat, *args, **kwargs):
        seen.append(_preferences())

    monkeypatch.setattr(session.plc, "run", run)
    session._launch_chrome("seat")
    assert seen == [BOTH_OFF]


# --- the seat directories ----------------------------------------------------------

POSIX_MODES = pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def open_umask():
    old = os.umask(0)
    yield
    os.umask(old)


@POSIX_MODES
def test_a_private_dir_is_made_0700_whatever_the_umask(tmp_path, open_umask):
    session.private_dir(str(tmp_path / "made"))
    assert _mode(tmp_path / "made") == 0o700


@POSIX_MODES
@pytest.mark.parametrize("mode", [0o775, 0o755, 0o750, 0o777, 0o710, 0o500, 0o000])
def test_a_private_dir_that_exists_is_set_to_0700(tmp_path, mode):
    target = tmp_path / "there"
    target.mkdir()
    os.chmod(target, mode)
    try:
        session.private_dir(str(target))
        assert _mode(target) == 0o700
    finally:
        os.chmod(target, 0o700)


@POSIX_MODES
def test_a_private_dir_that_is_closed_already_is_left_alone(tmp_path, monkeypatch):
    target = tmp_path / "there"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    monkeypatch.setattr(session.os, "chmod", lambda *a: pytest.fail("a closed directory was chmod-ed"))
    session.private_dir(str(target))


def test_a_private_dir_is_a_directory_and_its_parents_are_made(tmp_path):
    session.private_dir(str(tmp_path / "a" / "b"))
    assert (tmp_path / "a" / "b").is_dir()


def test_a_private_dir_that_cannot_be_tightened_is_left_as_it_is(tmp_path, monkeypatch):
    target = tmp_path / "there"
    target.mkdir()
    os.chmod(target, 0o755)

    def refuse(*a):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(session.os, "chmod", refuse)
    session.private_dir(str(target))
    assert target.is_dir()


@POSIX_MODES
def test_the_seat_home_and_its_scratch_directory_are_closed_when_made(tmp_path, open_umask):
    session.secure_seat("seat")
    assert _mode(session.seat_home("seat")) == 0o700
    assert _mode(os.path.join(session.seat_home("seat"), "scratch")) == 0o700


@POSIX_MODES
def test_the_seat_home_and_its_scratch_directory_are_tightened_when_they_exist(tmp_path):
    home = session.seat_home("seat")
    scratch = os.path.join(home, "scratch")
    os.makedirs(scratch)
    os.chmod(home, 0o775)
    os.chmod(scratch, 0o755)
    session.secure_seat("seat")
    assert _mode(home) == 0o700 and _mode(scratch) == 0o700


@POSIX_MODES
def test_the_scratch_directory_call_tightens_both_directories(tmp_path):
    home = session.seat_home("seat")
    scratch = os.path.join(home, "scratch")
    os.makedirs(scratch)
    os.chmod(home, 0o775)
    os.chmod(scratch, 0o775)
    assert session.scratch_dir("seat") == scratch
    assert _mode(home) == 0o700 and _mode(scratch) == 0o700


def _create_stop_epoch(seat):
    session.bump_stop_epoch(seat)


def _create_activity(seat):
    session.touch_activity(seat)


def _create_launch_lock(seat):
    with session.launch_lock(seat):
        pass


def _create_seat_lock(seat):
    from browser import seatlock

    with seatlock.hold(seat, "test"):
        pass


def _create_seat_lock_without_use(seat):
    from browser import seatlock

    with seatlock.hold(seat, "test", activity=False):
        pass


def _create_downloads(seat):
    session.downloads_dir(seat)


def _create_video_state(seat):
    session.save_video_state(seat, {"path": "x"})


def _create_state_file(seat, monkeypatch=None):
    session.save_state(seat)


def _create_artifact(seat):
    from browser import commands

    commands.artifact(seat, "x.txt")


def _create_store(seat):
    from browser import secretstore

    secretstore.set_secret(seat, "PASSWORD", "value-one", ["https://app.example.com"])


def _create_profile(seat):
    session.private_dir(session.seat_home(seat))
    session._launch_chrome(seat)


@POSIX_MODES
@pytest.mark.parametrize(
    "create",
    [
        _create_stop_epoch, _create_activity, _create_launch_lock, _create_seat_lock,
        _create_seat_lock_without_use, _create_downloads,
        _create_video_state, _create_state_file, _create_artifact, _create_store, _create_profile,
    ],
    ids=lambda func: func.__name__.replace("_create_", ""),
)
def test_whatever_makes_the_seat_home_makes_it_0700(monkeypatch, tmp_path, open_umask, create):
    monkeypatch.setattr(session.plc, "run", lambda *a, **k: "")
    # Through monkeypatch, so it is undone: a value left in os.environ reaches every later test, and the
    # live tests pass the environment on to the tool they start.
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    create("seat")
    assert _mode(session.seat_home("seat")) == 0o700


# --- the state directory ----------------------------------------------------------


def _root():
    root = session.state_root()
    os.makedirs(os.path.join(root, "seat"), exist_ok=True)
    return root


def test_the_root_and_everything_in_it_is_inside(tmp_path):
    root = _root()
    store = os.path.join(root, "seat", "secrets.json")
    open(store, "w").close()
    for inside in (root, root + "/", os.path.join(root, "seat"), store, os.path.join(root, "seat", "never-was"),
                   os.path.join(root, "other-seat", "secrets.json"), os.path.join(root, "seat", "..", "seat", "secrets.json"),
                   os.path.join(root, ".", "seat"), root + "//seat//secrets.json"):
        assert session.inside_state_root(inside), inside


def test_a_path_beside_the_root_is_not_inside(tmp_path):
    root = _root()
    for outside in (str(tmp_path), str(tmp_path / "page.html"), root + "-sibling", root + "-sibling/x",
                    os.path.dirname(root), "/", "/tmp", os.path.join(os.path.dirname(root), "browser2", "x")):
        assert not session.inside_state_root(outside), outside


def test_a_symlink_that_leads_in_is_inside(tmp_path):
    root = _root()
    store = os.path.join(root, "seat", "secrets.json")
    open(store, "w").close()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").symlink_to(store)
    (outside / "dir").symlink_to(root)
    (outside / "chain").symlink_to(outside / "file")
    for inside in (outside / "file", outside / "dir", outside / "dir" / "seat" / "secrets.json", outside / "chain"):
        assert session.inside_state_root(str(inside)), inside


def test_a_symlink_that_leads_out_is_not_inside(tmp_path):
    root = _root()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "page.html").write_text("x")
    os.symlink(elsewhere, os.path.join(root, "seat", "to-elsewhere"))
    # A path through the link is outside once the link is followed.
    assert not session.inside_state_root(os.path.join(root, "seat", "to-elsewhere", "page.html"))


def test_a_dot_dot_after_a_link_is_read_both_ways(tmp_path):
    """The browser drops `link/..` before it looks at the file system. The kernel follows the link first."""
    root = _root()
    deep = tmp_path / "elsewhere" / "deep"
    deep.mkdir(parents=True)
    os.symlink(deep, os.path.join(root, "seat", "link"))
    # Read by the URL: root/seat/secrets.json, inside. Read by the kernel: elsewhere/secrets.json.
    assert session.inside_state_root(os.path.join(root, "seat", "link", "..", "secrets.json"))
    # The other way round: outside/link leads to root/seat, and `..` from there is the root.
    (tmp_path / "out").mkdir()
    os.symlink(os.path.join(root, "seat"), tmp_path / "out" / "link")
    assert session.inside_state_root(str(tmp_path / "out" / "link" / ".." / "seat"))


def test_a_path_with_a_nul_counts_as_inside_so_it_is_refused(tmp_path):
    _root()
    assert session.inside_state_root("/tmp/a\0b")


def test_the_root_may_not_exist_yet(tmp_path):
    root = session.state_root()
    assert not os.path.exists(root)
    assert session.inside_state_root(os.path.join(root, "seat", "secrets.json"))
    assert not session.inside_state_root(str(tmp_path / "page.html"))


def test_a_second_name_for_the_root_is_inside_by_device_and_inode(tmp_path, monkeypatch):
    """A case-insensitive file system, or a bind mount, lets the root be written another way. The
    path cannot be compared as text then, so a directory in the chain that is the same device and
    inode as the root counts. Here the second name is a path the system resolves to the root."""
    root = _root()
    alias = str(tmp_path / "SECOND-NAME")
    real_stat = os.stat
    monkeypatch.setattr(
        session.os, "stat", lambda path, *a, **k: real_stat(root if str(path) == alias else path, *a, **k)
    )
    assert session.inside_state_root(alias)
    assert session.inside_state_root(alias + "/seat/secrets.json")
    assert not session.inside_state_root(str(tmp_path / "SECOND-NAME-NOT"))


# --- the leftovers of a run that died ------------------------------------------------------------


def _scratch_dir_aged(seat, name, age):
    path = os.path.join(session.scratch_dir(seat), name)
    os.makedirs(path)
    with open(os.path.join(path, "code.js"), "w") as handle:
        handle.write("async (page) => 1")
    past = time.time() - age
    os.utime(path, (past, past))
    return path


def test_a_prune_removes_the_old_private_directories_of_a_secret_fill_and_of_a_code_file():
    old_fill = _scratch_dir_aged("seat", "fill-secret-abc", 3600)
    old_code = _scratch_dir_aged("seat", "run-code-abc", 3600)
    young_code = _scratch_dir_aged("seat", "run-code-new", 5)
    other = _scratch_dir_aged("seat", "unrelated-dir", 3600)
    session.prune_scratch("seat")
    assert not os.path.exists(old_fill) and not os.path.exists(old_code)
    assert os.path.isdir(young_code) and os.path.isdir(other)


def test_daemon_cleanup_uses_the_same_identity_as_driver_launch(monkeypatch):
    from types import SimpleNamespace
    import re
    calls = []
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    monkeypatch.setattr(session.subprocess, "run", lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(stdout="42\n"))
    assert session._daemon_pids("a_b") == ["42"]
    assert re.escape(session.plc.session_name("a_b")) in calls[0][-1]
    assert re.escape(session.plc.session_name("a-b")) not in calls[0][-1]


# --- the page list, over the driver's pipe ---------------------------------------------


def _answering(monkeypatch, answer):
    """The driver returns `answer(nonce, code)` for the page-list code; returns the code files it was handed."""
    import re as _re
    seen = []

    def run_code_file(seat, code, directory, timeout=60):
        seen.append(code)
        nonce = json.loads(_re.search(r"return \{nonce: (\"[0-9a-f]+\"), value\}", code).group(1))
        return answer(nonce, code)

    monkeypatch.setattr(session.plc, "run_code_file", run_code_file)
    return seen


def test_the_page_list_is_chromes_target_list_with_a_fresh_token(monkeypatch):
    seen = _answering(monkeypatch, lambda nonce, code: json.dumps(
        {"nonce": nonce, "value": [{"id": "9F3D1A5C", "url": "https://example.com/"}, {"id": "7478-1CA1", "url": "about:blank"}]}
    ) + "\n")
    assert session.pages("seat") == [("9F3D1A5C", "https://example.com/"), ("7478-1CA1", "about:blank")]
    session.pages("seat")
    assert "Target.getTargets" in seen[0] and "title" not in seen[0]
    assert seen[0] != seen[1]


@pytest.mark.parametrize("answer", [
    lambda nonce: json.dumps({"nonce": "0" * 32, "value": []}),
    lambda nonce: json.dumps({"value": []}),
    lambda nonce: json.dumps({"nonce": nonce, "value": [], "extra": 1}),
    lambda nonce: json.dumps({"nonce": nonce, "value": {"id": "A", "url": "x"}}),
    lambda nonce: json.dumps({"nonce": nonce, "value": ["A"]}),
    lambda nonce: json.dumps({"nonce": nonce, "value": [{"id": "A", "url": "x", "title": "t"}]}),
    lambda nonce: json.dumps({"nonce": nonce, "value": [{"id": "A/../B", "url": "x"}]}),
    lambda nonce: json.dumps({"nonce": nonce, "value": [{"id": "", "url": "x"}]}),
    lambda nonce: json.dumps({"nonce": nonce, "value": [{"id": "A", "url": 7}]}),
    lambda nonce: json.dumps({"nonce": nonce, "value": [{"id": 5, "url": "x"}]}),
    lambda nonce: "### Result\n- 0: (current) [t](file:///etc/passwd)",
    lambda nonce: "",
    lambda nonce: "not json",
], ids=["wrong-token", "no-token", "extra-key", "not-a-list", "not-an-object", "a-title", "id-path", "empty-id",
        "url-not-text", "id-not-text", "tab-list-text", "nothing", "not-json"])
def test_an_answer_that_is_not_exactly_the_one_asked_for_is_no_page_list(monkeypatch, answer):
    _answering(monkeypatch, lambda nonce, code: answer(nonce) + "\n")
    assert session.pages("seat") is None


def test_a_page_title_that_holds_a_fake_answer_cannot_pass_for_the_list(monkeypatch):
    """A hostile title or console line that prints a well-formed answer lacks this call's token."""
    forged = json.dumps({"nonce": "f" * 32, "value": [{"id": "X", "url": "file:///etc/passwd"}]})
    _answering(monkeypatch, lambda nonce, code: forged + "\n")
    assert session.pages("seat") is None


def test_a_page_list_the_driver_cannot_give_is_none(monkeypatch):
    def refused(*a, **k):
        raise session.plc.BrowserError("The browser 'seat' is not open")
    monkeypatch.setattr(session.plc, "run_code_file", refused)
    assert session.pages("seat") is None
    assert session.close_page("seat", "ABC") is False


def test_close_page_sends_the_id_as_data_and_needs_chromes_yes(monkeypatch):
    seen = _answering(monkeypatch, lambda nonce, code: json.dumps({"nonce": nonce, "value": True}) + "\n")
    assert session.close_page("seat", "9F3D-1A5C") is True
    assert "Target.closeTarget" in seen[0] and "JSON.parse(" in seen[0]
    _answering(monkeypatch, lambda nonce, code: json.dumps({"nonce": nonce, "value": "yes"}) + "\n")
    assert session.close_page("seat", "9F3D-1A5C") is False


@pytest.mark.parametrize("page_id", ["", "a'b", "x/../y", "A" * 65, None])
def test_close_page_refuses_an_id_that_is_not_a_target_id_and_calls_nothing(monkeypatch, page_id):
    seen = _answering(monkeypatch, lambda nonce, code: "")
    assert session.close_page("seat", page_id) is False
    assert seen == []


def test_windows_starts_no_driver_and_no_browser(monkeypatch, tmp_path):
    """The seat opens on Linux and macOS only: on Windows every opener stops before any process starts."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    monkeypatch.setattr(session.platform, "system", lambda: "Windows")
    started = []
    monkeypatch.setattr(session.plc, "is_open", lambda seat: False)
    monkeypatch.setattr(session.plc, "run", lambda *a, **k: started.append(("run", a)))
    monkeypatch.setattr(session.plc, "run_code", lambda *a, **k: started.append(("run_code", a)))
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: started.append(("subprocess", a)))
    for opener in (lambda: session.open_if_down("seat", None), lambda: session.ensure_running("seat"),
                   lambda: session._start_chrome("seat"), lambda: session._launch_chrome("seat")):
        with pytest.raises(session.plc.BrowserError, match="Linux and macOS only"):
            opener()
    assert started == []
    assert not os.path.exists(session.launch_config_path("seat"))
    assert session.owned_by_driver("seat") is False


# --- CDP attach and custom PROFILE directory ---------------------------------------------


def test_cdp_resolves_devtools_active_port_with_full_websocket_path(monkeypatch, tmp_path):
    """Chrome's DevToolsActivePort file has the port on line 1 and the browser WebSocket path on line 2."""
    profile = tmp_path / "chrome-profile"
    profile.mkdir()
    port_file = profile / "DevToolsActivePort"
    port_file.write_text("41892\n/devtools/browser/75eae945-a64f-4484-aa48-8da603b6b33c\n")
    assert session._resolve_cdp_endpoint(str(profile)) == (
        "ws://127.0.0.1:41892/devtools/browser/75eae945-a64f-4484-aa48-8da603b6b33c"
    )
    assert session._resolve_cdp_endpoint(str(port_file)) == (
        "ws://127.0.0.1:41892/devtools/browser/75eae945-a64f-4484-aa48-8da603b6b33c"
    )
    assert session._resolve_cdp_endpoint("9222") == "http://127.0.0.1:9222"
    assert session._resolve_cdp_endpoint("ws://127.0.0.1:9222/devtools/browser/abc") == (
        "ws://127.0.0.1:9222/devtools/browser/abc"
    )
    assert session._resolve_cdp_endpoint("http://127.0.0.1:9222") == "http://127.0.0.1:9222"


def test_cdp_channel_resolves_default_chrome_user_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(session.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    chrome_dir = tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
    chrome_dir.mkdir(parents=True)
    (chrome_dir / "DevToolsActivePort").write_text("51234\n/devtools/browser/mac-uuid\n")
    assert session._resolve_cdp_endpoint("chrome") == "ws://127.0.0.1:51234/devtools/browser/mac-uuid"


def test_cdp_missing_or_invalid_devtools_active_port_raises_browser_error(tmp_path):
    with pytest.raises(session.plc.BrowserError, match="chrome://inspect/#remote-debugging"):
        session._resolve_cdp_endpoint(str(tmp_path / "missing"))
    bad = tmp_path / "DevToolsActivePort"
    bad.write_text("not-a-port\n/devtools/browser/x\n")
    with pytest.raises(session.plc.BrowserError, match="invalid port"):
        session._resolve_cdp_endpoint(str(bad))
    bad.write_text("9222\nhttp://evil.example/\n")
    with pytest.raises(session.plc.BrowserError, match="invalid WebSocket path"):
        session._resolve_cdp_endpoint(str(bad))
    with pytest.raises(session.plc.BrowserError, match="1..65535"):
        session._resolve_cdp_endpoint("70000")


def test_cdp_attach_runs_attach_without_launching_chrome_or_touching_preferences(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    profile = tmp_path / "user-chrome"
    (profile / "Default").mkdir(parents=True)
    prefs_file = profile / "Default" / "Preferences"
    prefs_file.write_text('{"credentials_enable_service":true}')
    (profile / "DevToolsActivePort").write_text("44111\n/devtools/browser/u-1\n")
    monkeypatch.setenv("AR3_BROWSER_CDP", str(profile))
    monkeypatch.setattr(session, "chrome_path", lambda: None)
    monkeypatch.setattr(session.plc, "is_open", lambda seat: False)
    monkeypatch.setattr(session, "_daemon_pids", lambda seat: ["88"])
    calls = []
    monkeypatch.setattr(session.plc, "run", lambda seat, *a, **k: calls.append((seat, a)))
    monkeypatch.setattr(session.plc, "run_code", lambda seat, body, **k: calls.append(("code", body)))
    session.open_if_down("seat", None)
    assert calls[0] == ("seat", ("attach", "--cdp=ws://127.0.0.1:44111/devtools/browser/u-1"))
    assert json.loads(prefs_file.read_text()) == {"credentials_enable_service": True}
    assert not os.path.exists(session.launch_config_path("seat"))


def test_cdp_close_detaches_without_browser_close_or_signaling_chrome(monkeypatch, tmp_path):
    """Closing an attached CDP seat stops only the seat's driver daemon and never quits or signals Chrome."""
    import signal
    real_terminate = session._terminate_profile
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CDP", "ws://127.0.0.1:9222/devtools/browser/abc")
    order = _fake_close(monkeypatch, per_step_seconds=0, budget=15)
    session.close_browser("seat")
    assert "browser-close" not in [n for n, _ in order]
    assert [n for n, _ in order] == ["dialog-dismiss", "state-save", "video-stop", "close", "terminate"]

    killed = []
    monkeypatch.setattr(session, "_terminate_profile", real_terminate)
    monkeypatch.setattr(session, "_main_pids", lambda seat: pytest.fail("_main_pids called in CDP mode"))
    monkeypatch.setattr(session, "_profile_pids", lambda seat: pytest.fail("_profile_pids called in CDP mode"))
    monkeypatch.setattr(session, "_daemon_pids", lambda seat: ["99"])
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(session, "_signal", lambda pid, sig: killed.append((pid, sig)))
    session._terminate_profile("seat")
    assert ("99", signal.SIGTERM) in killed


def test_custom_profile_launches_with_user_data_dir_and_preserves_preferences(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    ext = tmp_path / "external-profile"
    (ext / "Default").mkdir(parents=True)
    prefs = ext / "Default" / "Preferences"
    prefs.write_text('{"credentials_enable_service":true}')
    monkeypatch.setenv("AR3_BROWSER_PROFILE", str(ext))
    launched = []
    monkeypatch.setattr(session.plc, "run", _launched_args(launched))
    session._launch_chrome("seat")
    assert f"--user-data-dir={ext}" in launched[0]
    assert json.loads(prefs.read_text()) == {"credentials_enable_service": True}
    assert session.inside_state_root(str(ext / "Default" / "Cookies"))


def test_custom_profile_refuses_when_already_locked_by_a_running_chrome(monkeypatch, tmp_path):
    """An external profile already held by a running Chrome is refused rather than killed."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    ext = tmp_path / "external-profile"
    ext.mkdir()
    os.symlink(f"host-{os.getpid()}", ext / "SingletonLock")
    monkeypatch.setenv("AR3_BROWSER_PROFILE", str(ext))
    monkeypatch.setattr(session.plc, "is_open", lambda seat: False)
    monkeypatch.setattr(session, "_profile_pids", lambda seat: [])
    monkeypatch.setattr(session, "_terminate_profile", lambda seat: pytest.fail("terminated running user Chrome"))
    with pytest.raises(session.plc.BrowserError, match="already in use by a running Chrome"):
        session.open_if_down("seat", None)


def test_setting_both_cdp_and_profile_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CDP", "chrome")
    monkeypatch.setenv("AR3_BROWSER_PROFILE", str(tmp_path / "prof"))
    monkeypatch.setattr(session.plc, "is_open", lambda seat: False)
    with pytest.raises(session.plc.BrowserError, match="cannot set both CDP and PROFILE"):
        session.open_if_down("seat", None)


def test_external_profile_close_idle_and_failed_launch_never_signal_unowned_chrome(monkeypatch, tmp_path):
    """If an external profile is held by the user's own Chrome, refusing open and
    then running close, idle, or launch-failure cleanup must never signal it."""
    from browser import cli
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    ext = tmp_path / "external-profile"
    ext.mkdir()
    monkeypatch.setenv("AR3_BROWSER_PROFILE", str(ext))
    monkeypatch.setattr(session.plc, "is_open", lambda seat: False)
    monkeypatch.setattr(session, "_main_pids", lambda seat: ["4242"])
    monkeypatch.setattr(session, "_profile_pids", lambda seat: ["4242", "4243"])
    monkeypatch.setattr(session, "_daemon_pids", lambda seat: [])
    monkeypatch.setattr(session, "_parent_pid", lambda pid: "1")
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    killed = []
    monkeypatch.setattr(session, "_signal", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(session.plc.BrowserError, match="already in use by a running Chrome"):
        session.open_if_down("seat", None)
    assert killed == []
    assert session.chrome_running("seat") is False
    assert cli._idle("seat", 1.0) == 0
    assert killed == []

    session.close_browser("seat")
    assert killed == []

    # Even if a race lets _launch_chrome run and fail while PID 4242 holds the profile,
    # _terminate_profile must not signal the unowned PID 4242.
    monkeypatch.setattr(session, "_profile_pids", lambda seat: [])
    monkeypatch.setattr(session, "_singleton_lock_alive", lambda path: False)

    def fail_after_user_chrome_appears(seat):
        monkeypatch.setattr(session, "_profile_pids", lambda s: ["4242", "4243"])
        raise session.plc.BrowserError("launch failed")

    monkeypatch.setattr(session, "_launch_chrome", fail_after_user_chrome_appears)
    with pytest.raises(session.plc.BrowserError, match="launch failed"):
        session._start_chrome("seat")
    assert killed == []


def test_external_profile_signals_seat_owned_chrome_even_when_closed_without_profile_flag(monkeypatch, tmp_path):
    """When the seat itself launched Chrome on an external profile, session.json
    binds that profile and PID start token so close (with or without --profile)
    terminates the seat-owned Chrome and nothing else."""
    import signal
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    ext = tmp_path / "external-profile"
    ext.mkdir()
    monkeypatch.setenv("AR3_BROWSER_PROFILE", str(ext))
    is_open = {"v": False}
    monkeypatch.setattr(session.plc, "is_open", lambda seat: is_open["v"])
    pids = {"main": [], "profile": [], "daemon": []}
    monkeypatch.setattr(session, "_main_pids", lambda seat: list(pids["main"]))
    monkeypatch.setattr(session, "_profile_pids", lambda seat: list(pids["profile"]))
    monkeypatch.setattr(session, "_daemon_pids", lambda seat: list(pids["daemon"]))
    monkeypatch.setattr(session, "_parent_pid", lambda pid: "40" if pid == "5000" else "1")
    tokens = {"5000": "tok-5000"}
    monkeypatch.setattr(session, "_pid_token", lambda pid: tokens.get(str(pid)))
    monkeypatch.setattr(session, "check_launch_argv", lambda seat: [])
    monkeypatch.setattr(session.time, "sleep", lambda s: None)

    def fake_run(seat, *args, **kw):
        if args and args[0] == "open":
            pids["main"] = ["5000"]
            pids["profile"] = ["5000", "5001"]
            pids["daemon"] = ["40"]
            is_open["v"] = True
        elif args and args[0] == "close":
            pids["daemon"] = []
            is_open["v"] = False
        return ""

    monkeypatch.setattr(session.plc, "run", fake_run)
    monkeypatch.setattr(session.plc, "run_code", lambda *a, **k: "")
    session.open_if_down("seat", None)
    assert os.path.exists(session.session_binding_path("seat"))

    # Clear AR3_BROWSER_PROFILE: bound profile is still protected and used by close_browser.
    monkeypatch.delenv("AR3_BROWSER_PROFILE", raising=False)
    assert session.profile_dir("seat") == str(ext)
    assert session.inside_state_root(str(ext / "Default" / "Cookies"))

    killed = []
    monkeypatch.setattr(session, "_signal", lambda pid, sig: killed.append((pid, sig)))
    session.close_browser("seat")
    assert ("5000", signal.SIGTERM) in killed
    assert ("5000", signal.SIGKILL) in killed
    assert not os.path.exists(session.session_binding_path("seat"))

    # If a stale session.json names PID 5000 but its start token changed (PID reuse), it is not signaled.
    session._write_session_binding("seat")
    with open(session.session_binding_path("seat"), "w", encoding="utf-8") as handle:
        json.dump({"mode": "profile", "profile": str(ext), "owned_pids": {"5000": "old-token"}}, handle)
    tokens["5000"] = "new-user-token"
    killed.clear()
    session.close_browser("seat")
    assert killed == []

    # Null or empty tokens in session.json or from _pid_token never establish saved ownership or permit SIGKILL.
    for bad_saved in (None, ""):
        tokens["5000"] = None
        pids["main"] = ["5000"]
        pids["profile"] = ["5000"]
        pids["daemon"] = []
        with open(session.session_binding_path("seat"), "w", encoding="utf-8") as handle:
            json.dump({"mode": "profile", "profile": str(ext), "owned_pids": {"5000": bad_saved}}, handle)
        assert session._owned_main_pids("seat") == []
        killed.clear()
        session.close_browser("seat")
        assert killed == []

    # If owned PID 5000 exits on SIGTERM and a new unowned Chrome (6000/6001) acquires
    # the profile during cleanup, escalation must never SIGKILL 6000/6001.
    pids["main"] = ["5000"]
    pids["profile"] = ["5000"]
    tokens["5000"] = "orig-token"
    tokens["6000"] = "user-6000"
    tokens["6001"] = "user-6001"
    with open(session.session_binding_path("seat"), "w", encoding="utf-8") as handle:
        json.dump({"mode": "profile", "profile": str(ext), "owned_pids": {"5000": "orig-token"}}, handle)

    def sigterm_replaces_with_user_chrome(pid, sig):
        killed.append((pid, sig))
        if pid == "5000" and sig == signal.SIGTERM:
            pids["main"] = ["6000"]
            pids["profile"] = ["6000", "6001"]

    killed.clear()
    monkeypatch.setattr(session, "_signal", sigterm_replaces_with_user_chrome)
    session.close_browser("seat")
    assert killed == [("5000", signal.SIGTERM)]


def test_cdp_binding_persists_so_close_without_cdp_flag_detaches_without_browser_close(monkeypatch, tmp_path):
    """Opening with --cdp and then running close without --cdp must still detach
    without sending Browser.close or signaling the user's Chrome."""
    import signal
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    profile = tmp_path / "user-chrome"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("44111\n/devtools/browser/u-1\n")
    monkeypatch.setenv("AR3_BROWSER_CDP", str(profile))
    is_open = {"v": False}
    monkeypatch.setattr(session.plc, "is_open", lambda seat: is_open["v"])
    monkeypatch.setattr(session, "_daemon_pids", lambda seat: ["88"])
    calls = []

    def fake_run(seat, *args, **kw):
        calls.append(("run", args[0]))
        if args[0] == "attach":
            is_open["v"] = True
        return ""

    def fake_run_code(seat, body, **kw):
        if "Browser.close" in body:
            calls.append(("browser-close", body))
        else:
            calls.append(("code", body))
        return ""

    monkeypatch.setattr(session.plc, "run", fake_run)
    monkeypatch.setattr(session.plc, "run_code", fake_run_code)
    session.open_if_down("seat", None)
    assert os.path.exists(session.session_binding_path("seat"))

    # Drop AR3_BROWSER_CDP (simulating `browser --seat seat close` without `--cdp`).
    monkeypatch.delenv("AR3_BROWSER_CDP", raising=False)
    assert session.inside_state_root(str(profile / "Default" / "Login Data"))

    killed = []
    monkeypatch.setattr(session, "_main_pids", lambda seat: pytest.fail("_main_pids called on CDP close"))
    monkeypatch.setattr(session, "_profile_pids", lambda seat: pytest.fail("_profile_pids called on CDP close"))
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    monkeypatch.setattr(session, "_signal", lambda pid, sig: killed.append((pid, sig)))
    calls.clear()
    session.close_browser("seat")
    assert ("browser-close",) not in [c[:1] for c in calls]
    assert ("88", signal.SIGTERM) in killed
    assert not os.path.exists(session.session_binding_path("seat"))


def test_session_binding_allows_same_mode_reuse_and_refuses_flag_or_var_mismatches(monkeypatch, tmp_path):
    """A live session can be reused with matching settings, but changing or
    dropping --cdp / --profile while the seat is open is refused without touching
    or snapshotting the live browser."""
    from browser import commands
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    profile = tmp_path / "user-chrome"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("44111\n/devtools/browser/u-1\n")
    monkeypatch.setenv("AR3_BROWSER_CDP", str(profile))
    is_open = {"v": False}
    monkeypatch.setattr(session.plc, "is_open", lambda seat: is_open["v"])
    monkeypatch.setattr(session, "_daemon_pids", lambda seat: ["88"] if is_open["v"] else [])
    monkeypatch.setattr(
        session.plc, "run",
        lambda seat, *args, **kw: is_open.__setitem__("v", True) or "" if args and args[0] == "attach" else "",
    )
    monkeypatch.setattr(
        session.plc, "run_code",
        lambda seat, body, **kw: '### Result\n"{\\"url\\":\\"https://example.com/\\",\\"visible\\":\\"visible\\",\\"window\\":1,\\"state\\":\\"normal\\"}"',
    )
    monkeypatch.setattr(session, "current_url", lambda seat: "https://example.com/")
    session.open_if_down("seat", None)

    # Reusing with the same CDP target succeeds.
    session.open_if_down("seat", None)
    session.ensure_running("seat")
    assert session.status("seat") == "seat: attached at https://example.com/"

    # Dropping --cdp, changing --cdp, or switching to --profile while open is refused.
    monkeypatch.delenv("AR3_BROWSER_CDP", raising=False)
    for call in (
        lambda: session.open_if_down("seat", None),
        lambda: session.ensure_running("seat"),
        lambda: session.status("seat"),
    ):
        with pytest.raises(session.plc.BrowserError, match="already open in CDP mode"):
            call()

    monkeypatch.setenv("AR3_BROWSER_CDP", "http://127.0.0.1:9333")
    with pytest.raises(session.plc.BrowserError, match="already open in CDP mode"):
        session.ensure_running("seat")

    monkeypatch.delenv("AR3_BROWSER_CDP", raising=False)
    monkeypatch.setenv("AR3_BROWSER_PROFILE", str(tmp_path / "other-prof"))
    monkeypatch.setattr(session, "snapshot", lambda seat: pytest.fail("snapshot called on mismatched session"))
    monkeypatch.setattr(commands, "_write_snapshot", lambda *a, **k: pytest.fail("_write_snapshot called on mismatched session"))
    monkeypatch.setattr(commands, "refuse_a_local_page", lambda *a, **k: pytest.fail("refuse_a_local_page called on mismatched session"))
    for bad_script in (
        "go https://example.com/",
        "go <<EOF\nhttps://example.com/",
        'click "unclosed',
    ):
        run = commands.run_script("seat", bad_script)
        assert not run.ok
        assert "already open in CDP mode" in run.error
        assert run.files == []

    # Setting both CDP and PROFILE is also rejected without touching or snapshotting the live session.
    monkeypatch.setenv("AR3_BROWSER_CDP", str(profile))
    monkeypatch.setenv("AR3_BROWSER_PROFILE", str(tmp_path / "other-prof"))
    for bad_script in (
        "go https://example.com/",
        "go <<EOF\nhttps://example.com/",
        'click "unclosed',
    ):
        run = commands.run_script("seat", bad_script)
        assert not run.ok
        assert "cannot set both CDP and PROFILE" in run.error
        assert run.files == []


def test_main_pids_uses_lf_on_darwin_and_falls_back_to_process_argv_when_cmdline_omitted(monkeypatch, tmp_path):
    """macOS BSD `pgrep` uses `-lf` (not `-af`) to print the full command line;
    if `pgrep` outputs only PIDs or short names without `--user-data-dir=`,
    `_main_pids` falls back to `_process_argv` and still filters out `--type=`
    helpers."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(session.platform, "system", lambda: "Darwin")
    profile = session.profile_dir("atb")
    seen_flags = []

    def fake_pgrep_lf(argv, **kwargs):
        seen_flags.append(argv[1])
        stdout = (
            f"101 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir={profile} about:blank\n"
            f"102 /Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Helper.app --type=gpu-process --user-data-dir={profile}\n"
            f"103 /Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Helper.app --type=renderer --user-data-dir={profile}\n"
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(session.subprocess, "run", fake_pgrep_lf)
    assert session._main_pids("atb") == ["101"]
    assert seen_flags == ["-lf"]

    # Fallback when pgrep prints bare PIDs (e.g., without command lines) or a process has exited.
    monkeypatch.setattr(
        session.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="101\n102\n104\n", stderr=""),
    )
    argv_map = {
        "101": ["/Applications/Google Chrome", f"--user-data-dir={profile}", "about:blank"],
        "102": ["/Applications/Google Chrome Helper", "--type=renderer", f"--user-data-dir={profile}"],
        "104": None,
    }
    monkeypatch.setattr(session, "_process_argv", lambda pid: argv_map.get(pid))
    assert session._main_pids("atb") == ["101"]
