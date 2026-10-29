"""Per-user node autostart: intent state plus one native startup entry per node.

No supervisor runs. The OS starts a node at the host's next startup event
(Linux cron `@reboot`, macOS LaunchAgent at login, Windows logon task) and the
node's own claim decides whether that start is still wanted.

State lives in `<a8s home>/autostart.json`, one record per concrete node:

    preference  the operator opted in; survives an explicit stop
    armed       a startup event may start the node
    epoch       identity of the current arming; an entry carries the epoch it
                was written with, and a claim whose epoch is not the record's
                is refused, so a queued or stale entry cannot revive a node
                that was stopped, removed or re-created since

Every read-modify-write holds `<a8s home>/autostart.lock`, and the claim at the
node's pid file holds it too, so a stop that disarms before it reads the
handler pid either sees the claim or beats it.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape, unescape as xml_unescape

from ar3.fsio import atomic_write_text
from ar3.locking import file_lock
from ar3.proc import launch_options
from ar3.ulid import new as new_ulid

from core import ENTRYPOINT, resolve_a8s_home

IS_WINDOWS = os.name == "nt"

# Proxy and CA settings a startup environment would otherwise lose. They are
# kept in a mode-0600 file under the config home, never in the OS entry,
# because a proxy URL may carry credentials.
STARTUP_ENV_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)


class AutostartError(Exception):
    """A startup entry could not be inspected, written or removed."""


class AutostartUnavailable(AutostartError):
    """The platform has no startup facility to inspect: nothing can be owned
    there, which is not the same as a facility that failed."""


def _account_lock_root() -> Path:
    """A private directory of the OS account itself, found from the account
    database and never from HOME, TMPDIR, LOGNAME or the config home, so every
    config home of the account reaches the same lock. Tests replace this
    function; nothing else may."""
    import pwd

    uid = os.getuid()
    root = Path(pwd.getpwuid(uid).pw_dir) / ".a8s-locks"
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        st = root.lstat()
    except OSError as e:
        raise AutostartError(f"cannot prepare the account lock directory {root}: {e}") from e
    if root.is_symlink() or st.st_uid != uid or st.st_mode & 0o077:
        raise AutostartError(f"{root} is not a private directory owned by this account")
    return root


account_lock_root = _account_lock_root


def home_dir() -> Path:
    return resolve_a8s_home()


def home_id() -> str:
    """Stable id of this config home, so two homes never share an entry."""
    return hashlib.sha256(str(home_dir().resolve()).encode()).hexdigest()[:10]


def state_path() -> Path:
    return home_dir() / "autostart.json"


def lock_path() -> Path:
    return home_dir() / "autostart.lock"


def env_path() -> Path:
    return home_dir() / "autostart.env"


def _lock():
    return file_lock(lock_path())


def _read_state(strict: bool = False) -> dict:
    """The saved nodes. A missing file is empty. Lenient reads treat any other
    failure as empty; strict reads, used where an explicit shutdown must not
    mistake unreadable state for nothing armed, raise AutostartError."""
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        if strict:
            raise AutostartError(
                f"startup state {path} cannot be read, the node may start again at boot or login: {e}"
            ) from e
        return {}
    nodes = data.get("nodes") if isinstance(data, dict) else None
    if isinstance(nodes, dict) and (not strict or all(isinstance(rec, dict) for rec in nodes.values())):
        return nodes
    if strict:
        raise AutostartError(
            f"startup state {path} is malformed, the node may start again at boot or login"
        )
    return {}


def _write_state(nodes: dict) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        state_path(), json.dumps({"nodes": nodes}, indent=2, sort_keys=True) + "\n"
    )


def record(node: str) -> dict | None:
    """The saved intent of one node, or None."""
    rec = _read_state().get(node.lower())
    return dict(rec) if isinstance(rec, dict) else None


def opted_in(strict: bool = False) -> list[str]:
    """Every node whose preference is set, in this config home."""
    return sorted(n for n, r in _read_state(strict=strict).items() if isinstance(r, dict) and r.get("preference"))


def _prune(state: dict, key: str) -> None:
    rec = state.get(key)
    if isinstance(rec, dict) and not (rec.get("preference") or rec.get("armed") or rec.get("manual")):
        state.pop(key)


def new_manual_claim(nodes: list[str]) -> str:
    """Reserve a fresh token for one explicit start of these concrete nodes.
    The detached child presents it at its pid claim; a later explicit start,
    stop, kill, exit or removal replaces or clears it, so a child that was
    spawned and not yet attached cannot outlive the operator's newer intent."""
    token = new_ulid()
    with _lock():
        state = _read_state()
        for node in nodes:
            key = node.lower()
            state[key] = {**(state.get(key) or {}), "manual": token}
        _write_state(state)
    return token


def invalidate_manual(nodes: list[str] | None = None) -> None:
    """Cancel pending manual claims of `nodes` (every node when None), whether
    or not a pid file or a startup preference exists. Raises AutostartError
    when the state cannot be locked or written."""
    try:
        with _lock():
            state = _read_state(strict=True)
            changed = False
            for key in list(state) if nodes is None else [n.lower() for n in nodes]:
                rec = state.get(key)
                if isinstance(rec, dict) and rec.get("manual"):
                    state[key] = {k: v for k, v in rec.items() if k != "manual"}
                    _prune(state, key)
                    changed = True
            if changed:
                _write_state(state)
    except OSError as e:
        raise AutostartError(f"startup state not updated, a pending start may still attach: {e}") from e


def armed_nodes(nodes: list[str]) -> list[str]:
    state = _read_state()
    return [n for n in nodes if (state.get(n.lower()) or {}).get("armed")]


# ---------- entry spec ----------

@dataclass(frozen=True)
class EntrySpec:
    node: str
    epoch: str
    home: str
    python: str
    entrypoint: str
    foreground: bool

    @property
    def argv(self) -> list[str]:
        """The command the OS runs. Cron starts a detached node and returns;
        a login job owns the node in the foreground with no wrapper. Both
        claim without takeover, and both name their config home explicitly."""
        verb = ["run", "--no-takeover"] if self.foreground else ["start"]
        return [
            self.python, self.entrypoint, *verb,
            "--startup", self.epoch, "--startup-home", self.home, self.node,
        ]


@dataclass(frozen=True)
class Entry:
    node: str
    epoch: str | None
    detail: str = ""
    command: tuple[str, ...] | None = None


def _spec(backend: "Backend", node: str, epoch: str) -> EntrySpec:
    return EntrySpec(
        node=node, epoch=epoch, home=str(home_dir().resolve()),
        python=backend.launcher(), entrypoint=str(ENTRYPOINT), foreground=backend.foreground,
    )


def _run(argv: list[str], *, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, input=input, capture_output=True, text=True, timeout=60,
            **launch_options(windows=IS_WINDOWS),
        )
    except FileNotFoundError as e:
        raise AutostartError(f"{argv[0]} is not installed") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AutostartError(f"{argv[0]} failed: {e}") from e


# ---------- backends ----------

def _epoch_of(argv) -> str | None:
    """The value after `--startup`, or None when absent or truncated."""
    try:
        i = list(argv).index("--startup")
        return str(argv[i + 1])
    except (ValueError, IndexError):
        return None


class Backend:
    name = ""
    event = ""
    foreground = False

    def launcher(self) -> str:
        """The interpreter an entry runs."""
        return sys.executable

    def check(self) -> None:
        """Raise AutostartError unless entries can be written."""

    def install(self, spec: EntrySpec) -> None:
        raise NotImplementedError

    def remove(self, node: str, unload: bool = False) -> None:
        """Delete the entry. `unload` is the final removal of a stopped node:
        a scheduler that keeps a loaded copy drops it too. Without it, the
        entry's future runs end and a running node is left alone."""
        raise NotImplementedError

    def loaded(self, node: str) -> bool:
        """Whether the scheduler still holds a loaded job for the node."""
        return False

    def entries(self) -> dict[str, Entry]:
        """Owned entries of this config home, by node."""
        raise NotImplementedError


_CRON_TAG = "a8s-autostart"
_CRON_RE = re.compile(
    rf"^@reboot (?P<cmd>.*) # {_CRON_TAG} home=(?P<home>\w+) node=(?P<node>\S+)$"
)


def _cron_running() -> bool:
    proc = Path("/proc")
    if not proc.is_dir():
        return True
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() in ("cron", "crond", "cronie", "fcron"):
                return True
        except OSError:
            continue
    return False


class CronBackend(Backend):
    name = "cron"
    event = "boot (@reboot, when the cron service starts)"
    foreground = False

    def check(self) -> None:
        if shutil.which("crontab") is None:
            raise AutostartUnavailable(
                "autostart on Linux needs cron, and `crontab` is not installed; "
                "install a cron package (a8s changes no system setting)"
            )

    def _read(self) -> str:
        proc = _run(["crontab", "-l"])
        if proc.returncode == 0:
            return proc.stdout
        if "no crontab" in (proc.stderr or "").lower():
            return ""
        raise AutostartError(f"crontab -l failed: {(proc.stderr or '').strip() or proc.returncode}")

    def _line(self, spec: EntrySpec) -> str:
        cmd = " ".join(shlex.quote(a) for a in spec.argv).replace("%", "\\%")
        return f"@reboot {cmd} # {_CRON_TAG} home={home_id()} node={spec.node}"

    @staticmethod
    def _owned(line: str, node: str | None = None) -> bool:
        m = _CRON_RE.match(line)
        return bool(m) and m["home"] == home_id() and (node is None or m["node"] == node)

    def _rewrite(self, drop_node: str, add: str | None) -> None:
        # Every config home of this account edits the same crontab.
        with file_lock(account_lock_root() / "crontab.lock"):
            self._rewrite_locked(drop_node, add)

    def _rewrite_locked(self, drop_node: str, add: str | None) -> None:
        for _ in range(2):
            before = self._read()
            lines = before.splitlines()
            kept = [ln for ln in lines if not self._owned(ln, drop_node)]
            if add is not None:
                kept.append(add)
            if self._read() == before:
                break
        else:
            raise AutostartError("crontab changed while it was being updated; try again")
        if kept == lines and (add is None or add in lines):
            return
        backup = home_dir() / "autostart-crontab.bak"
        if before:
            atomic_write_text(backup, before, mode=0o600)
        if kept:
            proc = _run(["crontab", "-"], input="\n".join(kept) + "\n")
        elif before:
            proc = _run(["crontab", "-r"])
        else:
            return
        if proc.returncode != 0:
            raise AutostartError(f"crontab rejected the update: {(proc.stderr or '').strip() or proc.returncode}")
        after = self._read().splitlines()
        unrelated_before = [ln for ln in lines if not self._owned(ln, drop_node)]
        unrelated_after = [ln for ln in after if not self._owned(ln, drop_node) or ln == add]
        if [ln for ln in unrelated_after if ln != add] != unrelated_before:
            raise AutostartError(
                f"crontab changed during the update; the prior copy is {backup}"
            )
        if (add is not None) != any(ln == add for ln in after):
            raise AutostartError("crontab does not hold the expected entry after the update")

    def install(self, spec: EntrySpec) -> None:
        self.check()
        if not _cron_running():
            print(
                "autostart: no cron service is running here; the entry starts the "
                "node only once cron runs at boot",
                file=sys.stderr,
            )
        self._rewrite(spec.node, self._line(spec))

    def remove(self, node: str, unload: bool = False) -> None:
        self.check()
        self._rewrite(node, None)

    def entries(self) -> dict[str, Entry]:
        self.check()
        found: dict[str, Entry] = {}
        for ln in self._read().splitlines():
            m = _CRON_RE.match(ln)
            if m and m["home"] == home_id():
                try:
                    parts = tuple(shlex.split(m["cmd"].replace("\\%", "%")))
                except ValueError:
                    found[m["node"]] = Entry(m["node"], None, "unreadable crontab line")
                    continue
                found[m["node"]] = Entry(m["node"], _epoch_of(parts), "crontab", parts)
        return found


class LaunchdBackend(Backend):
    name = "launchd"
    event = "user login (LaunchAgent, RunAtLoad, no KeepAlive)"
    foreground = True

    def __init__(self, agents_dir: Path | None = None):
        self.agents_dir = agents_dir or Path.home() / "Library" / "LaunchAgents"

    def label(self, node: str) -> str:
        return f"com.a8s.autostart.{home_id()}.{node}"

    def path(self, node: str) -> Path:
        return self.agents_dir / f"{self.label(node)}.plist"

    def install(self, spec: EntrySpec) -> None:
        plist = {
            "Label": self.label(spec.node),
            "ProgramArguments": spec.argv,
            "RunAtLoad": True,
        }
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        try:
            atomic_write_text(self.path(spec.node), plistlib.dumps(plist).decode())
        except OSError as e:
            raise AutostartError(f"could not write {self.path(spec.node)}: {e}") from e
        if not self.path(spec.node).is_file():
            raise AutostartError(f"{self.path(spec.node)} is missing after the write")

    def loaded(self, node: str) -> bool:
        try:
            return _run(["launchctl", "print", f"gui/{os.getuid()}/{self.label(node)}"]).returncode == 0
        except AutostartError:
            return False

    def remove(self, node: str, unload: bool = False) -> None:
        path = self.path(node)
        if unload and self.loaded(node):
            _run_quiet(["launchctl", "bootout", f"gui/{os.getuid()}/{self.label(node)}"])
            if self.loaded(node):
                raise AutostartError(f"launchd still holds the loaded job {self.label(node)}")
        if not path.exists():
            return
        try:
            path.unlink()
        except OSError as e:
            raise AutostartError(f"could not remove {path}: {e}") from e
        if path.exists():
            raise AutostartError(f"{path} is still present after removal")

    def entries(self) -> dict[str, Entry]:
        found: dict[str, Entry] = {}
        prefix = f"com.a8s.autostart.{home_id()}."
        if not self.agents_dir.is_dir():
            return found
        for p in sorted(self.agents_dir.glob(f"{prefix}*.plist")):
            node = p.name[len(prefix):-len(".plist")]
            try:
                args = plistlib.loads(p.read_bytes()).get("ProgramArguments", [])
                if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                    raise ValueError("ProgramArguments is not a string list")
            except (OSError, ValueError, AttributeError):
                found[node] = Entry(node, None, "unreadable plist")
                continue
            found[node] = Entry(node, _epoch_of(args), str(p), tuple(args))
        return found


def _run_quiet(argv: list[str]) -> None:
    try:
        _run(argv)
    except AutostartError:
        pass


def _split_winargs(text: str) -> list[str]:
    try:
        return [t.strip('"') for t in shlex.split(text, posix=False)]
    except ValueError:
        return []


class SchtasksBackend(Backend):
    name = "task-scheduler"
    event = "user logon (scheduled task, interactive token, no saved password)"
    foreground = True

    def task_name(self, node: str) -> str:
        return f"\\a8s\\{home_id()}-{node}"

    def launcher(self) -> str:
        """The logon task runs with no console: `pythonw.exe` beside the
        interpreter, else the interpreter itself, which `install` refuses."""
        gui = Path(sys.executable).with_name("pythonw.exe")
        return str(gui) if gui.is_file() else sys.executable

    def _user_sid(self) -> str:
        """The SID of the account running this process, from the OS. Never
        built from USERNAME/USERDOMAIN, which an environment can set to
        anything; a trigger or principal with no certain user is refused."""
        proc = _run(["whoami", "/user", "/fo", "csv", "/nh"])
        found = re.search(r'"(S-1-\d+(?:-\d+)+)"\s*$', proc.stdout.strip())
        if proc.returncode != 0 or not found:
            raise AutostartError("cannot determine the current user's SID; refusing to write a logon task")
        return found.group(1)

    def _xml(self, spec: EntrySpec) -> str:
        user = xml_escape(self._user_sid())
        args = subprocess.list2cmdline(spec.argv[1:])
        return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{xml_escape(spec.python)}</Command><Arguments>{xml_escape(args)}</Arguments></Exec></Actions>
</Task>
"""

    def check(self) -> None:
        if shutil.which("schtasks") is None:
            raise AutostartUnavailable("autostart on Windows needs schtasks, which was not found")

    def install(self, spec: EntrySpec) -> None:
        self.check()
        if Path(spec.python).name.lower() != "pythonw.exe":
            raise AutostartError(
                "a logon task needs pythonw.exe beside the interpreter so no console window opens; none was found"
            )
        xml_file = home_dir() / f"autostart-{spec.node}.xml"
        xml_file.parent.mkdir(parents=True, exist_ok=True)
        xml_file.write_text(self._xml(spec), encoding="utf-16")
        try:
            proc = _run(["schtasks", "/Create", "/TN", self.task_name(spec.node),
                         "/XML", str(xml_file), "/F"])
        finally:
            xml_file.unlink(missing_ok=True)
        if proc.returncode != 0:
            raise AutostartError(f"schtasks rejected the task: {(proc.stderr or proc.stdout).strip()}")
        if spec.node not in self.entries():
            raise AutostartError("the task is not listed after creation")

    def remove(self, node: str, unload: bool = False) -> None:
        self.check()
        if node not in self.entries():
            return
        proc = _run(["schtasks", "/Delete", "/TN", self.task_name(node), "/F"])
        if proc.returncode != 0:
            raise AutostartError(f"schtasks could not delete the task: {(proc.stderr or proc.stdout).strip()}")
        if node in self.entries():
            raise AutostartError("the task is still listed after deletion")

    def entries(self) -> dict[str, Entry]:
        self.check()
        proc = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
        if proc.returncode != 0:
            raise AutostartError(f"schtasks /Query failed: {(proc.stderr or proc.stdout).strip()}")
        prefix = f"\\a8s\\{home_id()}-"
        found: dict[str, Entry] = {}
        for ln in proc.stdout.splitlines():
            name = ln.split(",")[0].strip().strip('"')
            if name.startswith(prefix):
                node = name[len(prefix):]
                xml = _run(["schtasks", "/Query", "/TN", name, "/XML"]).stdout
                cmd = re.search(r"<Command>(.*?)</Command>", xml, re.S)
                arg = re.search(r"<Arguments>(.*?)</Arguments>", xml, re.S)
                if not cmd or not arg:
                    found[node] = Entry(node, None, name)
                    continue
                argv = (xml_unescape(cmd.group(1)), *_split_winargs(xml_unescape(arg.group(1))))
                found[node] = Entry(node, _epoch_of(argv), name, argv)
        return found


def select_backend() -> Backend:
    if sys.platform.startswith("linux"):
        return CronBackend()
    if sys.platform == "darwin":
        return LaunchdBackend()
    if IS_WINDOWS:
        return SchtasksBackend()
    raise AutostartUnavailable(f"autostart is not supported on {sys.platform}")


# ---------- operations ----------

def _read_env_file() -> dict[str, dict[str, str]]:
    try:
        saved = json.loads(env_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    return {
        node: {k: v for k, v in env.items() if k in STARTUP_ENV_NAMES and isinstance(v, str)}
        for node, env in saved.items() if isinstance(env, dict)
    }


def _write_env_file(saved: dict[str, dict[str, str]]) -> None:
    saved = {n: e for n, e in saved.items() if e}
    if saved:
        atomic_write_text(env_path(), json.dumps(saved, sort_keys=True), mode=0o600)
    else:
        env_path().unlink(missing_ok=True)


def _capture_startup_env(nodes: list[str]) -> None:
    """Merge this shell's proxy and CA settings into each node's snapshot: a
    value set here replaces the node's saved one, a name this shell lacks
    keeps what was saved. Nothing is ever deleted by a capture."""
    current = {k: os.environ[k] for k in STARTUP_ENV_NAMES if os.environ.get(k)}
    if not current:
        return
    saved = _read_env_file()
    for node in nodes:
        saved[node.lower()] = {**saved.get(node.lower(), {}), **current}
    _write_env_file(saved)


def _forget_startup_env(nodes: list[str]) -> None:
    saved = _read_env_file()
    if any([saved.pop(n.lower(), None) is not None for n in nodes]):
        _write_env_file(saved)


def load_startup_env(node: str | None = None) -> None:
    """Put the node's saved proxy and CA settings in this process's
    environment without overriding anything already set. The read waits for
    the autostart lock, so it never sees a snapshot half way through an
    `enable`; nothing else is taken while it is held."""
    if node is None:
        return
    with _lock():
        saved = _read_env_file().get(node.lower(), {})
    for key, value in saved.items():
        os.environ.setdefault(key, value)


def enable(nodes: list[str], *, capture_env: bool = False) -> None:
    """Remember the preference, arm every node and write its entry. Raises
    AutostartError naming each node the OS refused; a refused node is left
    disarmed with the preference unchanged from before. `capture_env` (an
    explicit request) saves this shell's proxy and CA settings for each node
    whose entry was written."""
    backend = select_backend()
    backend.check()
    problems: list[str] = []
    with _lock():
        state = _read_state()
        installed: list[str] = []
        priors: dict[str, dict] = {}
        for node in nodes:
            key = node.lower()
            prior = dict(state.get(key) or {})
            priors[key] = prior
            rec = {**prior, "preference": True, "armed": True,
                   "epoch": prior.get("epoch") if prior.get("armed") and prior.get("epoch") else new_ulid(),
                   "backend": backend.name}
            state[key] = rec
            _write_state(state)
            try:
                backend.install(_spec(backend, key, rec["epoch"]))
                installed.append(key)
            except AutostartError as e:
                state[key] = {**prior, "armed": False, "epoch": None}
                if not prior:
                    state.pop(key)
                _write_state(state)
                problems.append(f"{node}: {e}")
        if capture_env and installed:
            try:
                _capture_startup_env(installed)
            except OSError as e:
                problems.extend(_unwind_unsaved(backend, state, installed, priors, e))
    if problems:
        raise AutostartError("; ".join(problems))


def _unwind_unsaved(backend: "Backend", state: dict, keys: list[str],
                    priors: dict[str, dict], cause: OSError) -> list[str]:
    """The requested proxy and CA settings could not be saved, so nothing may
    start with them missing: disarm every entry just written (its epoch no
    longer matches any claim), keep each node's earlier preference, and try
    to delete the entries. One that stays is inert and named."""
    for key in keys:
        prior = priors.get(key) or {}
        if prior:
            state[key] = {**prior, "armed": False, "epoch": None}
        else:
            state.pop(key, None)
    _write_state(state)
    out: list[str] = []
    for key in keys:
        msg = f"{key}: proxy/CA settings could not be saved ({cause}); autostart disarmed"
        try:
            backend.remove(key)
        except AutostartError as e:
            msg += f"; its entry stays but is inert (not armed) until removed: {e}"
        out.append(msg)
    return out


def rearm_remembered(nodes: list[str]) -> None:
    """Plain `start`: arm the nodes that opted in earlier and were stopped."""
    state = _read_state()
    due = [n for n in nodes
           if (state.get(n.lower()) or {}).get("preference")
           and not (state.get(n.lower()) or {}).get("armed")]
    if due:
        enable(due)


def disable(nodes: list[str], *, forget_preference: bool = False) -> None:
    """Disarm and delete the entry. The state commits first, so a stale entry
    the OS refuses to drop cannot revive the node. Raises AutostartError for
    each node whose entry stays, and when the state cannot be locked or
    written. A node with no record is only cleaned up best-effort: a host
    with no startup facility owns nothing to remove."""
    try:
        _disable(nodes, forget_preference)
    except OSError as e:
        raise AutostartError(
            f"startup state not updated, the node may start again at boot or login: {e}"
        ) from e


def _disable(nodes: list[str], forget_preference: bool) -> None:
    state = _read_state(strict=True)
    if not forget_preference and not any(
        (state.get(n.lower()) or {}).get("armed") for n in nodes
    ):
        return
    problems: list[str] = []
    with _lock():
        state = _read_state(strict=True)
        known: list[str] = []
        unknown: list[str] = []
        for node in nodes:
            key = node.lower()
            rec = state.get(key)
            if rec is None:
                if forget_preference:
                    unknown.append(key)
                continue
            state[key] = {**rec, "armed": False, "epoch": None,
                          "preference": False if forget_preference else rec.get("preference", False)}
            known.append(key)
        _write_state(state)
        if not known and not unknown:
            return
        try:
            backend = select_backend()
        except AutostartUnavailable as e:
            if known:
                raise AutostartError(f"{', '.join(known)}: {e}") from e
            return
        except AutostartError as e:
            raise AutostartError(f"{', '.join(known + unknown)}: {e}") from e
        for key in known + unknown:
            try:
                backend.remove(key)
            except AutostartUnavailable as e:
                if key in known:
                    problems.append(f"{key}: {e}")
            except AutostartError as e:
                problems.append(f"{key}: {e}")
        if forget_preference:
            done = [k for k in known if not any(p.startswith(f"{k}:") for p in problems)]
            for key in done:
                state.pop(key, None)
            _write_state(state)
            _forget_startup_env(done)
    if problems:
        raise AutostartError("; ".join(problems))


def purge(node: str) -> None:
    """Delete every owned entry of one node, whatever the saved record says.
    On failure the record stays disarmed so repair remains possible."""
    key = node.lower()
    with _lock():
        state = _read_state()
        rec = state.get(key)
        if rec is not None:
            state[key] = {**{k: v for k, v in rec.items() if k != "manual"},
                          "armed": False, "epoch": None, "preference": False}
            _write_state(state)
        try:
            backend = select_backend()
            if key in backend.entries() or backend.loaded(key):
                backend.remove(key, unload=True)
        except AutostartUnavailable:
            if rec is not None:
                raise
            return  # no startup facility on this host: nothing can be owned
        except AutostartError as e:
            raise AutostartError(f"{node}: {e}") from e
        state.pop(key, None)
        _write_state(state)
        _forget_startup_env([key])


def disarm_all() -> None:
    """`a8s exit`: every opted-in node of this config home, running or not."""
    problems: list[str] = []
    try:
        invalidate_manual()
    except AutostartError as e:
        problems.append(str(e))
    try:
        nodes = opted_in(strict=True)
        if nodes:
            disable(nodes)
    except AutostartError as e:
        problems.append(str(e))
    if problems:
        raise AutostartError("; ".join(problems))


@contextmanager
def startup_claim(node: str, epoch: str | None, manual: str | None = None):
    """Hold the autostart lock across a node's pid-file claim. Yields whether
    the claim is allowed: always without a token (public run takeover), only
    for the record's armed epoch with a startup epoch, and only for the
    record's current manual token with one. A manual token is spent by the
    claim attempt that checks it."""
    if epoch is None and manual is None:
        yield True
        return
    key = node.lower()
    with _lock():
        state = _read_state()
        rec = state.get(key) or {}
        if epoch is not None:
            allowed = bool(rec.get("armed")) and rec.get("epoch") == epoch
        else:
            allowed = bool(manual) and rec.get("manual") == manual
            if allowed:
                state[key] = {k: v for k, v in rec.items() if k != "manual"}
                _prune(state, key)
                _write_state(state)
        yield allowed


def claim_context(node: str, epoch: str | None, manual: str | None = None):
    return startup_claim(node, epoch, manual) if (epoch or manual) else nullcontext(True)


def pop_manual_claim(args: list[str]) -> tuple[list[str], str | None]:
    """Pull the internal `--manual-claim TOKEN` out of `args`."""
    rest: list[str] = []
    token = None
    i = 0
    while i < len(args):
        if args[i] == "--manual-claim":
            if i + 1 >= len(args):
                raise ValueError("missing value for --manual-claim")
            token = args[i + 1]
            i += 1
        else:
            rest.append(args[i])
        i += 1
    return rest, token


# ---------- flags ----------

def split_lifecycle_flags(args: list[str]) -> tuple[list[str], str | None, str | None, str | None]:
    """Pull `--autostart` / `--no-autostart` and the internal
    `--startup EPOCH` / `--startup-home DIR` out of `args`.

    Returns (rest, mode, epoch, home); mode is "on", "off" or None. Raises
    ValueError for contradictory or incomplete flags."""
    rest: list[str] = []
    mode: str | None = None
    epoch = home = None
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in ("--autostart", "--no-autostart"):
            new = "on" if tok == "--autostart" else "off"
            if mode not in (None, new):
                raise ValueError("--autostart and --no-autostart contradict each other")
            mode = new
        elif tok in ("--startup", "--startup-home"):
            if i + 1 >= len(args):
                raise ValueError(f"missing value for {tok}")
            if tok == "--startup":
                epoch = args[i + 1]
            else:
                home = args[i + 1]
            i += 1
        else:
            rest.append(tok)
        i += 1
    return rest, mode, epoch, home


def enter_startup(home: str | None, node: str | None = None) -> None:
    """Select the config home an OS entry named and load the node's saved
    settings. A logon task has no console, so a missing stdio stream becomes
    the null device rather than a crash on the first write."""
    if home:
        os.environ["A8S_HOME"] = home
    load_startup_env(node)
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))


def listing(nodes: list[str]) -> list[tuple[str, str, str, str, str]]:
    """Rows for `a8s ls --autostart`: node, preference, state, event, entry."""
    state = _read_state()
    try:
        backend: Backend | None = select_backend()
        found = backend.entries()
        problem = ""
    except AutostartError as e:
        backend, found, problem = None, {}, str(e)
    event = backend.event if backend else "unavailable"
    rows = []
    for node in nodes:
        rec = state.get(node.lower()) or {}
        pref = "yes" if rec.get("preference") else "no"
        armed = bool(rec.get("armed"))
        entry = found.get(node.lower())
        if problem:
            status = f"unavailable: {problem}"
        elif armed and entry is None:
            status = "missing"
        elif armed and (
            entry.epoch != rec.get("epoch")
            or entry.command is None
            or list(entry.command) != _spec(backend, node.lower(), rec.get("epoch") or "").argv
        ):
            status = "mismatched"
        elif not armed and entry is not None:
            status = "stale (inert: not armed)"
        else:
            status = "ok" if armed else "-"
        rows.append((node, pref, "armed" if armed else "stopped", event, status))
    listed = {n.lower() for n in nodes}
    for node, entry in sorted(found.items()):
        if node not in listed:
            rows.append((node, "-", "-", event, "orphaned (no such node)"))
    return rows
