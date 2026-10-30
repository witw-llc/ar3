"""Seat-keyed browser session.

One seat, one Chrome profile, one playwright-cli session. playwright-cli
launches the seat's Chrome with the seat's own flags and none of its defaults,
and drives it over `--remote-debugging-pipe`: two inherited descriptors, so
Chrome opens no DevTools port that another local user could reach. The profile
directory is what makes a seat persistent: cookies survive a restart because
Chrome stores them the way it does for a person.

Derived from b3t's session layer (neilobremski/bin), generalised so the seat
name selects the profile.
"""
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from ar3.locking import file_lock
from ar3.proc import launch_options

from . import plc

# How long `open` may take to start Chrome and load its first page, under the launch lock.
CHROME_START_SECONDS = 15
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


def private_dir(path):
    """Make `path` a directory only its owner can enter: made 0700, and set to 0700
    when it exists with looser permissions. A directory this user does not own
    stays as it is."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        if stat.S_IMODE(os.stat(path).st_mode) != 0o700:
            os.chmod(path, 0o700)
    except OSError:
        pass


def secure_seat(seat):
    """Close the seat's home and scratch directories to everyone but their owner.
    playwright-cli writes its snapshots and console logs under the scratch
    directory with the permissions its umask gives, and a snapshot can hold a
    value typed into a field. Every run starts here, so a directory made earlier
    with looser permissions is tightened."""
    private_dir(seat_home(seat))
    private_dir(os.path.join(seat_home(seat), "scratch"))


def inside_state_root(path):
    """True when `path` is the browser state root or anything under it. That root
    holds every seat's home: its secret store, profile, cookies and state file.
    The path counts as the system resolves it, so a symlink that leads in is in,
    and so is a spelling of the root a case-insensitive file system accepts. A
    `..` is read both as a URL reads it (before any link is followed) and as the
    kernel reads it (after). A path that cannot be resolved counts as in."""
    try:
        root = os.path.realpath(state_root())
        try:
            root_stat = os.stat(root)
        except OSError:
            root_stat = None
        for candidate in {os.path.realpath(path), os.path.realpath(os.path.normpath(path))}:
            while True:
                if candidate == root:
                    return True
                if root_stat is not None:
                    try:
                        if os.path.samestat(os.stat(candidate), root_stat):
                            return True
                    except OSError:
                        pass
                parent = os.path.dirname(candidate)
                if parent == candidate:
                    break
                candidate = parent
    except ValueError:
        return True
    return False


STOP_MESSAGE = "seat closed by a8s stop"


def _stop_epoch_path(seat):
    return os.path.join(seat_home(seat), "stop-epoch")


def stop_epoch(seat):
    """The opaque token of the last `a8s stop` on this seat; "" when none."""
    try:
        with open(_stop_epoch_path(seat), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def bump_stop_epoch(seat):
    """Write a new stop token atomically. Call it BEFORE anything is killed."""
    home = seat_home(seat)
    private_dir(home)
    token = str(time.time_ns())
    tmp = os.path.join(home, f".stop-epoch.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(token)
    os.replace(tmp, _stop_epoch_path(seat))
    return token


def _activity_path(seat):
    return os.path.join(seat_home(seat), "last-active")


def touch_activity(seat):
    """Stamp now as the last time a script used this seat. The write is atomic,
    and a failed write is ignored: a missed stamp only makes the seat look idle
    sooner."""
    home = seat_home(seat)
    tmp = os.path.join(home, f".last-active.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        private_dir(home)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
        os.replace(tmp, _activity_path(seat))
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def last_activity(seat):
    """When a script last used this seat, in seconds since the epoch; None when
    the stamp is missing, unreadable or not a number."""
    try:
        with open(_activity_path(seat), encoding="utf-8") as handle:
            stamp = float(handle.read().strip())
    except (OSError, ValueError):
        return None
    return stamp if math.isfinite(stamp) else None


def launch_lock(seat, blocking=True):
    """The launch lock: held by a launch decision (milliseconds, plus Chrome's
    start, `CHROME_START_SECONDS` at most plus the teardown of a failed start) and by the stop's
    epoch-and-kill. It is separate from the seat lock; a stop never forces past it."""
    private_dir(seat_home(seat))
    return file_lock(Path(seat_home(seat)) / "launch-lock", blocking=blocking)


def profile_dir(seat):
    return os.path.join(seat_home(seat), "chrome")


def artifacts_dir(seat):
    return os.path.join(seat_home(seat), "artifacts")


def downloads_dir(seat):
    """Where the seat's Chrome saves downloads — never the profile default,
    which is a person's own Downloads folder."""
    private_dir(seat_home(seat))
    path = os.path.join(seat_home(seat), "downloads")
    os.makedirs(path, exist_ok=True)
    return path


def scratch_dir(seat):
    """The seat's working directory — playwright-cli drops per-command scratch
    (page-*.yml, console-*.log) under a `.playwright-cli` dir beneath its cwd,
    so the CLI runs from here instead of the caller's directory."""
    secure_seat(seat)
    return os.path.join(seat_home(seat), "scratch")


def cli_output_dir(seat):
    """Where playwright-cli writes the files its replies link to: page snapshots
    and console logs."""
    return os.path.join(scratch_dir(seat), ".playwright-cli")


def prune_scratch(seat, max_age=86400):
    """Bound the scratch playwright-cli leaves behind — it writes a file per
    command, so an unpruned dir grows without limit on a busy seat. Also remove
    the private directory of a secret fill that a killed run left behind."""
    _prune_secret_leftovers(seat)
    directory = cli_output_dir(seat)
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


def _prune_secret_leftovers(seat, max_age=300):
    """A secret fill, or a recipe's code file, ends within its call timeout, so a
    directory this old is a run that died between writing the file and removing it."""
    scratch = scratch_dir(seat)
    try:
        names = os.listdir(scratch)
    except OSError:
        return
    cutoff = time.time() - max_age
    for name in names:
        path = os.path.join(scratch, name)
        try:
            if name.startswith((plc.SECRET_DIR_PREFIX, plc.CODE_DIR_PREFIX)) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path)
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
    private_dir(seat_home(seat))
    path = video_state_path(seat)
    with open(path + ".tmp", "w") as handle:
        json.dump(state, handle)
    os.replace(path + ".tmp", path)


def clear_video_state(seat):
    try:
        os.remove(video_state_path(seat))
    except OSError:
        pass


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


def _profile_pattern(seat):
    """Regex for the seat's profile flag. The path is escaped and closed by a
    space or the end of the line, so a sibling seat whose name starts with this
    one (or differs only in a regex character) never matches."""
    return f"--user-data-dir={re.escape(profile_dir(seat))}([[:space:]]|$)"


def _profile_pids(seat):
    """Chromes holding this seat's profile, whether or not the driver holds them."""
    if platform.system() == "Windows":
        return []
    result = subprocess.run(
        ["pgrep", "-f", "--", _profile_pattern(seat)], capture_output=True, text=True,
        **launch_options(),
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
        ["pgrep", "-af", "--", _profile_pattern(seat)], capture_output=True, text=True,
        **launch_options(),
    )
    pids = []
    for line in result.stdout.splitlines():
        pid, _, cmdline = line.partition(" ")
        if pid.isdigit() and " --type=" not in f" {cmdline}":
            pids.append(pid)
    return pids


def chrome_running(seat):
    """True when a Chrome holds this seat's profile, attached or not. One
    pgrep; it starts nothing."""
    return bool(_profile_pids(seat))


def _daemon_pids(seat):
    """playwright-cli's per-seat daemon; a broken one can outlive its browser."""
    if platform.system() == "Windows":
        return []
    result = subprocess.run(
        ["pgrep", "-f", f"cliDaemon.js {re.escape(plc.session_name(seat))}([[:space:]]|$)"],
        capture_output=True, text=True,
        **launch_options(),
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


# Chrome has no launch flag for either password setting; each is a profile preference.
# `credentials_enable_service` is "Offer to save passwords and passkeys".
# `profile.password_manager_leak_detection` is "Warn you if passwords are exposed
# in a data breach": Chrome runs a breach check on each credential a form submits.
PASSWORD_PREFERENCES = (
    (("credentials_enable_service",), False),
    (("profile", "password_manager_leak_detection"), False),
)


def _set_password_preferences(seat):
    """Turn Chrome's password offer and its breach check off in the profile before
    Chrome starts.

    With the offer on, Chrome asks to keep a password typed into a sign-in form
    that submits. A secret the tool types must stay in the seat's secret store,
    not be copied into the profile. Chrome keeps filling passwords it already
    holds. With the check on, Chrome runs its breach check on each credential a
    form submits, and the seat has no use for it. Both are plain profile
    preferences, which Chrome keeps when it rewrites the file on exit. A damaged
    file is left for Chrome to replace.
    """
    directory = os.path.join(profile_dir(seat), "Default")
    target = os.path.join(directory, "Preferences")
    try:
        with open(target, encoding="utf-8") as handle:
            prefs = json.load(handle)
    except FileNotFoundError:
        prefs = {}
    except (OSError, ValueError):
        return
    if not isinstance(prefs, dict):
        return
    changed = False
    for keys, wanted in PASSWORD_PREFERENCES:
        section = prefs
        for key in keys[:-1]:
            section = section.setdefault(key, {})
            if not isinstance(section, dict):
                return
        if section.get(keys[-1]) is not wanted:
            section[keys[-1]] = wanted
            changed = True
    if not changed:
        return
    os.makedirs(directory, exist_ok=True)
    aside = os.path.join(directory, f".Preferences.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(aside, "w", encoding="utf-8") as handle:
        json.dump(prefs, handle, separators=(",", ":"))
    os.replace(aside, target)


def launch_config_path(seat):
    return os.path.join(seat_home(seat), "launch.json")


# playwright-cli adds this flag to every Chromium launch whose arguments do not already hold one that
# contains `--disable-blink-features`, and `ignoreDefaultArgs` does not stop it. The value it adds,
# `AutomationControlled`, is what keeps `navigator.webdriver` false over the debugging pipe, so the seat
# keeps it. Chrome shows its "unsupported command-line flag" bar for the flag with any value, and
# `--test-type` is the switch that turns that bar off (ChromeDriver passes it for the same reason).
BAR_OFF_FLAG = "--test-type=webdriver"
PLAYWRIGHT_ADDED_FLAGS = ("--disable-blink-features=AutomationControlled",)


def _launch_args(seat):
    """Chrome's whole command line after the binary. Playwright adds none of its defaults (`ignoreDefaultArgs`
    is true), so the seat's Chrome carries the flags a person's would, plus the two Playwright needs: the
    profile and the pipe it drives Chrome over. `BAR_OFF_FLAG` keeps Chrome from warning about the one flag
    playwright-cli adds on its own (`PLAYWRIGHT_ADDED_FLAGS`). With its defaults Playwright would also add
    `--no-sandbox`, `--password-store=basic` (cookies encrypted under a fixed key) and `--enable-automation`."""
    argv = [
        f"--user-data-dir={profile_dir(seat)}",
        "--remote-debugging-pipe",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-backgrounding-occluded-windows",
        BAR_OFF_FLAG,
    ]
    if _headless():
        argv.append("--headless=new")
    argv.append("about:blank")
    return argv


def _flag_name(argument):
    return argument.split("=", 1)[0]


def unasked_flags(seat, argv):
    """The flags in a Chrome's command line (`argv`, the binary left out) that neither `_launch_args` nor
    playwright-cli's one known addition (with its value) accounts for, as they appear. Words that are not flags (the start page)
    are not looked at."""
    asked = {_flag_name(a) for a in _launch_args(seat) if a.startswith("--")}
    return [a for a in argv if a.startswith("--") and _flag_name(a) not in asked and a not in PLAYWRIGHT_ADDED_FLAGS]


def _process_argv(pid):
    """The command line of process `pid`, split into arguments, or None when it cannot be read. Linux reads the
    exact argument list; elsewhere `ps` gives one line, split on spaces."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as handle:
            return [a.decode("utf-8", "replace") for a in handle.read().split(b"\0") if a]
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, **launch_options())
    except OSError:
        return None
    return result.stdout.split() or None


def check_launch_argv(seat):
    """After a launch, compare the seat's real Chrome command line with what `_launch_args` asked for, and
    write one line to stderr (the node's agent log) if a flag nobody asked for is in it. Chrome, or an update of
    playwright-cli, can add flags that trigger bars or show the browser as automated. It never raises and never
    ends the Chrome. Returns the unasked flags, [] when there are none or the line cannot be read."""
    if platform.system() == "Windows":
        return []
    try:
        main = _main_pids(seat)
        argv = _process_argv(main[0]) if len(main) == 1 else None
        found = unasked_flags(seat, argv[1:]) if argv else []
    except Exception:
        return []
    if found:
        print(
            f"{seat}: Chrome started with a flag the seat did not ask for: {' '.join(found)}. "
            "Check the installed playwright-cli version with `ar3 doctor`.",
            file=sys.stderr,
        )
    return found


def _write_launch_config(seat, binary):
    """The playwright-cli config that launches this seat's Chrome, written 0600 in the seat's 0700 home. It holds
    paths and flags, nothing of a caller or a page. `viewport: null` leaves the window its own size, as a
    person's Chrome has it."""
    config = {
        "browser": {
            "browserName": "chromium",
            "userDataDir": profile_dir(seat),
            "launchOptions": {
                "executablePath": binary,
                "headless": _headless(),
                "chromiumSandbox": True,
                "ignoreDefaultArgs": True,
                "args": _launch_args(seat),
            },
            "contextOptions": {"viewport": None},
        },
    }
    target = launch_config_path(seat)
    aside = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
    descriptor = os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    os.replace(aside, target)
    return target


def _parent_pid(pid):
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[1]
    except (OSError, IndexError, ValueError):
        result = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, **launch_options())
        return result.stdout.strip()


UNSUPPORTED_MESSAGE = "a browser seat opens on Linux and macOS only"


def _refuse_unsupported():
    """A seat opens on Linux and macOS only. On Windows the tool can find no process of the seat, and the driver
    listens on a named pipe whose access the tool does not set, so nothing is started there."""
    if platform.system() == "Windows":
        raise plc.BrowserError(UNSUPPORTED_MESSAGE)


def owned_by_driver(seat):
    """True when exactly one browser process holds this seat's profile, and its parent is this seat's
    playwright-cli daemon. The daemon holds the other end of Chrome's pipe; nothing else can drive it. Never true
    on Windows, where no process of the seat can be found."""
    if platform.system() == "Windows":
        return False
    main = _main_pids(seat)
    return len(main) == 1 and _parent_pid(main[0]) in set(_daemon_pids(seat))


def _launch_chrome(seat):
    """Have playwright-cli launch the seat's Chrome. The daemon it starts runs in its own session, so a wake
    killed at its time limit leaves the daemon, and the signed-in browser it holds, running; only `close`
    ends them. Chrome lives as long as the daemon: its pipe closes with it."""
    _refuse_unsupported()
    binary = chrome_path()
    if not binary:
        raise plc.BrowserError("no Chrome found — set AR3_BROWSER_CHROME")
    private_dir(seat_home(seat))
    os.makedirs(profile_dir(seat), exist_ok=True)
    _set_password_preferences(seat)
    config = _write_launch_config(seat, binary)
    plc.run(seat, "open", plc.Own(f"--config={config}"), timeout=CHROME_START_SECONDS)


def _route_downloads_js(seat, directory=None):
    """Page-less JS that points Chrome's downloads at the seat (or at `directory`, one of the seat's own).

    Playwright would save each download under a generated name in a temporary directory of its own. The
    Browser.setDownloadBehavior override replaces that, keeps the file's own name, and lasts only while the CDP
    session that set it stays attached, so one session is kept on the driver's Browser object for the life of
    the daemon and reused on every call.
    """
    return plc.Own(
        "const br = page.context().browser();"
        " if (!br.__a8sDownloads) br.__a8sDownloads = await br.newBrowserCDPSession();"
        " await br.__a8sDownloads.send('Browser.setDownloadBehavior',"
        " {behavior: 'allow', downloadPath:"
        f" '{plc.js_string(directory or downloads_dir(seat))}'}});"
    )


def _start_chrome(seat):
    """Launch Chrome through the driver when the seat has no session. True when this call launched it.

    A Chrome that holds the profile with no session of the driver is no one's to drive (its pipe is gone), and
    Chrome refuses a second instance on one profile, so it is ended first. A launch that does not leave exactly
    the seat's own Chrome under the seat's own daemon is ended too."""
    _refuse_unsupported()
    if plc.is_open(seat):
        return False
    if _profile_pids(seat):
        _terminate_profile(seat)
    try:
        _launch_chrome(seat)
    except plc.BrowserError:
        _terminate_profile(seat)
        raise
    if not owned_by_driver(seat):
        _terminate_profile(seat)
        raise plc.BrowserError(f"Chrome for seat {seat!r} did not come up under the seat's driver")
    check_launch_argv(seat)
    return True


def _attach(seat):
    """Route the new Chrome's downloads; its first call over the pipe."""
    try:
        plc.run_code(seat, _route_downloads_js(seat), timeout=15)
    except plc.BrowserError:
        _terminate_profile(seat)
        raise


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
        # Browser.close over the pipe quits exactly this Chrome instance with a
        # clean exit (profile flushes, no crash flag). osascript "tell application ... to quit"
        # is unscoped — it can take down a person's own browser running
        # beside the seat's.
        graceful(15, lambda t: plc.run_code(
            seat,
            plc.Own(
                "const s = await page.context().browser().newBrowserCDPSession();"
                " await s.send('Browser.close');"
            ),
            timeout=t,
        ))
        graceful(30, lambda t: plc.run(seat, "close", timeout=t))

    _terminate_profile(seat)


def _probe_js(seat):
    """Route downloads, then report the driven page's URL, window and visibility.

    Which window holds the page is asked of CDP. A page no window holds is the
    genuine zero-window Chrome; visibilityState cannot tell that apart from a
    window that is merely covered or minimised, which also read "hidden".
    """
    return plc.Own(_route_downloads_js(seat) + (
        " const s = await page.context().newCDPSession(page);"
        " try {"
        " let w = null;"
        " try { w = await s.send('Browser.getWindowForTarget'); }"
        " catch (e) { if (!/window not found/i.test(e.message)) throw e; }"
        " const visible = await page.evaluate(() => document.visibilityState);"
        " return JSON.stringify({url: page.url(), visible,"
        " window: w && w.windowId, state: w && w.bounds.windowState});"
        " } finally { await s.detach(); }"
    ))


BLANK_URLS = ("", "about:blank")


def _window_of_page(seat):
    return plc.parse_json(plc.result_of(plc.run_code(seat, _probe_js(seat), timeout=15), seat), seat)


def _unminimise(seat, window_id):
    plc.run_code(
        seat,
        plc.Own(
            "const s = await page.context().newCDPSession(page);"
            f" try {{ await s.send('Browser.setWindowBounds', {{windowId: {int(window_id)},"
            " bounds: {windowState: 'normal'}}); } finally { await s.detach(); }"
        ),
        timeout=15,
    )


def _bring_to_front(seat):
    plc.run_code(
        seat,
        plc.Own(
            "const s = await page.context().newCDPSession(page);"
            " try { await s.send('Page.bringToFront'); } finally { await s.detach(); }"
        ),
        timeout=15,
    )


def _restart_browser(seat, url, epoch=None):
    """Cycle Chrome and land back on `url`, so a restart never moves the caller."""
    close_browser(seat)
    open_if_down(seat, epoch)
    if url not in BLANK_URLS:
        try:
            plc.run(seat, "goto", url, timeout=60)
        except plc.BrowserError as exc:
            raise plc.BrowserError(
                f"restarted Chrome but could not return to {url}: {exc}"
            ) from exc


def open_if_down(seat, epoch):
    """The one way to start Chrome. Every opener calls it with the epoch it read
    at its own entry: ensure_running, the CLI verbs and the restart repair.

    Chrome is down, so this call would launch it. An operation that began
    before an `a8s stop` must not relaunch what the stop closed. The stop
    writes its epoch and kills under the launch lock; the launch reads the
    epoch under the same lock, so it sees the new epoch. The lock is released
    before any page work. An epoch of None skips the check (tests only).
    """
    if plc.is_open(seat):
        return
    with launch_lock(seat):
        if plc.is_open(seat):
            return
        if epoch is not None and stop_epoch(seat) != epoch:
            raise plc.BrowserError(STOP_MESSAGE)
        if _before_spawn is not None:
            _before_spawn()
        launched = _start_chrome(seat)
        # Post-spawn barrier: a stop that gave up on the launch lock may have
        # bumped the epoch and scanned before this Chrome existed. Re-read it;
        # if it moved, end the Chrome we made.
        if epoch is not None and stop_epoch(seat) != epoch:
            _terminate_profile(seat)
            raise plc.BrowserError(STOP_MESSAGE)
    if launched:
        _attach(seat)


_before_spawn = None  # test hook: called between the epoch check and the spawn


def ensure_running(seat, script_epoch=None):
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
        open_if_down(seat, script_epoch)
    try:
        page = _window_of_page(seat)
    except plc.BrowserError:
        return
    if page.get("window") is None:
        _restart_browser(seat, page.get("url") or "", script_epoch)
    elif page.get("state") == "minimized":
        _unminimise(seat, page["window"])
    elif page.get("visible") == "hidden":
        _bring_to_front(seat)


def _make_private(path):
    """The file at `path` exists and is mode 0600 (made empty, at that mode, when it is not there)."""
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    os.chmod(path, 0o600)


def save_state(seat, timeout=30):
    """Write the profile's storage state, its cookies and the local storage of each site, to `state.json` in
    the seat's directory, and return the path. The file is mode 0600 before the driver writes into it and
    after. A site keeps in its storage whatever it chooses, a value typed into one of its fields among it, so
    the file is as private as the profile it comes from. No verb attaches, returns or uploads it."""
    path = os.path.join(seat_home(seat), "state.json")
    private_dir(seat_home(seat))
    existed = os.path.exists(path)
    _make_private(path)
    try:
        plc.run(seat, "state-save", plc.Own(path), timeout=timeout)
    except plc.BrowserError:
        if not existed:
            try:
                os.remove(path)  # the empty file this call made
            except OSError:
                pass
        raise
    _make_private(path)
    return path


def snapshot(seat):
    return plc.run(seat, "snapshot", timeout=30)


def current_url(seat):
    return plc.evaluate(seat, plc.Own("window.location.href"))


# A page's target id, as Chrome makes it.
TARGET_ID = re.compile(r"[0-9A-Za-z-]{1,64}")


def _browser_call_code(nonce, body):
    """The text of one async function that runs `body` with `s`, a CDP session on the browser itself, and
    returns `{nonce, value}`. The nonce is fresh for each call and is in no page, so text a page puts in the
    output (a title, a URL, a console line) cannot pass for the answer."""
    return (
        "async (page) => {\n"
        "  const s = await page.context().browser().newBrowserCDPSession();\n"
        "  try {\n"
        f"{body}\n"
        f"    return {{nonce: {json.dumps(nonce)}, value}};\n"
        "  } finally { await s.detach(); }\n"
        "}\n"
    )


def _browser_call(seat, body, timeout=15, strict=False):
    """The value `body` sets, read from the driver's structured answer (see `_browser_call_code`). None when
    the seat has no session (the driver opens no browser for this call), the call fails, or the answer is
    not exactly the one this call asked for. Strict callers receive a fixed error instead of None."""
    nonce = secrets.token_hex(16)
    try:
        answer = plc.result_json(plc.run_code_file(seat, _browser_call_code(nonce, body), scratch_dir(seat), timeout=timeout))
    except (plc.BrowserError, OSError):
        if strict:
            raise plc.BrowserError("tabs: could not read the browser's structured response") from None
        return None
    if not isinstance(answer, dict) or set(answer) != {"nonce", "value"} or answer["nonce"] != nonce:
        if strict:
            raise plc.BrowserError("tabs: invalid structured browser response")
        return None
    return answer["value"]


def tabs(seat):
    """Tabs in driver order, with stable target identities and the driver's selected page.

    Both the driver and Playwright's context append pages on creation and remove them on closure.
    Target.getTargets order is unrelated to the driver's zero-based indices.
    """
    listing = _browser_call(seat, (
        "    const value = [];\n"
        "    for (const tab of page.context().pages()) {\n"
        "      const channel = await page.context().newCDPSession(tab);\n"
        "      try {\n"
        "        const {targetInfo} = await channel.send('Target.getTargetInfo');\n"
        "        value.push({id: targetInfo.targetId, url: tab.url(), selected: tab === page});\n"
        "      } finally { await channel.detach(); }\n"
        "    }"
    ), strict=True)
    if not isinstance(listing, list) or not listing:
        raise plc.BrowserError("tabs: invalid structured tab list")
    ids = set()
    for tab in listing:
        if not (
            isinstance(tab, dict) and set(tab) == {"id", "url", "selected"}
            and isinstance(tab["id"], str) and TARGET_ID.fullmatch(tab["id"])
            and isinstance(tab["url"], str) and type(tab["selected"]) is bool
            and tab["id"] not in ids
        ):
            raise plc.BrowserError("tabs: invalid structured tab list")
        ids.add(tab["id"])
    if sum(tab["selected"] for tab in listing) != 1:
        raise plc.BrowserError("tabs: invalid selected tab")
    return listing


def indexed_screenshot(seat, page_id, path):
    """Capture the verified selected target, refusing local-file navigation during the capture.

    The file is provisional until the driver confirms both identity and safe navigation. A refused or
    interrupted capture removes it; the command's usual post-step cleanup still runs afterwards.
    """
    body = (
        "    const context = page.context();\n"
        "    const local = (url) => {\n"
        "      let head = url;\n"
        "      while (true) {\n"
        "        head = head.replace(/^[\\x00-\\x20]+/, '').slice(0, 128).replace(/[\\t\\n\\r]/g, '').toLowerCase();\n"
        "        if (!head.startsWith('view-source:') && !head.startsWith('filesystem:')) return head.startsWith('file:');\n"
        "        head = head.slice(head.indexOf(':') + 1);\n"
        "      }\n"
        "    };\n"
        "    let unsafe = false;\n"
        "    const watched = new Set();\n"
        "    const navigated = (frame) => { if (local(frame.url())) unsafe = true; };\n"
        "    const watch = (tab) => {\n"
        "      watched.add(tab);\n"
        "      if (local(tab.url())) unsafe = true;\n"
        "      tab.on('framenavigated', navigated);\n"
        "    };\n"
        "    context.on('page', watch);\n"
        "    context.pages().forEach(watch);\n"
        "    let value = false;\n"
        "    try {\n"
        "      const channel = await context.newCDPSession(page);\n"
        "      let id;\n"
        "      try { id = (await channel.send('Target.getTargetInfo')).targetInfo.targetId; }\n"
        "      finally { await channel.detach(); }\n"
        f"      if (id === {json.dumps(page_id)} && !unsafe) {{\n"
        "        await page.bringToFront();\n"
        "        if (!unsafe) {\n"
        f"          await page.screenshot({{path: {json.dumps(path)}}});\n"
        "          value = !unsafe && !page.isClosed() && !context.pages().some((tab) => local(tab.url()));\n"
        "        }\n"
        "      }\n"
        "    } finally {\n"
        "      context.off('page', watch);\n"
        "      for (const tab of watched) tab.off('framenavigated', navigated);\n"
        "    }"
    )
    confirmed = False
    try:
        confirmed = _browser_call(seat, body, timeout=60, strict=True) is True
        if not confirmed or not os.path.isfile(path):
            confirmed = False
            raise plc.BrowserError("shot: tab changed or capture was refused; run tabs again")
        return path
    finally:
        if not confirmed:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def pages(seat):
    """[(id, url)] for every page (tab) that the seat's own Chrome has open, as Chrome's own target list gives
    it over the driver's pipe: one driver call. The titles are not read. None when the seat has no session or
    the answer is malformed (a page that is not an object, an id that is not a target id, a url that is not
    text); the caller then asks the driver for the page it drives."""
    listing = _browser_call(seat, (
        "    const {targetInfos} = await s.send('Target.getTargets');\n"
        "    const value = targetInfos.filter((t) => t.type === 'page').map((t) => ({id: t.targetId, url: t.url}));"
    ))
    if not isinstance(listing, list):
        return None
    found = []
    for target in listing:
        if not (isinstance(target, dict) and set(target) == {"id", "url"}):
            return None
        page_id, url = target["id"], target["url"]
        if not (isinstance(page_id, str) and TARGET_ID.fullmatch(page_id) and isinstance(url, str)):
            return None
        found.append((page_id, url))
    return found


def close_page(seat, page_id):
    """Close one page by its id (`pages`). True when Chrome took the request."""
    if not TARGET_ID.fullmatch(page_id or ""):
        return False
    closed = _browser_call(seat, (
        f"    const request = JSON.parse({plc.literal({'targetId': page_id})});\n"
        "    const value = (await s.send('Target.closeTarget', request)).success === true;"
    ))
    return closed is True


def _sweep_mark_path(seat):
    return os.path.join(seat_home(seat), "swept-at")


def sweep_mark(seat):
    """The time, in nanoseconds since the epoch, that the last sweep of the driver's files began (see
    `commands.sweep_outputs`); None when there was none or the mark cannot be read."""
    try:
        with open(_sweep_mark_path(seat), encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def mark_swept(seat, stamp):
    """Record `stamp` (nanoseconds since the epoch) as the start of the last sweep. The write is atomic and
    the file is mode 0600. A write that fails changes nothing: the next sweep looks at more files."""
    target = _sweep_mark_path(seat)
    aside = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        private_dir(seat_home(seat))
        try:
            descriptor = os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            os.unlink(aside)
            descriptor = os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(int(stamp)))
        os.replace(aside, target)
    except OSError:
        try:
            os.unlink(aside)
        except OSError:
            pass


def status(seat):
    if not plc.is_open(seat):
        return f"{seat}: not attached (chrome {'running' if _main_pids(seat) else 'down'})"
    return f"{seat}: attached at {current_url(seat)}"
