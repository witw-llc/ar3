"""Seat-keyed browser session.

One seat, one Chrome profile, one CDP port (chosen by Chrome, read from the profile), one playwright-cli session. Chrome
is launched directly rather than by playwright so it carries no automation
flags, and playwright-cli attaches afterwards over CDP. The profile directory
is what makes a seat persistent: cookies survive a restart because Chrome
stores them the way it does for a person.

Derived from b3t's session layer (neilobremski/bin), generalised so the seat
name selects the profile.
"""
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

from ar3.proc import spawn

from . import plc

PORT_FILE = "DevToolsActivePort"
CHROME_STARTUP_WAIT = 60  # x 0.2s
# a8s kills a stop hook after 30 s (STOP_HOOK_SECONDS). The whole graceful part
# of close_browser shares this one budget, and the forced teardown needs about
# 5 s after it (SIGTERM wait), so the force step always runs before 30 s.
GRACEFUL_CLOSE_SECONDS = 15


def state_root():
    """Where seats live. Separate from the install dir, which updates replace."""
    return os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
        "ar3",
        "browser",
    )


def seat_home(seat):
    return os.path.join(state_root(), seat)


def profile_dir(seat):
    return os.path.join(seat_home(seat), "chrome")


def artifacts_dir(seat):
    return os.path.join(seat_home(seat), "artifacts")


def downloads_dir(seat):
    """Where the seat's Chrome saves downloads — never the profile default,
    which is a person's own Downloads folder."""
    path = os.path.join(seat_home(seat), "downloads")
    os.makedirs(path, exist_ok=True)
    return path


def scratch_dir(seat):
    """The seat's working directory — playwright-cli drops per-command scratch
    (page-*.yml, console-*.log) under a `.playwright-cli` dir beneath its cwd,
    so the CLI runs from here instead of the caller's directory."""
    path = os.path.join(seat_home(seat), "scratch")
    os.makedirs(path, exist_ok=True)
    return path


def prune_scratch(seat, max_age=86400):
    """Bound the scratch playwright-cli leaves behind — it writes a file per
    command, so an unpruned dir grows without limit on a busy seat."""
    directory = os.path.join(scratch_dir(seat), ".playwright-cli")
    try:
        names = os.listdir(directory)
    except OSError:
        return
    cutoff = time.time() - max_age
    for name in names:
        path = os.path.join(directory, name)
        try:
            stale = os.path.getmtime(path) < cutoff
            if os.path.isfile(path) and not os.path.islink(path) and stale:
                os.remove(path)
        except OSError:
            pass


def video_state_path(seat):
    return os.path.join(seat_home(seat), "video.json")


def video_state(seat):
    """The active recording's {path, started_at, chapters}, or None."""
    try:
        with open(video_state_path(seat)) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def save_video_state(seat, state):
    os.makedirs(seat_home(seat), exist_ok=True)
    path = video_state_path(seat)
    with open(path + ".tmp", "w") as handle:
        json.dump(state, handle)
    os.replace(path + ".tmp", path)


def clear_video_state(seat):
    try:
        os.remove(video_state_path(seat))
    except OSError:
        pass


def _port_override():
    override = os.environ.get("AR3_BROWSER_PORT")
    return int(override) if override else None


def _port_file(seat):
    return os.path.join(profile_dir(seat), PORT_FILE)


def _recorded_port(seat):
    """The port Chrome wrote into this seat's own profile, or None."""
    try:
        with open(_port_file(seat)) as handle:
            return int(handle.readline().strip())
    except (OSError, ValueError):
        return None


LOOPBACK_HOSTS = ("127.0.0.1", "::1")


def _loopback_hosts(address):
    """The loopback hosts a listener's local address serves: one of the two,
    both for a wildcard, none for anything else."""
    host = address.rsplit(":", 1)[0].strip("[]") if ":" in address else address
    if host in ("*", "0.0.0.0", "::", ""):
        return LOOPBACK_HOSTS
    return (host,) if host in LOOPBACK_HOSTS else ()


def _listening_endpoints(port):
    """{host: {pid, ...}} for the processes holding a listening TCP socket on
    `port` at each loopback host, from lsof (macOS, Linux) or ss (Linux). Empty
    when neither tool answers: no proof, no owner. The host matters: when
    127.0.0.1:port is taken Chrome falls back to [::1]:port, so two Chromes
    share one port number and only the exact endpoint names the owner."""
    endpoints = {}

    def hold(address, pid):
        for host in _loopback_hosts(address):
            endpoints.setdefault(host, set()).add(pid)

    if shutil.which("lsof"):
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "pn"],
            capture_output=True, text=True,
        )
        pid = None
        for line in result.stdout.splitlines():
            if line.startswith("p"):
                pid = line[1:]
            elif line.startswith("n") and pid and pid.isdigit():
                hold(line[1:], pid)
        return endpoints
    if shutil.which("ss"):
        result = subprocess.run(
            ["ss", "-Hltnp", f"sport = :{port}"], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            for pid in re.findall(r"pid=(\d+)", line):
                hold(fields[3], pid)
        return endpoints
    return endpoints


def _owned_hosts(seat, port):
    """The loopback hosts where a process of this seat's profile holds the
    listener on `port`, 127.0.0.1 first. Empty means not ours (or no proof)."""
    ours = set(_profile_pids(seat))
    if not ours:
        return []
    endpoints = _listening_endpoints(port)
    return [host for host in LOOPBACK_HOSTS if endpoints.get(host, set()) & ours]


def cdp_port(seat):
    """The CDP port of this seat's own Chrome.

    Chrome started with port 0 records the port it picked in its own profile
    (DevToolsActivePort); that record is the port. An owner-set port
    (AR3_BROWSER_PORT) is the launch port, never proof of ownership by itself:
    Chrome writes no record for a fixed port, so the override counts only
    when a process of this seat's profile holds a listener on it. A record
    that differs from the override is a mismatch, and attaching to the
    override would drive someone else's browser. No proof, no port: None."""
    recorded = _recorded_port(seat)
    override = _port_override()
    if override and recorded and recorded != override:
        raise plc.BrowserError(
            f"seat {seat!r} runs Chrome on port {recorded}, not the requested "
            f"{override}; close it first or drop --port"
        )
    if recorded:
        return recorded
    if override and _owned_hosts(seat, override):
        return override
    return None


def _port_in_use(port):
    """True when anything accepts a connection on the loopback port."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                if probe.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def chrome_path():
    explicit = os.environ.get("AR3_BROWSER_CHROME")
    if explicit:
        return explicit
    if platform.system() == "Darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def cdp_base(seat):
    """The CDP base URL of this seat's own Chrome, or None.

    Ours means four facts together: a Chrome process of our profile runs, our
    profile recorded a port (or the owner set one), a process of our profile
    holds the listener at that exact loopback endpoint, and it answers. A
    port that answers with no Chrome of ours behind it is another process;
    so is the other address family's listener on the same port number.
    """
    if not _main_pids(seat):
        return None
    port = cdp_port(seat)
    if not port:
        return None
    for host in _owned_hosts(seat, port):
        base = f"http://{host}:{port}" if ":" not in host else f"http://[{host}]:{port}"
        try:
            urllib.request.urlopen(f"{base}/json/version", timeout=2)
            return base
        except OSError:
            continue
    return None


def _wait_for_cdp(seat):
    for _ in range(CHROME_STARTUP_WAIT):
        base = cdp_base(seat)
        if base:
            return base
        time.sleep(0.2)
    return None


def _profile_pattern(seat):
    """Regex for the seat's profile flag. The path is escaped and closed by a
    space or the end of the line, so a sibling seat whose name starts with this
    one (or differs only in a regex character) never matches."""
    return f"--user-data-dir={re.escape(profile_dir(seat))}([[:space:]]|$)"


def _profile_pids(seat):
    """Chromes holding this seat's profile, whether or not CDP answers yet."""
    if platform.system() == "Windows":
        return []
    result = subprocess.run(
        ["pgrep", "-f", "--", _profile_pattern(seat)], capture_output=True, text=True
    )
    return result.stdout.split()


def _main_pids(seat):
    """Just the browser process: it holds the seat's profile flag and carries
    no `--type=`. Helpers carry `--type=` and die on their own when the parent
    exits. The binary name is not matched: a wrapper such as `google-chrome`
    execs a differently named binary, so the process name is not ours to know."""
    if platform.system() == "Windows":
        return []
    result = subprocess.run(
        ["pgrep", "-af", "--", _profile_pattern(seat)], capture_output=True, text=True
    )
    pids = []
    for line in result.stdout.splitlines():
        pid, _, cmdline = line.partition(" ")
        if pid.isdigit() and " --type=" not in f" {cmdline}":
            pids.append(pid)
    return pids


def _daemon_pids(seat):
    """playwright-cli's per-seat daemon; a broken one can outlive its browser."""
    if platform.system() == "Windows":
        return []
    result = subprocess.run(
        ["pgrep", "-f", f"cliDaemon.js {re.escape(seat)}([[:space:]]|$)"],
        capture_output=True, text=True,
    )
    return result.stdout.split()


def _signal(pid, sig):
    try:
        os.kill(int(pid), sig)
    except (ProcessLookupError, PermissionError, ValueError):
        pass


def _terminate_profile(seat):
    """Nothing the seat owns gets to stay: SIGTERM the browser process, wait,
    then SIGKILL whatever is still holding the profile, and stop the seat's
    playwright daemon. A wedged Chrome is how a stray dock icon outlives its
    seat — SIGTERM alone can stall behind a busy or dialog-blocked browser."""
    for pid in _main_pids(seat):
        _signal(pid, signal.SIGTERM)
    for _ in range(20):
        if not _profile_pids(seat):
            break
        time.sleep(0.25)
    for pid in _profile_pids(seat):
        _signal(pid, signal.SIGKILL)
    for pid in _daemon_pids(seat):
        _signal(pid, signal.SIGTERM)


def _headless():
    """Linux with no display gets a headless Chrome; macOS and Windows are
    always headed. A headless seat can use a signed-in profile, but nobody can
    sign in through it."""
    return platform.system() == "Linux" and not os.environ.get("DISPLAY")


def _spawn_quiet(argv, cwd):
    """`ar3.proc.spawn` with stdout and stderr on the null device. The browser
    must not hold a wake's pipes open, and `spawn` leaves them inherited, so
    the two descriptors point at the null device for the length of the spawn."""
    if os.name != "posix":
        return spawn(argv, cwd=cwd)
    sys.stdout.flush()
    sys.stderr.flush()
    saved = (os.dup(1), os.dup(2))
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
        return spawn(argv, cwd=cwd)
    finally:
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in (null, *saved):
            os.close(fd)


def _launch_chrome(seat):
    """Start Chrome in its own session, so a wake killed at its time limit
    leaves the signed-in browser running; only `close` ends it."""
    binary = chrome_path()
    if not binary:
        raise plc.BrowserError("no Chrome found — set AR3_BROWSER_CHROME")
    os.makedirs(profile_dir(seat), exist_ok=True)
    # A stale record must never be read as the new Chrome's port.
    try:
        os.remove(_port_file(seat))
    except OSError:
        pass
    port = _port_override() or 0  # 0: Chrome picks a free port and records it
    argv = [
        binary,
        f"--user-data-dir={profile_dir(seat)}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-backgrounding-occluded-windows",
    ]
    if _headless():
        argv.append("--headless=new")
    argv.append("about:blank")
    _spawn_quiet(argv, cwd=seat_home(seat))


def _route_downloads_js(seat, directory=None):
    """Page-less JS that points the attached Chrome's downloads at the seat
    (or at `directory`, one of the seat's own).

    The seat attaches to a Chrome it launched as a person would, so Playwright
    manages no downloads and Chrome saves to its profile default. The
    Browser.setDownloadBehavior override lasts only while the CDP session that
    set it stays attached, so one session is kept on the driver's Browser object
    for the life of the attach and reused on every call.
    """
    return (
        "const br = page.context().browser();"
        " if (!br.__a8sDownloads) br.__a8sDownloads = await br.newBrowserCDPSession();"
        " await br.__a8sDownloads.send('Browser.setDownloadBehavior',"
        " {behavior: 'allow', downloadPath:"
        f" '{plc.js_string(directory or downloads_dir(seat))}'}});"
    )


def open_browser(seat):
    if plc.is_open(seat):
        return

    base = cdp_base(seat)
    if not base:
        if not _profile_pids(seat):
            override = _port_override()
            if override and _port_in_use(override):
                raise plc.BrowserError(
                    f"port {override} is in use by a process that is not Chrome "
                    f"for seat {seat!r}; free it or choose another --port"
                )
            _launch_chrome(seat)
        base = _wait_for_cdp(seat)
    if not base:
        # A launch that never serves CDP would otherwise leave a browser
        # nobody can drive — and an icon nobody can quit — running forever.
        _terminate_profile(seat)
        raise plc.BrowserError(f"Chrome for seat {seat!r} did not come up (no live CDP port in its profile)")

    try:
        plc.run(seat, "attach", f"--cdp={base}", timeout=15)
    except plc.BrowserError:
        _terminate_profile(seat)
        raise
    plc.run_code(seat, _route_downloads_js(seat), timeout=15)


def _salvage_recording(seat, timeout=30):
    """Stop an active recording so the file flushes before the browser dies."""
    if not video_state(seat):
        return
    try:
        plc.run(seat, "video-stop", timeout=timeout)
    except plc.BrowserError:
        pass
    clear_video_state(seat)


def close_browser(seat):
    started = time.monotonic()

    def left(own):
        """Own timeout, capped by what is left of the graceful budget."""
        return min(own, GRACEFUL_CLOSE_SECONDS - (time.monotonic() - started))

    def graceful(own, step):
        """Run one graceful step if budget is left; a spent budget skips it."""
        timeout = left(own)
        if timeout <= 0:
            return
        try:
            step(timeout)
        except plc.BrowserError:
            pass

    if plc.is_open(seat):
        # A modal dialog can keep a graceful quit — and even state-save —
        # from ever completing; clear one before anything else.
        graceful(5, lambda t: plc.run(seat, "dialog-dismiss", timeout=t))
        graceful(30, lambda t: save_state(seat, timeout=t))
        graceful(30, lambda t: _salvage_recording(seat, timeout=t))
        # Browser.close over CDP quits exactly this Chrome instance with a
        # clean exit (profile flushes, no crash flag). browser().close()
        # only disconnects, and osascript "tell application ... to quit"
        # is unscoped — it can take down a person's own browser running
        # beside the seat's.
        graceful(15, lambda t: plc.run_code(
            seat,
            "const s = await page.context().browser().newBrowserCDPSession();"
            " await s.send('Browser.close');",
            timeout=t,
        ))
        graceful(30, lambda t: plc.run(seat, "close", timeout=t))

    _terminate_profile(seat)

    for _ in range(20):
        if not cdp_base(seat):
            break
        time.sleep(0.25)


def _probe_js(seat):
    """Route downloads, then report the driven page's URL, window and visibility.

    Which window holds the page is asked of CDP. A page no window holds is the
    genuine zero-window Chrome; visibilityState cannot tell that apart from a
    window that is merely covered or minimised, which also read "hidden".
    """
    return _route_downloads_js(seat) + (
        " const s = await page.context().newCDPSession(page);"
        " try {"
        " let w = null;"
        " try { w = await s.send('Browser.getWindowForTarget'); }"
        " catch (e) { if (!/window not found/i.test(e.message)) throw e; }"
        " const visible = await page.evaluate(() => document.visibilityState);"
        " return JSON.stringify({url: page.url(), visible,"
        " window: w && w.windowId, state: w && w.bounds.windowState});"
        " } finally { await s.detach(); }"
    )


BLANK_URLS = ("", "about:blank")


def _window_of_page(seat):
    return plc.parse_json(plc.result_of(plc.run_code(seat, _probe_js(seat), timeout=15)))


def _unminimise(seat, window_id):
    plc.run_code(
        seat,
        "const s = await page.context().newCDPSession(page);"
        f" try {{ await s.send('Browser.setWindowBounds', {{windowId: {int(window_id)},"
        " bounds: {windowState: 'normal'}}); } finally { await s.detach(); }",
        timeout=15,
    )


def _bring_to_front(seat):
    plc.run_code(
        seat,
        "const s = await page.context().newCDPSession(page);"
        " try { await s.send('Page.bringToFront'); } finally { await s.detach(); }",
        timeout=15,
    )


def _restart_browser(seat, url):
    """Cycle Chrome and land back on `url`, so a restart never moves the caller."""
    close_browser(seat)
    open_browser(seat)
    if url not in BLANK_URLS:
        try:
            plc.run(seat, "goto", url, timeout=60)
        except plc.BrowserError as exc:
            raise plc.BrowserError(
                f"restarted Chrome but could not return to {url}: {exc}"
            ) from exc


def ensure_running(seat):
    """Open the browser, and make sure the driven page is one a click can reach.

    A page no window holds takes no input in its out-of-process iframes —
    clicks silently no-op — so that Chrome is cycled, and the page it was on
    is reopened. A minimised window is restored. A window behind other windows
    is left alone: Chrome is launched with --disable-backgrounding-occluded-windows,
    so its pages stay visible. A page that still reads hidden in a normal window
    — its app hidden — is brought to the front, because Playwright waits on
    animation frames a hidden page never draws.

    A session that is listed but refuses the probe is busy, not dead — an open
    dialog blocks the driver — so the failure falls through to the verb, which
    either handles the modal state or reports the real error.
    """
    if not plc.is_open(seat):
        open_browser(seat)
    try:
        page = _window_of_page(seat)
    except plc.BrowserError:
        return
    if page.get("window") is None:
        _restart_browser(seat, page.get("url") or "")
    elif page.get("state") == "minimized":
        _unminimise(seat, page["window"])
    elif page.get("visible") == "hidden":
        _bring_to_front(seat)


def save_state(seat, timeout=30):
    path = os.path.join(seat_home(seat), "state.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    plc.run(seat, "state-save", path, timeout=timeout)
    return path


def snapshot(seat):
    return plc.run(seat, "snapshot", timeout=30)


def current_url(seat):
    return plc.evaluate(seat, "window.location.href")


def status(seat):
    if not plc.is_open(seat):
        try:
            port = cdp_port(seat) if _main_pids(seat) else None
        except plc.BrowserError as exc:
            return f"{seat}: not attached ({exc})"
        return f"{seat}: not attached (cdp {port if port else 'none'})"
    return f"{seat}: attached at {current_url(seat)}"
