import http.server
import json
import os
import socket
import shutil
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
