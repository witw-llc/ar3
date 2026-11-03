"""Nightly auto-update of one AR3 install, only when every seat is idle.

`ar3 update --auto on` arms a scheduler entry (`autoupdate_trigger`) that runs
`ar3 update --auto run` every 15 minutes. Each tick decides in code, from the
install's state file, whether tonight's attempt is due; most ticks exit at
once and touch no network. The state, a log and the last installer output sit
under `<ar3 home>/update/<install id>.*`.

This module imports nothing from `cli`: the pieces it shares with `ar3 update`
(the busy check, the refusal rules, the version read) arrive as `host`, the
running `cli` module, so a test that replaces one of them on `cli` replaces it
here too.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import ar3ver
import autoupdate_trigger as trigger_mod
from ar3 import clock
from ar3 import nodes as ar3_nodes
from ar3.envseam import TELL_OUTBOX_DIR_ENV, UPDATE_WAIT_ENV
from ar3.fsio import atomic_write_text
from ar3.home import app_home
from ar3.locking import file_lock
from ar3.proc import launch_options, terminate_group

DEFAULT_WINDOW = "02:00-05:00"
TICK_MINUTES = 15
LOG_CAP = 200

# The proxy and CA settings a scheduled run would otherwise lose, the same set
# a8s autostart keeps for a startup entry; PATH (a scheduler's PATH is minimal,
# and on macOS it hides the git the install was made with); and the suite's
# home settings, so the run asks and restarts the seats the operator armed it
# for. The ar3 home itself rides the entry's command line (`--state-home`):
# the state that holds these must be found before they apply.
CAPTURED_ENV = (
    "PATH",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "A8S_HOME", "R4T_HOME", "K7E_HOME",
)

OFF_ENV = "AR3_AUTO_UPDATE"


# ---------- state ----------

def ar3_home() -> Path:
    return app_home("ar3", os.environ.get("AR3_HOME"))


def state_dir() -> Path:
    return ar3_home() / "update"


def install_id(root: Path) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:10]


def state_path(root: Path) -> Path:
    return state_dir() / f"{install_id(root)}.json"


def log_path(root: Path) -> Path:
    return state_dir() / f"{install_id(root)}.log"


def last_run_path(root: Path) -> Path:
    return state_dir() / f"{install_id(root)}.last-run.txt"


def lock_path(root: Path) -> Path:
    return state_dir() / f"{install_id(root)}.lock"


def load(root: Path) -> dict:
    """The saved state, `{}` when none is saved or the file is unreadable.
    For display and settings only: whatever reads the state before it
    changes a tree, restarts seats or hands the install to a person uses
    `load_strict`."""
    try:
        return load_strict(root)
    except StateError:
        return {}


class StateError(Exception):
    """The state file exists but cannot be read, or is not a state."""


def load_strict(root: Path) -> dict:
    """The saved state, `{}` only when no state file exists."""
    path = state_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise StateError(f"cannot read the auto-update state {path}: {e}") from e
    if not isinstance(data, dict):
        raise StateError(f"the auto-update state {path} is not a JSON object")
    return data


def save(root: Path, state: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        state_path(root), json.dumps(state, indent=2, sort_keys=True) + "\n", mode=0o600
    )


def append_log(root: Path, line: str) -> None:
    path = log_path(root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    lines.append(line)
    atomic_write_text(path, "\n".join(lines[-LOG_CAP:]) + "\n", mode=0o600)


def capture_env() -> dict[str, str]:
    return {k: os.environ[k] for k in CAPTURED_ENV if k in os.environ}


# ---------- window ----------

def parse_window(text: str) -> tuple[int, int]:
    """`HH:MM-HH:MM` as (start, end) minutes after midnight. The end may be
    earlier than the start: the window then crosses midnight."""
    try:
        left, right = text.split("-")
        out = []
        for part in (left, right):
            hh, mm = part.split(":")
            h, m = int(hh), int(mm)
            if not (0 <= h < 24 and 0 <= m < 60) or len(mm) != 2:
                raise ValueError
            out.append(h * 60 + m)
    except ValueError:
        raise ValueError(f"window {text!r} is not HH:MM-HH:MM") from None
    if out[0] == out[1]:
        raise ValueError(f"window {text!r} is empty")
    return out[0], out[1]


def window_minutes(window: tuple[int, int]) -> int:
    return (window[1] - window[0]) % 1440


def slot_offset(install: str, night: str, minutes: int) -> int:
    """Minutes after the window opens when this install may first try, spread
    by install and night so a fleet does not fetch at once."""
    digest = int(hashlib.sha256(f"{install}{night}".encode()).hexdigest(), 16)
    return digest % max(1, minutes - 60)


def night_for(window: tuple[int, int], night_date) -> tuple[datetime, datetime]:
    start = datetime.combine(night_date, datetime.min.time()) + timedelta(minutes=window[0])
    return start, start + timedelta(minutes=window_minutes(window))


def current_night(window: tuple[int, int], now: datetime):
    """The window instance holding `now` as (night date, start, end), or None."""
    for back in (0, 1):
        day = now.date() - timedelta(days=back)
        start, end = night_for(window, day)
        if start <= now < end:
            return day, start, end
    return None


def slot_time(root: Path, window: tuple[int, int], night_date) -> datetime:
    start, _end = night_for(window, night_date)
    offset = slot_offset(install_id(root), night_date.isoformat(), window_minutes(window))
    return start + timedelta(minutes=offset)


def next_attempt(root: Path, state: dict, now: datetime) -> datetime | None:
    """The first tick from `now` on that may start an attempt, or None when
    auto-update is off or the window setting is unreadable."""
    if not state.get("enabled"):
        return None
    try:
        window = parse_window(state.get("window") or DEFAULT_WINDOW)
    except ValueError:
        return None
    tick = now.replace(second=0, microsecond=0)
    if tick < now:
        tick += timedelta(minutes=1)
    tick += timedelta(minutes=(-tick.minute) % TICK_MINUTES)
    for ahead in range(-1, 3):
        day = now.date() + timedelta(days=ahead)
        if day.isoformat() == state.get("done_night"):
            continue
        candidate = max(slot_time(root, window, day), tick)
        if candidate < night_for(window, day)[1]:
            return candidate
    return None


# ---------- arming ----------

def _scheduler(root: Path):
    return trigger_mod.select(state_dir())


def launcher_path(root: Path) -> Path:
    return state_dir() / f"{install_id(root)}.launch.py"


# The scheduler runs this file, not the install's own cli.py: an update that
# ended between get.sh's two moves leaves the install at `<install>.prev`, and
# the tick that puts it back must still start.
LAUNCHER = """\
# ar3 auto-update launcher, written by `ar3 update --auto on`.
import os
import subprocess
import sys

INSTALL = {install!r}
HOME = {home!r}
for tree in (INSTALL, INSTALL + ".prev"):
    cli = os.path.join(tree, "apps", "ar3", "cli.py")
    if os.path.isfile(cli):
        sys.exit(subprocess.call([
            sys.executable, cli, "update", "--auto", "run", "--state-home", HOME, "--install", INSTALL,
        ]))
sys.exit("ar3 auto-update: no ar3 at " + INSTALL)
"""


def arm(root: Path, *, python: str | None = None) -> str:
    """Write the launcher and the scheduler entry, and mark the install
    enabled. Returns the scheduler's note ('' when it has none). Raises
    TriggerError."""
    root = Path(root)
    state = load(root)
    python = python or sys.executable
    state.setdefault("window", DEFAULT_WINDOW)
    state.update(install=str(root.resolve()), python=python, env=capture_env())
    launcher = launcher_path(root)
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            launcher, LAUNCHER.format(install=state["install"], home=str(ar3_home().resolve())), mode=0o600,
        )
    except OSError as e:
        raise trigger_mod.TriggerError(f"could not write {launcher}: {e}") from e
    note = _scheduler(root).install(install_id(root), launcher, python, state["env"])
    state["enabled"] = True
    save(root, state)
    return note


def disarm(root: Path) -> None:
    root = Path(root)
    try:
        _scheduler(root).remove(install_id(root))
    except trigger_mod.TriggerUnavailable:
        pass
    launcher_path(root).unlink(missing_ok=True)
    state = load(root)
    state.update(install=str(root.resolve()), enabled=False)
    save(root, state)


def writable(root: Path) -> bool:
    """Whether this account can update the tree: a system install it only
    reads is updated by whoever owns it, not by a job of this account."""
    return os.access(root, os.W_OK)


def ensure(root: Path) -> str:
    """Arm an install that has no say yet. Returns what happened: `off` (the
    operator turned it off, or AR3_AUTO_UPDATE=0 says so), `system` (the tree
    is not this account's to write), `armed`, `present` or `unavailable`."""
    root = Path(root)
    state = load(root)
    if os.environ.get(OFF_ENV, "").strip() == "0":
        state.update(install=str(root.resolve()), enabled=False)
        save(root, state)
        return "off"
    if state.get("enabled") is False:
        return "off"
    if not writable(root):
        return "system"
    if state.get("enabled") and launcher_path(root).is_file():
        try:
            if _scheduler(root).installed(install_id(root)):
                return "present"
        except trigger_mod.TriggerError:
            return "unavailable"
    try:
        arm(root)
    except trigger_mod.TriggerError:
        return "unavailable"
    return "armed"


# ---------- the night run ----------

PULL_TIMEOUT_S = 600
RESTART_TIMEOUT_S = 1800
LATEST_TIMEOUT_S = 15.0
MAX_FAILURES_PER_NIGHT = 3
HEALTH_POLL_S = 90.0
HEALTH_STEP_S = 3.0
VERSION_TIMEOUT_S = 60
GIT_TIMEOUT_S = 300
TELL_TIMEOUT_S = 60
SUITE_COMMANDS = ("ar3", "a8s", "r4t", "k7e")

# What is worth a message: a change, a failure, or a pull that needs a person.
NOTIFY_OUTCOMES = frozenset({
    "updated", "rolled-back", "rollback-partial", "rollback-failed", "rollback-blocked", "failed", "skip-input",
})

# Told at most once a night, even when other outcomes come between.
ONCE_A_NIGHT = frozenset({"rollback-blocked"})

PUBLIC_ORIGIN = re.compile(r"github\.com[:/]witw-llc/ar3(\.git)?/?$")
# Where get.sh clones from when it turns a release unpack into a clone.
PUBLIC_REPO = "https://github.com/witw-llc/ar3.git"
LS_REMOTE_TIMEOUT_S = 60

# What git and ssh print when they wanted a person to type something.
NEEDS_INPUT = re.compile(
    r"terminal prompts disabled|could not read (?:Username|Password)|"
    r"Host key verification failed|publickey|password is required",
    re.IGNORECASE,
)

# Settings of the operator's shell that would point the installer somewhere
# other than the install being updated.
INSTALLER_OWNED_ENV = ("AR3_VERSION", "AR3_CHANNEL", "AR3_REPO", "AR3_SYSTEM", "AR3_BIN")

ENDS_NIGHT = frozenset({
    "skip-refused", "skip-pinned", "skip-channel", "skip-bad-release", "noop",
    "updated", "rolled-back", "rollback-failed",
})


def _stamp(now: datetime) -> str:
    return now.astimezone().isoformat(timespec="seconds")


@contextmanager
def _applied_env(env: dict[str, str]):
    """The captured scheduler settings, in effect for this process alone."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def shim(host, root: Path, name: str) -> str:
    return str(Path(root) / (f"{name}.cmd" if host.IS_WINDOWS else name))


class Night:
    """One attempt: the state it reads, the record it leaves."""

    def __init__(self, host, root: Path, state: dict, night: str, now: datetime, env: dict[str, str]):
        self.host, self.root, self.state, self.night, self.now, self.env = host, root, state, night, now, env

    def record(self, outcome: str, detail: str = "", *, frm=None, to=None) -> str:
        previous = self.state.get("last_result") or {}
        changed = (previous.get("outcome"), previous.get("night")) != (outcome, self.night)
        at = _stamp(self.now)
        self.state["last_result"] = {
            "at": at, "night": self.night, "outcome": outcome, "from": frm, "to": to, "detail": detail,
        }
        if self.state.get("pending"):
            # A night is never done while something is pending: the next tick finishes it.
            if self.state.get("done_night") == self.night:
                del self.state["done_night"]
        elif outcome in ENDS_NIGHT:
            self.state["done_night"] = self.night
        save(self.root, self.state)
        if changed:
            move = f" {frm} -> {to}" if frm or to else ""
            append_log(self.root, f"{at} {outcome}{move}{' ' + detail if detail else ''}")
            told = self.state.get("told") or {}
            if outcome in NOTIFY_OUTCOMES and told.get(outcome) != self.night:
                if outcome in ONCE_A_NIGHT:
                    self.state["told"] = {**told, outcome: self.night}
                    save(self.root, self.state)
                self.notify(outcome, detail, frm, to)
        return outcome

    def notify(self, outcome: str, detail: str, frm, to) -> None:
        """Tell the configured seat in one line. Best effort: a message that
        cannot be sent is logged and changes nothing else."""
        target = self.state.get("tell") or {}
        to_name, from_name = target.get("to"), target.get("from")
        if not to_name or not from_name:
            return
        move = f" {frm} -> {to}" if frm or to else ""
        text = " ".join(f"ar3 auto-update: {outcome}{move}: {detail}".split())[:300]
        cwd = ar3_nodes.read()[0].get(from_name, {}).get("root")
        try:
            if not isinstance(cwd, str) or not os.path.isabs(cwd):
                raise OSError(f"{from_name} is not a registered node")
            env = {k: v for k, v in os.environ.items() if k != TELL_OUTBOX_DIR_ENV}
            done = self.host.subprocess.run(
                [shim(self.host, self.root, "tell"), to_name, text], cwd=cwd, env=env,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                timeout=TELL_TIMEOUT_S, **launch_options(),
            )
            if done.returncode != 0:
                raise OSError(f"tell exited {done.returncode}")
        except (OSError, subprocess.SubprocessError) as e:
            append_log(self.root, f"{_stamp(self.now)} tell failed: {e}")


def run(host, *, now: datetime | None = None, force: bool = False, root: Path | None = None) -> str | None:
    """One tick of the trigger for the install at `root` (default: the one
    `host` runs from; the launcher names it, because it may run the copy an
    unfinished update left at `<root>.prev`). Returns the outcome it
    recorded, or None when the tick had nothing to do (outside the window,
    before the slot, the night already done, another tick running)."""
    root = Path(root or host.REPO_ROOT)
    try:
        state = load_strict(root)
    except StateError:
        # Writing a result here would replace the state, and a record in it.
        return None
    now = now or datetime.now()
    env = dict(state.get("env") or {})
    with _applied_env(env):
        if not state.get("enabled"):
            return Night(host, root, state, now.date().isoformat(), now, env).record("skip-off")
        try:
            window = parse_window(state.get("window") or DEFAULT_WINDOW)
        except ValueError:
            window = parse_window(DEFAULT_WINDOW)
        inside = current_night(window, now)
        if inside is not None:
            night_date = inside[0]
        else:
            night_date = now.date()
        night = night_date.isoformat()
        if not force:
            if inside is None or state.get("done_night") == night:
                return None
            if now < slot_time(root, window, night_date):
                return None
        with ExitStack() as stack:
            try:
                stack.enter_context(file_lock(lock_path(root), blocking=False))
            except BlockingIOError:
                return None
            try:
                state = load_strict(root)
            except StateError:
                return None
            if not force and state.get("done_night") == night:
                return None
            if not state.get("enabled"):
                return None
            return _attempt(Night(host, root, state, night, now, env))


def _version(root: Path) -> str:
    try:
        return (Path(root) / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _snapshot(host, root: Path) -> dict:
    """The tree at `root` as it is on disk: its kind, its commit or release
    tag, its VERSION, and whether git sees a change to a tracked file. A ref of
    None is a tree that is not there or cannot be read."""
    root = Path(root)
    if (root / ".git").exists():
        status = host._git_out(root, "status", "--porcelain", "--untracked-files=no")
        return {
            "kind": "git", "ref": host._git_out(root, "rev-parse", "HEAD"),
            "version": _version(root), "clean": status == "",
        }
    try:
        ref = (root / ".ar3-release").read_text(encoding="utf-8").strip() or None
    except OSError:
        ref = None
    return {"kind": "tarball", "ref": ref, "version": _version(root), "clean": True}


def _tree(disk: dict) -> dict:
    return {k: disk.get(k) for k in ("kind", "ref", "version")}


def _is(disk: dict, tree: dict | None) -> bool:
    """Whether the tree on disk is exactly `tree`: the same kind and commit or
    release tag, the same VERSION when `tree` names one, and no change to a
    tracked file. A tree that cannot be read is nothing."""
    if not tree or not disk.get("ref") or not disk.get("clean"):
        return False
    return all(disk.get(k) == tree[k] for k in ("kind", "ref", "version") if k in tree)


def _where(night: Night, pending: dict) -> str | None:
    """Which tree of `pending` is on disk: `previous`, `target`, `half` (a
    release unpack moved aside to `<root>.prev` with nothing in its place), or
    None: a tree auto-update did not make, which it never changes."""
    root = Path(night.root)
    previous = pending.get("previous")
    disk = _snapshot(night.host, root)
    if _is(disk, previous):
        return "previous"
    if _is(disk, pending.get("target")):
        return "target"
    if (
        not root.exists() and (previous or {}).get("kind") == "tarball"
        and _is(_snapshot(night.host, _prev(root)), previous)
    ):
        return "half"
    return None


def _installer_env(night: Night) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in INSTALLER_OWNED_ENV}
    env.update(night.env)
    env.update({
        "AR3_DIR": str(night.root),
        "AR3_NO_RESTART": "1",
        UPDATE_WAIT_ENV: "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
    })
    return env


def _has_git(env: dict[str, str]) -> bool:
    return shutil.which("git", path=env.get("PATH")) is not None


def _expected(night: Night, previous: dict, latest: str, env: dict[str, str]):
    """The tree the installer is to leave, read before it runs: the commit the
    remote's default branch is at (a clone pulls it; a release unpack that git
    turns into a clone clones it), or the release tag a release unpack is told
    to fetch. Returns (target, settings for the installer, the outcome and
    detail that stop here or None)."""
    tag = f"v{latest}"
    if previous["kind"] == "tarball" and not _has_git(env):
        return {"kind": "tarball", "ref": tag}, {"AR3_VERSION": tag}, None
    remote = "origin" if previous["kind"] == "git" else PUBLIC_REPO
    try:
        done = night.host.subprocess.run(
            ["git", "-C", str(night.root), "ls-remote", remote, "HEAD"], capture_output=True, text=True,
            errors="replace", timeout=LS_REMOTE_TIMEOUT_S, stdin=subprocess.DEVNULL, env=env,
            **launch_options(new_session=True),
        )
    except (OSError, subprocess.SubprocessError) as e:
        said, rows = str(e), []
    else:
        said = (done.stdout or "") + (done.stderr or "")
        rows = [ln.split() for ln in (done.stdout or "").splitlines()] if done.returncode == 0 else []
    sha = next((row[0] for row in rows if len(row) == 2 and row[1] == "HEAD"), None)
    if sha:
        return {"kind": "git", "ref": sha}, {}, None
    if NEEDS_INPUT.search(said):
        return None, {}, ("skip-input", "the remote wanted a password, a key or a host check; run `ar3 update` by hand once")
    return None, {}, ("skip-network", f"cannot read the commit {remote} is at")


def run_installer(host, root: Path, env: dict[str, str], timeout: float) -> tuple[int | None, str, bool]:
    """get.sh with no terminal to ask anything of: stdin is /dev/null and the
    run is its own session, so a prompt can neither read nor open a tty. Returns
    (exit code or None, combined output, timed out). A run past `timeout` is
    taken to be waiting for a person; its whole process group ends."""
    sh = host._posix_sh()
    if sh is None:
        return None, "no sh to run get.sh with", False
    try:
        proc = subprocess.Popen(
            [sh, str(root / "get.sh")], cwd=str(root), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", **launch_options(new_session=True),
        )
    except OSError as e:
        return None, f"cannot run get.sh: {e}", False
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", False
    except subprocess.TimeoutExpired:
        terminate_group(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.SubprocessError:
            out = ""
        return None, out or "", True


def _write_last_run(root: Path, text: str) -> None:
    try:
        atomic_write_text(last_run_path(root), text, mode=0o600)
    except OSError:
        pass


def _restart_nodes(night: Night) -> tuple[int | None, str]:
    """`a8s update --wait 0` of the tree at the install: it judges each handler
    group again at the moment it restarts it, and leaves a busy one alone."""
    host = night.host
    argv = [shim(host, night.root, "a8s"), "update", "--wait", "0"]
    try:
        done = host.subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=RESTART_TIMEOUT_S,
            stdin=subprocess.DEVNULL, env={**os.environ, **night.env}, **launch_options(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"cannot run a8s update: {e}"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def health(night: Night, target: str, expected: list[str]) -> tuple[bool, str]:
    """Whether the tree at `night.root` came up: each suite command answers
    `--version` with `target`, `a8s ps --json` parses, and every seat in
    `expected` (those running before the update) is running again, polled for
    up to HEALTH_POLL_S while restarted handlers come back. Local only; no
    network."""
    host = night.host
    env = {**os.environ, **night.env}
    for name in SUITE_COMMANDS:
        try:
            done = host.subprocess.run(
                [shim(host, night.root, name), "--version"], capture_output=True, text=True,
                errors="replace", timeout=VERSION_TIMEOUT_S, stdin=subprocess.DEVNULL, env=env,
                **launch_options(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"{name} --version did not run: {e}"
        if done.returncode != 0 or f"{name} {target} " not in (done.stdout or ""):
            said = " ".join(((done.stdout or "") + (done.stderr or "")).split())[:120]
            return False, f"{name} --version did not answer {target}: {said or done.returncode}"
    a8s = shim(host, night.root, "a8s")
    deadline = time.monotonic() + HEALTH_POLL_S
    while True:
        listed = host._ps_nodes(a8s)
        if listed is not None:
            missing = [n for n in expected if n not in {row["name"] for row in listed}]
            if not missing:
                return True, ""
            why = f"{', '.join(missing)} not running after the restart"
        else:
            why = "`a8s ps --json` did not answer"
        if time.monotonic() >= deadline:
            return False, why
        time.sleep(HEALTH_STEP_S)


def rollback(night: Night, previous: dict) -> tuple[bool, str]:
    """Put the tree back to `previous`: a git install resets to the recorded
    commit; a release unpack swaps `<root>.prev` back, also when the installer
    had already moved the unpack aside or replaced it with a clone. Reached
    only through `_do`, which has just checked both trees."""
    if not previous.get("ref"):
        return False, "no record of the previous tree"
    if previous.get("kind") == "git":
        return _git_reset(night, previous["ref"])
    return _swap_back(night)


def _git_reset(night: Night, ref: str) -> tuple[bool, str]:
    try:
        done = night.host.subprocess.run(
            ["git", "-C", str(night.root), "reset", "--hard", ref], capture_output=True, text=True,
            errors="replace", timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL, **launch_options(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"git reset failed: {e}"
    if done.returncode != 0:
        return False, f"git reset exited {done.returncode}: {' '.join((done.stderr or '').split())[:120]}"
    return True, ""


def _swap_back(night: Night) -> tuple[bool, str]:
    """The install moves aside before `.prev` moves in, so a run that ends
    between the two moves leaves the one half state `_where` knows."""
    root = Path(night.root)
    prev, bad = _prev(root), Path(str(root) + ".bad")
    if not prev.is_dir():
        return False, f"{prev} is gone"
    try:
        shutil.rmtree(bad, ignore_errors=True)
        moved = root.exists()
        if moved:
            os.rename(root, bad)
        try:
            os.rename(prev, root)
        except OSError:
            if moved:
                os.rename(bad, root)
            raise
        shutil.rmtree(bad, ignore_errors=True)
    except OSError as e:
        return False, f"cannot swap {prev} back: {e}"
    return True, ""


def _prev(root: Path) -> Path:
    return Path(str(root) + ".prev")


def _source_is(night: Night, previous: dict) -> bool:
    """Whether the tree a restore would bring back is exactly `previous`: the
    recorded commit exists in the clone, or `<root>.prev` is the recorded
    release unpack (tag and VERSION)."""
    if previous.get("kind") == "git":
        ref = previous.get("ref")
        return bool(ref) and night.host._git_out(
            night.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"
        ) == ref
    return _is(_snapshot(night.host, _prev(night.root)), previous)


def _guard(night: Night, on_disk: tuple[str, ...], restore: bool) -> tuple[bool, str] | None:
    """Re-read both trees a step depends on. None clears the step: the install
    is exactly one of `on_disk` of the pending record (`_where`) and, for a
    restore, the tree it brings back is exactly the recorded previous tree.
    Otherwise (foreign, why): foreign when the install itself is not a tree
    auto-update made, so its record ends."""
    pending = night.state.get("pending") or {}
    where = _where(night, pending)
    if where not in on_disk:
        return True, (
            f"the tree on disk is {where or 'not one auto-update made'}, not the "
            f"{' or '.join(on_disk)} tree this step needs; it is left alone"
        )
    if restore and not _source_is(night, pending["previous"]):
        previous = pending["previous"]
        prev = _prev(Path(night.root))
        if previous.get("kind") == "git":
            pending["blocked"] = "the recorded commit is missing"
        elif not prev.is_dir():
            pending["blocked"] = f"{prev} is missing"
        else:
            pending["blocked"] = f"{prev} is not the recorded release"
        return False, (
            f"{pending['blocked']}: the tree to put back is not {previous.get('ref')} "
            f"({previous.get('version')}); nothing is changed"
        )
    return None


def _do(night: Night, on_disk: tuple[str, ...], restore: bool, step):
    """The one door to every step that changes the install or restarts seats
    onto it: the installer, a put-back or rollback, and `a8s update`.
    `_guard` reads the trees on disk immediately before `step` runs. Returns
    (True, what step returned) or (False, why it did not run)."""
    stop = _guard(night, on_disk, restore)
    if stop is not None:
        return False, stop
    return True, step()


def _refused(night: Night, stop: tuple[bool, str]) -> str:
    """A step `_guard` stopped. A rollback that cannot restore its tree is
    `rollback-blocked`, told once a night: the seats may be on a release
    that failed its health check. Anything else is `skip-unknown`."""
    foreign, why = stop
    pending = night.state.get("pending") or {}
    if foreign:
        night.state.pop("pending", None)
    elif pending.get("stage") == "rollback":
        return night.record(
            "rollback-blocked", f"{_failed(pending)}; {why}",
            frm=pending["previous"].get("version"), to=pending["target"].get("version"),
        )
    return night.record("skip-unknown", why, frm=pending.get("from"), to=pending.get("to"))


def _restored(night: Night, previous: dict) -> bool:
    return _is(_snapshot(night.host, night.root), previous)


def _busy(night: Night) -> tuple[tuple[str, str] | None, list[str]]:
    """Whether every seat is idle: (the outcome and detail that stop here, or
    None; the running seats). A seat that cannot be asked about is not idle."""
    host = night.host
    a8s = host._a8s()
    if a8s is None:
        return ("skip-unknown", "no a8s to ask which seats are busy"), []
    nodes = host._ps_nodes(a8s)
    if nodes is None:
        return ("skip-unknown", "`a8s ps` could not say which seats are busy"), []
    held = host._held_by(nodes)
    if held:
        return ("skip-busy", ", ".join(sorted(held)) + " not idle"), []
    return None, [n["name"] for n in nodes]


def _put_back(night: Night, pending: dict, outcome: str, why: str) -> str:
    """Restore the tree an installer changed and did not finish. The seats
    never left the old code, so the files alone go back. A tree that cannot
    be restored keeps the record, and the next tick tries again."""
    previous = pending["previous"]
    frm, to = pending.get("from"), pending.get("to")
    ok, got = _do(night, ("target", "half"), True, lambda: rollback(night, previous))
    if not ok:
        return _refused(night, got)
    restored, how = got
    if not restored:
        return night.record("rollback-failed", f"{why}; cannot put the tree back: {how}", frm=frm, to=to)
    if not _restored(night, previous):
        return night.record("rollback-failed", f"{why}; the tree on disk is not {previous.get('version')} after the put-back", frm=frm, to=to)
    night.state.pop("pending", None)
    return night.record(outcome, f"{why}; the tree is put back at {previous.get('version')}", frm=frm, to=to)


def _restart(night: Night, pending: dict, outputs: list[str]) -> str:
    """Restart the seats onto the installed tree and check it against every
    seat that ran before the update. A seat held busy, or a restart that
    failed, keeps the record for the next idle tick; a failed check rolls
    back."""
    target = pending["target"]
    frm, to = pending.get("from"), target.get("version")
    ok, got = _do(night, ("target",), False, lambda: _restart_nodes(night))
    if not ok:
        return _refused(night, got)
    rc, output = got
    outputs.append("--- a8s update ---\n" + output)
    _write_last_run(night.root, "\n".join(outputs))
    seats = pending.get("seats") or []
    ok, why = health(night, to or "", seats)
    if not ok:
        return _roll_back(night, pending, why, outputs)
    if rc is None or rc != 0:
        return night.record("failed", f"a8s update exited {rc}" if rc is not None else output.strip(), frm=frm, to=to)
    if "left on its current handler" in output:
        return night.record("partial", "a seat that turned busy was left on its old code", frm=frm, to=to)
    night.state.pop("pending", None)
    night.state["failures"] = {}
    return night.record("updated", f"{len(seats)} seat(s) restarted", frm=frm, to=to)


def _roll_back(night: Night, pending: dict, why: str, outputs: list[str]) -> str:
    state = night.state
    state["bad_release"] = pending["target"].get("version")
    pending.update(stage="rollback", why=why)
    save(night.root, state)
    stop, _running = _busy(night)
    if stop:
        previous = pending["previous"].get("version")
        return night.record(
            "rollback-partial", f"{_failed(pending)}; {stop[1]}, so {previous} goes back on the next idle tick",
            frm=previous, to=state["bad_release"],
        )
    return _finish_rollback(night, pending, outputs)


def _failed(pending: dict) -> str:
    return f"{pending['target'].get('version')} failed its health check ({pending.get('why')})"


def _finish_rollback(night: Night, pending: dict, outputs: list[str]) -> str:
    """Put the previous tree on disk when it is not there yet, restart the
    seats onto it, and check it against every seat that ran before the
    update. The rollback is done only when that restart succeeded and held no
    seat on the failed release; until then the record stays and the next idle
    tick goes on from the tree on disk."""
    previous = pending["previous"]
    target = pending["target"].get("version")
    frm = previous.get("version")
    head = _failed(pending)
    if _where(night, pending) != "previous":
        ok, got = _do(night, ("target", "half"), True, lambda: rollback(night, previous))
        if not ok:
            return _refused(night, got)
        restored, how = got
        if not restored:
            return night.record("rollback-failed", f"{head}; {how}", frm=frm, to=target)
        if not _restored(night, previous):
            return night.record("rollback-failed", f"{head}; the tree on disk is not {frm} after the rollback", frm=frm, to=target)
    ok, got = _do(night, ("previous",), False, lambda: _restart_nodes(night))
    if not ok:
        return _refused(night, got)
    rc, output = got
    outputs.append("--- rollback: a8s update ---\n" + output)
    _write_last_run(night.root, "\n".join(outputs))
    if rc is None or rc != 0:
        said = f"a8s update exited {rc}" if rc is not None else output.strip()
        return night.record(
            "rollback-partial", f"{head}; {frm} is back on disk, but {said}; the next idle tick restarts the seats",
            frm=frm, to=target,
        )
    if "left on its current handler" in output:
        return night.record(
            "rollback-partial", f"{head}; {frm} is back on disk, but a busy seat is still on {target}; "
            "the next idle tick restarts it", frm=frm, to=target,
        )
    night.state.pop("pending", None)
    ok, why_again = health(night, frm or "", pending.get("seats") or [])
    if ok:
        return night.record("rolled-back", head, frm=frm, to=target)
    return night.record(
        "rollback-failed", f"{head}; the previous tree did not come up either ({why_again})", frm=frm, to=target,
    )


def _recover(
    night: Night, outcome: str = "failed",
    why: str = "an earlier run ended while the installer was changing the tree",
) -> str | None:
    """Finish what an earlier run, or this run's installer, left pending,
    judged by the tree on disk. None when nothing is pending or an install
    never changed the tree. A tree auto-update did not make, or a restart
    whose installed tree is gone, is left alone: the record is dropped and
    nothing changes. Nothing is put back or restarted while a seat is busy
    or cannot be asked about."""
    pending = night.state.get("pending")
    if not isinstance(pending, dict):
        return None
    stage = pending.get("stage")
    where = _where(night, pending)
    if where is None or stage not in ("install", "restart", "rollback") or (stage == "restart" and where != "target"):
        night.state.pop("pending")
        return night.record("skip-unknown", "the tree on disk is not one auto-update made; it is left alone")
    if stage == "install" and where == "previous":
        night.state.pop("pending")
        save(night.root, night.state)
        return None
    stop, _running = _busy(night)
    if stop:
        return night.record(*stop)
    if stage == "install":
        return _put_back(night, pending, outcome, why)
    if stage == "restart":
        return _restart(night, pending, [])
    return _finish_rollback(night, pending, [])


def _attempt(night: Night) -> str:
    host, root, state = night.host, night.root, night.state

    left = _recover(night)
    if left is not None:
        return left

    refusal = host.update_refusal(root)
    if refusal:
        return night.record("skip-refused", refusal)
    if (root / ".git").exists():
        if host._git_out(root, "symbolic-ref", "-q", "HEAD") is None:
            return night.record("skip-pinned", "this install is pinned to a release tag")
        origin = host._git_out(root, "remote", "get-url", "origin")
        if not origin or not PUBLIC_ORIGIN.search(origin.strip()):
            return night.record("skip-channel", "this install does not follow the public releases")
    elif not (root / ".ar3-release").is_file():
        return night.record("skip-channel", "this tree is neither a clone nor a release unpack")

    installed = _version(root)
    latest = ar3ver.latest_public_version(LATEST_TIMEOUT_S)
    state["last_check"] = {"at": _stamp(night.now), "latest": latest, "ok": latest is not None}
    save(root, state)
    if latest is None:
        return night.record("skip-network", "the public releases could not be reached")
    have, want = ar3ver.parse_version(installed), ar3ver.parse_version(latest)
    if have is None or want is None:
        return night.record("failed", f"cannot compare {installed!r} with {latest!r}", frm=installed, to=latest)
    if have >= want:
        return night.record("noop", f"already at {installed}", frm=installed, to=installed)
    if latest == state.get("bad_release"):
        return night.record("skip-bad-release", f"{latest} failed its health check here", frm=installed, to=latest)

    stop, running = _busy(night)
    if stop:
        return night.record(*stop)

    disk = _snapshot(host, root)
    if not disk.get("ref") or not disk.get("clean"):
        return night.record(
            "skip-unknown", "cannot read the current commit or release tag to roll back to",
            frm=installed, to=latest,
        )
    previous = _tree(disk)
    env = _installer_env(night)
    target, extra, stop = _expected(night, previous, latest, env)
    if stop:
        return night.record(*stop, frm=installed, to=latest)
    pending = {
        "stage": "install", "previous": previous, "target": target, "seats": running,
        "from": installed, "to": latest,
    }
    state["pending"] = pending
    save(root, state)
    ok, got = _do(night, ("previous",), False, lambda: run_installer(host, root, {**env, **extra}, PULL_TIMEOUT_S))
    if not ok:
        return _refused(night, got)
    rc, output, timed_out = got
    outputs = [output]
    _write_last_run(root, output)
    where = _where(night, pending)
    if timed_out or rc != 0:
        if timed_out or NEEDS_INPUT.search(output):
            outcome = "skip-input"
            detail = "the pull wanted a password, a key or a host check; run `ar3 update` by hand once"
        else:
            failures = state.get("failures") or {}
            count = (failures.get("count", 0) + 1) if failures.get("night") == night.night else 1
            state["failures"] = {"night": night.night, "count": count}
            if count >= MAX_FAILURES_PER_NIGHT:
                state["done_night"] = night.night
            outcome, detail = "failed", f"the installer exited {rc}"
        if where == "previous":
            state.pop("pending")
            return night.record(outcome, detail, frm=installed, to=latest)
        return _recover(night, outcome, detail)
    if where == "previous":
        state.pop("pending")
        return night.record("noop", f"the pull found nothing newer than {installed}", frm=installed, to=installed)
    if where != "target":
        return _recover(night, "failed", "the installer left the tree moved aside")
    pending.update(stage="restart", target=_tree(_snapshot(host, root)))
    save(root, state)
    return _restart(night, pending, outputs)


class HandoffError(Exception):
    """A manual update cannot take this install over from auto-update. The
    message is one line: what failed, the path, and the way out."""


def end_pending(root: Path, why: str) -> None:
    """Drop what auto-update had pending for this install, durably. The
    caller holds the lock. Raises HandoffError when the state cannot be read
    or written; a state file that does not exist has nothing pending."""
    path = state_path(root)
    try:
        state = load_strict(root)
    except StateError as e:
        raise HandoffError(
            f"{e}; fix permissions on {path}, or remove it to discard auto-update recovery"
        ) from e
    pending = state.pop("pending", None)
    if not pending:
        return
    try:
        save(root, state)
    except OSError as e:
        raise HandoffError(
            f"cannot write the auto-update state {path}: {e}; fix permissions on {path.parent}, "
            f"or remove {path} to discard auto-update recovery"
        ) from e
    try:
        append_log(
            root, f"{_stamp(datetime.now())} recovery cleared: {why}; auto-update no longer "
            f"finishes the {pending.get('stage')} an earlier run left",
        )
    except OSError:
        pass


def handoff(root: Path) -> None:
    """`get.sh` run by a person has installed or updated this tree: end what
    auto-update had pending, so no tick undoes or restarts their update. When
    the lock is held, its holder decides: a manual `ar3 update` (which runs
    get.sh) ends it itself, and a running tick is not interrupted. A state
    that cannot be read or written is said on stderr."""
    root = Path(root)
    try:
        with file_lock(lock_path(root), blocking=False):
            end_pending(root, "get.sh ran by hand")
    except HandoffError as e:
        print(f"ar3 update: {e}", file=sys.stderr)
    except OSError:
        pass


@contextmanager
def manual_update(root: Path):
    """A person's `ar3 update` of this install, run inside this block. Before
    the block runs it takes the lock a tick takes (so no tick runs beside
    it) and ends, durably, whatever an earlier run left pending: the tree and
    the seats are the person's from then on, so no tick undoes, restarts or
    rolls back their update, even one that is interrupted or whose process
    dies. When the lock cannot be taken or the state cannot be read or
    written, it raises HandoffError before the block runs."""
    root = Path(root)
    path = lock_path(root)
    with ExitStack() as stack:
        try:
            state_dir().mkdir(parents=True, exist_ok=True)
            stack.enter_context(file_lock(path))
        except OSError as e:
            raise HandoffError(
                f"cannot take the auto-update lock {path}: {e}; fix permissions on {path} "
                f"(or its directory), or remove it"
            ) from e
        end_pending(root, "a manual `ar3 update` started")
        yield


# ---------- status and the command ----------

def _tonight(root: Path, window: tuple[int, int], now: datetime):
    for back in (-1, 0, 1):
        day = now.date() + timedelta(days=back)
        if night_for(window, day)[1] > now:
            return day
    return now.date()


def status(host, now: datetime | None = None) -> dict:
    root = Path(host.REPO_ROOT)
    now = now or datetime.now()
    state = load(root)
    enabled = state.get("enabled")
    window_text = state.get("window") or DEFAULT_WINDOW
    try:
        window = parse_window(window_text)
    except ValueError:
        window_text, window = DEFAULT_WINDOW, parse_window(DEFAULT_WINDOW)
    trigger = {"kind": None, "installed": None, "error": None}
    try:
        scheduler = _scheduler(root)
        trigger["kind"] = scheduler.name
        trigger["installed"] = scheduler.installed(install_id(root)) is not None
    except trigger_mod.TriggerError as e:
        trigger["error"] = str(e)
    day = _tonight(root, window, now)
    slot = slot_time(root, window, day)
    attempt = next_attempt(root, state, now)
    if enabled is False:
        word = "off"
    elif enabled and trigger["installed"]:
        word = "on"
    elif enabled:
        word = "on, but its trigger is missing"
    else:
        word = "not armed"
    return {
        "install": str(root),
        "id": install_id(root),
        "version": host._suite_version(),
        "state": word,
        "enabled": bool(enabled),
        "writable": writable(root),
        "window": window_text,
        "trigger": trigger,
        "tonight": {"night": day.isoformat(), "slot": slot.strftime("%H:%M"), "done": state.get("done_night") == day.isoformat()},
        "last_check": state.get("last_check"),
        "last_result": state.get("last_result"),
        "recovery": (state.get("pending") or {}).get("stage"),
        "blocked": _blocked(state),
        "bad_release": state.get("bad_release"),
        "next_attempt": attempt.astimezone().isoformat(timespec="minutes") if attempt else None,
        "tell": state.get("tell") or None,
    }


def _local(text) -> str:
    return clock.stamp(text) if text else "never"


def render_status(data: dict) -> list[str]:
    lines = [f"auto-update: {data['state']}"]
    if not data["writable"]:
        lines.append("  this install is not writable by this account; whoever owns it updates it")
    lines.append(f"  install       {data['install']}  (suite {data['version']})")
    lines.append(f"  window        {data['window']} local")
    trig = data["trigger"]
    if trig["error"]:
        lines.append(f"  trigger       unavailable: {trig['error']}")
    elif trig["installed"] is None:
        lines.append("  trigger       unknown")
    else:
        lines.append(f"  trigger       {trig['kind']}: {'installed' if trig['installed'] else 'not installed'}")
    night = data["tonight"]
    lines.append(f"  tonight       {night['night']}, first try at {night['slot']}{' (done)' if night['done'] else ''}")
    check = data["last_check"]
    if check:
        said = f"latest {check.get('latest')}" if check.get("ok") else "could not reach the public releases"
        lines.append(f"  last check    {_local(check.get('at'))}, {said}")
    else:
        lines.append("  last check    never")
    result = data["last_result"]
    if result:
        move = f" {result.get('from')} -> {result.get('to')}" if result.get("from") or result.get("to") else ""
        detail = f" ({result['detail']})" if result.get("detail") else ""
        lines.append(f"  last result   {result.get('outcome')}{move}{detail} at {_local(result.get('at'))}")
    else:
        lines.append("  last result   none yet")
    if data["recovery"] == "install":
        lines.append("  recovery      an installer run did not finish; the next idle tick puts the tree back")
    elif data["recovery"] == "restart":
        lines.append("  recovery      a seat is still on the old code; the next idle tick restarts it")
    elif data["recovery"] == "rollback":
        if data["blocked"]:
            lines.append(f"  recovery      rollback blocked: {data['blocked']}; the seats may be on "
                         f"{data['bad_release']}; `ar3 update` by hand ends it")
        else:
            lines.append("  recovery      a rollback is not finished; the next idle tick finishes it")
    if data["bad_release"]:
        lines.append(f"  skipping      {data['bad_release']} failed its health check here")
    lines.append(f"  next attempt  {_local(data['next_attempt']) if data['next_attempt'] else 'none'}")
    tell = data["tell"]
    lines.append(f"  tell          {tell['to']} (from {tell['from']})" if tell else "  tell          nobody")
    return lines


def _blocked(state: dict) -> str | None:
    """Why a pending rollback cannot restore its tree, or None."""
    pending = state.get("pending") or {}
    return pending.get("blocked") if pending.get("stage") == "rollback" else None


def status_line(host) -> str:
    """One line for `ar3` and `ar3 doctor`, read from the state file alone."""
    try:
        state = load_strict(Path(host.REPO_ROOT))
    except StateError as e:
        return f"auto-update: {e}; fix its permissions, or remove it to discard auto-update recovery"
    blocked = _blocked(state)
    if blocked:
        return f"auto-update: rollback blocked ({blocked}); see `ar3 update --auto`"
    if state.get("enabled"):
        return f"auto-update: on, nightly {state.get('window') or DEFAULT_WINDOW} local"
    if state.get("enabled") is False:
        return "auto-update: off (turn on: ar3 update --auto on)"
    if not writable(Path(host.REPO_ROOT)):
        return "auto-update: not armed; this install is not writable by this account"
    return "auto-update: not armed (try: ar3 update --auto on)"


def _fail(message: str) -> int:
    print(f"ar3 update: {message}", file=sys.stderr)
    return 2


def cmd_auto(args, host) -> int:
    root = Path(host.REPO_ROOT)
    action = args.auto
    if args.engines or args.all_engines:
        return _fail("--auto and --engine/--all-engines do not combine; auto-update never touches engines")
    if getattr(args, "now", False) and action != "run":
        return _fail("--now goes with --auto run")
    if getattr(args, "json", False) and action != "status":
        return _fail("--json goes with --auto (the status)")
    state_home = getattr(args, "state_home", None)
    install = getattr(args, "install_root", None)
    if (state_home or install) and action != "run":
        return _fail("--state-home and --install go with --auto run")
    window = getattr(args, "window", None)
    tell_to = getattr(args, "tell", None)
    tell_from = getattr(args, "tell_from", None)
    if (window or tell_to or tell_from) and action in ("run", "ensure", "off"):
        return _fail("--window, --tell and --from go with --auto or --auto on")
    if window:
        try:
            parse_window(window)
        except ValueError as e:
            return _fail(str(e))
    if tell_from and not tell_to:
        return _fail("--from goes with --tell")
    if tell_to and tell_to.lower() != "none":
        if not tell_from:
            return _fail("--tell NAME needs --from NODE, the registered node that sends the message")
        if tell_from not in ar3_nodes.read()[0]:
            return _fail(f"no node named {tell_from!r} in the a8s registry (see `a8s ls`)")
    if window or tell_to:
        state = load(root)
        state["install"] = str(root.resolve())
        if window:
            state["window"] = window
        if tell_to:
            if tell_to.lower() == "none":
                state.pop("tell", None)
            else:
                state["tell"] = {"to": tell_to, "from": tell_from}
        save(root, state)

    if action == "run":
        with _applied_env({"AR3_HOME": state_home} if state_home else {}):
            if install:
                root = Path(install)
            outcome = run(host, force=bool(getattr(args, "now", False)), root=root)
            result = (load(root).get("last_result") or {}) if outcome else {}
        if getattr(args, "now", False):
            detail = f": {result['detail']}" if result.get("detail") else ""
            print(f"auto-update: {outcome or 'nothing to do'}{detail}")
        return 0
    if action == "ensure":
        # get.sh ends a person's install or update with this.
        handoff(root)
        did = ensure(root)
        if did == "armed":
            print(f"auto-update: on, nightly {load(root).get('window')} local; change or stop it with `ar3 update --auto`")
        elif did == "unavailable":
            print("auto-update: not armed, this machine has no scheduler to use (try: ar3 update --auto on)", file=sys.stderr)
        return 0
    if action == "on":
        try:
            note = arm(root)
        except trigger_mod.TriggerError as e:
            print(f"ar3 update: cannot arm auto-update: {e}", file=sys.stderr)
            return 1
        print(f"auto-update: on, nightly {load(root).get('window')} local" + (f" ({note})" if note else ""))
        return 0
    if action == "off":
        try:
            disarm(root)
        except trigger_mod.TriggerError as e:
            print(f"ar3 update: cannot remove the auto-update trigger: {e}", file=sys.stderr)
            return 1
        print("auto-update: off")
        return 0
    data = status(host)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        for line in render_status(data):
            print(line)
    return 0
