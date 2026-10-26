import http.server
import json
import os
import socket
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


def _record_port(seat, port):
    os.makedirs(session.profile_dir(seat), exist_ok=True)
    with open(session._port_file(seat), "w") as handle:
        handle.write(f"{port}\n/devtools/browser/x\n")


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def foreign_listener():
    """A real local listener that answers /json/version, owned by no seat."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _drive_open(monkeypatch, seat, main_pids):
    """Run open_if_down with every process call faked; return the recorded calls."""
    calls = []
    monkeypatch.setattr(session.plc, "is_open", lambda s: False)
    monkeypatch.setattr(session, "_main_pids", lambda s: main_pids)
    monkeypatch.setattr(session, "_profile_pids", lambda s: main_pids)
    monkeypatch.setattr(session, "_listening_endpoints", lambda port: {"127.0.0.1": set(main_pids)})
    monkeypatch.setattr(session, "_launch_chrome", lambda s: calls.append("launch"))
    monkeypatch.setattr(session, "_terminate_profile", lambda s: calls.append("terminate"))
    monkeypatch.setattr(session.plc, "run", lambda s, *a, **k: calls.append(a[0] + ":" + a[1]))
    monkeypatch.setattr(session.plc, "run_code", lambda *a, **k: "")
    monkeypatch.setattr(session, "CHROME_STARTUP_WAIT", 1)
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    return calls


def test_cdp_port_comes_from_the_profile_not_a_hash(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("AR3_BROWSER_PORT", raising=False)
    assert session.cdp_port("garmin") is None
    _record_port("garmin", 41234)
    assert session.cdp_port("garmin") == 41234


def test_the_override_alone_is_no_port_to_attach_to(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_PORT", "9500")
    monkeypatch.setattr(session, "_profile_pids", lambda s: ["7"])
    monkeypatch.setattr(session, "_listening_endpoints", lambda port: {})
    assert session.cdp_port("garmin") is None


def test_the_override_held_by_a_profile_process_is_the_port(monkeypatch, tmp_path):
    """Fixed-port Chrome writes no DevToolsActivePort (Artemis, round 7): the
    seat owns the port when one of its own processes holds the listener."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_PORT", "9500")
    monkeypatch.setattr(session, "_profile_pids", lambda s: ["7", "8"])
    monkeypatch.setattr(
        session, "_listening_endpoints", lambda port: {"127.0.0.1": {"8"}} if port == 9500 else {}
    )
    assert session.cdp_port("garmin") == 9500


def test_the_override_held_by_a_foreign_process_is_no_port(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_PORT", "9500")
    monkeypatch.setattr(session, "_profile_pids", lambda s: ["7"])
    monkeypatch.setattr(session, "_listening_endpoints", lambda port: {"127.0.0.1": {"999"}})
    assert session.cdp_port("garmin") is None


def test_listening_endpoints_name_the_real_socket_holder_per_host(foreign_listener):
    """Positive control for the proof itself: this test process holds the
    IPv4 listener, so lsof/ss must name it at 127.0.0.1 and nowhere else."""
    me = str(os.getpid())
    assert session._listening_endpoints(foreign_listener) == {"127.0.0.1": {me}}
    with socket.socket(socket.AF_INET6) as six:
        six.bind(("::1", foreign_listener))
        six.listen()
        endpoints = session._listening_endpoints(foreign_listener)
        assert endpoints["::1"] == {me}
        assert endpoints["127.0.0.1"] == {me}


def test_loopback_hosts_of_listener_addresses():
    assert session._loopback_hosts("127.0.0.1:52729") == ("127.0.0.1",)
    assert session._loopback_hosts("[::1]:52729") == ("::1",)
    assert session._loopback_hosts("*:52729") == ("127.0.0.1", "::1")
    assert session._loopback_hosts("0.0.0.0:52729") == ("127.0.0.1", "::1")
    assert session._loopback_hosts("10.0.0.5:52729") == ()


def test_attach_goes_only_to_the_endpoint_our_own_chrome_holds(monkeypatch, tmp_path):
    """Artemis's round-8 probe: two fixed-port Chromes on one port number, A on
    127.0.0.1 (foreign pid), B on ::1 (ours). B must attach to [::1], never
    to A."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_PORT", "52729")
    calls = _drive_open(monkeypatch, "seat", main_pids=["7"])
    monkeypatch.setattr(
        session, "_listening_endpoints",
        lambda port: {"127.0.0.1": {"999"}, "::1": {"7"}},
    )
    asked = []

    def fake_urlopen(url, timeout=None):
        asked.append(url)
        return object()

    monkeypatch.setattr(session.urllib.request, "urlopen", fake_urlopen)
    session.open_if_down("seat", None)
    assert asked == ["http://[::1]:52729/json/version"]
    assert calls == ["attach:--cdp=http://[::1]:52729"]


def test_a_port_held_only_by_a_foreign_pid_is_not_ours_even_when_it_answers(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_PORT", "52729")
    calls = _drive_open(monkeypatch, "seat", main_pids=["7"])
    monkeypatch.setattr(session, "_listening_endpoints", lambda port: {"127.0.0.1": {"999"}})
    asked = []
    monkeypatch.setattr(session.urllib.request, "urlopen", lambda url, timeout=None: asked.append(url))
    with pytest.raises(session.plc.BrowserError, match="did not come up"):
        session.open_if_down("seat", None)
    assert asked == []
    assert not any(c.startswith("attach") for c in calls)


def test_the_override_matching_the_record_is_the_port(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_PORT", "9500")
    _record_port("garmin", 9500)
    assert session.cdp_port("garmin") == 9500


def test_an_override_that_differs_from_the_record_is_a_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_PORT", "9500")
    _record_port("garmin", 41234)
    with pytest.raises(session.plc.BrowserError, match="41234, not the requested 9500"):
        session.cdp_port("garmin")


def test_two_running_seats_never_share_a_port(monkeypatch, tmp_path, foreign_listener):
    """review-5 and review-12 hashed to one port; now each reads its own profile."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("AR3_BROWSER_PORT", raising=False)
    monkeypatch.setattr(session, "_main_pids", lambda s: ["1"])
    monkeypatch.setattr(session, "_profile_pids", lambda s: ["1"])
    monkeypatch.setattr(session, "_listening_endpoints", lambda port: {"127.0.0.1": {"1"}})
    _record_port("review-5", foreign_listener)
    _record_port("review-12", foreign_listener + 1)
    assert session.cdp_port("review-5") != session.cdp_port("review-12")
    assert session.cdp_base("review-5").endswith(f":{foreign_listener}")
    assert session.cdp_base("review-12") is None  # nothing answers there


def test_a_listener_with_no_chrome_of_ours_is_never_attached(
    monkeypatch, tmp_path, foreign_listener
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("AR3_BROWSER_PORT", raising=False)
    _record_port("seat", foreign_listener)
    calls = _drive_open(monkeypatch, "seat", main_pids=[])
    with pytest.raises(session.plc.BrowserError):
        session.open_if_down("seat", None)
    assert calls[0] == "launch"
    assert not any(c.startswith("attach") for c in calls)


def test_a_seat_with_a_live_chrome_of_its_own_attaches_to_its_recorded_port(
    monkeypatch, tmp_path, foreign_listener
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("AR3_BROWSER_PORT", raising=False)
    _record_port("seat", foreign_listener)
    calls = _drive_open(monkeypatch, "seat", main_pids=["7"])
    session.open_if_down("seat", None)
    assert calls == [f"attach:--cdp=http://127.0.0.1:{foreign_listener}"]


def test_the_override_fails_closed_on_a_port_in_use(monkeypatch, tmp_path, foreign_listener):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_PORT", str(foreign_listener))
    calls = _drive_open(monkeypatch, "seat", main_pids=[])
    with pytest.raises(session.plc.BrowserError, match="in use"):
        session.open_if_down("seat", None)
    assert calls == []


def test_a_live_seat_is_never_attached_through_a_foreign_override(
    monkeypatch, tmp_path, foreign_listener
):
    """Artemis's round-6 probe: seat A runs on its own port, the override names
    another live listener (seat B). open_if_down must not attach to B."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    _record_port("seat", foreign_listener)
    monkeypatch.setenv("AR3_BROWSER_PORT", str(foreign_listener + 1))
    calls = _drive_open(monkeypatch, "seat", main_pids=["7"])
    with pytest.raises(session.plc.BrowserError, match="not the requested"):
        session.open_if_down("seat", None)
    assert calls == []


def test_status_names_a_port_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_PORT", "9500")
    _record_port("seat", 41234)
    monkeypatch.setattr(session.plc, "is_open", lambda s: False)
    monkeypatch.setattr(session, "_main_pids", lambda s: ["7"])
    assert "not the requested 9500" in session.status("seat")


def test_a_stale_port_record_is_removed_before_launch(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    monkeypatch.delenv("AR3_BROWSER_PORT", raising=False)
    _record_port("seat", 9999)
    launched = []
    monkeypatch.setattr(session, "spawn", lambda cmd, **k: launched.append(cmd))
    session._launch_chrome("seat")
    assert not os.path.exists(session._port_file("seat"))
    assert "--remote-debugging-port=0" in launched[0]


def test_the_override_is_the_fixed_launch_port(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    monkeypatch.setenv("AR3_BROWSER_PORT", "9500")
    launched = []
    monkeypatch.setattr(session, "spawn", lambda cmd, **k: launched.append(cmd))
    session._launch_chrome("seat")
    assert "--remote-debugging-port=9500" in launched[0]


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
    monkeypatch.setattr(session, "cdp_base", lambda seat: None)
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
    monkeypatch.setattr(session, "cdp_base", lambda seat: None)

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


def test_open_cleans_up_chrome_when_attach_fails(monkeypatch, tmp_path):
    """A failed attach must not leave a browser nobody can drive running."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(session.plc, "is_open", lambda seat: False)
    monkeypatch.setattr(session, "cdp_base", lambda seat: "http://127.0.0.1:9222")
    def failing_run(*a, **k):
        raise session.plc.BrowserError("attach refused")
    monkeypatch.setattr(session.plc, "run", failing_run)
    reaped = []
    monkeypatch.setattr(session, "_terminate_profile", lambda seat: reaped.append(seat))
    try:
        session.open_if_down("seat", None)
    except session.plc.BrowserError:
        pass
    assert reaped == ["seat"]


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
    monkeypatch.setattr(session, "spawn", lambda cmd, **k: launched.append(cmd))
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


def test_open_routes_downloads_right_after_attaching(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(session.plc, "is_open", lambda seat: False)
    monkeypatch.setattr(session, "cdp_base", lambda seat: "http://127.0.0.1:9222")
    steps = []
    monkeypatch.setattr(session.plc, "run", lambda seat, *a, **k: steps.append(a[0]))
    monkeypatch.setattr(
        session.plc, "run_code",
        lambda seat, body, timeout=60: steps.append(
            "route" if "setDownloadBehavior" in body else "code"
        ),
    )
    session.open_if_down("seat", None)
    assert steps == ["attach", "route"]


def test_chrome_launches_headless_on_linux_without_a_display(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    launched = []
    monkeypatch.setattr(session, "spawn", lambda cmd, **k: launched.append(cmd))
    session._launch_chrome("seat")
    assert "--headless=new" in launched[0]


def test_chrome_launches_headed_when_a_display_is_set_or_off_linux(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("AR3_BROWSER_CHROME", "/bin/chrome")
    launched = []
    monkeypatch.setattr(session, "spawn", lambda cmd, **k: launched.append(cmd))
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    monkeypatch.setenv("DISPLAY", ":0")
    session._launch_chrome("seat")
    monkeypatch.setattr(session.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("DISPLAY")
    session._launch_chrome("seat")
    assert not any("--headless=new" in cmd for cmd in launched)


def test_chrome_starts_in_its_own_session_with_quiet_output(monkeypatch, tmp_path):
    """spawn leads a new session; the browser holds no pipe of the wake."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    out = tmp_path / "out"
    proc = session._spawn_quiet(
        ["sh", "-c", "echo noise; echo err >&2; ps -o sid= -p $$ > " + str(out)],
        cwd=str(tmp_path),
    )
    proc.wait()
    assert int(out.read_text()) == proc.pid


def test_ensure_running_with_a_running_chrome_ignores_the_epoch(monkeypatch):
    seat = FakeSeat(monkeypatch, "https://example.com/")
    monkeypatch.setattr(session, "stop_epoch", lambda s: pytest.fail("epoch read"))
    session.ensure_running("seat", script_epoch="old")


def _closed_chrome(monkeypatch):
    launched = []
    monkeypatch.setattr(session.plc, "is_open", lambda s: False)
    monkeypatch.setattr(session, "_start_chrome", lambda s: launched.append(s))
    monkeypatch.setattr(session, "_attach", lambda s, b: None)
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
    monkeypatch.setattr(session, "_attach", lambda s, b: events.append("attach"))
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
    monkeypatch.setattr(session, "spawn", lambda cmd, **k: launched.append(cmd))
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

    def spawn(cmd, **kwargs):
        seen.append(_preferences())

    monkeypatch.setattr(session, "spawn", spawn)
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
    monkeypatch.setattr(session, "spawn", lambda cmd, **k: None)
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
